"""DeliveryLanes projection for queued messages and synchronous turn bundles."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import integer, mapping, text
from .keys import OpaqueKeyFactory


_STATUS = {
    "pending": ("planned", "计划发送"),
    "queued": ("planned", "计划发送"),
    "leased": ("sending", "正在发送"),
    "sending": ("sending", "正在发送"),
    "delivered": ("confirmed", "平台已确认"),
    "skipped": ("confirmed", "已确认无需重发"),
    "partially_sent": ("failed", "部分送达"),
    "failed": ("failed", "发送失败"),
    "permanently_failed": ("failed", "发送失败"),
    "retry_wait": ("retry_wait", "等待重试"),
    "cancelled": ("cancelled", "已取消"),
    "webui_only": ("planned", "仅面板展示"),
}


def _safe_status(value: Any) -> tuple[str, str]:
    return _STATUS.get(text(value, limit=40).lower(), ("failed", "状态待确认"))


def _turn_bundle(
    raw: Mapping[str, Any],
    *,
    index: int,
    keys: OpaqueKeyFactory,
) -> dict[str, Any]:
    bundle_state, bundle_label = _safe_status(raw.get("status"))
    parts: list[dict[str, Any]] = []
    for part_index, part in enumerate(raw.get("parts") or ()):
        if not isinstance(part, Mapping):
            continue
        state, label = _safe_status(part.get("status"))
        parts.append(
            {
                "key": keys.key(
                    "segment",
                    f"turn:{index}:{part_index}:{part.get('kind') or ''}",
                ),
                "order": part_index + 1,
                "label": f"第 {part_index + 1} 段",
                "state": state,
                "state_label": label,
                "attempts": max(0, integer(part.get("attempts"), 0)),
                "last_safe_error": (
                    "该段未能送达；系统已保留确认过的段。"
                    if state == "failed"
                    else ""
                ),
            }
        )
    total_parts = max(0, integer(raw.get("total_parts"), len(parts)))
    confirmed = sum(1 for item in parts if item["state"] == "confirmed")
    next_index = max(0, integer(raw.get("next_part_index"), confirmed))
    remaining = max(0, total_parts - next_index)
    return {
        "_cursor_identity": text(
            raw.get("run_id")
            or raw.get("id")
            or raw.get("run_key")
            or raw.get("operation_id")
            or f"turn:{raw.get('created_at') or ''}:{index}",
            limit=300,
        ),
        "key": keys.key("delivery", f"turn:{index}:{raw.get('created_at') or ''}"),
        "label": "本轮同步回复",
        "channel_label": "同步回复",
        "state": bundle_state,
        "state_label": bundle_label,
        "segments": parts,
        "confirmed_segments": confirmed,
        "total_segments": total_parts,
        "remaining_segments": remaining,
        "attempts": max(0, integer(raw.get("attempt_count"), 0)),
        "next_retry_at": "",
        "last_safe_error": (
            "本轮回复尚未全部送达；已确认段不会重发。"
            if bundle_state in {"failed", "retry_wait"}
            else ""
        ),
        "capabilities": {"can_retry": False, "can_cancel": False},
        "updated_at": text(raw.get("updated_at"), limit=80),
    }


def _queued_bundle(
    raw: Mapping[str, Any],
    *,
    index: int,
    keys: OpaqueKeyFactory,
    privileged: bool,
) -> dict[str, Any]:
    state, state_label = _safe_status(raw.get("status"))
    sent = max(0, integer(raw.get("sent_parts"), 0))
    total = max(0, integer(raw.get("total_parts"), 0))
    remaining = max(0, total - sent) if total else 0
    label = text(
        raw.get("recipient_label") or raw.get("recipient_name"),
        limit=100,
        default="平台消息",
    )
    return {
        "_cursor_identity": text(
            raw.get("delivery_id")
            or f"queued:{raw.get('created_at') or ''}:{index}",
            limit=300,
        ),
        "key": keys.key("delivery", f"queued:{index}:{raw.get('status') or ''}"),
        "label": f"发给「{label}」",
        "channel_label": text(raw.get("channel"), limit=40, default="平台消息"),
        "state": state,
        "state_label": text(raw.get("status_label"), limit=60, default=state_label),
        "segments": [],
        "confirmed_segments": sent,
        "total_segments": total if total > 0 else None,
        "remaining_segments": remaining if total > 0 else None,
        "attempts": max(0, integer(raw.get("attempts"), 0)),
        "next_retry_at": text(raw.get("next_retry_at"), limit=80),
        "last_safe_error": (
            "消息暂未送达；系统已保留投递记录。" if state == "failed" else ""
        ),
        "capabilities": {
            "can_retry": bool(privileged and raw.get("can_retry")),
            "can_cancel": bool(privileged and raw.get("can_cancel")),
        },
        "updated_at": text(raw.get("updated_at"), limit=80),
    }


def project_deliveries(
    *,
    turn_runs: Sequence[Mapping[str, Any]] | None,
    queued: Sequence[Mapping[str, Any]] | None,
    privileged: bool,
    keys: OpaqueKeyFactory,
    cursor: str = "",
    page_size: int = 10,
) -> dict[str, Any]:
    all_items = [
        _turn_bundle(raw, index=index, keys=keys)
        for index, raw in enumerate(turn_runs or ())
        if isinstance(raw, Mapping)
    ]
    all_items.extend(
        _queued_bundle(raw, index=index, keys=keys, privileged=privileged)
        for index, raw in enumerate(queued or ())
        if isinstance(raw, Mapping)
    )
    all_items.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("_cursor_identity") or ""),
        ),
        reverse=True,
    )
    identities = [item["_cursor_identity"] for item in all_items]
    offset = keys.after_anchor("deliveries", cursor, identities) if cursor else 0
    page_size = max(1, min(50, int(page_size)))
    selected = all_items[offset : offset + page_size]
    items = [
        {key: value for key, value in item.items() if key != "_cursor_identity"}
        for item in selected
    ]
    next_offset = offset + len(items)
    has_more = next_offset < len(all_items)
    return {
        "items": items,
        "next_cursor": (
            keys.anchor_cursor("deliveries", selected[-1]["_cursor_identity"])
            if has_more and selected
            else ""
        ),
        "has_more": has_more,
        "page_size": page_size,
        "total_items": len(all_items),
        "problems": [],
    }


__all__ = ["project_deliveries"]
