from __future__ import annotations

from typing import Any


def health_state(value: Any) -> str:
    return {
        "ready": "正常",
        "healthy": "正常",
        "degraded": "正在恢复",
        "blocked": "不可用",
        "maintenance": "维护中",
        "unknown": "尚未确认",
    }.get(str(value or "").strip().lower(), "尚未确认")


def health_summary(label: str, state: str) -> str:
    if state == "正常":
        return f"{label}运行正常。"
    if state == "正在恢复":
        return f"{label}正在自动恢复，相关操作可能变慢。"
    if state == "不可用":
        return f"{label}暂时不可用，相关操作会受影响。"
    if state == "维护中":
        return f"{label}处于维护窗口，危险写入保持暂停。"
    return f"{label}状态尚未确认。"


__all__ = ["health_state", "health_summary"]

