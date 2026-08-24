from __future__ import annotations

from .timers_support import *


class TimerPolicyRepositoryMixin:
    async def get_timer_policy(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_timer_policy, session_id)

    def _get_timer_policy(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            row = connection.execute(
                """
                SELECT * FROM timer_policies WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            switches = json_load(
                row["switches_json"] if row else "",
                {},
            )
            switches = switches if isinstance(switches, Mapping) else {}
            global_enabled = bool(row["global_enabled"] if row else False)
            return {
                "session_id": session_id,
                "global_enabled": global_enabled,
                "switches": {
                    timer_type: bool(switches.get(timer_type, False))
                    for timer_type in COUNTDOWN_TYPES
                },
                "effective": {
                    timer_type: bool(
                        global_enabled and switches.get(timer_type, False)
                    )
                    for timer_type in COUNTDOWN_TYPES
                },
                "revision": int(row["revision"] if row else 0),
                "updated_at": str(row["updated_at"] if row else ""),
            }

    async def set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_timer_policy,
            session_id,
            timer_type,
            enabled,
            actor_id,
        )

    def _set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        timer_type = str(timer_type or "").strip().lower()
        if timer_type not in {"all", *COUNTDOWN_TYPES}:
            raise ValueError("不支持的倒计时分类")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                row = connection.execute(
                    """
                    SELECT * FROM timer_policies WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                global_enabled = bool(
                    row["global_enabled"] if row else False
                )
                switches = json_load(
                    row["switches_json"] if row else "",
                    {},
                )
                switches = (
                    dict(switches)
                    if isinstance(switches, Mapping)
                    else {}
                )
                before = {
                    item: bool(
                        global_enabled and switches.get(item, False)
                    )
                    for item in COUNTDOWN_TYPES
                }
                if timer_type == "all":
                    global_enabled = bool(enabled)
                    for item in COUNTDOWN_TYPES:
                        switches[item] = bool(enabled)
                else:
                    switches[timer_type] = bool(enabled)
                after = {
                    item: bool(
                        global_enabled and switches.get(item, False)
                    )
                    for item in COUNTDOWN_TYPES
                }
                connection.execute(
                    """
                    INSERT INTO timer_policies(
                        session_id, global_enabled, switches_json,
                        revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        global_enabled = excluded.global_enabled,
                        switches_json = excluded.switches_json,
                        revision = timer_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        int(global_enabled),
                        json_dump(switches),
                        actor_id,
                        now,
                    ),
                )
                changed = [
                    item for item in COUNTDOWN_TYPES
                    if before[item] != after[item]
                ]
                if changed:
                    placeholders = ",".join("?" for _ in changed)
                    rows = connection.execute(
                        f"""
                        SELECT * FROM timer_instances
                        WHERE session_id = ?
                          AND timer_type IN ({placeholders})
                          AND status IN ('active', 'paused')
                        """,
                        (session_id, *changed),
                    ).fetchall()
                    for timer in rows:
                        payload = json_load(timer["action_json"], {})
                        payload = (
                            dict(payload)
                            if isinstance(payload, Mapping)
                            else {}
                        )
                        current_type = str(timer["timer_type"])
                        if not after[current_type]:
                            if timer["status"] != "active":
                                continue
                            remaining = timer["remaining_seconds"]
                            deadline = str(timer["deadline_at"] or "")
                            if deadline:
                                try:
                                    remaining = max(
                                        0,
                                        int(
                                            (
                                                datetime.fromisoformat(deadline)
                                                - now_dt
                                            ).total_seconds()
                                        ),
                                    )
                                except ValueError:
                                    pass
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'paused', deadline_at = '',
                                    remaining_seconds = ?, reminder_at = '',
                                    reminder_sent = 0, action_json = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    remaining,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                        elif (
                            timer["status"] == "paused"
                            and payload.get("paused_by_policy")
                        ):
                            if (
                                current_type != "card_completion"
                                and not payload.get(
                                    "reminder_interval_seconds"
                                )
                            ):
                                payload["reminder_interval_seconds"] = (
                                    TIMER_REMINDER_INTERVAL_SECONDS
                                )
                            seconds_left = max(
                                1,
                                int(timer["remaining_seconds"] or 1),
                            )
                            deadline_dt = now_dt + timedelta(
                                seconds=seconds_left
                            )
                            interval = timer_reminder_interval(
                                current_type,
                                payload,
                            )
                            next_reminder = deadline_dt - timedelta(
                                seconds=interval
                            )
                            payload.pop("paused_by_policy", None)
                            reminder_at = ""
                            if (
                                timer_reminder_enabled(
                                    current_type,
                                    payload,
                                )
                                and next_reminder < deadline_dt
                            ):
                                reminder_at = next_reminder.isoformat(
                                    timespec="seconds"
                                )
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'active', deadline_at = ?,
                                    reminder_at = ?, reminder_sent = 0,
                                    action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    deadline_dt.isoformat(
                                        timespec="seconds"
                                    ),
                                    reminder_at,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.policy",
                    timer_type,
                    {
                        "enabled": bool(enabled),
                        "changed_types": changed,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._get_timer_policy(session_id)
