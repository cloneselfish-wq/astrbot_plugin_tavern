from __future__ import annotations

from .characters_support import *


class CharacterRuntimeQueriesRepositoryMixin:
    def _abandon_card_seat(self, private_origin: str) -> dict[str, Any]:
        private_origin = clean_text(private_origin, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*
                    FROM participants pt
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND s.state <> 'finished'
                    ORDER BY pt.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有可放弃的席位")
                if str(row["character_card_id"] or ""):
                    raise ValueError(
                        "正式角色不能放弃建卡席位；请使用正式退场流程"
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = CASE
                            WHEN status = 'active' THEN 'cancelled'
                            ELSE status
                        END,
                        cancel_reason = CASE
                            WHEN status = 'active' THEN 'seat_abandoned'
                            ELSE cancel_reason
                        END,
                        updated_at = ?
                    WHERE participant_id = ?
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes
                    SET status = 'revoked',
                        failure_reason = 'seat_abandoned'
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (row["id"],),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', deadline_at = '',
                        reminder_at = '', updated_at = ?
                    WHERE participant_id = ?
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        private_user_id = '', private_origin = '',
                        card_status = 'uncreated', ready = 0,
                        participation_status = 'archived',
                        exit_reason = 'seat_abandoned', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.seat_abandon",
                    row["id"],
                    {},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "seat_released": True,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_ready,
            session_id,
            user_id,
            ready,
        )

    async def force_all_ready(
        self,
        session_id: str,
        actor_id: str,
        *,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        result = await self._run(
            self._force_all_ready,
            session_id,
            actor_id,
            expected_revision,
            idempotency_key,
        )
        result["preflight"] = await self.opening_preflight(session_id)
        return result

    def _force_all_ready(
        self,
        session_id: str,
        actor_id: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "session_id": clean_text(session_id, max_chars=240),
            "expected_revision": expected_revision,
        }
        input_hash = content_hash(request_payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request_key:
                    receipt = connection.execute(
                        "SELECT * FROM operation_commits WHERE operation_id=?",
                        (request_key,),
                    ).fetchone()
                    if receipt is not None:
                        if str(receipt["input_hash"] or "") != input_hash:
                            raise DatabaseConflictError(
                                "相同幂等键已用于另一份强制准备请求"
                            )
                        if str(receipt["status"] or "") == "completed":
                            replay = json_load(receipt["result_json"], {})
                            replay["replayed"] = True
                            connection.execute("COMMIT")
                            return replay
                        raise DatabaseConflictError(
                            "强制准备仍在处理中，请稍后重试"
                        )
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session or session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有准备大厅可以强制全员准备"
                    )
                if (
                    expected_revision is not None
                    and int(session["revision"] or 0) != int(expected_revision)
                ):
                    raise DatabaseConflictError("准备大厅状态已经变化")
                now = utc_now()
                eligible = connection.execute(
                    """
                    SELECT id, display_name, character_name
                    FROM participants
                    WHERE session_id = ? AND card_status = 'approved'
                      AND participation_status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                ids = [str(row["id"]) for row in eligible]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""
                        UPDATE participants SET ready = 1, updated_at = ?
                        WHERE id IN ({placeholders})
                        """,
                        (now, *ids),
                    )
                    connection.execute(
                        f"""
                        UPDATE timer_instances
                        SET status = 'completed', deadline_at = '',
                            reminder_at = '', updated_at = ?
                        WHERE participant_id IN ({placeholders})
                          AND timer_type = 'ready'
                          AND status IN ('active', 'paused')
                        """,
                        (now, *ids),
                    )
                skipped = connection.execute(
                    """
                    SELECT id, display_name, character_name, card_status,
                           participation_status
                    FROM participants
                    WHERE session_id = ?
                      AND NOT (
                        card_status = 'approved'
                        AND participation_status = 'active'
                      )
                      AND participation_status NOT IN ('retired', 'archived')
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "participant.force_ready_all",
                    session_id,
                    {
                        "ready_count": len(ids),
                        "skipped_count": len(skipped),
                    },
                )
                result = {
                    "session_id": session_id,
                    "changed": len(ids),
                    "ready_count": len(ids),
                    "skipped": [
                        {
                            "participant_id": str(row["id"]),
                            "name": row["character_name"]
                            or row["display_name"],
                            "card_status": row["card_status"],
                            "participation_status": row[
                                "participation_status"
                            ],
                            "reason": (
                                row["card_status"]
                                if row["card_status"] != CARD_APPROVED
                                else row["participation_status"]
                            ),
                        }
                        for row in skipped
                    ],
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

    def _set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError("只能在准备大厅确认准备")
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if row["card_status"] != CARD_APPROVED:
                    raise ValueError("角色卡尚未通过审核")
                if row["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能进入本次阵容")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        ready = ?, participation_status = 'active',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (int(bool(ready)), now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status = 'active'
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.ready",
                    row["id"],
                    {"ready": bool(ready)},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise
