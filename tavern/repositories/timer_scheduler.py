from __future__ import annotations

from .timers_support import *


class TimerSchedulerRepositoryMixin:
    async def process_due_timers(self) -> list[dict[str, Any]]:
        return await self._run(self._process_due_timers)

    def _process_due_timers(self) -> list[dict[str, Any]]:
        notifications: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                now_dt = datetime.fromisoformat(now)
                reminder_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active'
                      AND reminder_at <> '' AND reminder_at <= ?
                      AND deadline_at <> '' AND deadline_at > ?
                    """,
                    (now, now),
                ).fetchall()
                # 分发前再查一次策略：即便有历史脏数据或并发写入
                # 让被关闭的计时器仍处于 active，也不会再推送提醒。
                policy_cache: dict[str, tuple[bool, Mapping[str, Any]]] = {}

                def _countdown_allowed(
                    session_key: str,
                    timer_type_key: str,
                ) -> bool:
                    cached = policy_cache.get(session_key)
                    if cached is None:
                        policy_row = connection.execute(
                            """
                            SELECT global_enabled, switches_json
                            FROM timer_policies WHERE session_id = ?
                            """,
                            (session_key,),
                        ).fetchone()
                        policy_switches = json_load(
                            policy_row["switches_json"]
                            if policy_row
                            else "",
                            {},
                        )
                        if not isinstance(policy_switches, Mapping):
                            policy_switches = {}
                        cached = (
                            bool(
                                policy_row["global_enabled"]
                                if policy_row
                                else 0
                            ),
                            policy_switches,
                        )
                        policy_cache[session_key] = cached
                    enabled, switch_map = cached
                    return bool(
                        enabled and switch_map.get(timer_type_key, False)
                    )

                for row in reminder_rows:
                    deadline = datetime.fromisoformat(row["deadline_at"])
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    # A24 以前的建卡计时器没有显式保存提醒开关和间隔。
                    # 首次轮询时迁移为 120 秒节奏，但不立即发送已经过时的
                    # 旧提醒，避免升级瞬间刷屏。
                    if (
                        row["timer_type"] == "card_completion"
                        and (
                            "reminder_enabled" not in payload
                            or "reminder_interval_seconds" not in payload
                        )
                    ):
                        migrated_payload = dict(payload)
                        migrated_payload["reminder_enabled"] = True
                        migrated_payload["reminder_interval_seconds"] = (
                            CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                        )
                        next_at = min(
                            deadline,
                            now_dt + timedelta(
                                seconds=CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET action_json = ?, reminder_at = ?,
                                reminder_sent = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(migrated_payload),
                                next_at.isoformat(timespec="seconds"),
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    reminder_interval = timer_reminder_interval(
                        row["timer_type"],
                        payload,
                    )
                    if not _countdown_allowed(
                        str(row["session_id"]),
                        str(row["timer_type"]),
                    ):
                        stale_payload = dict(payload)
                        stale_payload["paused_by_policy"] = True
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, action_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                max(
                                    0,
                                    int(
                                        (deadline - now_dt).total_seconds()
                                    ),
                                ),
                                json_dump(stale_payload),
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    if not timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET reminder_at = ?, reminder_sent = 0,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (row["deadline_at"], now, row["id"]),
                        )
                        continue
                    remaining = max(
                        1,
                        int((deadline - now_dt).total_seconds()),
                    )
                    next_reminder = now_dt + timedelta(
                        seconds=max(1, int(reminder_interval or 1))
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET reminder_at = ?, reminder_sent = 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            (
                                next_reminder.isoformat(timespec="seconds")
                                if next_reminder < deadline
                                else ""
                            ),
                            now,
                            row["id"],
                        ),
                    )
                    notifications.append(
                        {
                            "kind": "reminder",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": remaining,
                            "reminder_interval_seconds": (
                                reminder_interval
                            ),
                            "targets": self._timer_notice_targets(
                                connection,
                                row,
                            ),
                        }
                    )
                due_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active' AND deadline_at <> ''
                      AND deadline_at <= ?
                    ORDER BY deadline_at, created_at
                    """,
                    (now,),
                ).fetchall()
                for row in due_rows:
                    targets = self._timer_notice_targets(connection, row)
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'expired', remaining_seconds = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    action = json_load(row["action_json"], {})
                    if row["timer_type"] == "card_code":
                        code = str(action.get("code") or "")
                        connection.execute(
                            """
                            UPDATE card_binding_codes SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (code,),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            action.get("timeout_action") or "standby"
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
                            if next_status == "archived":
                                connection.execute(
                                    """
                                    UPDATE character_card_drafts
                                    SET status = 'expired', updated_at = ?
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
                            actor_id="system",
                            forced=False,
                            reason="standby_timeout",
                        )
                    notifications.append(
                        {
                            "kind": "expired",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": 0,
                            "targets": targets,
                        }
                    )
                running_rows = connection.execute(
                    """
                    SELECT s.id, s.updated_at, ic.time_rules_json,
                           ic.phase_meta_json
                    FROM sessions s
                    JOIN instance_configs ic ON ic.session_id = s.id
                    WHERE s.state = 'running'
                    """
                ).fetchall()
                for session_row in running_rows:
                    rules = normalize_time_rules(
                        json_load(session_row["time_rules_json"], {})
                    )
                    idle_seconds = rules["all_idle_pause_seconds"]
                    if idle_seconds is None:
                        continue
                    idle_policy = connection.execute(
                        """
                        SELECT global_enabled, switches_json
                        FROM timer_policies WHERE session_id = ?
                        """,
                        (session_row["id"],),
                    ).fetchone()
                    idle_switches = json_load(
                        idle_policy["switches_json"] if idle_policy else "",
                        {},
                    )
                    if not (
                        idle_policy
                        and bool(idle_policy["global_enabled"])
                        and isinstance(idle_switches, Mapping)
                        and bool(idle_switches.get("all_idle", False))
                    ):
                        continue
                    phase_meta = json_load(
                        session_row["phase_meta_json"],
                        {},
                    )
                    activity_values = [
                        str(
                            phase_meta.get("started_at")
                            or session_row["updated_at"]
                            or ""
                        )
                    ]
                    activity_values.extend(
                        str(item[0] or "")
                        for item in connection.execute(
                            """
                            SELECT MAX(created_at) FROM events
                            WHERE session_id = ? AND role = 'player'
                            UNION ALL
                            SELECT MAX(vb.updated_at)
                            FROM vote_ballots vb
                            JOIN group_votes gv ON gv.id = vb.vote_id
                            WHERE gv.session_id = ?
                            UNION ALL
                            SELECT MAX(updated_at) FROM choice_sets
                            WHERE session_id = ? AND reroll_count > 0
                            """,
                            (
                                session_row["id"],
                                session_row["id"],
                                session_row["id"],
                            ),
                        ).fetchall()
                    )
                    last_activity = max(
                        (value for value in activity_values if value),
                        default=now,
                    )
                    try:
                        last_dt = datetime.fromisoformat(last_activity)
                    except ValueError:
                        continue
                    if (now_dt - last_dt).total_seconds() < idle_seconds:
                        continue
                    timer_rows = connection.execute(
                        """
                        SELECT * FROM timer_instances
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (session_row["id"],),
                    ).fetchall()
                    for timer_row in timer_rows:
                        remaining = timer_row["remaining_seconds"]
                        deadline = str(timer_row["deadline_at"] or "")
                        if deadline:
                            try:
                                deadline_dt = datetime.fromisoformat(deadline)
                                remaining = max(
                                    0,
                                    int(
                                        (deadline_dt - now_dt).total_seconds()
                                    ),
                                )
                            except ValueError:
                                pass
                        connection.execute(
                            """
                            UPDATE timer_instances SET
                                status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (remaining, now, timer_row["id"]),
                        )
                    connection.execute(
                        """
                        UPDATE sessions
                        SET state = 'paused', revision = revision + 1,
                            updated_at = ?
                        WHERE id = ? AND state = 'running'
                        """,
                        (now, session_row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        session_row["id"],
                        "system",
                        "session.idle_pause",
                        session_row["id"],
                        {
                            "idle_seconds": idle_seconds,
                            "last_activity": last_activity,
                            "paused_timers": len(timer_rows),
                        },
                    )
                    notifications.append(
                        {
                            "kind": "idle_pause",
                            "session_id": session_row["id"],
                            "timer_type": "all_idle",
                            "participant_id": "",
                        }
                    )
                connection.execute("COMMIT")
                return notifications
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _expire_turn_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (row["session_id"],),
        ).fetchone()
        if not session or session["state"] != SESSION_RUNNING:
            return
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (row["participant_id"],),
        ).fetchone()
        if not participant:
            return
        state = json_load(session["world_state_json"], {})
        turn = turn_state_from_world(state)
        if turn["current_user_id"] != participant["group_user_id"]:
            return
        timeout_count = int(participant["consecutive_timeouts"]) + 1
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        if rules["turn_timeout_action"] == "hold":
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = ?, updated_at = ?
                WHERE id = ?
                """,
                (timeout_count, now, participant["id"]),
            )
            append_event(
                connection,
                session_id=session["id"],
                turn_no=session["turn_no"],
                role="system",
                actor_id="system",
                actor_name="回合计时",
                content=(
                    f"{participant['character_name'] or participant['display_name']}"
                    "本回合超时；按副本规则保留行动权与原选项。"
                ),
                meta={
                    "kind": "turn_timeout",
                    "participant_id": participant["id"],
                    "consecutive": timeout_count,
                    "action": "hold",
                },
                created_at=now,
            )
            return
        next_turn = advance_turn(turn, participant["group_user_id"])
        moved_to_standby = (
            timeout_count >= rules["max_consecutive_timeouts"]
        )
        if moved_to_standby:
            next_turn, _ = leave_turn(
                next_turn,
                participant["group_user_id"],
            )
        connection.execute(
            """
            UPDATE participants SET
                consecutive_timeouts = ?,
                participation_status = CASE
                    WHEN ? THEN 'standby' ELSE participation_status
                END,
                ready = CASE WHEN ? THEN 0 ELSE ready END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                timeout_count,
                int(moved_to_standby),
                int(moved_to_standby),
                now,
                participant["id"],
            ),
        )
        if moved_to_standby:
            self._start_standby_timer(
                connection,
                session_id=session["id"],
                participant_id=participant["id"],
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, str(action.get("choice_set_id") or "")),
        )
        connection.execute(
            """
            UPDATE sessions SET
                world_state_json = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json_dump(
                    embed_turn_state(public_world_state(state), next_turn)
                ),
                now,
                session["id"],
            ),
        )
        append_event(
            connection,
            session_id=session["id"],
            turn_no=session["turn_no"],
            role="system",
            actor_id="system",
            actor_name="回合计时",
            content=(
                f"{participant['character_name'] or participant['display_name']}"
                "本回合超时，行动权已安全移交。"
                + (
                    "连续超时达到上限，已转入候补席。"
                    if moved_to_standby
                    else ""
                )
            ),
            meta={
                "kind": "turn_timeout",
                "participant_id": participant["id"],
                "consecutive": timeout_count,
                "standby": moved_to_standby,
            },
            created_at=now,
        )
        next_user = str(next_turn["current_user_id"] or "")
        if not next_user:
            return
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
            """,
            (session["id"], next_user),
        ).fetchone()
        if not next_participant:
            return
        choice_id = new_id("choices")
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                next_participant["id"],
                next_turn["round_no"],
                int(session["revision"]) + 1,
                json_dump(fallback_choices(state)),
                f"timeout:{row['id']}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={"choice_set_id": choice_id, "user_id": next_user},
        )

    def _expire_vote_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        vote_id = str(action.get("vote_id") or "")
        vote_row = connection.execute(
            """
            SELECT * FROM group_votes
            WHERE id = ? AND status = 'open'
            """,
            (vote_id,),
        ).fetchone()
        if not vote_row:
            return
        vote = self._vote(vote_row)
        ballots = [
            dict(item)
            for item in connection.execute(
                """
                SELECT user_id, option_key FROM vote_ballots
                WHERE vote_id = ?
                """,
                (vote_id,),
            ).fetchall()
        ]
        tally = vote_result(
            eligible_count=len(vote["eligible_user_ids"]),
            ballots=ballots,
            option_keys=[
                str(item.get("key")) for item in vote["options"]
            ],
        )
        # 0.11.3：超时结束时按实际票数判定——截止前已形成多数则通过，
        # 并标记 pending_resolution 供下次输入自动推进叙事；
        # 旧实现无条件判 rejected，多数票在截止前达成也会被否决。
        winner = str(tally.get("winner") or "")
        passed = bool(winner)
        status = "decided" if passed else "rejected"
        result_payload: dict[str, Any] = {**tally, "reason": "timeout"}
        if passed:
            result_payload["pending_resolution"] = True
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (vote["session_id"],),
        ).fetchone()
        if session is None:
            raise DatabaseNotFoundError("会话不存在")
        operation_id = f"vote-resolution:{vote_id}" if passed else ""
        connection.execute(
            """
            UPDATE group_votes SET
                status = ?, winner_key = ?, decision_status = ?,
                resolution_status = ?, resolution_operation_id = ?,
                decision_revision = ?, decided_at = ?,
                result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                winner,
                "decided" if passed else "rejected",
                "pending" if passed else "not_started",
                operation_id,
                int(session["revision"] or 0),
                now,
                json_dump(result_payload),
                now,
                vote_id,
            ),
        )
        if operation_id:
            request_payload = {
                "vote_id": vote_id,
                "winner_key": winner,
                "decision_revision": int(session["revision"] or 0),
                "suspended_user_id": str(vote.get("suspended_user_id") or ""),
            }
            connection.execute(
                """
                INSERT INTO operation_receipts(
                    operation_id, session_id, operation_type, request_json,
                    result_json, status, phase, input_hash,
                    created_at, updated_at
                ) VALUES (?, ?, 'vote_resolution', ?, ?, 'reserved',
                          'decision_locked', ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    operation_id,
                    vote["session_id"],
                    json_dump(request_payload),
                    json_dump({"phase": "decision_locked", "vote_id": vote_id}),
                    content_hash(request_payload),
                    now,
                    now,
                ),
            )
        self._resume_after_vote(
            connection,
            session=session,
            vote=vote,
            now=now,
        )
        self._apply_return_vote_result(
            connection,
            vote_id=vote_id,
            passed=passed,
            now=now,
        )
