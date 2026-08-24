from __future__ import annotations

from .rules_support import *


_STORY_OPERATION_TYPES = frozenset({"turn", "vote_resolution", "dm_beat"})


def _operation_input_hash(
    operation_type: str,
    request_payload: Mapping[str, Any],
) -> str:
    """Hash caller input without reminder metadata added after reservation.

    ``arm_generation_reminder`` persists its frozen runtime snapshot beside the
    original turn request so recovery and backup projections can read it.  That
    snapshot is not caller input and must not change the operation identity.
    """

    identity_payload = dict(request_payload)
    if str(operation_type) in _STORY_OPERATION_TYPES:
        identity_payload.pop("reminder_config", None)
    return content_hash(identity_payload)


class RuleRuntimeRepositoryMixin:
    async def save_instance_time_rules(
        self,
        session_id: str,
        rules: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_time_rules(rules)
        await self._run(
            self._save_instance_time_rules,
            session_id,
            normalized,
            actor_id,
        )
        return await self.get_instance_config(session_id)

    def _save_instance_time_rules(
        self,
        session_id: str,
        rules: dict[str, Any],
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM instance_configs WHERE session_id = ?",
                    (session_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("副本配置不存在")
                self._assert_session_writable(connection, session_id)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET time_rules_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(rules), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timing.rules_update",
                    session_id,
                    {"rules": rules},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_operation_state(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._get_operation_state, operation_id)

    def _get_operation_state(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT operation_id, session_id, operation_type, status,
                       phase, result_json, cancel_requested_at,
                       cancel_requested_by, last_progress_stage,
                       last_progress_at, reminder_acknowledged,
                       reminder_enabled, reminder_interval_seconds,
                       reminder_sequence, reminder_config_revision,
                       reminder_last_at, reminder_next_at,
                       committed_revision, updated_at
                FROM operation_receipts
                WHERE operation_id = ?
                """,
                (clean_text(operation_id, max_chars=240),),
            ).fetchone()
            if not row:
                return None
            return {
                "operation_id": row["operation_id"],
                "session_id": row["session_id"],
                "operation_type": row["operation_type"],
                "status": row["status"],
                "phase": row["phase"],
                "result": json_load(row["result_json"], {}),
                "cancel_requested_at": row["cancel_requested_at"],
                "cancel_requested_by": row["cancel_requested_by"],
                "last_progress_stage": row["last_progress_stage"],
                "last_progress_at": row["last_progress_at"],
                "reminder": {
                    "acknowledged": bool(row["reminder_acknowledged"]),
                    "enabled": bool(row["reminder_enabled"]),
                    "interval_seconds": int(
                        row["reminder_interval_seconds"] or 60
                    ),
                    "sequence": int(row["reminder_sequence"] or 0),
                    "config_revision": int(
                        row["reminder_config_revision"] or 0
                    ),
                    "last_at": row["reminder_last_at"],
                    "next_at": row["reminder_next_at"],
                },
                "committed_revision": int(row["committed_revision"] or 0),
                "updated_at": row["updated_at"],
            }

    async def active_session_operation(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_session_operation, session_id)

    def _active_session_operation(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM operation_receipts
                WHERE session_id=? AND status IN ({placeholders})
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (session_id, *sorted(ACTIVE_OPERATION_STATUSES)),
            ).fetchone()
            return self._operation_public(row) if row else None

    async def latest_retryable_session_operation(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._latest_retryable_session_operation,
            session_id,
        )

    def _latest_retryable_session_operation(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE session_id=? AND status='failed_retryable'
                  AND operation_type IN ('turn', 'vote_resolution')
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return self._operation_public(row) if row else None

    @staticmethod
    def _operation_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "operation_id": row["operation_id"],
            "session_id": row["session_id"],
            "operation_type": row["operation_type"],
            "request": json_load(row["request_json"], {}),
            "result": json_load(row["result_json"], {}),
            "status": row["status"],
            "phase": row["phase"],
            "retry_count": int(row["retry_count"] or 0),
            "lease_expires_at": row["lease_expires_at"],
            "last_error_code": row["last_error_code"],
            "cancel_requested_at": row["cancel_requested_at"],
            "cancel_requested_by": row["cancel_requested_by"],
            "last_progress_stage": row["last_progress_stage"],
            "last_progress_at": row["last_progress_at"],
            "reminder_enabled": bool(row["reminder_enabled"]),
            "reminder_interval_seconds": int(
                row["reminder_interval_seconds"] or 60
            ),
            "reminder_sequence": int(row["reminder_sequence"] or 0),
            "reminder_config_revision": int(
                row["reminder_config_revision"] or 0
            ),
            "last_reminder_at": row["reminder_last_at"],
            "next_reminder_at": row["reminder_next_at"],
            "committed_revision": int(row["committed_revision"] or 0),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def request_session_operation_cancel(
        self,
        session_id: str,
        actor_id: str,
        *,
        operation_id: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._request_session_operation_cancel,
            session_id,
            actor_id,
            operation_id,
            expected_revision,
            idempotency_key,
            reason,
        )

    def _request_session_operation_cancel(
        self,
        session_id: str,
        actor_id: str,
        operation_id: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        placeholders = ",".join("?" for _ in ACTIVE_OPERATION_STATUSES)
        requested_operation = clean_text(operation_id, max_chars=240)
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "operation_id": requested_operation,
            "expected_revision": expected_revision,
            "reason": clean_text(reason, max_chars=500),
        }
        input_hash = content_hash(request_payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request_key:
                    commit = connection.execute(
                        "SELECT * FROM operation_commits WHERE operation_id=?",
                        (request_key,),
                    ).fetchone()
                    if commit is not None:
                        if str(commit["input_hash"] or "") != input_hash:
                            raise DatabaseConflictError(
                                "相同幂等键已用于另一份事务取消请求"
                            )
                        if str(commit["status"] or "") == "completed":
                            replay = json_load(commit["result_json"], {})
                            replay["replayed"] = True
                            connection.execute("COMMIT")
                            return replay
                        raise DatabaseConflictError(
                            "事务取消请求仍在处理中，请稍后重试"
                        )
                if requested_operation:
                    row = connection.execute(
                        """
                        SELECT * FROM operation_receipts
                        WHERE operation_id=? AND session_id=?
                        """,
                        (requested_operation, session_id),
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"""
                        SELECT * FROM operation_receipts
                        WHERE session_id=? AND status IN ({placeholders})
                        ORDER BY updated_at DESC, created_at DESC LIMIT 1
                        """,
                        (session_id, *sorted(ACTIVE_OPERATION_STATUSES)),
                    ).fetchone()
                if row is None:
                    if requested_operation:
                        raise DatabaseNotFoundError("事务不存在或不属于当前副本")
                    connection.execute("COMMIT")
                    return {
                        "found": False,
                        "status": "none",
                        "session_id": session_id,
                    }
                if (
                    expected_revision is not None
                    and operation_revision(dict(row))
                    != int(expected_revision)
                ):
                    raise DatabaseConflictError("事务状态已经变化")
                current = str(row["status"] or "")
                if current == "cancel_requested":
                    result = {
                        **self._operation_public(row),
                        "found": True,
                        "changed": False,
                    }
                    if request_key:
                        connection.execute(
                            """
                            INSERT INTO operation_commits(
                                operation_id, session_id, input_hash, status,
                                result_json, rollback_json, created_at, updated_at
                            ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                            """,
                            (
                                request_key,
                                session_id,
                                input_hash,
                                json_dump(result),
                                now,
                                now,
                            ),
                        )
                    connection.execute("COMMIT")
                    return result
                transition = OperationStateMachine.transition(
                    current,
                    "cancel_requested",
                )
                if not transition.allowed or current not in CANCELLABLE_OPERATION_STATUSES:
                    connection.execute("COMMIT")
                    return {**self._operation_public(row), "found": True, "changed": False}
                cursor = connection.execute(
                    f"""
                    UPDATE operation_receipts SET
                        status='cancel_requested', phase='cancel_requested',
                        cancel_requested_at=?, cancel_requested_by=?,
                        last_error_code='', updated_at=?
                    WHERE operation_id=? AND status IN ({','.join('?' for _ in CANCELLABLE_OPERATION_STATUSES)})
                    """,
                    (
                        now,
                        clean_text(actor_id, max_chars=160),
                        now,
                        row["operation_id"],
                        *sorted(CANCELLABLE_OPERATION_STATUSES),
                    ),
                )
                refreshed = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "operation.cancel_requested",
                    str(row["operation_id"]),
                    {
                        "previous_status": current,
                        "changed": cursor.rowcount == 1,
                        "reason": clean_text(reason, max_chars=500),
                    },
                )
                result = {
                    **self._operation_public(refreshed),
                    "found": True,
                    "changed": cursor.rowcount == 1,
                    "previous_status": current,
                }
                if request_key:
                    connection.execute(
                        """
                        INSERT INTO operation_commits(
                            operation_id, session_id, input_hash, status,
                            result_json, rollback_json, created_at, updated_at
                        ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                        """,
                        (
                            request_key,
                            session_id,
                            input_hash,
                            json_dump(result),
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def mark_operation_cancelled(
        self,
        operation_id: str,
        *,
        reason: str = "generation.cancelled",
    ) -> dict[str, Any]:
        return await self._run(
            self._mark_operation_cancelled,
            operation_id,
            reason,
        )

    def _mark_operation_cancelled(
        self,
        operation_id: str,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (clean_text(operation_id, max_chars=240),),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("事务不存在")
                current = str(row["status"] or "")
                if current == "completed":
                    connection.execute("COMMIT")
                    return self._operation_public(row)
                if current not in {"cancel_requested", "cancelled"}:
                    raise DatabaseConflictError("事务未进入取消握手")
                connection.execute(
                    """
                    UPDATE operation_receipts SET
                        status='cancelled', phase='cancelled',
                        last_error_code=?, lease_expires_at='',
                        reminder_next_at='', updated_at=?
                    WHERE operation_id=? AND status='cancel_requested'
                    """,
                    (clean_text(reason, max_chars=80), now, row["operation_id"]),
                )
                refreshed = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (row["operation_id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._operation_public(refreshed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def record_operation_progress(
        self,
        operation_id: str,
        stage: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_operation_progress,
            operation_id,
            stage,
        )

    def _record_operation_progress(
        self,
        operation_id: str,
        stage: str,
    ) -> dict[str, Any]:
        now = utc_now()
        lease_expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=OPERATION_LEASE_SECONDS)
        ).isoformat(timespec="seconds")
        stage = clean_text(stage, max_chars=80)
        if not stage:
            raise ValueError("进度阶段不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (clean_text(operation_id, max_chars=240),),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("事务不存在")
                status = str(row["status"] or "")
                if status not in ACTIVE_OPERATION_STATUSES:
                    connection.execute("COMMIT")
                    return {"emit": False, "status": status, "stage": stage}
                emit = str(row["last_progress_stage"] or "") != stage
                connection.execute(
                    """
                    UPDATE operation_receipts SET
                        last_progress_stage=?, last_progress_at=?,
                        lease_expires_at=?, updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        stage,
                        now,
                        lease_expires_at,
                        now,
                        row["operation_id"],
                    ),
                )
                connection.execute("COMMIT")
                return {"emit": emit, "status": status, "stage": stage, "at": now}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def reserve_operation(
        self,
        operation_id: str,
        session_id: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
        *,
        lease_seconds: int = OPERATION_LEASE_SECONDS,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_operation,
            operation_id,
            session_id,
            operation_type,
            dict(request_payload),
            max(60, int(lease_seconds)),
        )

    def _reserve_operation(
        self,
        operation_id: str,
        session_id: str,
        operation_type: str,
        request_payload: dict[str, Any],
        lease_seconds: int,
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240)
        operation_type = clean_text(operation_type, max_chars=80)
        if not operation_id or not session_id or not operation_type:
            raise ValueError("事务 ID、副本 ID 与类型不能为空")
        now = utc_now()
        input_hash = _operation_input_hash(operation_type, request_payload)
        legacy_input_hash = content_hash(request_payload)
        lease_expires = (
            datetime.now(timezone.utc)
            + timedelta(seconds=lease_seconds)
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                created = False
                if not row:
                    connection.execute(
                        """
                        INSERT INTO operation_receipts(
                            operation_id, session_id, operation_type,
                            request_json, result_json, status, phase,
                            retry_count, lease_expires_at, input_hash,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 'reserved', 'reserved',
                                  0, ?, ?, ?, ?)
                        """,
                        (
                            operation_id,
                            session_id,
                            operation_type,
                            json_dump(request_payload),
                            json_dump({"phase": "reserved"}),
                            lease_expires,
                            input_hash,
                            now,
                            now,
                        ),
                    )
                    created = True
                    row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                elif (
                    row["session_id"] != session_id
                    or row["operation_type"] != operation_type
                ):
                    raise DatabaseConflictError("事务 ID 已被不同请求占用")
                elif (
                    str(row["input_hash"] or "")
                    and str(row["input_hash"])
                    not in {input_hash, legacy_input_hash}
                ):
                    raise DatabaseConflictError(
                        "事务 ID 已被不同请求占用（输入不一致）"
                    )
                elif row["status"] == "completed":
                    pass
                elif row["status"] in {
                    "failed",
                    "needs_recovery",
                    "compensated",
                    "cancelled",
                }:
                    pass
                elif row["status"] == "failed_retryable" or (
                    row["status"] in OPERATION_GENERATION_STATUSES
                    and (
                        not row["lease_expires_at"]
                        or str(row["lease_expires_at"]) <= now
                    )
                ):
                    # 可重试失败或租约过期：回收并重新武装（重试语义）。
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            status = 'reserved', phase = 'reserved',
                            retry_count = retry_count + 1,
                            lease_expires_at = ?, last_error_code = '',
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (lease_expires, now, row["operation_id"]),
                    )
                    created = True
                    row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                connection.execute("COMMIT")
                return {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": json_load(row["result_json"], {}),
                    "status": row["status"],
                    "phase": row["phase"],
                    "retry_count": int(row["retry_count"] or 0),
                    "lease_expires_at": row["lease_expires_at"],
                    "last_error_code": row["last_error_code"],
                    "input_hash": row["input_hash"],
                    "created": created,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def update_operation(
        self,
        operation_id: str,
        *,
        status: str | None = None,
        phase: str = "",
        result: Mapping[str, Any] | None = None,
        actor_id: str = "system",
    ) -> dict[str, Any]:
        return await self._run(
            self._update_operation,
            operation_id,
            status,
            phase,
            dict(result or {}),
            actor_id,
        )

    def _update_operation(
        self,
        operation_id: str,
        status: str,
        phase: str,
        result: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        requested_status = (
            str(status).lower() if status is not None and str(status) else None
        )
        if requested_status is not None and requested_status not in OPERATION_ALL_STATUSES:
            raise ValueError("事务状态无效")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (clean_text(operation_id, max_chars=240),),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("事务不存在")
                status = requested_status or str(row["status"] or "pending")
                transition = OperationStateMachine.transition(
                    str(row["status"] or "pending"),
                    status,
                )
                if not transition.allowed:
                    raise DatabaseConflictError(
                        "操作状态已变化，当前阶段不能覆盖该结果"
                    )
                merged = json_load(row["result_json"], {})
                merged = merged if isinstance(merged, dict) else {}
                merged.update(result)
                if phase:
                    merged["phase"] = clean_text(phase, max_chars=80)
                last_error_code = str(
                    result.get("last_error_code") or ""
                )
                if (
                    not last_error_code
                    and status in {"failed", "failed_retryable", "needs_recovery"}
                ):
                    last_error_code = clean_text(
                        str(phase or "failed"),
                        max_chars=80,
                    )
                lease_sql = ""
                lease_params: list[Any] = []
                if status in OPERATION_GENERATION_STATUSES:
                    lease_sql = ", lease_expires_at = ?"
                    lease_params.append(
                        (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=OPERATION_LEASE_SECONDS)
                        ).isoformat(timespec="seconds")
                    )
                reminder_sql = (
                    ", reminder_next_at = ''"
                    if status in OPERATION_TERMINAL_STATUSES
                    else ""
                )
                update_sql = (
                    "UPDATE operation_receipts SET result_json = ?, "
                    "status = ?, phase = ?, last_error_code = ?, "
                    "updated_at = ?" + lease_sql + reminder_sql
                    + " WHERE operation_id = ?"
                )
                connection.execute(
                    update_sql,
                    (
                        json_dump(merged),
                        status,
                        clean_text(phase or merged.get("phase", ""), max_chars=80),
                        last_error_code,
                        now,
                        *lease_params,
                        row["operation_id"],
                    ),
                )
                refreshed = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (row["operation_id"],),
                ).fetchone()
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "operation.update",
                    row["operation_id"],
                    {"status": status, "phase": phase},
                )
                connection.execute("COMMIT")
                return {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": merged,
                    "status": status,
                    "phase": clean_text(phase or merged.get("phase", ""), max_chars=80),
                    "retry_count": int(refreshed["retry_count"] or 0),
                    "lease_expires_at": refreshed["lease_expires_at"],
                    "last_error_code": last_error_code,
                    "created_at": row["created_at"],
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def recover_expired_operations(
        self,
        *,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """回收租约过期的生成阶段回执，避免永久停留在“处理中”。

        幂等：只处理 status 处于生成阶段且 lease_expires_at 已过期的行，
        置为 failed_retryable（phase=lease_expired），后续重试可重新武装。
        """
        return await self._run(
            self._recover_expired_operations,
            now or utc_now(),
        )

    def _recover_expired_operations(
        self,
        now: str,
    ) -> list[dict[str, Any]]:
        now = str(now or utc_now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                recoverable = OPERATION_GENERATION_STATUSES | {"cancel_requested"}
                placeholders = ",".join("?" for _ in recoverable)
                rows = connection.execute(
                    f"""
                    SELECT * FROM operation_receipts
                    WHERE status IN ({placeholders})
                      AND lease_expires_at <> ''
                      AND lease_expires_at <= ?
                    """,
                    (*sorted(recoverable), now),
                ).fetchall()
                recovered: list[dict[str, Any]] = []
                for row in rows:
                    recovered_status = (
                        "cancelled"
                        if str(row["status"] or "") == "cancel_requested"
                        else "failed_retryable"
                    )
                    recovered_phase = (
                        "cancelled_after_lease"
                        if recovered_status == "cancelled"
                        else "lease_expired"
                    )
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            status = ?, phase = ?, last_error_code = ?,
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (
                            recovered_status,
                            recovered_phase,
                            recovered_phase,
                            now,
                            row["operation_id"],
                        ),
                    )
                    recovered.append(
                        {
                            "operation_id": row["operation_id"],
                            "session_id": row["session_id"],
                            "operation_type": row["operation_type"],
                            "status": recovered_status,
                            "phase": recovered_phase,
                            "last_error_code": recovered_phase,
                            "retry_count": int(row["retry_count"] or 0),
                        }
                    )
                connection.execute("COMMIT")
                return recovered
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def active_operations(
        self,
        session_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        return await self._run(
            self._active_operations,
            [clean_text(item, max_chars=240) for item in session_ids],
        )

    def _active_operations(
        self,
        session_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        values = list(dict.fromkeys(item for item in session_ids if item))[:500]
        if not values:
            return {}
        session_placeholders = ",".join("?" for _ in values)
        status_placeholders = ",".join("?" for _ in CANCELLABLE_OPERATION_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM operation_receipts
                WHERE session_id IN ({session_placeholders})
                  AND status IN ({status_placeholders})
                ORDER BY updated_at DESC, created_at DESC
                """,
                (*values, *sorted(CANCELLABLE_OPERATION_STATUSES)),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            session_id = str(row["session_id"] or "")
            if session_id in result:
                continue
            projected = self._operation_public(row)
            projected["revision"] = operation_revision(dict(row))
            result[session_id] = projected
        return result

    async def list_session_operations(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_session_operations, session_id, limit)

    def _list_session_operations(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE session_id = ? ORDER BY updated_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            return [
                {
                    "operation_id": row["operation_id"],
                    "session_id": row["session_id"],
                    "operation_type": row["operation_type"],
                    "request": json_load(row["request_json"], {}),
                    "result": json_load(row["result_json"], {}),
                    "status": row["status"],
                    "phase": row["phase"],
                    "retry_count": int(row["retry_count"] or 0),
                    "last_error_code": row["last_error_code"],
                    "last_progress_stage": row["last_progress_stage"],
                    "last_progress_at": row["last_progress_at"],
                    "reminder_enabled": bool(row["reminder_enabled"]),
                    "reminder_interval_seconds": int(
                        row["reminder_interval_seconds"] or 60
                    ),
                    "reminder_sequence": int(
                        row["reminder_sequence"] or 0
                    ),
                    "last_reminder_at": row["reminder_last_at"],
                    "next_reminder_at": row["reminder_next_at"],
                    "reminder_config_revision": int(
                        row["reminder_config_revision"] or 0
                    ),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    async def lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._lock_check_result,
            operation_id,
            session_id,
            dict(request_payload),
            dict(result_payload),
        )
