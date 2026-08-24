from __future__ import annotations

from .workflow_support import *


class ChoicesQueriesRepositoryMixin:
    def _restore_actor_choices(
        self,
        session_id: str,
        user_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not user_id:
            return {"created": False, "reason": "missing_user"}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_RUNNING:
                    return {"created": False, "reason": "not_running"}
                active = connection.execute(
                    """
                    SELECT 1 FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchone()
                if active:
                    return {"created": False, "reason": "already_active"}
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                      AND participation_status = 'active'
                      AND card_status = 'approved'
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    return {
                        "created": False,
                        "reason": "no_active_participant",
                    }
                choice_id = self._insert_fallback_choices(
                    connection,
                    session=session,
                    participant=participant,
                    now=utc_now(),
                    idempotency_key=(
                        f"recover:{reason or 'actor'}:{session['revision']}"
                    ),
                )
                connection.execute("COMMIT")
                return {"created": True, "choice_set_id": choice_id}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def complete_vote_without_narrative(
        self,
        vote_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._complete_vote_without_narrative, vote_id)

    def _complete_vote_without_narrative(
        self,
        vote_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM group_votes WHERE id=?",
                    (vote_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("集体投票不存在")
                if str(row["decision_status"] or "") != "decided":
                    raise InvalidTransitionError("集体投票尚未形成决定")
                operation_id = str(row["resolution_operation_id"] or "")
                connection.execute(
                    """
                    UPDATE group_votes SET status='resolved',
                        resolution_status='committed', resolved_at=?,
                        updated_at=? WHERE id=?
                    """,
                    (now, now, vote_id),
                )
                connection.execute(
                    """
                    UPDATE operation_receipts SET status='completed',
                        phase='not_required', result_json=?,
                        lease_expires_at='', updated_at=?
                    WHERE operation_id=? AND status<>'completed'
                    """,
                    (
                        json_dump(
                            {
                                "phase": "not_required",
                                "vote_id": vote_id,
                                "world_changed": False,
                            }
                        ),
                        now,
                        operation_id,
                    ),
                )
                refreshed = connection.execute(
                    "SELECT * FROM group_votes WHERE id=?",
                    (vote_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._vote(refreshed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def supersede_active_choices(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        """把当前活跃的 A–D 选项集标记为 superseded（防 actor_id 错位）。"""
        return await self._run(
            self._supersede_active_choices, session_id, actor_id
        )

    def _supersede_active_choices(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'superseded', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn.choices_superseded",
                    "",
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return cursor.rowcount
