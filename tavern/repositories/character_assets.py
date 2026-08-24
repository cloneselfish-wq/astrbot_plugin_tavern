from __future__ import annotations

from .characters_support import *


class CharacterAssetsRepositoryMixin:
    async def set_card_delivery_state(
        self,
        private_origin: str,
        state: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_card_delivery_state,
            private_origin,
            dict(state or {}),
        )

    def _set_card_delivery_state(
        self,
        private_origin: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        from ..card_delivery import WIZARD_DELIVERY_KEY

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT d.id, d.fields_json, pt.session_id,
                           pt.private_user_id
                    FROM character_card_drafts d
                    JOIN participants pt ON pt.id = d.participant_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡"
                    )
                fields = json_load(row["fields_json"], {})
                fields = dict(fields) if isinstance(fields, Mapping) else {}
                if state:
                    fields[WIZARD_DELIVERY_KEY] = state
                    snapshot = state.get("candidate_snapshot")
                    field_key = str(state.get("field_key") or "")
                    if field_key and isinstance(snapshot, Mapping):
                        snapshots = fields.get(CANDIDATE_SNAPSHOTS_KEY)
                        snapshots = (
                            dict(snapshots)
                            if isinstance(snapshots, Mapping)
                            else {}
                        )
                        snapshots[field_key] = dict(snapshot)
                        fields[CANDIDATE_SNAPSHOTS_KEY] = snapshots
                else:
                    fields.pop(WIZARD_DELIVERY_KEY, None)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET fields_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(fields), now, row["id"]),
                )
                connection.execute("COMMIT")
                return dict(state)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_card_completion_reminder,
            private_origin,
            enabled,
        )

    def _set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        private_origin = clean_text(private_origin, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT t.*, pt.private_user_id
                    FROM timer_instances t
                    JOIN participants pt ON pt.id = t.participant_id
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND t.timer_type = 'card_completion'
                      AND t.status IN ('active', 'paused')
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡创建倒计时"
                    )

                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, dict):
                    payload = {}
                current_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                desired = (
                    current_enabled if enabled is None else bool(enabled)
                )
                reminder_at = str(row["reminder_at"] or "")
                deadline_text = str(row["deadline_at"] or "")
                if enabled is not None:
                    payload["reminder_enabled"] = desired
                    payload["reminder_interval_seconds"] = (
                        CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                    )
                    if row["status"] == "active" and deadline_text:
                        deadline = datetime.fromisoformat(deadline_text)
                        if desired:
                            next_reminder = now_dt + timedelta(
                                seconds=(
                                    CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                                )
                            )
                            reminder_at = (
                                next_reminder.isoformat(timespec="seconds")
                                if next_reminder < deadline
                                else ""
                            )
                        else:
                            # Keep the expiry event active while preventing
                            # periodic reminder scans before the deadline.
                            reminder_at = deadline_text
                    else:
                        reminder_at = ""
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET action_json = ?, reminder_at = ?,
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(payload),
                            reminder_at,
                            now,
                            row["id"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        str(row["private_user_id"] or ""),
                        "card.reminder_toggle",
                        row["participant_id"],
                        {"enabled": desired},
                    )

                remaining_seconds: int | None
                if row["status"] == "active" and deadline_text:
                    remaining_seconds = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(deadline_text)
                                - now_dt
                            ).total_seconds()
                        ),
                    )
                elif row["status"] == "paused":
                    remaining_seconds = max(
                        0,
                        int(row["remaining_seconds"] or 0),
                    )
                else:
                    remaining_seconds = None
                connection.execute("COMMIT")
                return {
                    "timer_id": row["id"],
                    "session_id": row["session_id"],
                    "participant_id": row["participant_id"],
                    "enabled": desired,
                    "status": row["status"],
                    "remaining_seconds": remaining_seconds,
                    "has_deadline": bool(
                        deadline_text
                        or (
                            row["status"] == "paused"
                            and row["remaining_seconds"] is not None
                        )
                    ),
                    "next_reminder_at": reminder_at,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
