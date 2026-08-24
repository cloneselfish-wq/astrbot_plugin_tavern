"""D1-UX-013 / D1-WEB-012：ActorFateView 与 TerminalView 纯投影。

角色命运输入适配 18_ACTOR_FATE_AND_TERMINAL_CONDITIONS.md 的世界声明
（``states[]`` 含 ``id/label/terminal/can_act``）与运行期状态映射；终局输入
适配 ``session_archives`` 行（``termination_type/reason/ended_by/readonly``）。
普通视图不输出 condition ID、幂等键或内部状态 ID。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..common import clean_label


TERMINATION_LABELS = {
    "completed": "正常",
    "failed": "失败",
    "aborted": "强制终止",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = clean_label(value)
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [clean_label(item) for item in value if clean_label(item)]
    return []


def project_actor_fate_view(
    *,
    actor: str = "",
    state: Mapping[str, Any] | None = None,
    can_act: bool | None = None,
    rescue_window: Mapping[str, Any] | None = None,
    available_actions: Sequence[str] | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把一个角色的命运状态规范化为队伍状态 / 角色卡共用视图。"""

    actor = clean_label(actor, "角色名称缺失")
    state = _mapping(state)
    state_id = str(state.get("id") or "unknown").strip()
    state_label = clean_label(state.get("label"), "状态未知")
    terminal = bool(state.get("terminal"))
    can_act_value = (
        bool(can_act)
        if can_act is not None
        else bool(state.get("can_act"))
    )
    window = _mapping(rescue_window)
    if window:
        rescue_view = {
            "open": bool(window.get("open")),
            "message": clean_label(
                window.get("message"),
                "需要在下一次场景推进前完成救援。",
            ),
        }
    else:
        rescue_view = {"open": False, "message": ""}
    view: dict[str, Any] = {
        "schema": "tavern-actor-fate/1.0.0-rc10",
        "actor": actor,
        "state": {"id": state_id, "label": state_label},
        "terminal": terminal,
        "can_act": can_act_value,
        "rescue_window": rescue_view,
        "available_actions": _text_list(available_actions),
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = {
            "state_id": state_id,
            "terminal": terminal,
        }
    return view


def project_terminal_view(
    archive: Mapping[str, Any] | None = None,
    *,
    ending_label: str = "",
    result_label: str = "",
    reason: str = "",
    readonly: bool | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把归档记录规范化为终局视图（正常 / 失败 / 强制终止）。"""

    archive = _mapping(archive)
    termination_type = str(
        archive.get("termination_type") or "aborted"
    ).strip().lower()
    if termination_type not in TERMINATION_LABELS:
        termination_type = "aborted"
    ending = clean_label(ending_label)
    if not ending:
        ending = clean_label(archive.get("ending_label"), "故事已结束")
    result = clean_label(result_label)
    if not result:
        result = TERMINATION_LABELS[termination_type]
    cause = clean_label(reason)
    if not cause:
        cause = clean_label(archive.get("reason"), "未提供结束原因。")
    readonly_value = (
        bool(readonly) if readonly is not None else bool(archive.get("readonly"))
    )
    view: dict[str, Any] = {
        "schema": "tavern-terminal/1.0.0-rc10",
        "ending_label": ending,
        "result_label": result,
        "reason": cause,
        "archive_label": "永久归档" if readonly_value else "已归档",
        "readonly": readonly_value,
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = {
            "termination_type": termination_type,
            "condition_id": str(archive.get("condition_id") or ""),
            "final_snapshot_id": str(archive.get("final_snapshot_id") or ""),
            "ended_by": str(archive.get("ended_by") or ""),
            "ended_at": str(archive.get("ended_at") or ""),
        }
    return view


def project_technical_detail_view(
    *,
    title: str = "技术详情",
    fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """技术详情容器：默认折叠、仅授权角色可见，与普通文案分离。"""

    return {
        "schema": "tavern-technical-detail/1.0.0-rc10",
        "title": clean_label(title, "技术详情"),
        "fields": dict(fields) if isinstance(fields, Mapping) else {},
    }


__all__ = [
    "TERMINATION_LABELS",
    "project_actor_fate_view",
    "project_technical_detail_view",
    "project_terminal_view",
]
