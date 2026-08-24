from __future__ import annotations

from .timers_support import *


class TimerRuntimeRepositoryMixin:
    async def list_timers(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_timers, session_id)

    def _list_timers(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, pt.character_name, pt.display_name
                FROM timer_instances t
                LEFT JOIN participants pt ON pt.id = t.participant_id
                WHERE t.session_id = ?
                ORDER BY
                    CASE t.status
                        WHEN 'active' THEN 0
                        WHEN 'paused' THEN 1
                        ELSE 2
                    END,
                    t.deadline_at, t.created_at
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["action"] = json_load(item.pop("action_json"), {})
                result.append(item)
            return result

    async def control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        *,
        seconds: int = 0,
        session_id: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._control_timer,
            timer_id,
            action,
            actor_id,
            seconds,
            session_id,
            expected_revision,
            idempotency_key,
        )

    def _control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        seconds: int,
        session_id: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in {
            "pause",
            "resume",
            "extend",
            "expire",
            "disable",
        }:
            raise ValueError("不支持的计时器操作")
        requested_session = clean_text(session_id, max_chars=240)
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "timer_id": clean_text(timer_id, max_chars=240),
            "action": action,
            "seconds": int(seconds or 0),
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
                                "相同幂等键已用于另一份计时器操作"
                            )
                        if str(receipt["status"] or "") == "completed":
                            result = json_load(receipt["result_json"], {})
                            result["replayed"] = True
                            connection.execute("COMMIT")
                            return result
                        raise DatabaseConflictError(
                            "计时器操作仍在处理中，请稍后重试"
                        )
                row = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("计时器不存在")
                if requested_session and str(row["session_id"] or "") != requested_session:
                    raise DatabaseNotFoundError("计时器不存在或不属于当前副本")
                if (
                    expected_revision is not None
                    and timer_revision(dict(row)) != int(expected_revision)
                ):
                    raise DatabaseConflictError("计时器状态已经变化")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, Mapping):
                    payload = {}
                reminder_interval = timer_reminder_interval(
                    row["timer_type"],
                    payload,
                )
                reminders_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                status = row["status"]
                deadline = str(row["deadline_at"] or "")
                remaining = row["remaining_seconds"]
                reminder_at = str(row["reminder_at"] or "")
                reminder_sent = int(row["reminder_sent"] or 0)
                if action == "pause":
                    if status != "active":
                        raise ValueError("只有运行中的计时器可以暂停")
                    if deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        remaining = max(
                            0,
                            int((deadline_dt - now_dt).total_seconds()),
                        )
                    status = "paused"
                    deadline = ""
                    reminder_at = ""
                    reminder_sent = 0
                elif action == "resume":
                    if status != "paused":
                        raise ValueError("只有暂停中的计时器可以恢复")
                    status = "active"
                    seconds_left = max(1, int(remaining or 1))
                    deadline_dt = now_dt + timedelta(seconds=seconds_left)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    next_reminder = deadline_dt - timedelta(
                        seconds=reminder_interval
                    )
                    if reminders_enabled:
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    reminder_sent = 0
                elif action == "extend":
                    if seconds <= 0:
                        raise ValueError("延长时间必须大于 0")
                    if status == "active" and deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        deadline_dt += timedelta(seconds=seconds)
                        deadline = deadline_dt.isoformat(timespec="seconds")
                        next_reminder = deadline_dt - timedelta(
                            seconds=reminder_interval
                        )
                        if not reminders_enabled:
                            reminder_at = deadline
                            reminder_sent = 0
                        elif not reminder_at and next_reminder < deadline_dt:
                            reminder_at = next_reminder.isoformat(
                                timespec="seconds"
                            )
                            reminder_sent = 0
                    else:
                        remaining = max(0, int(remaining or 0)) + seconds
                elif action == "expire":
                    status = "expired"
                    deadline = now
                    remaining = 0
                    reminder_at = ""
                else:
                    status = "cancelled"
                    deadline = ""
                    reminder_at = ""
                connection.execute(
                    """
                    UPDATE timer_instances SET
                        status = ?, deadline_at = ?,
                        remaining_seconds = ?, reminder_at = ?,
                        reminder_sent = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        deadline,
                        remaining,
                        reminder_at,
                        reminder_sent,
                        now,
                        timer_id,
                    ),
                )
                if action == "expire":
                    if row["timer_type"] == "card_code":
                        connection.execute(
                            """
                            UPDATE card_binding_codes
                            SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (str(payload.get("code") or ""),),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            payload.get("timeout_action") or "standby"
                        )
                        if timeout_action != "remind":
                            next_status = (
                                "archived"
                                if row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                                else "standby"
                            )
                            connection.execute(
                                """
                                UPDATE participants SET
                                    participation_status = ?,
                                    ready = 0, updated_at = ?
                                WHERE id = ?
                                  AND participation_status IN (
                                      'reserved', 'active'
                                  )
                                """,
                                (
                                    next_status,
                                    now,
                                    row["participant_id"],
                                ),
                            )
                            if (
                                row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                            ):
                                connection.execute(
                                    """
                                    UPDATE character_card_drafts
                                    SET status = 'expired',
                                        updated_at = ?
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (now, row["participant_id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE card_binding_codes
                                    SET status = 'expired'
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (row["participant_id"],),
                                )
                            elif next_status == PARTICIPANT_STANDBY:
                                self._start_standby_timer(
                                    connection,
                                    session_id=row["session_id"],
                                    participant_id=row["participant_id"],
                                )
                    elif (
                        row["timer_type"] == "standby"
                        and row["participant_id"]
                    ):
                        self._retire_participant_in_tx(
                            connection,
                            session_id=row["session_id"],
                            participant_id=row["participant_id"],
                            actor_id=actor_id,
                            forced=False,
                            reason="standby_timeout",
                        )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    f"timer.{action}",
                    timer_id,
                    {"seconds": seconds},
                )
                updated = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                item = dict(updated)
                item["action"] = json_load(item.pop("action_json"), {})
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
                            str(row["session_id"] or ""),
                            input_hash,
                            json_dump(item),
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
                return item
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._extend_active_timer,
            session_id,
            target,
            seconds,
            actor_id,
        )

    def _extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        reference = str(target or "").strip()
        with self._connect() as connection:
            if reference in {"准备阶段", "准备", "preparation"}:
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            else:
                participant = self._get_participant(
                    session_id,
                    "",
                    reference,
                    True,
                )
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND participant_id = ?
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, participant["id"]),
                ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有找到对应的活动计时器")
        return self._control_timer(
            str(row["id"]),
            "extend",
            actor_id,
            seconds,
        )

    async def pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._pause_session_timers,
            session_id,
            actor_id,
        )

    def _pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                for row in rows:
                    remaining = row["remaining_seconds"]
                    if row["deadline_at"]:
                        remaining = max(
                            0,
                            int(
                                (
                                    datetime.fromisoformat(row["deadline_at"])
                                    - now_dt
                                ).total_seconds()
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'paused', deadline_at = '',
                            remaining_seconds = ?, reminder_at = '',
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (remaining, now, row["id"]),
                    )
                    payload = json_load(row["action_json"], {})
                    if (
                        row["timer_type"] == "vote"
                        and isinstance(payload, Mapping)
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = '', updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.pause_all",
                    session_id,
                    {"count": len(rows)},
                )
                connection.execute("COMMIT")
                return len(rows)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._resume_session_timers,
            session_id,
            actor_id,
        )

    def _resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'paused'
                    """,
                    (session_id,),
                ).fetchall()
                # 后台关掉的倒计时不能被「恢复/继续/读档」重新唤醒。
                # 旧实现无条件恢复全部 paused 行，导致管理员关掉倒计时后
                # 只要有人发 /团 继续，提醒就会重新开始刷屏。
                policy = connection.execute(
                    """
                    SELECT global_enabled, switches_json FROM timer_policies
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                switches = json_load(
                    policy["switches_json"] if policy else "",
                    {},
                )
                switches = switches if isinstance(switches, Mapping) else {}
                global_enabled = bool(
                    policy["global_enabled"] if policy else 0
                )
                resumed = 0
                for row in rows:
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    timer_type = str(row["timer_type"])
                    countdown_enabled = bool(
                        global_enabled and switches.get(timer_type, False)
                    )
                    if not countdown_enabled:
                        # 策略仍为关闭：保持暂停，并补齐标记，
                        # 便于之后重新打开时由 _set_timer_policy 正确复活。
                        if not payload.get("paused_by_policy"):
                            payload = dict(payload)
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances
                                SET action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (json_dump(payload), now, row["id"]),
                            )
                        continue
                    if payload.get("paused_by_policy"):
                        # 策略已重新打开的场景由 _set_timer_policy 负责恢复，
                        # 这里不越权唤醒，避免与策略状态打架。
                        continue
                    resumed += 1
                    remaining = max(1, int(row["remaining_seconds"] or 1))
                    deadline_dt = now_dt + timedelta(seconds=remaining)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    interval = timer_reminder_interval(
                        row["timer_type"],
                        payload,
                    )
                    next_reminder = deadline_dt - timedelta(
                        seconds=interval
                    )
                    if timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'active', deadline_at = ?,
                            reminder_at = ?, reminder_sent = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (deadline, reminder_at, now, row["id"]),
                    )
                    if (
                        row["timer_type"] == "vote"
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = ?, updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (deadline, now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.resume_all",
                    session_id,
                    {
                        "count": resumed,
                        "skipped_by_policy": len(rows) - resumed,
                    },
                )
                connection.execute("COMMIT")
                return resumed
            except Exception:
                connection.execute("ROLLBACK")
                raise
