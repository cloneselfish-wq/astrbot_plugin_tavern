from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ..candidates import (
    normalize_candidate_constraints,
    validate_constraint_graph,
)
from ..card_wizard import field_visible, preset_options
from ..security import clean_text
from ..stat_generation import (
    calculate_preset_stack_stats,
    stat_generation_config,
    uses_preset_stack_stats,
    validate_stat_generation_config,
)
from ..world_contract import attribute_lookup, stats_mode, world_contract
from ..presets import dimension_fields, normalize_preset_dimensions
from ..generation_reminders import GenerationReminderConfig


CARD_UNCREATED = "uncreated"
CARD_DRAFT = "draft"
CARD_PENDING = "pending_review"
CARD_APPROVED = "approved"
CARD_REJECTED = "rejected"
CARD_STATUSES = {
    CARD_UNCREATED,
    CARD_DRAFT,
    CARD_PENDING,
    CARD_APPROVED,
    CARD_REJECTED,
}

# D1：分阶段建卡（16_STAGED_CHARACTER_CREATION_AND_OPENING_PACING）。
# 每个字段必须明确属于 A（开演前）/ B（第一幕补充）/ C（第一幕后补充）组，
# 不允许由前端或运行时代码临时决定。
CARD_STAGE_A = "A"
CARD_STAGE_B = "B"
CARD_STAGE_C = "C"
CARD_STAGES = (CARD_STAGE_A, CARD_STAGE_B, CARD_STAGE_C)
CARD_STAGE_STATES = ("core_ready", "staged_pending", "stage_locked", "complete")
# 草稿字段 JSON 中记录“已由剧情确认”的字段 key 列表。
STAGED_FIELDS_KEY = "_staged_fields"

PARTICIPANT_RESERVED = "reserved"
PARTICIPANT_ACTIVE = "active"
PARTICIPANT_STANDBY = "standby"
PARTICIPANT_AWAY = "away"
PARTICIPANT_RETIRED = "retired"
PARTICIPANT_ARCHIVED = "archived"
PARTICIPANT_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
    PARTICIPANT_RETIRED,
    PARTICIPANT_ARCHIVED,
}

SEAT_HOLDING_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
}

CHOICE_KEYS = ("A", "B", "C", "D")
_RISK_ALIASES = {
    "low": "safe",
    "standard": "controlled",
    "high": "dangerous",
    "safe": "safe",
    "controlled": "controlled",
    "dangerous": "dangerous",
    "desperate": "desperate",
    "lethal": "lethal",
}
_RISK_LABELS = {
    "safe": "安全",
    "controlled": "可控",
    "dangerous": "危险",
    "desperate": "绝境",
    "lethal": "致命",
}
SUPPORTED_CARD_FIELD_TYPES = frozenset(
    {
        "text",
        "textarea",
        "integer",
        "select",
        "preset_select",
        "multi_select",
        "boolean",
        "derived",
    }
)

DEFAULT_TIME_RULES: dict[str, Any] = {
    "card_code_ttl_seconds": 30 * 60,
    "card_draft_ttl_seconds": 7 * 24 * 60 * 60,
    "card_completion_timeout_seconds": None,
    "preparation_timeout_seconds": None,
    "ready_timeout_seconds": None,
    "turn_timeout_seconds": None,
    "turn_reminder_seconds": None,
    "max_consecutive_timeouts": -1,
    "standby_timeout_seconds": None,
    "delegation_ttl_seconds": None,
    "vote_round_one_seconds": None,
    "vote_round_two_seconds": None,
    "vote_reminder_seconds": None,
    "all_idle_pause_seconds": None,
    "pause_stops_clock": True,
    "announce_timeouts": False,
    "turn_timeout_action": "hold",
    "card_timeout_action": "remind",
    "ready_timeout_action": "remind",
    "story_generation_reminder": {
        "enabled": True,
        "interval_seconds": 60,
        "source": "implicit_default",
        "revision": 0,
    },
}

