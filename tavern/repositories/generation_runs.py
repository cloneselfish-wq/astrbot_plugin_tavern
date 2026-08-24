from __future__ import annotations

from .story_support import *


class GenerationRunsRepositoryMixin:
    @staticmethod
    def _choice_set(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "participant_id": row["participant_id"],
            "actor_id": row["actor_id"] if "actor_id" in keys else None,
            "round_no": row["round_no"],
            "session_revision": row["session_revision"],
            "choices": json_load(row["choices_json"], []),
            "status": row["status"],
            "reroll_count": row["reroll_count"],
            "selected_key": row["selected_key"],
            "flavor_text": row["flavor_text"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _vote(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "source_event_id": row["source_event_id"],
            "question": row["question"],
            "options": json_load(row["options_json"], []),
            "eligible_user_ids": json_load(
                row["eligible_user_ids_json"],
                [],
            ),
            "stage": row["stage"],
            "status": row["status"],
            "decision_status": row["decision_status"],
            "resolution_status": row["resolution_status"],
            "resolution_operation_id": row["resolution_operation_id"],
            "decision_revision": int(row["decision_revision"] or 0),
            "decided_at": row["decided_at"],
            "resolved_at": row["resolved_at"],
            "committed_event_id": row["committed_event_id"],
            "winner_key": row["winner_key"],
            "suspended_user_id": row["suspended_user_id"],
            "deadline_at": row["deadline_at"],
            "result": json_load(row["result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


    # ── 1.0.0-A7：背包 / 状态 / 开局物资 权威操作 ──────────────────────
    async def remove_participant_status(
        self,
        session_id: str,
        target_ref: str,
        name_keywords: Sequence[str],
        actor_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """按名称关键字移除角色状态（1.0.0-A7：治疗/剧情恢复“重创”等）。"""
        keywords = tuple(
            str(item).strip().casefold()
            for item in name_keywords
            if str(item).strip()
        )
        return await self._run(
            self._remove_participant_status,
            session_id,
            clean_text(target_ref, max_chars=128),
            keywords,
            str(actor_id or "").strip(),
            clean_text(reason, max_chars=300),
        )

    def _remove_participant_status(
        self,
        session_id: str,
        target_ref: str,
        keywords: tuple[str, ...],
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._remove_participant_status_locked(
                    connection,
                    session_id=session_id,
                    target_ref=target_ref,
                    keywords=keywords,
                    actor_id=actor_id,
                    reason=reason,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _remove_participant_status_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        target_ref: str,
        keywords: Sequence[str],
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """在调用方事务内移除参与者状态（回合提交时与道具同事务）。"""
        if not target_ref:
            raise ValueError("缺少状态目标")
        self._assert_session_writable(connection, session_id)
        row = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND (
                id = ? OR group_user_id = ? OR
                lower(character_name) = lower(?) OR
                lower(character_code) = lower(?) OR
                lower(display_name) = lower(?)
            )
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (
                session_id,
                target_ref,
                target_ref,
                target_ref,
                target_ref,
                target_ref,
                target_ref,
            ),
        ).fetchone()
        if not row:
            raise DatabaseNotFoundError("状态目标角色不存在")
        runtime = connection.execute(
            """
            SELECT * FROM character_runtime_states
            WHERE session_id = ? AND participant_id = ?
            """,
            (session_id, row["id"]),
        ).fetchone()
        if not runtime:
            return {
                "ok": True,
                "participant_id": row["id"],
                "removed": [],
                "message": "该角色没有运行状态",
            }
        state = json_load(runtime["state_json"], {})
        state = dict(state) if isinstance(state, Mapping) else {}
        statuses = [
            dict(item)
            for item in state.get("statuses", [])
            if isinstance(item, Mapping)
        ]
        kept: list[dict[str, Any]] = []
        removed: list[str] = []
        for status in statuses:
            name = str(status.get("name") or "").casefold()
            effect = str(status.get("effect") or "").casefold()
            if keywords and any(
                keyword in name or keyword in effect
                for keyword in keywords
            ):
                removed.append(str(status.get("name") or ""))
            else:
                kept.append(status)
        if removed:
            state["statuses"] = kept[:40]
            now = utc_now()
            connection.execute(
                """
                UPDATE character_runtime_states SET
                    state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (json_dump(state), now, runtime["id"]),
            )
            self._insert_audit(
                connection,
                session_id,
                actor_id,
                "status.remove",
                row["id"],
                {
                    "target_ref": target_ref,
                    "removed": removed,
                    "reason": reason,
                },
            )
            self._enqueue_storage_sync(connection, [session_id], "sync")
        return {
            "ok": True,
            "participant_id": row["id"],
            "character_name": row["character_name"] or row["display_name"],
            "removed": removed,
        }
