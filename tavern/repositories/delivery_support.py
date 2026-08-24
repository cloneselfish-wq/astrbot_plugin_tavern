"""D1 Schema 20 persistent delivery outbox.

在 C6 最小 outbox 基础上补齐 D1-DEL-005/006 要求的：租约、下一次重试
时间、指数退避、物理分片游标、目标快照、优先级、audience 与完整状态机。
独立后台 worker 通过 ``claim_deliveries`` 原子领取租约；``finish_delivery``
失败时进入 ``retry_wait``/``permanently_failed``，不再依赖下一条入站消息补发。
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Sequence

from ..database_support import (
    insert_session_event,
    json_dump,
    json_load,
    new_id,
    retry_backoff_after,
    utc_now,
)
from ..delivery.privacy import AUDIENCE_PRIVATE_OWNER


# D1 完整状态：pending / leased / partially_sent / retry_wait / delivered /
# permanently_failed / cancelled / webui_only；
# 保留 sent / delivered_on_reply / dismissed 供既有调用方读取。
DELIVERY_ACTIVE_STATUSES = frozenset(
    {"pending", "leased", "partially_sent", "retry_wait"}
)
DELIVERY_DEFAULT_MAX_ATTEMPTS = 8
DELIVERY_LEASE_TTL_SECONDS = 300

#: 投递状态 -> 玩家可见中文状态（event:delivery.updated payload 只含安全语义）。
_DELIVERY_STATUS_LABELS = {
    "delivered": "已送达",
    "delivered_on_reply": "已送达（回复时）",
    "partially_sent": "部分送达",
    "retry_wait": "等待重试",
    "permanently_failed": "发送失败（已达上限）",
    "cancelled": "已取消",
}


def _json_or_text(value: Any) -> Any:
    """DB JSON 列 → 原值：合法 JSON 解析为对象，纯文本原样返回。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return json_load(value, value)
    return value


def _delivery_record(row: Any) -> dict[str, Any]:
    """DB 行 → DeliveryOutboxRepository 协议记录（service/worker 使用）。"""

    return {
        "delivery_id": str(row["id"] or ""),
        "session_id": str(row["session_id"] or ""),
        "audience": str(row["audience"] or "player"),
        "target_snapshot": _json_or_text(row["target_snapshot_json"]) or {},
        "message_type": str(row["kind"] or "notice"),
        "projection_snapshot": _json_or_text(row["projection_snapshot"]),
        "rendered_parts": _json_or_text(row["rendered_parts_json"]) or [],
        "next_part_index": int(row["next_part_index"] or 0),
        "status": str(row["status"] or "pending"),
        "priority": int(row["priority"] or 100),
        "attempts": int(row["attempts"] or 0),
        "next_retry_at": str(row["next_retry_at"] or ""),
        "last_error_code": str(row["last_error_code"] or ""),
        "last_error_message": str(row["last_error"] or ""),
        "dedupe_key": str(row["dedupe_key"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "delivered_at": str(row["delivered_at"] or ""),
        "cancelled_at": str(row["cancelled_at"] or ""),
        "lease_token": str(row["lease_owner"] or ""),
        "lease_until": str(row["leased_at"] or ""),
        "meta": _json_or_text(row["meta_json"]) or {},
    }


def _turn_part_record(row: Any) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"] or ""),
        "part_index": int(row["part_index"] or 0),
        "kind": str(row["kind"] or "notice"),
        "message_type": str(row["message_type"] or ""),
        "dedupe_key": str(row["dedupe_key"] or ""),
        "payload": json_load(row["payload_json"], {}),
        "rendered_text": str(row["rendered_text"] or ""),
        "status": str(row["status"] or "pending"),
        "attempts": int(row["attempts"] or 0),
        "last_error": str(row["last_error"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "delivered_at": str(row["delivered_at"] or ""),
    }


def _turn_run_record(row: Any, parts: Sequence[Any]) -> dict[str, Any]:
    projected_parts = [_turn_part_record(part) for part in parts]
    return {
        "run_id": str(row["id"] or ""),
        "run_key": str(row["run_key"] or ""),
        "session_id": str(row["session_id"] or ""),
        "operation_id": str(row["operation_id"] or ""),
        "actor_id": str(row["actor_id"] or ""),
        "state_revision": str(row["state_revision"] or ""),
        "origin": str(row["origin"] or ""),
        "status": str(row["status"] or "pending"),
        "next_part_index": int(row["next_part_index"] or 0),
        "total_parts": int(row["total_parts"] or 0),
        "attempt_count": int(row["attempt_count"] or 0),
        "last_error": str(row["last_error"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "delivered_at": str(row["delivered_at"] or ""),
        "cancelled_at": str(row["cancelled_at"] or ""),
        "parts": projected_parts,
        "delivered_dedupes": {
            part["dedupe_key"]
            for part in projected_parts
            if part["status"] in {"delivered", "skipped"}
            and part["dedupe_key"]
        },
    }



__all__ = [name for name in globals() if not name.startswith('__')]