DEFAULT_CARD_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "name",
        "label": "角色姓名",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "code",
        "label": "副本代号",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "appearance",
        "label": "外貌特征",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "background",
        "label": "角色背景",
        "required": True,
        "private": False,
        "max_chars": 800,
    },
    {
        "key": "personality",
        "label": "性格与行事方式",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "goal",
        "label": "当前目标",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "belief",
        "label": "核心信念",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "bond",
        "label": "重要羁绊",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "specialties",
        "label": "专长标签（2—4 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 240,
    },
    {
        "key": "flaws",
        "label": "缺陷标签（1—2 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 200,
    },
    {
        "key": "weakness",
        "label": "弱点或限制",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "knowledge_boundary",
        "label": "知识边界",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "secret",
        "label": "私人秘密",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
    {
        "key": "content_boundaries",
        "label": "个人内容边界",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
)

DEFAULT_CARD_STATS: dict[str, Any] = {
    "budget": 10,
    "attributes": [
        {
            "key": "body",
            "label": "体魄",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "agility",
            "label": "敏捷",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "will",
            "label": "意志",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "knowledge",
            "label": "学识",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
    ],
    "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2,
    },
}

DEFAULT_OPENING_CHOICES: tuple[dict[str, Any], ...] = (
    {
        "key": "A",
        "text": "先观察周围环境，确认眼前最明显的异常",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "B",
        "text": "与当前场景中最容易接触的人交谈，询问公开信息",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "C",
        "text": "检查自己能够合理接触的物品与随身资源",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "D",
        "text": "保持警戒，暂不冒进，等待局势显露更多线索",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
)

DEFAULT_PROGRESS: dict[str, Any] = {
    "chapter": "序章",
    "current_objective": "等待剧情目标",
    "completed_milestones": 0,
    "total_milestones": 0,
}

DEFAULT_CONTENT_BOUNDARIES: dict[str, Any] = {
    "character_death": "ask",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
}

DEFAULT_NPC_POLICY: dict[str, Any] = {
    "enabled": True,
    "auto_register": True,
    "max_new_per_turn": 3,
    "require_named_or_relevant": True,
    "generated_requires_review": True,
    "archive_after_inactive_rounds": 12,
}

DEFAULT_CONTEXT_BUDGET: dict[str, Any] = {
    "recent_turns": 6,
    "memories": 6,
    "active_npcs": 6,
    "ledger_items": 8,
    "locked_facts_always_include": True,
}

DEFAULT_DICE_RULES: dict[str, Any] = {
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": False,
    "opposites_cancel": True,
    "outcome_bands": True,
    "visibility": "public",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deadline_after(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))
    ).isoformat(timespec="seconds")


def _bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _optional_seconds(value: Any, default: int | None) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "unlimited",
        "none",
    }:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == -1:
        return None
    if parsed == 0 or parsed < -1:
        raise ValueError("时间值必须大于 0，或使用 -1/留空表示不限时")
    return min(365 * 24 * 60 * 60, parsed)


