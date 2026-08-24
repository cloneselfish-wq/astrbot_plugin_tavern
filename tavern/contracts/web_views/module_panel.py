"""D1-UX-008 / D1-WEB-002：ModulePanelView 纯投影。

任务、阵营、NPC、时钟、知识、场景与结局面板共用该视图。页面是否存在的
判据是 ``capability_declared``，不是 ``items`` 是否为空；读取失败必须
进入 ``error`` 状态并给出明确说明，禁止用空数组掩盖。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..common import clean_label


MODULE_STATES = (
    "disabled",
    "waiting_initialization",
    "empty",
    "ready",
    "error",
)

MODULE_STATE_LABELS = {
    "disabled": "模块未启用",
    "waiting_initialization": "等待副本初始化",
    "empty": "暂无内容",
    "ready": "正常",
    "error": "读取失败",
}

_NOT_INITIALIZED_STATES = frozenset({"closed", "preparing"})


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def project_module_panel_view(
    *,
    module_id: str,
    label: str,
    capability_declared: bool,
    items: Sequence[Mapping[str, Any]] | None = None,
    problems: Sequence[Mapping[str, Any]] | None = None,
    session_state: str = "",
    initialized: bool | None = None,
    snapshot_info: str = "",
    available_actions: Sequence[str] | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """按五状态机投影一个模块面板。

    状态优先级：disabled → waiting_initialization → error → empty → ready。
    ``snapshot_info`` 形如“14:32 的上次成功数据”，仅在 error 且存在安全
    快照时由调用方传入。
    """

    module_id = str(module_id or "").strip()
    label = clean_label(label, "数据模块")
    items = list(items or [])
    problems = list(problems or [])

    if not capability_declared:
        state = "disabled"
        message = f"当前世界包未启用{label}模块。"
    elif initialized is False or str(session_state or "").strip() in _NOT_INITIALIZED_STATES:
        state = "waiting_initialization"
        message = f"副本尚未开演；开演后将载入初始{label}。"
    elif problems:
        state = "error"
        snapshot_text = f"正在显示{snapshot_info}。" if snapshot_info else ""
        message = (
            f"{label}数据读取失败，当前无法安全显示{label}。"
            f"系统没有修改副本数据，{snapshot_text}请刷新后重试。"
        )
    elif not items:
        state = "empty"
        message = f"{label}模块已启用，目前没有可展示的{label}。"
    else:
        state = "ready"
        message = ""

    view: dict[str, Any] = {
        "schema": "tavern-module-panel/1.0.0-rc10",
        "module_id": module_id,
        "label": label,
        "capability_declared": bool(capability_declared),
        "state": state,
        "state_label": MODULE_STATE_LABELS[state],
        "message": message,
        "summary": {"count": len(items)},
        "items": items,
        "available_actions": [
            clean_label(item) for item in (available_actions or ()) if clean_label(item)
        ],
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = {
            "module_id": module_id,
            "problem_count": len(problems),
        }
    return view


__all__ = [
    "MODULE_STATE_LABELS",
    "MODULE_STATES",
    "project_module_panel_view",
]
