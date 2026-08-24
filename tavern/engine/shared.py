from __future__ import annotations

import asyncio
import hashlib
import json
import inspect
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from ..config import TavernConfig
from ..constants import SESSION_FINISHED
from ..database import (
    DatabaseConflictError,
    InvalidTransitionError,
    TavernDatabase,
)
from ..events import EventBroker
from ..api.registry import ExtensionRegistry
from ..lifecycle.risk_resolution import (
    fallback_choices,
    normalize_model_choices,
)
from ..lifecycle.state_transitions import format_choices
from ..prompts import (
    checked_resolution_prompt,
    choice_generation_prompt,
    choice_repair_prompt,
    choice_system_prompt,
    dm_beat_prompt,
    planning_prompt,
    repair_prompt,
    system_prompt,
)
from ..resolution import (
    CheckRequest,
    DiceResult,
    Resolution,
    apply_state_patch,
    extract_json_object,
    roll_check,
    roll_group_check,
    roll_opposed_check,
    validate_resolution,
)
from ..entity_resolver import (
    build_participant_labels,
    normalize_relationship_ops,
)
from ..copy.story_entities import (
    build_story_entity_catalog,
    decorate_story_entities,
)
from ..security import RateLimiter, clean_text
from ..world_contract import world_contract
from ..market_projection import project_market_view
from ..operations import operation_key, transport_event_id
from ..narrative_modes import (
    narrative_mode_from_session,
    narrative_quality_policy,
    normalize_narrative_mode,
)
from ..chat_experience import normalize_chat_experience
from ..projections.character import project_actor_view
from ..projections.world import actor_values_for_roles
from ..story_context import project_opening_scene
from ..turn_budget import (
    GenerationBudgetExceeded,
    TurnGenerationBudget,
    player_generation_stage_label,
)
from ..choice_command import ChoiceCommand
from ..messaging.player import PlayerMessage
from ..messaging.turn_bundle import TurnMessageBundle
from ..generation_reminders import GenerationReminderConfig
from ..contracts.narrative_document import (
    NarrativeDocument,
    inspect_narrative_document,
    narrative_document_to_plain_text,
)


logger = logging.getLogger(__name__)


def _choice_risk_summary(
    choices: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    counts = {
        "safe": 0,
        "controlled": 0,
        "dangerous": 0,
        "desperate": 0,
        "lethal": 0,
        "missing": 0,
        "invalid": 0,
    }
    check_count = 0
    automatic_count = 0
    for item in choices:
        raw = str(item.get("danger_id") or item.get("risk") or "").strip().lower()
        aliases = {"low": "safe", "standard": "controlled", "high": "dangerous"}
        risk = aliases.get(raw, raw)
        if not risk:
            counts["missing"] += 1
        elif risk in counts:
            counts[risk] += 1
        else:
            counts["invalid"] += 1
        kind = str(item.get("resolution_kind") or "").strip().lower()
        if kind == "check" or bool(item.get("requires_check")):
            check_count += 1
        if kind == "automatic_consequence":
            automatic_count += 1
    return {
        "choice_count": len(choices),
        "risk_counts": counts,
        "safe_count": counts["safe"],
        "non_safe_count": sum(
            counts[key]
            for key in (
                "controlled",
                "dangerous",
                "desperate",
                "lethal",
            )
        ),
        "check_count": check_count,
        "automatic_consequence_count": automatic_count,
    }


def _choice_failure_kind(validation_error: str) -> str:
    error = str(validation_error or "")
    if error == "模型未提供 next_choices":
        return "missing_choices"
    if "缺少 risk/danger_id" in error:
        return "missing_risk"
    if "未知危险度" in error or "不允许危险度" in error:
        return "invalid_risk"
    if "resolution_kind" in error or "检定" in error or "后果" in error:
        return "invalid_resolution"
    return "invalid_choices"


def _session_game_time(session: Mapping[str, Any]) -> str:
    """从会话世界状态取当前游戏时间（用于事实元数据）。"""
    state = session.get("world_state")
    if isinstance(state, Mapping):
        return str(state.get("time") or "")
    return ""


def _effective_narrative_transition_patch(
    state_patch: Mapping[str, Any] | None,
    current_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return only scene/time patch members that change committed state.

    Narrative continuity is about an observable transition, not the presence
    of an idempotent model field.  Other state-patch members intentionally do
    not participate in the NarrativeDocument transition contract.
    """

    patch = state_patch if isinstance(state_patch, Mapping) else {}
    current = current_state if isinstance(current_state, Mapping) else {}
    effective: dict[str, Any] = {}
    for key in (
        "location",
        "current_scene",
        "scene_ref",
        "time",
        "game_time",
        "world_time",
    ):
        if key not in patch:
            continue
        value = patch.get(key)
        if value in (None, ""):
            continue
        if value != current.get(key):
            effective[key] = value
    return effective


# 0.11.1：单次结构化生成（检定/选项/直述）的全局模型调用上限。
# 默认 json_repair_attempts=1 时正常路径仅 1-2 次调用；此上限用于兜底
# 多 provider × 多 repair 叠加造成分钟级延迟的场景。
_MAX_TOTAL_MODEL_ATTEMPTS = 8


def _builtin_d20_provider(
    *,
    check: CheckRequest,
    check_type: str,
    actors: list[Mapping[str, Any]] | None = None,
    outcome_policy: Mapping[str, Any] | None = None,
) -> DiceResult:
    if check_type in {"group", "resistance"}:
        return roll_group_check(check, list(actors or []), outcome_policy)
    if check_type == "opposed":
        return roll_opposed_check(check, outcome_policy=outcome_policy)
    return roll_check(check, outcome_policy)

__all__ = [name for name in globals() if not name.startswith("__")]
