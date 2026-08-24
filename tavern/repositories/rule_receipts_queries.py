from __future__ import annotations

from .rules_support import *
from .configuration_revision_security import (
    chronological_fingerprint as _chronological_fingerprint,
    configuration_digest as _configuration_digest,
    safe_configuration_payload,
    sanitize_configuration_revisions,
)


class RuleReceiptsQueriesRepositoryMixin:
    def _record_provider_result(
        self,
        provider_id: str,
        success: bool,
        reason: str,
        probe: bool,
        probe_status: str,
        probe_latency_ms: int,
        probe_error_code: str,
        probe_expires_at: str,
        probe_idempotency_key: str,
    ) -> dict[str, Any]:
        provider_id = clean_text(provider_id, max_chars=200)
        if not provider_id:
            return {}
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if probe:
                normalized_probe_status = str(probe_status or "").strip()
                if normalized_probe_status not in {
                    "never", "running", "healthy", "unavailable"
                }:
                    raise ValueError("模型探测状态无效")
                connection.execute(
                    """
                    INSERT INTO provider_health(
                        provider_id, status, consecutive_failures,
                        last_failure_reason, last_failure_at,
                        last_success_at, circuit_until,
                        probe_status, last_probe_at,
                        last_probe_latency_ms, last_probe_error_code,
                        probe_expires_at, probe_idempotency_key, updated_at
                    ) VALUES (?, 'healthy', 0, '', '', '', '', ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id) DO UPDATE SET
                        probe_status = excluded.probe_status,
                        last_probe_at = excluded.last_probe_at,
                        last_probe_latency_ms = excluded.last_probe_latency_ms,
                        last_probe_error_code = excluded.last_probe_error_code,
                        probe_expires_at = excluded.probe_expires_at,
                        probe_idempotency_key = excluded.probe_idempotency_key,
                        updated_at = excluded.updated_at
                    """,
                    (
                        provider_id,
                        normalized_probe_status,
                        now,
                        max(0, int(probe_latency_ms)),
                        clean_text(probe_error_code, max_chars=120),
                        clean_text(probe_expires_at, max_chars=80),
                        clean_text(probe_idempotency_key, max_chars=240),
                        now,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM provider_health WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
                return dict(updated)
            failures = 0 if success else int(
                row["consecutive_failures"] if row else 0
            ) + 1
            status = "healthy"
            circuit_until = ""
            if not success and failures >= 3:
                status = "open"
                minutes = min(60, 5 * (2 ** min(3, failures - 3)))
                circuit_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes)
                ).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider_id, status, consecutive_failures,
                    last_failure_reason, last_failure_at, last_success_at,
                    circuit_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    status = excluded.status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_failure_reason = excluded.last_failure_reason,
                    last_failure_at = excluded.last_failure_at,
                    last_success_at = excluded.last_success_at,
                    circuit_until = excluded.circuit_until,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    status,
                    failures,
                    "" if success else clean_text(reason, max_chars=500),
                    "" if success else now,
                    now if success else (row["last_success_at"] if row else ""),
                    circuit_until,
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return dict(updated)

    async def filter_healthy_providers(
        self,
        provider_ids: Sequence[str],
    ) -> list[str]:
        return await self._run(
            self._filter_healthy_providers,
            list(provider_ids),
        )

    def _filter_healthy_providers(
        self,
        provider_ids: list[str],
    ) -> list[str]:
        normalized = list(
            dict.fromkeys(
                clean_text(item, max_chars=200)
                for item in provider_ids
                if clean_text(item, max_chars=200)
            )
        )
        if not normalized:
            return []
        now = datetime.now(timezone.utc)
        result: list[str] = []
        blocked: list[str] = []
        with self._connect() as connection:
            for provider_id in normalized:
                row = connection.execute(
                    "SELECT * FROM provider_health WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
                if not row or row["status"] != "open":
                    result.append(provider_id)
                    continue
                try:
                    until = datetime.fromisoformat(row["circuit_until"])
                except (TypeError, ValueError):
                    until = now
                if until <= now:
                    connection.execute(
                        """
                        UPDATE provider_health
                        SET status = 'half_open', updated_at = ?
                        WHERE provider_id = ?
                        """,
                        (utc_now(), provider_id),
                    )
                    result.append(provider_id)
                else:
                    blocked.append(provider_id)
        return result or blocked[:1]

    async def list_provider_health(self) -> list[dict[str, Any]]:
        return await self._run(self._list_provider_health)

    def _list_provider_health(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM provider_health
                    ORDER BY
                        CASE status WHEN 'open' THEN 0
                                    WHEN 'half_open' THEN 1 ELSE 2 END,
                        updated_at DESC
                    """
                ).fetchall()
            ]

    async def record_configuration_revision(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_configuration_revision,
            dict(payload),
            actor_id,
        )

    def _record_configuration_revision(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        encoded = json_dump(safe_configuration_payload(payload))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                sanitize_configuration_revisions(connection)
                row = connection.execute(
                    "SELECT * FROM configuration_revisions "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row is None or str(row["payload_json"] or "") != encoded:
                    next_revision = int(row["id"] or 0) + 1 if row else 1
                    fingerprint = _chronological_fingerprint(
                        encoded, next_revision
                    )
                    cursor = connection.execute(
                        """
                        INSERT INTO configuration_revisions(
                            fingerprint, payload_json, saved_by, saved_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (fingerprint, encoded, actor_id, now),
                    )
                    row = connection.execute(
                        "SELECT * FROM configuration_revisions WHERE id=?",
                        (cursor.lastrowid,),
                    ).fetchone()
                connection.execute("COMMIT")
                return {
                    "revision": int(row["id"]),
                    "latest_revision": int(row["id"]),
                    "fingerprint": str(row["fingerprint"] or ""),
                    "saved_by": row["saved_by"],
                    "saved_at": row["saved_at"],
                    "current": True,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def prepare_configuration_update(
        self,
        operation_id: str,
        expected_revision: int,
        current_payload: Mapping[str, Any],
        candidate_payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._prepare_configuration_update,
            operation_id,
            int(expected_revision),
            dict(current_payload),
            dict(candidate_payload),
            actor_id,
        )

    def _prepare_configuration_update(
        self,
        operation_id: str,
        expected_revision: int,
        current_payload: dict[str, Any],
        candidate_payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        request_key = clean_text(operation_id, max_chars=240)
        if not request_key:
            raise ValueError("保存设置需要幂等键")
        legacy_candidate_encoded = json_dump(candidate_payload)
        current_encoded = json_dump(safe_configuration_payload(current_payload))
        candidate_encoded = json_dump(safe_configuration_payload(candidate_payload))
        current_fingerprint = _configuration_digest(current_encoded)
        candidate_fingerprint = _configuration_digest(candidate_encoded)
        legacy_candidate_fingerprint = _configuration_digest(
            legacy_candidate_encoded
        )
        request_payload = {
            "expected_revision": int(expected_revision),
            "candidate_fingerprint": candidate_fingerprint,
        }
        input_hash = content_hash(request_payload)
        legacy_input_hash = content_hash(
            {
                "expected_revision": int(expected_revision),
                "candidate_fingerprint": legacy_candidate_fingerprint,
            }
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                sanitize_configuration_revisions(connection)
                latest = connection.execute(
                    """
                    SELECT * FROM configuration_revisions
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
                latest_revision = int(latest["id"] or 0) if latest else 0
                latest_fingerprint = (
                    _configuration_digest(str(latest["payload_json"] or ""))
                    if latest
                    else ""
                )
                receipt = connection.execute(
                    "SELECT * FROM operation_commits WHERE operation_id=?",
                    (request_key,),
                ).fetchone()
                if receipt is not None:
                    stored_input_hash = str(receipt["input_hash"] or "")
                    if stored_input_hash not in {input_hash, legacy_input_hash}:
                        raise DatabaseConflictError(
                            "相同幂等键已用于另一份设置修改"
                        )
                    reserved = json_load(receipt["result_json"], {})
                    stored_candidate = str(
                        reserved.get("candidate_fingerprint") or ""
                    )
                    if (
                        str(receipt["status"] or "") != "completed"
                        and stored_candidate == legacy_candidate_fingerprint
                        and legacy_candidate_fingerprint != candidate_fingerprint
                    ):
                        connection.execute(
                            """
                            UPDATE operation_commits
                            SET input_hash=?, result_json=?, updated_at=?
                            WHERE operation_id=? AND status<>'completed'
                            """,
                            (
                                input_hash,
                                json_dump(
                                    {
                                        "candidate_fingerprint": (
                                            candidate_fingerprint
                                        ),
                                        "credential_safe": True,
                                    }
                                ),
                                now,
                                request_key,
                            ),
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    if latest_revision != int(expected_revision):
                        raise DatabaseConflictError("设置修订已经变化")
                    if current_fingerprint == candidate_fingerprint:
                        next_revision = latest_revision + 1
                        cursor = connection.execute(
                            """
                            INSERT INTO configuration_revisions(
                                fingerprint, payload_json, saved_by, saved_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                _chronological_fingerprint(
                                    candidate_encoded, next_revision
                                ),
                                candidate_encoded,
                                clean_text(actor_id, max_chars=160),
                                now,
                            ),
                        )
                        row = connection.execute(
                            "SELECT * FROM configuration_revisions WHERE id=?",
                            (cursor.lastrowid,),
                        ).fetchone()
                        result = {
                            "revision": int(row["id"]),
                            "saved_at": str(row["saved_at"] or now),
                            "replayed": True,
                            "recovered": True,
                        }
                        connection.execute(
                            """
                            UPDATE operation_commits
                            SET status='completed', result_json=?, updated_at=?
                            WHERE operation_id=?
                            """,
                            (json_dump(result), now, request_key),
                        )
                        connection.execute("COMMIT")
                        return result
                    if latest_fingerprint and current_fingerprint != latest_fingerprint:
                        raise DatabaseConflictError(
                            "宿主设置在未记录修订的情况下发生变化"
                        )
                    connection.execute("COMMIT")
                    return {"status": "reserved", "retry": True}
                if latest_revision != int(expected_revision):
                    raise DatabaseConflictError("设置修订已经变化")
                if latest_fingerprint and current_fingerprint != latest_fingerprint:
                    raise DatabaseConflictError(
                        "宿主设置与当前修订不一致"
                    )
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, '', ?, 'reserved', ?, '{}', ?, ?)
                    """,
                    (
                        request_key,
                        input_hash,
                        json_dump(
                            {
                                "candidate_fingerprint": candidate_fingerprint,
                                "credential_safe": True,
                            }
                        ),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {"status": "reserved", "retry": False}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def complete_configuration_update(
        self,
        operation_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._complete_configuration_update,
            operation_id,
            dict(payload),
            actor_id,
        )

    def _complete_configuration_update(
        self,
        operation_id: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        request_key = clean_text(operation_id, max_chars=240)
        legacy_encoded = json_dump(payload)
        encoded = json_dump(safe_configuration_payload(payload))
        fingerprint = _configuration_digest(encoded)
        legacy_fingerprint = _configuration_digest(legacy_encoded)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                sanitize_configuration_revisions(connection)
                receipt = connection.execute(
                    "SELECT * FROM operation_commits WHERE operation_id=?",
                    (request_key,),
                ).fetchone()
                if receipt is None:
                    raise DatabaseConflictError("设置保存回执不存在")
                if str(receipt["status"] or "") == "completed":
                    result = json_load(receipt["result_json"], {})
                    result["replayed"] = True
                    connection.execute("COMMIT")
                    return result
                reserved = json_load(receipt["result_json"], {})
                reserved_fingerprint = str(
                    reserved.get("candidate_fingerprint") or ""
                )
                if reserved_fingerprint not in {
                    fingerprint,
                    legacy_fingerprint,
                }:
                    raise DatabaseConflictError("设置保存内容与预留回执不一致")
                if reserved_fingerprint != fingerprint:
                    connection.execute(
                        """
                        UPDATE operation_commits
                        SET result_json=?, updated_at=?
                        WHERE operation_id=? AND status='reserved'
                        """,
                        (
                            json_dump(
                                {
                                    "candidate_fingerprint": fingerprint,
                                    "credential_safe": True,
                                }
                            ),
                            now,
                            request_key,
                        ),
                    )
                latest = connection.execute(
                    "SELECT MAX(id) AS latest_id FROM configuration_revisions"
                ).fetchone()
                next_revision = int(latest["latest_id"] or 0) + 1
                cursor = connection.execute(
                    """
                    INSERT INTO configuration_revisions(
                        fingerprint, payload_json, saved_by, saved_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        _chronological_fingerprint(encoded, next_revision),
                        encoded,
                        clean_text(actor_id, max_chars=160),
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM configuration_revisions WHERE id=?",
                    (cursor.lastrowid,),
                ).fetchone()
                result = {
                    "revision": int(row["id"]),
                    "saved_at": str(row["saved_at"] or now),
                    "replayed": False,
                }
                connection.execute(
                    """
                    UPDATE operation_commits
                    SET status='completed', result_json=?, updated_at=?
                    WHERE operation_id=? AND status='reserved'
                    """,
                    (json_dump(result), now, request_key),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "settings.save",
                    "plugin",
                    {"revision": result["revision"]},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_operation_receipt,
            operation_id,
        )

    def _get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE operation_id = ? AND status = 'completed'
                """,
                (clean_text(operation_id, max_chars=240),),
            ).fetchone()
            if not row:
                return None
            return {
                "operation_id": row["operation_id"],
                "session_id": row["session_id"],
                "operation_type": row["operation_type"],
                "request": json_load(row["request_json"], {}),
                "result": json_load(row["result_json"], {}),
                "plan": json_load(row["plan_json"], {}),
                "rollback": json_load(row["rollback_json"], {}),
                "status": row["status"],
                "phase": row["phase"],
                "retry_count": int(row["retry_count"] or 0),
                "lease_expires_at": row["lease_expires_at"],
                "last_error_code": row["last_error_code"],
                "input_hash": row["input_hash"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def revoke_operation_receipt(
        self,
        operation_id: str,
    ) -> bool:
        """0.11.3：作废一个已锁定的操作回执（如本轮未提交时的骰值）。

        幂等：回执不存在时返回 False，不抛错。
        """
        return await self._run(
            self._revoke_operation_receipt,
            operation_id,
        )

    def _revoke_operation_receipt(
        self,
        operation_id: str,
    ) -> bool:
        operation_id = clean_text(operation_id, max_chars=240)
        if not operation_id:
            return False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                )
                connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception:
                connection.execute("ROLLBACK")
                raise
