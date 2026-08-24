from __future__ import annotations

from .characters_support import *


class CharacterRuntimeRepositoryMixin:
    async def list_roster(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_roster, session_id)

    def _list_roster(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    pt.*,
                    d.status AS draft_status,
                    d.current_step AS draft_step,
                    d.expires_at AS draft_expires_at,
                    d.fields_json AS draft_profile_json,
                    d.template_version AS draft_template_version,
                    (
                        SELECT c.code FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_code,
                    (
                        SELECT c.expires_at FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_expires_at,
                    rs.state_json AS runtime_state_json,
                    rs.revision AS runtime_revision,
                    ccv.profile_json AS card_profile_json,
                    ccv.stats_json AS card_stats_json,
                    ccv.version_no AS card_version_no,
                    ccv.template_version AS card_template_version,
                    ccv.status AS card_version_status,
                    ccv.review_note AS card_review_note,
                    ccv.reviewed_by AS card_reviewed_by,
                    ccv.created_at AS card_version_created_at
                FROM participants pt
                LEFT JOIN character_card_drafts d ON d.id = (
                    SELECT draft.id
                    FROM character_card_drafts draft
                    WHERE draft.participant_id = pt.id
                    ORDER BY
                        CASE WHEN draft.status = 'active' THEN 0 ELSE 1 END,
                        draft.generation DESC,
                        draft.updated_at DESC
                    LIMIT 1
                )
                LEFT JOIN character_runtime_states rs
                  ON rs.participant_id = pt.id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ?
                ORDER BY
                    CASE pt.participation_status
                        WHEN 'active' THEN 0
                        WHEN 'reserved' THEN 1
                        WHEN 'standby' THEN 2
                        WHEN 'away' THEN 3
                        WHEN 'retired' THEN 4
                        ELSE 5
                    END,
                    pt.created_at
                """,
                (session_id,),
            ).fetchall()
            return [self._participant(row) for row in rows]

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
        include_retired: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._get_participant,
            session_id,
            user_id,
            participant_ref,
            include_retired,
        )

    def _get_participant(
        self,
        session_id: str,
        user_id: str,
        participant_ref: str,
        include_retired: bool,
    ) -> dict[str, Any]:
        reference = str(participant_ref or "").strip()
        with self._connect() as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家尚未加入当前副本")
                return self._participant(row)
            rows = connection.execute(
                """
                SELECT * FROM participants WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
            matches: list[sqlite3.Row] = []
            lowered = reference.casefold()
            for row in rows:
                if not include_retired and row["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    continue
                aliases = json_load(row["aliases_json"], [])
                names = {
                    str(row["id"]),
                    str(row["character_name"]),
                    str(row["character_code"]),
                    *(str(item) for item in aliases),
                }
                if any(item and item.casefold() == lowered for item in names):
                    matches.append(row)
            if not matches:
                raise DatabaseNotFoundError("未找到精确匹配的角色名或代号")
            if len(matches) > 1:
                raise ValueError("角色标识不唯一，请改用副本内唯一代号")
            return self._participant(matches[0])

    async def reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_participant,
            session_id,
            user_id,
            display_name,
        )

    def _reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100)
        if not display_name:
            raise ValueError("加入失败：平台没有提供可公开显示的名称")
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
                    raise InvalidTransitionError(
                        "只有准备大厅开放加入；请先由主持人开启副本"
                    )
                ban = self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                )
                if ban:
                    reason = str(ban["reason"] or "未注明")
                    raise PermissionError(f"当前无法加入：{reason}")

                existing = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing and existing["participation_status"] not in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    code_row = connection.execute(
                        """
                        SELECT * FROM card_binding_codes
                        WHERE participant_id = ? AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (existing["id"],),
                    ).fetchone()
                    now = utc_now()
                    # expires_at 为空串表示「不限时」，永远不判过期。
                    replaced_code_id = ""
                    if (
                        code_row
                        and code_row["expires_at"]
                        and code_row["expires_at"] <= now
                    ):
                        connection.execute(
                            "UPDATE card_binding_codes SET status = 'expired' WHERE id = ?",
                            (code_row["id"],),
                        )
                        replaced_code_id = str(code_row["id"])
                        code_row = None
                    renewed = False
                    if (
                        not code_row
                        and existing["card_status"] in {CARD_UNCREATED, CARD_DRAFT}
                        and not str(existing["private_origin"] or "")
                    ):
                        config_row = connection.execute(
                            "SELECT time_rules_json FROM instance_configs WHERE session_id = ?",
                            (session_id,),
                        ).fetchone()
                        time_rules = normalize_time_rules(
                            json_load(config_row["time_rules_json"] if config_row else "", {})
                        )
                        code = ""
                        for _ in range(20):
                            candidate = secrets.token_hex(3).upper()
                            if not connection.execute(
                                "SELECT 1 FROM card_binding_codes WHERE code = ?",
                                (candidate,),
                            ).fetchone():
                                code = candidate
                                break
                        if not code:
                            raise RuntimeError("无法生成唯一建卡码")
                        expires_at = deadline_after(time_rules["card_code_ttl_seconds"])
                        replacement_id = new_id("cardcode")
                        connection.execute(
                            """
                            INSERT INTO card_binding_codes(
                                id, participant_id, code, status, expires_at, created_at
                            ) VALUES (?, ?, ?, 'active', ?, ?)
                            """,
                            (replacement_id, existing["id"], code, expires_at, now),
                        )
                        if replaced_code_id:
                            connection.execute(
                                """
                                UPDATE card_binding_codes
                                SET status = 'replaced', replaced_by = ?,
                                    failure_reason = 'expired_join_reissue'
                                WHERE id = ?
                                """,
                                (replacement_id, replaced_code_id),
                            )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=existing["id"],
                            timer_type="card_code",
                            timeout_seconds=time_rules["card_code_ttl_seconds"],
                            reminder_seconds=None,
                            action={"code": code, "renewed": True},
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "card.code_reissued",
                            existing["id"],
                            {"reason": "join_after_expiry"},
                        )
                        code_row = {"code": code, "expires_at": expires_at}
                        renewed = True
                    connection.execute("COMMIT")
                    result = self._participant(existing)
                    result["joined"] = False
                    result["binding_code"] = (
                        code_row["code"] if code_row else ""
                    )
                    result["binding_expires_at"] = (
                        code_row["expires_at"] if code_row else ""
                    )
                    result["binding_code_reissued"] = renewed
                    return result
                if existing:
                    if (
                        existing["participation_status"]
                        in {PARTICIPANT_RETIRED, PARTICIPANT_ARCHIVED}
                        and not str(existing["character_card_id"] or "")
                    ):
                        config_row = connection.execute(
                            """
                            SELECT * FROM instance_configs
                            WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        if not config_row:
                            raise DatabaseNotFoundError("副本配置不存在")
                        now = utc_now()
                        world = json_load(
                            config_row["world_snapshot_json"],
                            {},
                        )
                        template = card_template(world)
                        time_rules = normalize_time_rules(
                            json_load(config_row["time_rules_json"], {})
                        )
                        connection.execute(
                            """
                            UPDATE character_card_drafts
                            SET status = 'superseded',
                                cancel_reason = 'seat_rejoined',
                                updated_at = ?
                            WHERE participant_id = ? AND status = 'active'
                            """,
                            (now, existing["id"]),
                        )
                        generation = int(
                            connection.execute(
                                """
                                SELECT COALESCE(MAX(generation), 0) + 1
                                FROM character_card_drafts
                                WHERE participant_id = ?
                                """,
                                (existing["id"],),
                            ).fetchone()[0]
                        )
                        draft_id = new_id("draft")
                        draft_expires_at = deadline_after(
                            time_rules["card_draft_ttl_seconds"]
                        )
                        connection.execute(
                            """
                            INSERT INTO character_card_drafts(
                                id, participant_id, generation,
                                template_version, template_revision,
                                world_revision, fields_json, current_step,
                                status, expires_at, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, '{}', 0,
                                      'active', ?, ?, ?)
                            """,
                            (
                                draft_id,
                                existing["id"],
                                generation,
                                template["version"],
                                f"actor@{template['version']}",
                                int(config_row["world_revision"] or 1),
                                draft_expires_at,
                                now,
                                now,
                            ),
                        )
                        code = ""
                        for _ in range(20):
                            candidate = secrets.token_hex(3).upper()
                            if not connection.execute(
                                "SELECT 1 FROM card_binding_codes WHERE code = ?",
                                (candidate,),
                            ).fetchone():
                                code = candidate
                                break
                        if not code:
                            raise RuntimeError("无法生成唯一建卡码")
                        code_expires_at = deadline_after(
                            time_rules["card_code_ttl_seconds"]
                        )
                        connection.execute(
                            """
                            INSERT INTO card_binding_codes(
                                id, participant_id, code, status,
                                expires_at, created_at
                            ) VALUES (?, ?, ?, 'active', ?, ?)
                            """,
                            (
                                new_id("cardcode"),
                                existing["id"],
                                code,
                                code_expires_at,
                                now,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE participants SET
                                private_user_id = '', private_origin = '',
                                card_status = 'uncreated', ready = 0,
                                participation_status = 'reserved',
                                exit_reason = '', seat_reserved_at = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (now, now, existing["id"]),
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=existing["id"],
                            timer_type="card_code",
                            timeout_seconds=time_rules[
                                "card_code_ttl_seconds"
                            ],
                            reminder_seconds=None,
                            action={"code": code, "rejoined": True},
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=existing["id"],
                            timer_type="card_completion",
                            timeout_seconds=time_rules[
                                "card_completion_timeout_seconds"
                            ],
                            reminder_seconds=None,
                            action={
                                "timeout_action": time_rules[
                                    "card_timeout_action"
                                ]
                            },
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "participant.reserve_again",
                            existing["id"],
                            {"draft_generation": generation},
                        )
                        updated = connection.execute(
                            "SELECT * FROM participants WHERE id = ?",
                            (existing["id"],),
                        ).fetchone()
                        connection.execute("COMMIT")
                        result = self._participant(updated)
                        result.update(
                            {
                                "joined": True,
                                "rejoined_uncreated": True,
                                "binding_code": code,
                                "binding_expires_at": code_expires_at,
                                "draft_generation": generation,
                            }
                        )
                        return result
                    raise ValueError(
                        "该角色已经正式退场；请使用 /团 申请返场"
                    )

                config_row = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not config_row:
                    raise DatabaseNotFoundError("副本配置不存在")
                world = json_load(config_row["world_snapshot_json"], {})
                limits = player_limits(world)
                placeholders = ",".join(
                    "?" for _ in SEAT_HOLDING_STATUSES
                )
                occupied = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM participants
                    WHERE session_id = ?
                      AND participation_status IN ({placeholders})
                    """,
                    (session_id, *sorted(SEAT_HOLDING_STATUSES)),
                ).fetchone()[0]
                if occupied >= limits["maximum"]:
                    raise ValueError(
                        f"当前副本已满（{occupied}/{limits['maximum']}）"
                    )

                now = utc_now()
                player = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            now,
                            now,
                        ),
                    )
                else:
                    player_id = player["id"]
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, enabled = 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (display_name, now, player_id),
                    )

                participant_id = new_id("participant")
                connection.execute(
                    """
                    INSERT INTO participants(
                        id, session_id, player_id, group_user_id,
                        display_name, aliases_json, card_status, ready,
                        participation_status, seat_reserved_at, joined_round,
                        consecutive_timeouts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '[]', 'uncreated', 0,
                              'reserved', ?, 1, 0, ?, ?)
                    """,
                    (
                        participant_id,
                        session_id,
                        player_id,
                        user_id,
                        display_name,
                        now,
                        now,
                        now,
                    ),
                )
                template = card_template(world)
                time_rules = normalize_time_rules(
                    json_load(config_row["time_rules_json"], {})
                )
                draft_id = new_id("draft")
                draft_expires_at = deadline_after(
                    time_rules["card_draft_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO character_card_drafts(
                        id, participant_id, generation, template_version,
                        template_revision, world_revision, fields_json,
                        current_step, status, expires_at, created_at, updated_at
                    ) VALUES (?, ?, 1, ?, ?, ?, '{}', 0, 'active', ?, ?, ?)
                    """,
                    (
                        draft_id,
                        participant_id,
                        template["version"],
                        f"actor@{template['version']}",
                        int(config_row["world_revision"] or 1),
                        draft_expires_at,
                        now,
                        now,
                    ),
                )
                code = ""
                for _ in range(20):
                    candidate = secrets.token_hex(3).upper()
                    if not connection.execute(
                        "SELECT 1 FROM card_binding_codes WHERE code = ?",
                        (candidate,),
                    ).fetchone():
                        code = candidate
                        break
                if not code:
                    raise RuntimeError("无法生成唯一建卡码")
                code_expires_at = deadline_after(
                    time_rules["card_code_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO card_binding_codes(
                        id, participant_id, code, status, expires_at,
                        created_at
                    ) VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        new_id("cardcode"),
                        participant_id,
                        code,
                        code_expires_at,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_code",
                    timeout_seconds=time_rules["card_code_ttl_seconds"],
                    reminder_seconds=None,
                    action={"code": code},
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_completion",
                    timeout_seconds=time_rules[
                        "card_completion_timeout_seconds"
                    ],
                    reminder_seconds=None,
                    action={
                        "timeout_action": time_rules[
                            "card_timeout_action"
                        ]
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.reserve",
                    participant_id,
                    {
                        "occupied": occupied + 1,
                        "maximum": limits["maximum"],
                    },
                )
                row = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result.update(
                    {
                        "joined": True,
                        "binding_code": code,
                        "binding_expires_at": code_expires_at,
                        "limits": limits,
                    }
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _create_timer(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        timer_type: str,
        timeout_seconds: int | None,
        reminder_seconds: int | None,
        action: Mapping[str, Any],
    ) -> str:
        timer_id = new_id("timer")
        now_dt = datetime.now(timezone.utc)
        action_payload = dict(action)
        reminder_interval = (
            CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
            if timer_type == "card_completion"
            else reminder_seconds
        )
        # 先作废同范围内仍存活的旧计时器，保证「一个范围一个计时器」。
        # 缺少这一步时，重复的 继续 / 读档 / 回合推进 会让 timer_instances
        # 里堆积多条 active 行，process_due_timers 便会一次性吐出多条提醒。
        supersede_sql = """
            UPDATE timer_instances
            SET status = 'cancelled', deadline_at = '',
                reminder_at = '', reminder_sent = 0, updated_at = ?
            WHERE session_id = ? AND timer_type = ?
              AND status IN ('active', 'paused')
        """
        supersede_args: tuple[Any, ...] = (
            now_dt.isoformat(timespec="seconds"),
            session_id,
            timer_type,
        )
        if timer_type not in SESSION_SINGLETON_TIMER_TYPES:
            supersede_sql += " AND participant_id = ?"
            supersede_args += (participant_id,)
        connection.execute(supersede_sql, supersede_args)
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
        countdown_enabled = bool(
            (policy["global_enabled"] if policy else 0)
            and switches.get(timer_type, False)
        )
        if not countdown_enabled:
            action_payload["paused_by_policy"] = True
        if timer_type == "card_completion":
            action_payload.setdefault("reminder_enabled", False)
            action_payload["reminder_interval_seconds"] = (
                CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
            )
        elif countdown_enabled:
            reminder_interval = (
                reminder_interval
                if reminder_interval is not None
                else TIMER_REMINDER_INTERVAL_SECONDS
            )
            action_payload["reminder_interval_seconds"] = reminder_interval
        deadline = (
            now_dt + timedelta(seconds=timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        # Reminder values mean "seconds before deadline" and fire once.
        reminder = (
            deadline - timedelta(seconds=reminder_interval)
            if deadline is not None
            and reminder_interval is not None
            and timeout_seconds > reminder_interval
            and timer_reminder_enabled(timer_type, action_payload)
            else None
        )
        status = "active" if countdown_enabled else "paused"
        now = now_dt.isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO timer_instances(
                id, session_id, participant_id, timer_type, status,
                deadline_at, remaining_seconds, reminder_at,
                reminder_sent, action_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                timer_id,
                session_id,
                participant_id,
                timer_type,
                status,
                (
                    deadline.isoformat(timespec="seconds")
                    if deadline is not None and countdown_enabled
                    else ""
                ),
                timeout_seconds,
                (
                    reminder.isoformat(timespec="seconds")
                    if reminder is not None and countdown_enabled
                    else ""
                ),
                json_dump(action_payload),
                now,
                now,
            ),
        )
        return timer_id

    def _start_standby_timer(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
    ) -> str | None:
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        timeout_seconds = rules["standby_timeout_seconds"]
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'standby'
              AND status IN ('active', 'paused')
            """,
            (utc_now(), session_id, participant_id),
        )
        if timeout_seconds is None:
            return None
        return self._create_timer(
            connection,
            session_id=session_id,
            participant_id=participant_id,
            timer_type="standby",
            timeout_seconds=timeout_seconds,
            reminder_seconds=None,
            action={"reason": "standby_timeout"},
        )

    async def pending_card_bindings_for_user(
        self,
        platform_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """查询同一平台账号唯一拥有的未绑定建卡入口。"""

        return await self._run(
            self._pending_card_bindings_for_user,
            platform_id,
            user_id,
        )

    def _pending_card_bindings_for_user(
        self,
        platform_id: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台实例",
        )
        normalized_user = validate_platform_id(user_id, label="用户 ID")
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pt.*, c.code AS binding_code,
                       c.expires_at AS binding_expires_at,
                       s.instance_name
                FROM participants pt
                JOIN sessions s ON s.id = pt.session_id
                JOIN character_card_drafts d
                  ON d.participant_id = pt.id AND d.status = 'active'
                JOIN card_binding_codes c
                  ON c.participant_id = pt.id AND c.status = 'active'
                WHERE (
                        s.platform_id = ?
                        OR (
                            ? = 'qq_official'
                            AND lower(s.platform_id) = 'qq'
                        )
                      )
                  AND pt.group_user_id = ?
                  AND pt.private_origin = ''
                  AND pt.participation_status NOT IN ('retired', 'archived')
                  AND s.state <> 'finished'
                  AND (c.expires_at = '' OR c.expires_at > ?)
                ORDER BY pt.updated_at DESC, c.created_at DESC
                """,
                (
                    normalized_platform,
                    normalized_platform.casefold(),
                    normalized_user,
                    now,
                ),
            ).fetchall()
            return [
                {
                    **self._participant(row),
                    "binding_code": row["binding_code"],
                    "binding_expires_at": row["binding_expires_at"],
                    "instance_name": row["instance_name"],
                }
                for row in rows
            ]

    async def abandon_card_seat(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(self._abandon_card_seat, private_origin)
