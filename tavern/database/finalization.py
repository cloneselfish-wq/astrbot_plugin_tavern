from .common import *

class RescueFinalizationMixin:

    # ── D1 Schema 20：救援窗口（actor_fate@1.0，18 §5）──────────────────

    async def open_rescue_window(
        self,
        *,
        session_id: str,
        character_id: str,
        kind: str = "default",
        opened_at: str = "",
        expires_on: str = "",
        allowed_rescue_commands: Sequence[str] = (),
        success_transition: Sequence[str] = (),
        failure_transition: Sequence[str] = (),
        command_labels: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Reject raw rescue-window creation outside a fate transition."""
        raise PermissionError(
            "直接开启救援窗口已停用。系统没有创建窗口；"
            "窗口只能由世界声明的命运转换原子开启。"
        )

    def _open_rescue_window(
        self,
        session_id: str,
        character_id: str,
        kind: str,
        opened_at: str,
        expires_on: str,
        allowed_rescue_commands: Sequence[str],
        success_transition: Sequence[str],
        failure_transition: Sequence[str],
        command_labels: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        character_id = str(character_id or "").strip()
        kind = str(kind or "default").strip()[:64] or "default"
        if not session_id or not character_id:
            raise ValueError("救援窗口必须包含副本与角色")
        now = opened_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                character = connection.execute(
                    """
                    SELECT session_id FROM session_characters WHERE id = ?
                    """,
                    (character_id,),
                ).fetchone()
                if character is None:
                    raise DatabaseNotFoundError("角色不存在")
                if str(character["session_id"]) != str(session_id):
                    raise ValueError("角色不属于该副本")
                existing = connection.execute(
                    """
                    SELECT * FROM rescue_windows
                    WHERE session_id = ? AND character_id = ?
                      AND kind = ? AND status = 'open'
                    """,
                    (session_id, character_id, kind),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return dict(existing)
                item_id = new_id("rescue_window")
                connection.execute(
                    """
                    INSERT INTO rescue_windows(
                        id, session_id, character_id, kind, status,
                        opened_at, expires_on,
                        allowed_rescue_commands_json,
                        success_transition_json,
                        failure_transition_json,
                        command_labels_json, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        item_id,
                        session_id,
                        character_id,
                        kind,
                        now,
                        str(expires_on or "")[:64],
                        json_dump(
                            [str(item) for item in allowed_rescue_commands]
                        ),
                        json_dump([str(item) for item in success_transition]),
                        json_dump([str(item) for item in failure_transition]),
                        json_dump(
                            dict(command_labels)
                            if command_labels
                            else {}
                        ),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM rescue_windows WHERE id = ?",
                    (item_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def get_open_rescue_window(
        self,
        session_id: str,
        character_id: str,
        kind: str = "default",
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_open_rescue_window,
            session_id,
            character_id,
            kind,
        )

    def _get_open_rescue_window(
        self,
        session_id: str,
        character_id: str,
        kind: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM rescue_windows
                WHERE session_id = ? AND character_id = ?
                  AND kind = ? AND status = 'open'
                """,
                (
                    str(session_id),
                    str(character_id),
                    str(kind or "default")[:64],
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    async def list_rescue_windows(
        self,
        session_id: str,
        character_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_rescue_windows,
            session_id,
            character_id,
            status,
        )

    def _list_rescue_windows(
        self,
        session_id: str,
        character_id: str,
        status: str,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [str(session_id)]
        if character_id:
            clauses.append("character_id = ?")
            values.append(str(character_id))
        if status:
            clauses.append("status = ?")
            values.append(str(status))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM rescue_windows
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, kind
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    async def complete_rescue_window(
        self,
        *,
        session_id: str,
        character_id: str,
        kind: str = "default",
        command: str = "",
        outcome: str = "succeeded",
        completed_at: str = "",
    ) -> dict[str, Any]:
        """Reject window-only completion that would not update actor fate."""
        raise PermissionError(
            "直接完成救援窗口已停用。系统没有修改窗口或角色状态；"
            "请使用角色救援语义操作。"
        )

    def _complete_rescue_window(
        self,
        session_id: str,
        character_id: str,
        kind: str,
        command: str,
        outcome: str,
        completed_at: str,
    ) -> dict[str, Any]:
        outcome = str(outcome or "succeeded").strip().lower()
        if outcome not in {"succeeded", "failed"}:
            raise ValueError("救援窗口结果必须为 succeeded 或 failed")
        now = completed_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM rescue_windows
                    WHERE session_id = ? AND character_id = ?
                      AND kind = ? AND status = 'open'
                    """,
                    (
                        str(session_id),
                        str(character_id),
                        str(kind or "default")[:64],
                    ),
                ).fetchone()
                if row is None:
                    existing = connection.execute(
                        """
                        SELECT * FROM rescue_windows
                        WHERE session_id = ? AND character_id = ?
                          AND kind = ?
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (
                            str(session_id),
                            str(character_id),
                            str(kind or "default")[:64],
                        ),
                    ).fetchone()
                    connection.execute("COMMIT")
                    return dict(existing) if existing is not None else {}
                connection.execute(
                    """
                    UPDATE rescue_windows
                    SET status = ?, command = ?, outcome = ?,
                        completed_at = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        outcome,
                        str(command or "")[:80],
                        outcome,
                        now,
                        now,
                        row["id"],
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM rescue_windows WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

    async def expire_rescue_windows(
        self,
        session_id: str,
        now: str = "",
    ) -> list[dict[str, Any]]:
        """Reject window-only expiry that bypasses the fate transition."""
        raise PermissionError(
            "直接过期救援窗口已停用。系统没有修改窗口或角色状态；"
            "请由场景推进调用原子命运结算。"
        )

    def _expire_rescue_windows(
        self,
        session_id: str,
        now: str,
    ) -> list[dict[str, Any]]:
        now = str(now or "") or utc_now()
        expired: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM rescue_windows
                    WHERE session_id = ? AND status = 'open'
                      AND expires_on <> '' AND expires_on <= ?
                    ORDER BY created_at
                    """,
                    (str(session_id), now),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE rescue_windows
                        SET status = 'failed', outcome = 'expired',
                            completed_at = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, row["id"]),
                    )
                    expired.append(
                        dict(
                            connection.execute(
                                "SELECT * FROM rescue_windows WHERE id = ?",
                                (row["id"],),
                            ).fetchone()
                        )
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return expired

    # ── D1 Schema 20：终局命中与最终化 ─────────────────────────────────

    async def record_terminal_receipt(
        self,
        *,
        session_id: str,
        condition_id: str,
        condition_label: str = "",
        priority: int = 0,
        ending_ref: str = "",
        termination_type: str = "failed",
        archive_policy: str = "automatic_readonly",
        trigger_revision: int = 0,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """记录一次终局条件命中（按 idempotency_key 幂等，18 §11）。"""
        return await self._run(
            self._record_terminal_receipt,
            session_id,
            condition_id,
            condition_label,
            priority,
            ending_ref,
            termination_type,
            archive_policy,
            trigger_revision,
            payload,
            idempotency_key,
        )

    def _record_terminal_receipt(
        self,
        session_id: str,
        condition_id: str,
        condition_label: str,
        priority: int,
        ending_ref: str,
        termination_type: str,
        archive_policy: str,
        trigger_revision: int,
        payload: Mapping[str, Any] | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        condition_id = str(condition_id or "").strip()
        if not session_id or not condition_id:
            raise ValueError("终局命中必须包含副本与条件 ID")
        termination_type = str(termination_type or "failed").strip().lower()
        if termination_type not in {"completed", "failed", "aborted"}:
            raise ValueError("终局类型必须为 completed、failed 或 aborted")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM terminal_receipts
                        WHERE idempotency_key = ?
                        """,
                        (str(idempotency_key),),
                    ).fetchone()
                    if existing is not None:
                        connection.execute("COMMIT")
                        return dict(existing)
                item_id = new_id("terminal")
                connection.execute(
                    """
                    INSERT INTO terminal_receipts(
                        id, session_id, condition_id, condition_label,
                        priority, ending_ref, termination_type,
                        archive_policy, trigger_revision, payload_json,
                        status, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        item_id,
                        session_id,
                        condition_id[:160],
                        str(condition_label or "")[:160],
                        max(0, int(priority or 0)),
                        str(ending_ref or "")[:160],
                        termination_type,
                        str(archive_policy or "automatic_readonly")[:80],
                        max(0, int(trigger_revision or 0)),
                        json_dump(dict(payload) if payload else {}),
                        str(idempotency_key or "")[:200],
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM terminal_receipts WHERE id = ?",
                    (item_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def list_terminal_receipts(
        self,
        session_id: str,
        status: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_terminal_receipts, session_id, status)

    def _list_terminal_receipts(
        self,
        session_id: str,
        status: str,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [str(session_id)]
        if status:
            clauses.append("status = ?")
            values.append(str(status))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM terminal_receipts
                WHERE {' AND '.join(clauses)}
                ORDER BY priority DESC, created_at, id
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    async def begin_finalization(
        self,
        *,
        session_id: str,
        termination_type: str,
        ending_ref: str = "",
        ending_label: str = "",
        archive_policy: str = "automatic_readonly",
        idempotency_key: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """预留终局与归档状态（finalization_pending，输入锁前提）。

        快照等事务外步骤失败后可恢复重试；重试复用同一 idempotency_key，
        不会生成第二条终局（D1-RUN-013）。
        """
        return await self._run(
            self._begin_finalization,
            session_id,
            termination_type,
            ending_ref,
            ending_label,
            archive_policy,
            idempotency_key,
            payload,
        )

    def _begin_finalization(
        self,
        session_id: str,
        termination_type: str,
        ending_ref: str,
        ending_label: str,
        archive_policy: str,
        idempotency_key: str,
        payload: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        termination_type = str(termination_type or "").strip().lower()
        if not session_id:
            raise ValueError("终局最终化必须指定副本")
        if termination_type not in {"completed", "failed", "aborted"}:
            raise ValueError("终局类型必须为 completed、failed 或 aborted")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM session_finalizations
                        WHERE idempotency_key = ?
                        """,
                        (str(idempotency_key),),
                    ).fetchone()
                    if existing is not None:
                        connection.execute("COMMIT")
                        return dict(existing)
                connection.execute(
                    """
                    INSERT INTO session_finalizations(
                        session_id, status, termination_type, ending_ref,
                        ending_label, archive_policy, idempotency_key,
                        input_locked, snapshot_status, final_snapshot_id,
                        payload_json, attempts, last_error,
                        created_at, updated_at
                    ) VALUES (
                        ?, 'pending', ?, ?, ?, ?, ?, 1, 'pending', '', ?, 0,
                        '', ?, ?
                    )
                    """,
                    (
                        session_id,
                        termination_type,
                        str(ending_ref or "")[:160],
                        str(ending_label or "")[:160],
                        str(archive_policy or "automatic_readonly")[:80],
                        str(idempotency_key or "")[:200],
                        json_dump(dict(payload) if payload else {}),
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM session_finalizations
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def get_finalization(self, session_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_finalization, session_id)

    def _get_finalization(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM session_finalizations WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    async def complete_finalization(
        self,
        *,
        session_id: str,
        final_snapshot_id: str,
        ending_label: str = "",
    ) -> dict[str, Any]:
        """快照完成后提交永久最终化（失败状态可重试）。"""
        return await self._run(
            self._complete_finalization,
            session_id,
            final_snapshot_id,
            ending_label,
        )

    def _complete_finalization(
        self,
        session_id: str,
        final_snapshot_id: str,
        ending_label: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE session_finalizations SET
                        status = 'finalized',
                        snapshot_status = 'completed',
                        final_snapshot_id = ?,
                        ending_label = CASE
                            WHEN ? <> '' THEN ? ELSE ending_label END,
                        last_error = '',
                        updated_at = ?
                    WHERE session_id = ? AND status IN ('pending', 'failed')
                    """,
                    (
                        str(final_snapshot_id),
                        str(ending_label),
                        str(ending_label),
                        now,
                        str(session_id),
                    ),
                )
                if cursor.rowcount == 0:
                    raise InvalidTransitionError("终局最终化当前不可完成")
                row = connection.execute(
                    """
                    SELECT * FROM session_finalizations
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def fail_finalization(
        self,
        *,
        session_id: str,
        error: str,
    ) -> dict[str, Any]:
        """标记终局快照失败（保留输入锁，等待恢复重试）。"""
        return await self._run(self._fail_finalization, session_id, error)

    def _fail_finalization(
        self,
        session_id: str,
        error: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE session_finalizations SET
                        status = 'failed',
                        snapshot_status = 'failed',
                        attempts = attempts + 1,
                        last_error = ?,
                        updated_at = ?
                    WHERE session_id = ? AND status <> 'finalized'
                    """,
                    (str(error or "终局快照失败")[:1000], now, str(session_id)),
                )
                row = connection.execute(
                    """
                    SELECT * FROM session_finalizations
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row) if row is not None else {}

    async def cancel_finalization(
        self,
        session_id: str,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        return await self._run(self._cancel_finalization, session_id, error)

    def _cancel_finalization(
        self,
        session_id: str,
        error: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE session_finalizations SET
                        status = 'cancelled',
                        last_error = ?,
                        updated_at = ?
                    WHERE session_id = ? AND status <> 'finalized'
                    """,
                    (str(error or "")[:1000], now, str(session_id)),
                )
                row = connection.execute(
                    """
                    SELECT * FROM session_finalizations
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row) if row is not None else {}

    async def append_archive_record(
        self,
        *,
        session_id: str,
        termination_type: str,
        reason: str,
        final_snapshot_id: str,
        ended_by: str,
        readonly: int = 1,
        ending_ref: str = "",
        ending_label: str = "",
    ) -> dict[str, Any]:
        """追加最终归档记录（含 failed；一个副本仅一条，重复拒绝）。"""
        return await self._run(
            self._append_archive_record,
            session_id,
            termination_type,
            reason,
            final_snapshot_id,
            ended_by,
            readonly,
            ending_ref,
            ending_label,
        )

    def _append_archive_record(
        self,
        session_id: str,
        termination_type: str,
        reason: str,
        final_snapshot_id: str,
        ended_by: str,
        readonly: int,
        ending_ref: str,
        ending_label: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = insert_session_archive(
                    connection,
                    session_id=session_id,
                    termination_type=termination_type,
                    reason=reason,
                    final_snapshot_id=final_snapshot_id,
                    ended_by=ended_by,
                    readonly=readonly,
                    ending_ref=ending_ref,
                    ending_label=ending_label,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result
