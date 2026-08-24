"""Persistent idempotency helpers shared by card review and supplement."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .database_support import DatabaseConflictError, json_load


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_expected_revision(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正整数")
    try:
        revision = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if revision < 1:
        raise ValueError(f"{label}必须是正整数")
    return revision


def require_idempotency_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key:
        raise ValueError("缺少幂等请求键，系统没有执行写入")
    if len(key) > 200:
        raise ValueError("幂等请求键过长，系统没有执行写入")
    return key


def replay_receipt(
    row: Any,
    *,
    fingerprint: str,
) -> dict[str, Any] | None:
    if row is None:
        return None
    if str(row["request_fingerprint"] or "") != str(fingerprint):
        raise DatabaseConflictError(
            "idempotency.payload_conflict：同一请求键对应的操作内容不同，"
            "系统没有覆盖原结果"
        )
    result = json_load(row["result_json"], {})
    if not isinstance(result, dict):
        raise RuntimeError("幂等回执内容损坏，系统已停止重复执行")
    return {**result, "idempotent": True}
