"""Durable idempotency receipts for local TWP ZIP imports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..database_support import DatabaseConflictError, clean_text, json_dump, json_load, utc_now
from ..resolution_receipts import content_hash


def _scope(actor_id: str) -> str:
    return f"twp-local:{content_hash({'actor': actor_id})[:32]}"


def _operation_id(actor_id: str, key: str) -> str:
    normalized_key = clean_text(key, max_chars=200)
    return f"twp-local-import:{content_hash({'actor': actor_id, 'key': normalized_key})[:40]}"


def _safe_result(value: Mapping[str, Any]) -> dict[str, Any]:
    item = value.get("item") if isinstance(value.get("item"), Mapping) else {}
    preflight = value.get("preflight") if isinstance(value.get("preflight"), Mapping) else {}
    summary = preflight.get("summary") if isinstance(preflight.get("summary"), Mapping) else {}
    issues = preflight.get("issues") if isinstance(preflight.get("issues"), list) else []
    safe_summary = {
        clean_text(key, max_chars=80): (
            clean_text(entry, max_chars=500)
            if isinstance(entry, str)
            else entry
        )
        for key, entry in list(summary.items())[:30]
        if isinstance(entry, (str, int, float, bool)) or entry is None
    }
    safe_issues = []
    for entry in issues[:100]:
        if not isinstance(entry, Mapping):
            continue
        safe_issues.append({
            clean_text(key, max_chars=80): clean_text(field, max_chars=1000)
            for key, field in list(entry.items())[:12]
            if isinstance(field, (str, int, float, bool)) or field is None
        })
    return {
        "item": {
            "name": clean_text(item.get("name"), max_chars=200),
            "revision": int(item.get("revision") or 0),
        },
        "preflight": {
            "compatible": bool(preflight.get("compatible", True)),
            "issues": safe_issues,
            "summary": safe_summary,
            "artifact_hash": clean_text(preflight.get("artifact_hash"), max_chars=64),
            "source_hash": clean_text(preflight.get("source_hash"), max_chars=64),
        },
        "mode": clean_text(value.get("mode"), max_chars=40),
    }


class TwpImportRepositoryMixin:
    async def prepare_local_twp_import(self, actor_id: str, *, idempotency_key: str, archive_sha256: str) -> dict[str, Any]:
        return await self._run(self._prepare_local_twp_import, actor_id, idempotency_key, archive_sha256)

    def _prepare_local_twp_import(self, actor_id: str, key: str, archive_hash: str) -> dict[str, Any]:
        key = clean_text(key, max_chars=200)
        archive_hash = clean_text(archive_hash, max_chars=64)
        if (
            not actor_id
            or not key
            or len(archive_hash) != 64
            or any(character not in "0123456789abcdef" for character in archive_hash.lower())
        ):
            raise ValueError("本地世界包导入缺少有效的防重复凭证或文件摘要")
        operation_id = _operation_id(actor_id, key)
        request = {"archive_sha256": archive_hash}
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM operation_receipts WHERE operation_id=?", (operation_id,)).fetchone()
                if row is not None:
                    if str(row["session_id"]) != _scope(actor_id) or str(row["input_hash"]) != archive_hash:
                        raise DatabaseConflictError("相同防重复凭证已用于另一个本地世界包")
                    if str(row["status"]) == "completed":
                        result = json_load(row["result_json"], {})
                        connection.execute("COMMIT")
                        return {**dict(result), "replayed": True}
                    if str(row["status"]) not in {"reserved", "failed_retryable"}:
                        raise DatabaseConflictError("本地世界包导入不能从当前状态继续")
                    connection.execute("UPDATE operation_receipts SET status='reserved', phase='install_pending', last_error_code='', updated_at=? WHERE operation_id=?", (now, operation_id))
                else:
                    connection.execute("INSERT INTO operation_receipts(operation_id,session_id,operation_type,request_json,result_json,status,phase,input_hash,created_at,updated_at) VALUES(?,?, 'twp.local.import', ?, '{}','reserved','install_pending',?,?,?)", (operation_id, _scope(actor_id), json_dump(request), archive_hash, now, now))
                connection.execute("COMMIT")
                return {"replayed": False}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def complete_local_twp_import(self, actor_id: str, *, idempotency_key: str, archive_sha256: str, result: Mapping[str, Any]) -> dict[str, Any]:
        return await self._run(self._complete_local_twp_import, actor_id, idempotency_key, archive_sha256, dict(result))

    def _complete_local_twp_import(self, actor_id: str, key: str, archive_hash: str, result: dict[str, Any]) -> dict[str, Any]:
        operation_id = _operation_id(actor_id, key)
        safe = _safe_result(result)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM operation_receipts WHERE operation_id=?", (operation_id,)).fetchone()
                if row is None or str(row["session_id"]) != _scope(actor_id) or str(row["input_hash"]) != archive_hash:
                    raise DatabaseConflictError("本地世界包导入回执与请求不匹配")
                if str(row["status"]) == "completed":
                    replay = json_load(row["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(replay), "replayed": True}
                connection.execute("UPDATE operation_receipts SET result_json=?,status='completed',phase='installed',updated_at=? WHERE operation_id=?", (json_dump(safe), utc_now(), operation_id))
                connection.execute("COMMIT")
                return {**safe, "replayed": False}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fail_local_twp_import(self, actor_id: str, *, idempotency_key: str, error_code: str) -> None:
        await self._run(self._fail_local_twp_import, actor_id, idempotency_key, error_code)

    def _fail_local_twp_import(self, actor_id: str, key: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE operation_receipts SET status='failed_retryable',phase='install_failed',last_error_code=?,updated_at=? WHERE operation_id=? AND session_id=? AND status!='completed'", (clean_text(error_code, max_chars=120), utc_now(), _operation_id(actor_id, key), _scope(actor_id)))
