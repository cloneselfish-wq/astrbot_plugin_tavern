"""ClockBoard projection with honest segments/time/state variants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import integer, number_or_none, text, visible
from .keys import OpaqueKeyFactory


_STATUS_LABELS = {
    "active": "进行中",
    "paused": "已暂停",
    "completed": "已触发",
    "triggered": "已触发",
    "archived": "已归档",
}


def project_clocks(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    keys: OpaqueKeyFactory,
    privileged: bool,
    limit: int = 6,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(rows or ()):
        if not isinstance(raw, Mapping):
            continue
        if not visible(raw.get("visibility"), privileged=privileged):
            continue
        label = text(raw.get("title") or raw.get("name"), limit=100)
        if not label:
            continue
        segments_raw = number_or_none(raw.get("segments"))
        current_raw = number_or_none(
            raw.get("current_value", raw.get("current"))
        )
        remaining = number_or_none(raw.get("remaining_seconds"))
        if segments_raw is not None and segments_raw > 0:
            clock_type = "segments"
            segments = int(segments_raw)
            current = (
                max(0, min(segments, int(current_raw)))
                if current_raw is not None
                else None
            )
        elif remaining is not None:
            clock_type = "time"
            segments = None
            current = None
            remaining = max(0, remaining)
        else:
            clock_type = "state"
            segments = None
            current = None
        status = text(raw.get("status"), limit=40, default="active").lower()
        threshold = raw.get("threshold")
        if isinstance(threshold, Mapping):
            threshold_summary = text(
                threshold.get("label") or threshold.get("summary"), limit=100
            )
        else:
            threshold_summary = text(threshold, limit=100)
        items.append(
            {
                "key": keys.key("clock", f"{index}:{label}"),
                "label": label,
                "type": clock_type,
                "current": current,
                "segments": segments,
                "remaining_seconds": remaining,
                "state": status,
                "state_label": _STATUS_LABELS.get(status, "状态待确认"),
                "threshold_summary": threshold_summary,
                "trigger_summary": text(
                    raw.get("trigger_text") or raw.get("trigger_summary"),
                    limit=160,
                ),
                "priority": integer(raw.get("priority"), 0),
                "updated_at": text(raw.get("updated_at"), limit=80),
            }
        )

    def urgency(item: Mapping[str, Any]) -> tuple[Any, ...]:
        active = 0 if item.get("state") == "active" else 1
        explicit = -integer(item.get("priority"), 0)
        if item.get("type") == "time" and item.get("remaining_seconds") is not None:
            pressure = float(item["remaining_seconds"])
        elif item.get("type") == "segments" and item.get("segments"):
            pressure = -float(item.get("current") or 0) / float(item["segments"])
        else:
            pressure = 0.0
        return (active, explicit, pressure, str(item.get("label")))

    items.sort(key=urgency)
    safe_total = len(items)
    limit = max(1, min(20, int(limit)))
    visible_items = items[:limit]
    return {
        "items": visible_items,
        "truncated": safe_total > len(visible_items),
        "total_items": safe_total,
        "problems": [],
    }


__all__ = ["project_clocks"]
