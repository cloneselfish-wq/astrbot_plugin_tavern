"""Player-visible turn-order notifications shared by Web and BOT adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..copy.entities import decorate_entity


_TURN_COMMAND_LABELS = {
    "reorder": "重新排序",
    "designate": "指定行动者",
    "skip": "跳过行动",
    "supersede_choices": "重置行动选项",
}


def turn_command_group_notice(
    command: str,
    turn: Mapping[str, Any],
) -> str:
    """Render a group-safe turn notice without account-ID name fallbacks."""

    current_name = str(turn.get("current_name") or "").strip()
    current_assigned = bool(turn.get("current_user_id"))
    order_names: list[str] = []
    missing_name = current_assigned and not current_name
    if command == "reorder":
        for item in turn.get("order", []):
            if not isinstance(item, Mapping):
                missing_name = True
                continue
            name = str(
                item.get("name")
                or item.get("character_name")
                or item.get("display_name")
                or ""
            ).strip()
            if not name:
                missing_name = True
                continue
            order_names.append(decorate_entity("character", name))
    if missing_name:
        return (
            "【行动顺序通知未完整显示】\n"
            "操作：公布本次行动顺序调整结果。\n"
            "原因：至少一名行动者缺少可公开显示的角色名称。\n"
            "自动处理：调整结果已保留；系统已中止详细通知，"
            "没有显示任何账号标识。\n"
            "下一步：请联系主持人修复角色资料后查看：\n\n"
            "/团 阵容"
        )
    command_label = _TURN_COMMAND_LABELS.get(command, "调整行动顺序")
    current = (
        decorate_entity("character", current_name)
        if current_name
        else "等待安排"
    )
    note = f"🎭 行动顺序已调整（{command_label}）\n当前行动者：{current}"
    if command == "reorder":
        note += "\n新顺序：" + " → ".join(order_names)
    return note


__all__ = ["turn_command_group_notice"]
