from __future__ import annotations

from .story_support import *


class MemoriesRepositoryMixin:
    async def memory_action_context(
        self,
        memory_id: str,
    ) -> dict[str, Any]:
        """Load only server-side scope/CAS facts needed to authorize governance."""

        return await self._run(self._memory_action_context, memory_id)

    def _memory_action_context(self, memory_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.*,
                       mg.visibility AS governance_visibility,
                       mg.locked AS governance_locked,
                       mg.pinned AS governance_pinned,
                       mg.invalidated AS governance_invalidated,
                       mg.supersedes_id AS governance_supersedes_id,
                       mg.conflict_status AS governance_conflict_status,
                       mg.note AS governance_note
                FROM memories m
                LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                WHERE m.id = ?
                """,
                (str(memory_id or ""),),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("记忆不存在")
            item = self._memory(row)
            return {
                "session_id": str(row["session_id"]),
                "locked": bool(item.get("locked")),
                "revision": memory_revision(item),
            }

    async def govern_memory(
        self,
        memory_id: str,
        operation: str,
        actor_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        reason: str = "",
        allow_locked: bool = False,
    ) -> dict[str, Any]:
        """Atomically govern one fact with CAS and durable response replay."""

        return await self._run(
            self._govern_memory,
            str(memory_id or ""),
            str(operation or ""),
            str(actor_id or ""),
            int(expected_revision),
            str(operation_id or ""),
            str(reason or ""),
            bool(allow_locked),
        )

    def _govern_memory(
        self,
        memory_id: str,
        operation: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
        reason: str,
        allow_locked: bool,
    ) -> dict[str, Any]:
        operation = clean_text(operation, max_chars=40).lower()
        if operation not in {"pin", "unpin", "invalidate", "restore", "resolve"}:
            raise ValueError("不支持的事实治理动作")
        operation_id = clean_text(operation_id, max_chars=160)
        if not operation_id:
            raise ValueError("事实治理缺少防重复凭据")
        reason = clean_text(reason, max_chars=500)
        request = {
            "memory_id": memory_id,
            "operation": operation,
            "expected_revision": int(expected_revision),
            "reason": reason,
        }
        input_hash = hashlib.sha256(
            json_dump(request).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    """
                    SELECT input_hash, result_json FROM operation_commits
                    WHERE operation_id = ? AND status = 'completed'
                    """,
                    (operation_id,),
                ).fetchone()
                if receipt:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项事实治理操作"
                        )
                    result = json_load(receipt["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(result), "replayed": True}
                row = connection.execute(
                    """
                    SELECT m.*,
                           mg.visibility AS governance_visibility,
                           mg.locked AS governance_locked,
                           mg.pinned AS governance_pinned,
                           mg.invalidated AS governance_invalidated,
                           mg.supersedes_id AS governance_supersedes_id,
                           mg.conflict_status AS governance_conflict_status,
                           mg.note AS governance_note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("记忆不存在")
                self._assert_session_writable(connection, str(row["session_id"]))
                current = self._memory(row)
                current_revision = memory_revision(current)
                if current_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "事实治理状态已经变化，请刷新后重试"
                    )
                if bool(current.get("locked")) and not allow_locked:
                    raise InvalidTransitionError(
                        "该事实已锁定，只能由管理员调整治理状态"
                    )
                pinned = bool(current.get("pinned"))
                invalidated = bool(current.get("invalidated"))
                conflict_status = str(current.get("conflict_status") or "clear")
                note = str(current.get("governance_note") or "")
                if operation == "pin":
                    pinned = True
                elif operation == "unpin":
                    pinned = False
                elif operation == "invalidate":
                    invalidated = True
                    note = reason or "主持人已将该事实标记为不再有效。"
                elif operation == "restore":
                    invalidated = False
                    note = reason or "主持人已恢复该事实。"
                else:
                    conflict_status = "resolved"
                    note = reason or "主持人已确认当前事实的冲突处理结果。"
                connection.execute(
                    """
                    INSERT INTO memory_governance(
                        memory_id, visibility, locked, pinned,
                        invalidated, supersedes_id, conflict_status,
                        note, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        pinned = excluded.pinned,
                        invalidated = excluded.invalidated,
                        conflict_status = excluded.conflict_status,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        str(current.get("visibility") or "public"),
                        int(bool(current.get("locked"))),
                        int(pinned),
                        int(invalidated),
                        str(current.get("supersedes_id") or ""),
                        conflict_status,
                        note,
                        actor_id,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE memories SET updated_at = ? WHERE id = ?",
                    (now, memory_id),
                )
                updated_row = connection.execute(
                    """
                    SELECT m.*,
                           mg.visibility AS governance_visibility,
                           mg.locked AS governance_locked,
                           mg.pinned AS governance_pinned,
                           mg.invalidated AS governance_invalidated,
                           mg.supersedes_id AS governance_supersedes_id,
                           mg.conflict_status AS governance_conflict_status,
                           mg.note AS governance_note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                updated = self._memory(updated_row)
                result = {
                    "operation": operation,
                    "revision": memory_revision(updated),
                    "state": (
                        "已失效"
                        if bool(updated.get("invalidated"))
                        else "冲突已处理"
                        if str(updated.get("conflict_status")) == "resolved"
                        else "当前有效"
                    ),
                    "pinned": bool(updated.get("pinned")),
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        operation_id,
                        str(row["session_id"]),
                        input_hash,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    str(row["session_id"]),
                    actor_id,
                    f"memory.govern.{operation}",
                    memory_id,
                    {"reason": reason},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def save_memory(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_memory, dict(payload), actor_id)

    def _save_memory(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        memory_id = str(payload.get("id") or "").strip()
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        scope = str(payload.get("scope", "world")).lower()
        if scope not in {"world", "player", "npc"}:
            raise ValueError("非法记忆范围")
        scope_id = clean_text(payload.get("scope_id"), max_chars=128)
        kind = clean_text(payload.get("kind") or "fact", max_chars=32)
        content = clean_text(payload.get("content"), max_chars=1000)
        if not content:
            raise ValueError("记忆内容不能为空")
        try:
            importance = max(1, min(5, int(payload.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        tags_value = payload.get("tags")
        tags = []
        if isinstance(tags_value, list):
            tags = [
                clean_text(item, max_chars=32)
                for item in tags_value[:12]
                if clean_text(item, max_chars=32)
            ]
        fingerprint = memory_fingerprint(
            session_id,
            scope,
            scope_id,
            kind,
            content,
        )
        visibility = str(payload.get("visibility") or "public").lower()
        if visibility not in {"public", "host", "private"}:
            visibility = "public"
        conflict_status = str(
            payload.get("conflict_status") or "clear"
        ).lower()
        if conflict_status not in {"clear", "conflict", "resolved"}:
            conflict_status = "clear"
        supersedes_id = clean_text(
            payload.get("supersedes_id"),
            max_chars=128,
        )
        governance_note = clean_text(
            payload.get("governance_note"),
            max_chars=500,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current_governance = None
                if memory_id:
                    row = connection.execute(
                        "SELECT * FROM memories WHERE id = ?",
                        (memory_id,),
                    ).fetchone()
                    if not row:
                        raise DatabaseNotFoundError("记忆不存在")
                    if row["session_id"] != session_id:
                        raise ValueError("不能把记忆移动到其他副本")
                    current_governance = connection.execute(
                        """
                        SELECT * FROM memory_governance
                        WHERE memory_id = ?
                        """,
                        (memory_id,),
                    ).fetchone()
                    if current_governance:
                        if "visibility" not in payload:
                            visibility = current_governance["visibility"]
                        if "conflict_status" not in payload:
                            conflict_status = current_governance[
                                "conflict_status"
                            ]
                        if "supersedes_id" not in payload:
                            supersedes_id = current_governance[
                                "supersedes_id"
                            ]
                        if "governance_note" not in payload:
                            governance_note = current_governance["note"]
                    connection.execute(
                        """
                        UPDATE memories SET
                            scope = ?, scope_id = ?, kind = ?, content = ?,
                            importance = ?, tags_json = ?, fingerprint = ?,
                            updated_at = ?, last_accessed_at = ?
                        WHERE id = ?
                        """,
                        (
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            memory_id,
                        ),
                    )
                    action = "memory.update"
                else:
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '', ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            tags_json = excluded.tags_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT * FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    memory_id = row["id"]
                    action = "memory.create"
                if supersedes_id:
                    replaced = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE id = ? AND session_id = ?
                        """,
                        (supersedes_id, session_id),
                    ).fetchone()
                    if not replaced or supersedes_id == memory_id:
                        raise ValueError("被替代记忆不存在或不能替代自身")
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, 'public', 0, 0, 1, '', 'resolved',
                                  '已被新事实替代', ?, ?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            invalidated = 1,
                            conflict_status = 'resolved',
                            note = CASE
                                WHEN note = '' THEN '已被新事实替代'
                                ELSE note
                            END,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                        """,
                        (supersedes_id, actor_id, now),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_governance(
                        memory_id, visibility, locked, pinned,
                        invalidated, supersedes_id, conflict_status,
                        note, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        visibility = excluded.visibility,
                        locked = excluded.locked,
                        pinned = excluded.pinned,
                        invalidated = excluded.invalidated,
                        supersedes_id = excluded.supersedes_id,
                        conflict_status = excluded.conflict_status,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        visibility,
                        int(
                            bool(
                                payload.get(
                                    "locked",
                                    current_governance["locked"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "pinned",
                                    current_governance["pinned"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "invalidated",
                                    current_governance["invalidated"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        supersedes_id,
                        conflict_status,
                        governance_note,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    memory_id,
                    {"scope": scope, "kind": kind},
                )
                row = connection.execute(
                    """
                    SELECT m.*,
                           mg.visibility AS governance_visibility,
                           mg.locked AS governance_locked,
                           mg.pinned AS governance_pinned,
                           mg.invalidated AS governance_invalidated,
                           mg.supersedes_id AS governance_supersedes_id,
                           mg.conflict_status AS governance_conflict_status,
                           mg.note AS governance_note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._memory(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_memory(
        self,
        memory_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_memory, memory_id, actor_id)

    def _delete_memory(self, memory_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("记忆不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM memories WHERE id = ?",
                    (memory_id,),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "memory.delete",
                    memory_id,
                    {"kind": row["kind"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