def normalize_time_rules(value: Any = None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result = dict(DEFAULT_TIME_RULES)
    for key in (
        "card_code_ttl_seconds",
        "card_draft_ttl_seconds",
        "card_completion_timeout_seconds",
        "preparation_timeout_seconds",
        "ready_timeout_seconds",
        "turn_timeout_seconds",
        "turn_reminder_seconds",
        "standby_timeout_seconds",
        "delegation_ttl_seconds",
        "vote_round_one_seconds",
        "vote_round_two_seconds",
        "vote_reminder_seconds",
        "all_idle_pause_seconds",
    ):
        # A missing field inherits its declared default; an explicitly blank
        # field still means unlimited.  ``dict.get`` cannot distinguish those
        # two cases and previously erased the two finite card TTL defaults.
        raw_value = source[key] if key in source else result[key]
        result[key] = _optional_seconds(raw_value, result[key])
    raw_timeout_limit = source.get(
        "max_consecutive_timeouts",
        DEFAULT_TIME_RULES["max_consecutive_timeouts"],
    )
    try:
        timeout_limit = int(raw_timeout_limit)
    except (TypeError, ValueError):
        timeout_limit = int(DEFAULT_TIME_RULES["max_consecutive_timeouts"])
    if timeout_limit == 0 or timeout_limit < -1:
        raise ValueError("连续超时次数必须为 1—20，或 -1 表示永不自动转候补")
    result["max_consecutive_timeouts"] = (
        -1 if timeout_limit == -1 else min(20, max(1, timeout_limit))
    )
    result["pause_stops_clock"] = bool(
        source.get("pause_stops_clock", True)
    )
    result["announce_timeouts"] = bool(
        source.get("announce_timeouts", False)
    )
    timeout_action = str(
        source.get("turn_timeout_action", "hold")
    ).strip()
    result["turn_timeout_action"] = (
        timeout_action
        if timeout_action in {"skip", "hold"}
        else "hold"
    )
    for key, allowed, default in (
        (
            "card_timeout_action",
            {"standby", "release", "remind"},
            "remind",
        ),
        (
            "ready_timeout_action",
            {"standby", "remind"},
            "remind",
        ),
    ):
        action = str(source.get(key, default)).strip()
        result[key] = action if action in allowed else default

    reminder_source = source.get("story_generation_reminder")
    reminder_mapping = (
        reminder_source if isinstance(reminder_source, Mapping) else {}
    )
    result["story_generation_reminder"] = (
        GenerationReminderConfig.from_mapping(
            reminder_mapping,
            source=str(
                reminder_mapping.get("source") or "implicit_default"
            ),
            revision=reminder_mapping.get("revision", 0),
            fail_safe=not bool(reminder_mapping),
        ).to_snapshot()
    )

    turn_seconds = result["turn_timeout_seconds"]
    reminder = result["turn_reminder_seconds"]
    if (
        turn_seconds is not None
        and reminder is not None
        and reminder >= turn_seconds
    ):
        result["turn_reminder_seconds"] = max(1, turn_seconds // 3)
    return result


def world_time_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return normalize_time_rules({})
    return normalize_time_rules(rules.get("time_rules"))


def normalize_progress(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    completed = _bounded_int(
        raw.get("completed_milestones"),
        0,
        0,
        1_000_000,
    )
    total = _bounded_int(
        raw.get("total_milestones"),
        0,
        0,
        1_000_000,
    )
    if total and completed > total:
        completed = total
    return {
        "chapter": clean_text(
            raw.get("chapter") or DEFAULT_PROGRESS["chapter"],
            max_chars=160,
        ),
        "current_objective": clean_text(
            raw.get("current_objective")
            or DEFAULT_PROGRESS["current_objective"],
            max_chars=400,
        ),
        "completed_milestones": completed,
        "total_milestones": total,
    }


def world_session_modules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    state = world.get("initial_state")
    state = state if isinstance(state, Mapping) else {}
    progress = rules.get("progress")
    if not isinstance(progress, Mapping):
        progress = state.get("progress")
    content_boundaries = dict(DEFAULT_CONTENT_BOUNDARIES)
    if isinstance(rules.get("content_boundaries"), Mapping):
        content_boundaries.update(dict(rules["content_boundaries"]))
    if isinstance(rules.get("content_boundary"), Mapping):
        boundary = dict(rules["content_boundary"])
        content_boundaries.update(boundary)
        # Protocol v4 uses hard_denials; the runtime's older policy key is
        # retained as a compatibility projection for all narrative paths.
        if boundary.get("hard_denials"):
            content_boundaries["hard_limits"] = list(
                boundary.get("hard_denials") or []
            )
    npc_policy = dict(DEFAULT_NPC_POLICY)
    if isinstance(rules.get("npc_policy"), Mapping):
        npc_policy.update(dict(rules["npc_policy"]))
    npc_policy["max_new_per_turn"] = _bounded_int(
        npc_policy.get("max_new_per_turn"),
        3,
        0,
        3,
    )
    npc_policy["archive_after_inactive_rounds"] = _bounded_int(
        npc_policy.get("archive_after_inactive_rounds"),
        12,
        1,
        10_000,
    )
    context_budget = dict(DEFAULT_CONTEXT_BUDGET)
    if isinstance(rules.get("context_budget"), Mapping):
        context_budget.update(dict(rules["context_budget"]))
    for key, default, maximum in (
        ("recent_turns", 6, 50),
        ("memories", 6, 40),
        ("active_npcs", 6, 40),
        ("ledger_items", 8, 100),
    ):
        context_budget[key] = _bounded_int(
            context_budget.get(key),
            default,
            0,
            maximum,
        )
    dice_rules = dict(DEFAULT_DICE_RULES)
    if isinstance(rules.get("dice_rules"), Mapping):
        dice_rules.update(dict(rules["dice_rules"]))
    visibility = str(dice_rules.get("visibility") or "public").lower()
    dice_rules["visibility"] = (
        visibility
        if visibility in {"public", "immersive", "hidden"}
        else "public"
    )
    return {
        "progress": normalize_progress(progress),
        "content_boundaries": content_boundaries,
        "npc_policy": npc_policy,
        "context_budget": context_budget,
        "dice_rules": dice_rules,
        "recovery": {
            "state": "idle",
            "message": "",
            "operation_id": "",
            "updated_at": utc_now(),
        },
    }


def initial_character_runtime_state() -> dict[str, Any]:
    return {
        "inspiration": 1,
        "inspiration_max": 3,
        "statuses": [],
        "equipment": {},
        "known_clues": [],
        "npc_relationships": {},
        "temporary_traits": [],
        "reputation": {},
        "current_location": "",
    }


def player_limits(world: Mapping[str, Any]) -> dict[str, int]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("player_limits")
    raw = raw if isinstance(raw, Mapping) else {}
    maximum = _bounded_int(raw.get("maximum"), 4, 1, 40)
    minimum = _bounded_int(raw.get("minimum_start"), 2, 1, maximum)
    recommended_min = _bounded_int(
        raw.get("recommended_min"),
        min(2, maximum),
        1,
        maximum,
    )
    recommended_max = _bounded_int(
        raw.get("recommended_max"),
        maximum,
        recommended_min,
        maximum,
    )
    return {
        "minimum_start": minimum,
        "maximum": maximum,
        "recommended_min": recommended_min,
        "recommended_max": recommended_max,
    }


def _json_copy(value: Any) -> Any:
    """JSON 安全深拷贝（简单可序列化结构）。"""
    import json as _json

    try:
        return _json.loads(_json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError):
        return {}


_ACTOR_TEXT_FIELDS = frozenset(
    {
        "label", "name", "title", "summary", "description", "help", "text",
        "advantages", "limitations", "narrative_benefits", "costs_and_limits",
        "story_hooks", "does_not_grant",
    }
)


def _localized_actor_source(
    world: Mapping[str, Any],
    actor: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay actor authoring text from the frozen default-locale catalog."""

    source = _json_copy(actor)
    catalogs = world.get("resolved_text_catalog")
    if not isinstance(catalogs, Mapping):
        return source
    metadata = world.get("localization_metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    localization = rules.get("localization")
    localization = localization if isinstance(localization, Mapping) else {}
    locale = str(
        metadata.get("default_locale")
        or localization.get("default_locale")
        or "zh-CN"
    )
    catalog = catalogs.get(locale)
    if not isinstance(catalog, Mapping):
        raise ValueError(f"角色模块缺少默认语言目录：{locale}")

    def overlay(value: Any, path: str, text_context: bool = False) -> Any:
        if isinstance(value, Mapping):
            result = dict(value)
            root = str(result.get("text_id") or path)
            for key, nested in list(result.items()):
                child = f"{root}.{key}" if root else str(key)
                if key in _ACTOR_TEXT_FIELDS:
                    if isinstance(nested, str):
                        localized = catalog.get(child)
                        if not isinstance(localized, str) or not localized.strip():
                            raise ValueError(f"冻结文本目录缺少：{child}")
                        result[key] = localized
                    elif isinstance(nested, (Mapping, list, tuple)):
                        result[key] = overlay(nested, child, True)
                elif isinstance(nested, (Mapping, list, tuple)):
                    result[key] = overlay(nested, child, False)
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result = []
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    segment = str(
                        item.get("id")
                        or item.get("key")
                        or item.get("slug")
                        or index
                    )
                    child = str(item.get("text_id") or f"{path}.{segment}")
                else:
                    child = f"{path}.{index}"
                if isinstance(item, str):
                    if text_context:
                        localized = catalog.get(child)
                        if not isinstance(localized, str) or not localized.strip():
                            raise ValueError(f"冻结文本目录缺少：{child}")
                        result.append(localized)
                    else:
                        result.append(item)
                else:
                    result.append(overlay(item, child, text_context))
            return result
        return value

    if isinstance(source.get("fields"), list):
        source["fields"] = overlay(source["fields"], "actor.fields")
    if isinstance(source.get("preset_sets"), Mapping):
        source["preset_sets"] = overlay(source["preset_sets"], "preset_sets")
    for key, value in list(source.items()):
        if key in {"fields", "preset_sets", "content_audit"}:
            continue
        if isinstance(value, (Mapping, list, tuple)):
            source[key] = overlay(value, f"actor.{key}")
    return source

__all__ = [name for name in globals() if not name.startswith('__')]
