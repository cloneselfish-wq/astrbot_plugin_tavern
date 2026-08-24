"""Credential-safe invariants for persisted configuration revisions."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from ..config import safe_config_projection
from ..database_support import json_dump


def safe_configuration_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("配置修订必须是对象")
    return safe_config_projection(payload)


def configuration_digest(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def chronological_fingerprint(encoded: str, revision: int) -> str:
    return f"{configuration_digest(encoded)}:{max(1, int(revision))}"


def safe_configuration_revision_row(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(row)
    try:
        revision = int(result.get("id") or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("配置修订 ID 无效") from error
    if revision < 1:
        raise ValueError("配置修订 ID 无效")
    try:
        payload = json.loads(str(result.get("payload_json") or ""))
    except json.JSONDecodeError as error:
        raise ValueError(f"配置修订 {revision} 内容损坏") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"配置修订 {revision} 必须是对象")
    encoded = json_dump(safe_configuration_payload(payload))
    result["payload_json"] = encoded
    result["fingerprint"] = chronological_fingerprint(encoded, revision)
    return result


def sanitize_configuration_revisions(
    connection: sqlite3.Connection,
    *,
    abandon_reserved: bool = False,
) -> dict[str, int]:
    """Rewrite legacy rows in the caller's transaction, preserving row order."""

    rows = connection.execute(
        "SELECT * FROM configuration_revisions ORDER BY id"
    ).fetchall()
    updated = 0
    for source in rows:
        original = dict(source)
        safe = safe_configuration_revision_row(original)
        if (
            str(original.get("payload_json") or "") == safe["payload_json"]
            and str(original.get("fingerprint") or "") == safe["fingerprint"]
        ):
            continue
        connection.execute(
            """
            UPDATE configuration_revisions
            SET payload_json=?, fingerprint=?
            WHERE id=?
            """,
            (safe["payload_json"], safe["fingerprint"], int(original["id"])),
        )
        updated += 1
    abandoned = (
        abandon_legacy_reserved_configuration_operations(connection)
        if abandon_reserved
        else 0
    )
    return {"scanned": len(rows), "updated": updated, "abandoned": abandoned}


def abandon_legacy_reserved_configuration_operations(
    connection: sqlite3.Connection,
) -> int:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_commits'"
    ).fetchone()
    if table is None:
        return 0
    rows = connection.execute(
        """
        SELECT * FROM operation_commits
        WHERE session_id='' AND status='reserved'
        ORDER BY created_at, operation_id
        """
    ).fetchall()
    abandoned = 0
    for row in rows:
        try:
            result = json.loads(str(row["result_json"] or "{}"))
            rollback = json.loads(str(row["rollback_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        candidate = result.get("candidate_fingerprint") if isinstance(result, Mapping) else None
        if (
            not isinstance(result, Mapping)
            or set(result) != {"candidate_fingerprint"}
            or not isinstance(candidate, str)
            or not re.fullmatch(r"[0-9a-f]{64}", candidate)
            or not isinstance(rollback, Mapping)
            or bool(rollback)
            or not re.fullmatch(r"[0-9a-f]{64}", str(row["input_hash"] or ""))
        ):
            continue
        operation_id = str(row["operation_id"] or "")
        audit = {
            "error_code": "settings.legacy_reservation_abandoned",
            "requires_new_idempotency_key": True,
        }
        connection.execute(
            """
            UPDATE operation_commits
            SET input_hash=?, status='failed', result_json=?, rollback_json=?
            WHERE operation_id=? AND status='reserved'
            """,
            (
                configuration_digest(f"abandoned-settings:{operation_id}"),
                json_dump(audit),
                json_dump({"reason": "credential_verifier_removed"}),
                operation_id,
            ),
        )
        abandoned += 1
    return abandoned


__all__ = [
    "chronological_fingerprint",
    "abandon_legacy_reserved_configuration_operations",
    "configuration_digest",
    "safe_configuration_payload",
    "safe_configuration_revision_row",
    "sanitize_configuration_revisions",
]
