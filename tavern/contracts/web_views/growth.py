"""D1-UX / D1-WEB：技能成长玩家可见投影（ability_track@1.0）。

对应 docs/D1_PLAN/17_SKILL_GROWTH_SYSTEM.md §10.3：角色能力面板显示
当前名称与等级、来源职业/专精、当前效果、成长证据、下一等级预览、
未满足条件与历史名称。

普通视图不输出 track_id、Effect key（``part:$.``）、capability_ref、
schema 版本等内部契约；仅授权角色可在 technical 详情中查看。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, max_chars: int = 1000) -> str:
    return str(value or "").strip()[:max_chars]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError, OverflowError):
        return default


def _evidence_views(items: Any) -> list[dict[str, Any]]:
    """成长证据/里程碑视图：只展示来源标注与说明，不暴露稳定 ID。"""

    views: list[dict[str, Any]] = []
    for item in _sequence(items):
        if not isinstance(item, Mapping):
            continue
        note = _text(item.get("note"), 300)
        kind = _text(item.get("kind"), 80)
        recorded_at = _text(item.get("recorded_at"), 40)
        view: dict[str, Any] = {}
        if kind:
            view["kind"] = kind
        if note:
            view["note"] = note
        if recorded_at:
            view["recorded_at"] = recorded_at
        if view:
            views.append(view)
    return views


def _history_views(items: Any) -> list[dict[str, Any]]:
    """历史名称：旧名称进入历史，不再作为可执行能力（17 §13）。"""

    views: list[dict[str, Any]] = []
    for item in _sequence(items):
        if not isinstance(item, Mapping):
            continue
        from_name = _text(item.get("from_name"), 80)
        to_name = _text(item.get("to_name"), 80)
        confirmed_at = _text(item.get("confirmed_at"), 40)
        if not to_name:
            continue
        view: dict[str, Any] = {
            "from_name": from_name,
            "to_name": to_name,
        }
        if confirmed_at:
            view["confirmed_at"] = confirmed_at
        views.append(view)
    return views


def _pending_view(
    pending: Mapping[str, Any] | None,
    *,
    impacts: Sequence[Mapping[str, Any]] | None = None,
    position_labels: Mapping[int, str] | None = None,
) -> dict[str, Any] | None:
    if not pending:
        return None
    pending = _mapping(pending)
    target = _int(pending.get("target_level"))
    labels = dict(position_labels or {})
    view: dict[str, Any] = {
        "target_level": target,
        "target_level_label": f"{target}级",
        "target_name": _text(pending.get("target_name"), 80),
        "position_label": labels.get(target, ""),
        "replaces_original": True,
        "impacts": [
            dict(item) for item in _sequence(impacts) if isinstance(item, Mapping)
        ],
    }
    for key, label in (
        ("effects", "added_effects"),
        ("limitations", "retained_limitations"),
        ("costs", "new_costs"),
        ("unlock_conditions", "unlock_conditions"),
    ):
        values = [
            _text(item, 500)
            for item in _sequence(pending.get(key))
            if _text(item, 500)
        ]
        if values:
            view[label] = values
    return view


def project_growth_profile_view(
    *,
    character_name: str = "",
    capability_name: str = "",
    level: int = 1,
    position_label: str = "",
    source_profession: str = "",
    source_specialization: str = "",
    summary: str = "",
    effects: Sequence[Any] | None = None,
    costs: Sequence[Any] | None = None,
    limitations: Sequence[Any] | None = None,
    evidence: Sequence[Any] | None = None,
    milestones: Sequence[Any] | None = None,
    history: Sequence[Any] | None = None,
    pending: Mapping[str, Any] | None = None,
    unmet_conditions: Sequence[Any] | None = None,
    maximum_level: int = 4,
    impacts: Sequence[Mapping[str, Any]] | None = None,
    include_technical_refs: bool = False,
    technical: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把一个技能的成长状态规范化为玩家可见面板视图。"""

    level = _int(level, 1)
    maximum = _int(maximum_level, 4)
    view: dict[str, Any] = {
        "character_name": _text(character_name, 80),
        "capability_name": _text(capability_name, 80),
        "level": level,
        "level_label": f"{level}级",
        "position_label": _text(position_label, 20),
        "source": {
            "profession": _text(source_profession, 40),
            "specialization": _text(source_specialization, 40),
        },
        "summary": _text(summary, 1000),
        "effects": [
            _text(item, 500) for item in _sequence(effects) if _text(item, 500)
        ],
        "costs": [
            _text(item, 500) for item in _sequence(costs) if _text(item, 500)
        ],
        "limitations": [
            _text(item, 500)
            for item in _sequence(limitations)
            if _text(item, 500)
        ],
        "evidence": _evidence_views(evidence),
        "milestones": _evidence_views(milestones),
        "history": _history_views(history),
        "pending": _pending_view(
            pending,
            impacts=impacts,
            position_labels={
                1: "初识",
                2: "熟练",
                3: "精通",
                4: "传奇",
            },
        ),
        "unmet_conditions": [
            _text(item, 300)
            for item in _sequence(unmet_conditions)
            if _text(item, 300)
        ],
        "maximum_reached": level >= maximum,
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = dict(technical) if isinstance(technical, Mapping) else {}
    return view


def project_growth_context_view(
    *,
    character_name: str = "",
    card_stage: str = "",
    turn_no: int = 0,
    session_state: str = "",
    tracks: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """私聊/本人上下文的成长总览（BOT 与 WebUI 共用）。"""

    return {
        "character_name": _text(character_name, 80),
        "card_stage": _text(card_stage, 40),
        "turn_no": _int(turn_no),
        "session_state": _text(session_state, 40),
        "tracks": [dict(item) for item in _sequence(tracks)],
    }


__all__ = [
    "project_growth_context_view",
    "project_growth_profile_view",
]
