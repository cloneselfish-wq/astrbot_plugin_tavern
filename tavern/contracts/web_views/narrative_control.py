"""D1-UX-007 / D1-WEB-001：NarrativeControlView 纯投影。

输入是 ``tavern.repositories.control.ControlRepositoryMixin._control_state``
的真实行形状（``mode/phase/active_dm_user_id/beat_no/directive/
current_actor_type/current_actor_ref/preserved_turn/revision``）。
普通视图只输出中文 label 与展示名；主持人用户 ID、修订号、指引原文等
一律进入 ``technical``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..common import clean_label


CONTROL_MODE_LABELS = {
    "auto": "AI 自动主持",
    "dm": "人工 DM",
}

CONTROL_PHASE_LABELS = {
    "auto": "AI 推进",
    "awaiting_dm": "等待主持指令",
    "generating": "AI 推进中",
    "player_handoff": "已交棒给玩家",
    "npc_handoff": "已交棒给角色",
}

_INPUT_LOCKED_PHASES = frozenset({"generating", "player_handoff", "npc_handoff"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _action_list(
    actions: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in actions or ():
        raw = _mapping(item)
        action_id = str(raw.get("id") or "").strip()
        label = clean_label(raw.get("label"))
        if action_id and label:
            result.append({"id": action_id, "label": label})
    return result


def project_narrative_control_view(
    control: Mapping[str, Any] | None,
    *,
    host_label: str = "",
    input_locked: bool | None = None,
    pending_actions: Sequence[Mapping[str, Any]] | None = None,
    permissions: Mapping[str, Any] | None = None,
    available_actions: Sequence[Mapping[str, Any]] | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把 dm_control_states 行规范化为 SESSION INSPECTOR / 跑团现场共用视图。"""

    control = _mapping(control)
    mode_id = str(control.get("mode") or "auto").strip().lower()
    if mode_id not in CONTROL_MODE_LABELS:
        mode_id = "auto"
    phase_id = str(control.get("phase") or "auto").strip().lower()
    if phase_id not in CONTROL_PHASE_LABELS:
        phase_id = "auto"
    dm_user_id = str(control.get("active_dm_user_id") or "").strip()
    is_dm = mode_id == "dm"

    if is_dm:
        display_name = clean_label(host_label)
        if not display_name:
            display_name = "主持人不详"
    else:
        display_name = "AI 主持"

    locked = (
        bool(input_locked)
        if input_locked is not None
        else phase_id in _INPUT_LOCKED_PHASES
    )

    default_actions = (
        [
            {"id": "dm_enable", "label": "开启人工主持"},
        ]
        if not is_dm
        else [
            {"id": "dm_disable", "label": "返回 AI 主持"},
            {"id": "dm_directive", "label": "设置主持指引"},
            {"id": "dm_direct", "label": "追加叙事"},
            {"id": "dm_handoff", "label": "交棒"},
            {"id": "dm_checkpoint", "label": "创建检查点"},
        ]
    )

    view: dict[str, Any] = {
        "schema": "tavern-narrative-control/1.0.0-rc10",
        "mode": {"id": mode_id, "label": CONTROL_MODE_LABELS[mode_id]},
        "active_host": {
            "display_name": display_name,
            "character_name": clean_label(control.get("current_actor_ref")),
            "technical_ref": None,
        },
        "phase": {"id": phase_id, "label": CONTROL_PHASE_LABELS[phase_id]},
        "input_lock": {
            "locked": locked,
            "label": "输入已锁定" if locked else "玩家可以输入",
        },
        "pending_actions": _action_list(pending_actions),
        "permissions": (
            dict(permissions) if isinstance(permissions, Mapping) else {}
        ),
        "available_actions": (
            _action_list(available_actions)
            if available_actions is not None
            else default_actions
        ),
        "technical": None,
    }
    if include_technical_refs:
        technical: dict[str, Any] = {
            "session_id": str(control.get("session_id") or ""),
            "dm_user_id": dm_user_id,
            "directive": str(control.get("directive") or ""),
            "beat_no": int(control.get("beat_no") or 0),
            "current_actor_type": str(control.get("current_actor_type") or ""),
            "current_actor_ref": str(control.get("current_actor_ref") or ""),
            "revision": int(control.get("revision") or 0),
            "updated_at": str(control.get("updated_at") or ""),
        }
        view["technical"] = technical
    return view


__all__ = [
    "CONTROL_MODE_LABELS",
    "CONTROL_PHASE_LABELS",
    "project_narrative_control_view",
]
