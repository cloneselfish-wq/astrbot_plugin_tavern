"""Version-independent presentation for persisted domain states.

The raw values in this module are accepted only as lookup keys.  Callers expose
the returned Chinese label, public tone and symbol, never the lookup value.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


UNKNOWN_STATE_LABEL = "数据状态无法识别"


_STATE_FAMILIES: Mapping[str, Mapping[str, tuple[str, str, str]]] = {
    "session": {
        "closed": ("已关闭", "neutral", "■"),
        "preparing": ("准备中", "neutral", "◷"),
        "running": ("进行中", "beneficial", "▶"),
        "paused": ("已暂停", "warning", "Ⅱ"),
        "finished": ("已结束", "neutral", "✓"),
        "maintenance": ("维护中", "warning", "↻"),
    },
    "participation": {
        "reserved": ("已占席", "neutral", "○"),
        "active": ("参与中", "beneficial", "●"),
        "standby": ("候补", "neutral", "◷"),
        "away": ("暂离", "warning", "…"),
        "retired": ("已退场", "neutral", "■"),
        "archived": ("已归档", "neutral", "□"),
    },
    "choice": {
        "active": ("待选择", "neutral", "●"),
        "selected": ("已选择", "beneficial", "✓"),
        "superseded": ("已替换", "neutral", "↗"),
        "cancelled": ("已取消", "neutral", "×"),
    },
    "vote": {
        "open": ("投票中", "neutral", "●"),
        "decided": ("已得出结果", "beneficial", "✓"),
        "resolved": ("已完成", "beneficial", "✓"),
        "rejected": ("已否决", "warning", "×"),
        "cancelled": ("已取消", "neutral", "×"),
        "needs_recovery": ("待恢复", "warning", "↻"),
    },
    "timer": {
        "active": ("进行中", "warning", "●"),
        "paused": ("已暂停", "warning", "Ⅱ"),
    },
    "quest": {
        "available": ("可接取", "neutral", "○"),
        "active": ("进行中", "beneficial", "▶"),
        "blocked": ("受阻", "warning", "!"),
        "completed": ("已完成", "beneficial", "✓"),
        "failed": ("已失败", "harmful", "×"),
        "abandoned": ("已放弃", "neutral", "■"),
    },
    "clock": {
        "active": ("进行中", "warning", "●"),
        "paused": ("已暂停", "warning", "Ⅱ"),
        "completed": ("已触发", "beneficial", "✓"),
        "triggered": ("已触发", "beneficial", "✓"),
        "archived": ("已归档", "neutral", "□"),
    },
    "relation": {
        "active": ("当前可见", "neutral", "●"),
    },
}


def public_state(
    value: Any,
    *,
    family: str,
    problem_code: str,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Return display-only state data and a local problem for unknown values."""

    raw = str(value or "").strip().casefold()
    presentation = _STATE_FAMILIES.get(str(family), {}).get(raw)
    if presentation is not None:
        label, tone, symbol = presentation
        return {"label": label, "tone": tone, "symbol": symbol}, None
    return (
        {"label": UNKNOWN_STATE_LABEL, "tone": "unknown", "symbol": "?"},
        {
            "code": str(problem_code or "visual.state.unknown"),
            "message": UNKNOWN_STATE_LABEL,
            "recovery": "请刷新当前板块；系统不会显示无法识别的内部状态。",
            "retryable": True,
        },
    )


def public_state_fields(
    value: Any,
    *,
    family: str,
    problem_code: str,
    value_key: str = "state",
    prefix: str = "state",
) -> dict[str, Any]:
    presentation, problem = public_state(
        value, family=family, problem_code=problem_code
    )
    result: dict[str, Any] = {
        value_key: presentation["label"],
        f"{prefix}_label": presentation["label"],
        f"{prefix}_tone": presentation["tone"],
        f"{prefix}_symbol": presentation["symbol"],
    }
    if problem is not None:
        result[f"{prefix}_problem"] = problem
    return result


def public_state_rows(
    data: dict[str, Any],
    *,
    collection: str,
    family: str,
    problem_code: str,
) -> dict[str, Any]:
    for item in data.get(collection) or ():
        if isinstance(item, dict):
            item.update(
                public_state_fields(
                    item.get("state"),
                    family=family,
                    problem_code=problem_code,
                )
            )
    return data


__all__ = [
    "UNKNOWN_STATE_LABEL",
    "public_state",
    "public_state_fields",
    "public_state_rows",
]
