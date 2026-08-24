"""QuestTracks projection from the existing semantic quest view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import mapping, number_or_none, text
from .keys import OpaqueKeyFactory


_SAFE_STATES = {
    "available",
    "active",
    "blocked",
    "completed",
    "failed",
    "abandoned",
}


def _problem_rows(value: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in value if isinstance(value, Sequence) else ():
        if not isinstance(raw, Mapping):
            continue
        rows.append(
            {
                "code": text(raw.get("code"), limit=80, default="projection.quest"),
                "message": text(
                    raw.get("message"),
                    limit=160,
                    default="任务数据暂时不完整。",
                ),
                "recovery": "请刷新任务板块；若仍缺失，请联系主持人。",
                "retryable": True,
            }
        )
    return rows


def project_quest_tracks(
    quest_view: Mapping[str, Any] | None,
    *,
    keys: OpaqueKeyFactory,
    limit: int = 5,
) -> dict[str, Any]:
    source = mapping(quest_view)
    projected: list[dict[str, Any]] = []
    for index, raw in enumerate(source.get("items") or ()):
        if not isinstance(raw, Mapping):
            continue
        label = text(raw.get("label"), limit=100)
        if not label:
            continue
        raw_state = text(raw.get("status_id"), limit=40).lower()
        state = raw_state if raw_state in _SAFE_STATES else "unknown"
        current_value = number_or_none(raw.get("completed_objectives"))
        total_value = number_or_none(raw.get("total_objectives"))
        current = (
            max(0, int(current_value)) if current_value is not None else None
        )
        total = max(0, int(total_value)) if total_value is not None else None
        if current is not None and total is not None:
            current = min(total, current)
        objectives = [
            text(item, limit=120)
            for item in raw.get("current_objectives") or ()
            if text(item, limit=120)
        ][:3]
        projected.append(
            {
                "key": keys.key("quest", f"{index}:{label}"),
                "label": label,
                "state": state,
                "state_label": text(raw.get("status_label"), limit=60),
                "state_description": text(
                    raw.get("status_description"), limit=160
                ),
                "current": current,
                "total": total,
                "phase": text(raw.get("phase"), limit=60),
                "current_objectives": objectives,
                "blocked_reason": text(raw.get("blocked_reason"), limit=160),
                "urgency": (
                    text(raw.get("urgency"), limit=40) or None
                ),
                "recent_change": text(raw.get("recent_change"), limit=120),
                "capabilities": [
                    {
                        "action": text(action, limit=40),
                        "enabled": True,
                    }
                    for action in raw.get("available_actions") or ()
                    if text(action, limit=40)
                ][:3],
            }
        )

    state_order = {
        "blocked": 0,
        "active": 1,
        "available": 2,
        "failed": 3,
        "abandoned": 4,
        "completed": 5,
        "unknown": 6,
    }
    projected.sort(
        key=lambda item: (
            state_order.get(str(item.get("state")), 6),
            str(item.get("label")),
        )
    )
    safe_total = len(projected)
    limit = max(1, min(20, int(limit)))
    visible_items = projected[:limit]
    return {
        "items": visible_items,
        "truncated": safe_total > len(visible_items),
        "total_items": safe_total,
        "completed_collapsed": sum(
            1 for item in projected if item.get("state") == "completed"
        ),
        "problems": _problem_rows(source.get("problems")),
    }


__all__ = ["project_quest_tracks"]
