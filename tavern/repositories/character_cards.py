from __future__ import annotations

from .characters_support import *


class CharacterCardsRepositoryMixin:
    async def authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authoritative_modifier,
            session_id,
            user_id,
            stat_ref,
        )

    def _authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.character_name, pt.display_name,
                       ccv.stats_json
                FROM participants pt
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            stats: Mapping[str, Any] = {}
            if row:
                stats = json_load(row["stats_json"], {})
            else:
                actor = next(
                    (
                        item
                        for item in connection.execute(
                            """
                            SELECT a.id, i.frozen_profile_json
                            FROM actors a
                            JOIN ai_companion_instances i
                              ON i.actor_id=a.id
                            WHERE a.session_id=?
                              AND a.actor_kind='ai_companion'
                              AND a.status='active'
                              AND i.status<>'retired'
                            """,
                            (session_id,),
                        ).fetchall()
                        if (
                            "public:actor:"
                            + hashlib.sha256(
                                str(item["id"]).encode("utf-8")
                            ).hexdigest()[:12].upper()
                        )
                        == str(user_id)
                    ),
                    None,
                )
                if actor is not None:
                    profile = json_load(
                        actor["frozen_profile_json"],
                        {},
                    )
                    stats = (
                        profile.get("stats")
                        if isinstance(profile, Mapping)
                        else {}
                    )
            if not isinstance(stats, Mapping) or not stats:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            modifiers = stats.get("modifiers")
            labels = stats.get("labels")
            modifiers = modifiers if isinstance(modifiers, Mapping) else {}
            labels = labels if isinstance(labels, Mapping) else {}
            reference = clean_text(stat_ref, max_chars=40).casefold()
            matched_key = ""
            for key, label in labels.items():
                candidates = {
                    str(key).casefold(),
                    str(label).casefold(),
                    f"{label}检定".casefold(),
                }
                if reference in candidates:
                    matched_key = str(key)
                    break
            if not matched_key and reference in {
                str(key).casefold() for key in modifiers
            }:
                matched_key = next(
                    str(key)
                    for key in modifiers
                    if str(key).casefold() == reference
                )
            if not matched_key:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            return {
                "stat": str(labels.get(matched_key) or matched_key),
                "modifier": max(
                    -10,
                    min(10, int(modifiers.get(matched_key, 0))),
                ),
                "matched": True,
            }

    @staticmethod
    def _active_ban_for(
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        user_id: str,
    ) -> sqlite3.Row | None:
        now = utc_now()
        connection.execute(
            """
            UPDATE ban_records SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
            """,
            (now, now),
        )
        return connection.execute(
            """
            SELECT * FROM ban_records
            WHERE user_id = ? AND status = 'active'
              AND (
                    scope = 'global'
                 OR (scope = 'group' AND platform_id = ? AND group_id = ?)
                 OR (scope = 'instance' AND session_id = ?)
              )
            ORDER BY
                CASE scope
                    WHEN 'global' THEN 0
                    WHEN 'group' THEN 1
                    ELSE 2
                END
            LIMIT 1
            """,
            (
                user_id,
                session["platform_id"],
                session["group_id"],
                session["id"],
            ),
        ).fetchone()

    async def bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._bind_card_code,
            code,
            private_user_id,
            private_origin,
        )

    def _bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        normalized_code = str(code or "").strip().upper()
        private_user_id = validate_platform_id(
            private_user_id,
            label="私聊用户 ID",
        )
        private_origin = clean_text(private_origin, max_chars=500)
        if not normalized_code:
            raise ValueError("建卡码不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                code_row = connection.execute(
                    """
                    SELECT c.*, pt.session_id
                    FROM card_binding_codes c
                    JOIN participants pt ON pt.id = c.participant_id
                    WHERE c.code = ?
                    """,
                    (normalized_code,),
                ).fetchone()
                if not code_row:
                    raise ValueError("建卡码不存在")
                if code_row["status"] != "active":
                    status_messages = {
                        "used": "建卡码已经绑定过私聊",
                        "expired": "建卡码已经过期",
                        "cancelled": "建卡码已随草稿取消",
                        "replaced": "建卡码已被新码替换",
                        "revoked": "建卡码已撤销",
                    }
                    raise ValueError(
                        status_messages.get(
                            str(code_row["status"]),
                            "建卡码当前不可使用",
                        )
                    )
                # 空串表示「不限时」，不参与过期判定。
                if code_row["expires_at"] and code_row["expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE card_binding_codes
                        SET status = 'expired' WHERE id = ?
                        """,
                        (code_row["id"],),
                    )
                    config = connection.execute(
                        "SELECT time_rules_json FROM instance_configs WHERE session_id = ?",
                        (code_row["session_id"],),
                    ).fetchone()
                    time_rules = normalize_time_rules(
                        json_load(config["time_rules_json"] if config else "", {})
                    )
                    replacement = ""
                    for _ in range(20):
                        candidate = secrets.token_hex(3).upper()
                        if not connection.execute(
                            "SELECT 1 FROM card_binding_codes WHERE code = ?",
                            (candidate,),
                        ).fetchone():
                            replacement = candidate
                            break
                    if not replacement:
                        raise RuntimeError("无法生成唯一建卡码")
                    expires_at = deadline_after(time_rules["card_code_ttl_seconds"])
                    replacement_id = new_id("cardcode")
                    connection.execute(
                        """
                        INSERT INTO card_binding_codes(
                            id, participant_id, code, status, expires_at, created_at
                        ) VALUES (?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            replacement_id, code_row["participant_id"],
                            replacement, expires_at, now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE card_binding_codes
                        SET status = 'replaced', replaced_by = ?,
                            failure_reason = 'expired_code_reissued'
                        WHERE id = ?
                        """,
                        (replacement_id, code_row["id"]),
                    )
                    self._create_timer(
                        connection,
                        session_id=code_row["session_id"],
                        participant_id=code_row["participant_id"],
                        timer_type="card_code",
                        timeout_seconds=time_rules["card_code_ttl_seconds"],
                        reminder_seconds=None,
                        action={"code": replacement, "renewed": True},
                    )
                    self._insert_audit(
                        connection,
                        code_row["session_id"],
                        private_user_id,
                        "card.code_reissued",
                        code_row["participant_id"],
                        {"reason": "expired_code_submitted"},
                    )
                    connection.execute("COMMIT")
                    return {
                        "session_id": code_row["session_id"],
                        "participant_id": code_row["participant_id"],
                        "binding_code_reissued": True,
                        "binding_code": replacement,
                        "binding_expires_at": expires_at,
                    }

                conflict = connection.execute(
                    """
                    SELECT pt.id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND pt.id <> ?
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                      AND pt.participation_status
                          NOT IN ('retired', 'archived')
                    LIMIT 1
                    """,
                    (private_origin, code_row["participant_id"]),
                ).fetchone()
                if conflict:
                    raise ValueError(
                        "当前私聊还有另一张未完成的角色卡；"
                        "请先完成或取消后再绑定"
                    )

                connection.execute(
                    """
                    UPDATE participants SET
                        private_user_id = ?, private_origin = ?,
                        card_status = CASE
                            WHEN card_status = 'uncreated' THEN 'draft'
                            ELSE card_status
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["participant_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes SET
                        status = 'used', private_user_id = ?,
                        private_origin = ?, used_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'card_code'
                      AND status = 'active'
                    """,
                    (now, code_row["participant_id"]),
                )
                self._insert_audit(
                    connection,
                    code_row["session_id"],
                    private_user_id,
                    "card.bind_private",
                    code_row["participant_id"],
                    {"private_origin_recorded": bool(private_origin)},
                )
                row = connection.execute(
                    """
                    SELECT pt.*, d.current_step AS draft_step,
                           d.status AS draft_status,
                           d.expires_at AS draft_expires_at
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    WHERE pt.id = ? AND d.status = 'active'
                    ORDER BY d.generation DESC
                    LIMIT 1
                    """,
                    (code_row["participant_id"],),
                ).fetchone()
                config = connection.execute(
                    """
                    SELECT world_snapshot_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (code_row["session_id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result["template"] = card_template(
                    json_load(config["world_snapshot_json"], {})
                )
                result["world"] = json_load(
                    config["world_snapshot_json"], {}
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._card_draft_for_private,
            private_origin,
        )

    def _card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json, d.current_step,
                           d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json,
                           s.state AS session_state
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND d.status IN ('active', 'suspended')
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return None
                world = json_load(row["world_snapshot_json"], {})
                template = card_template(world)
                fields = json_load(row["fields_json"], {})
                fields = fields if isinstance(fields, dict) else {}
                if (
                    str(row["draft_status"] or "") == "suspended"
                    or str(row["session_state"] or "") == "closed"
                ):
                    result = self._participant(row)
                    result["fields"] = fields
                    result["current_step"] = int(row["current_step"] or 0)
                    result["template"] = template
                    result["world"] = world
                    result["session_state"] = str(
                        row["session_state"] or ""
                    )
                    result["suspended"] = True
                    result["content_update_notice"] = (
                        "【建卡已暂停】\n"
                        "当前副本已由管理员关闭，系统已保留你的建卡资料，"
                        "但不会继续写入。\n\n"
                        "副本重新开放后发送：\n/团 当前步骤"
                    )
                    connection.execute("COMMIT")
                    return result
                dependency_check = revalidate_dependent_selections(
                    template,
                    fields,
                )
                dependency_issues = [
                    *dependency_check.get("cleared", []),
                    *dependency_check.get("needs_revision", []),
                ]
                current_step = next_player_fillable_step(
                    template,
                    fields,
                    0,
                    allow_stages=(
                        (CARD_STAGE_A,)
                        if staged_creation(template)
                        else None
                    ),
                )
                if dependency_issues:
                    auto_fill_for_phase(template, fields, "resume_repair")
                    current_step = next_player_fillable_step(
                        template,
                        fields,
                        0,
                        allow_stages=(
                            (CARD_STAGE_A,)
                            if staged_creation(template)
                            else None
                        ),
                    )
                changed = bool(
                    dependency_issues
                    or dependency_check.get("canonicalized")
                    or dependency_check.get("retained")
                )
                if changed:
                    now = utc_now()
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(fields),
                            current_step,
                            now,
                            row["draft_id"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        row["private_user_id"],
                        "card.dependencies_revalidated",
                        row["id"],
                        {
                            "cleared": dependency_check.get("cleared", []),
                            "retained": dependency_check.get("retained", []),
                            "needs_revision": dependency_check.get(
                                "needs_revision", []
                            ),
                            "target_step": current_step,
                            "trigger": "draft_resume",
                        },
                    )
                result = self._participant(row)
                result["fields"] = fields
                result["current_step"] = current_step
                result["template"] = template
                result["world"] = world
                result["session_state"] = str(row["session_state"] or "")
                result["suspended"] = (
                    str(row["draft_status"] or "") == "suspended"
                    or str(row["session_state"] or "") == "closed"
                )
                if dependency_issues:
                    result["needs_revision"] = True
                    result["dependency_issues"] = dependency_issues
                    result["content_update_notice"] = (
                        "世界内容已更新，先前选择已不再可用；"
                        "系统已保留其他建卡资料，请重新选择当前项目。"
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fill_card_draft(
        self,
        private_origin: str,
        value: str,
        source_event_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._fill_card_draft,
            private_origin,
            value,
            source_event_id,
        )

    def _allowed_option_values(
        self, definition: Mapping[str, Any]
    ) -> set[str]:
        result: set[str] = set()
        for option in definition.get("options") or []:
            if isinstance(option, Mapping):
                value = option.get("value")
            else:
                value = option
            text = str(value or "")
            if text:
                result.add(text)
        return result

    def _sync_card_stage_column(
        self,
        connection: Any,
        participant_id: str,
        stage: str,
    ) -> None:
        """D1：向后可注入的阶段持久化。

        当数据库尚未合入 ``participants.card_stage`` 列时静默跳过；
        合入后每次确认建卡都同步最新阶段，保证派生与持久化一致。
        """

        try:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(participants)"
                ).fetchall()
            }
        except Exception:
            return
        if "card_stage" not in columns:
            return
        connection.execute(
            """
            UPDATE participants SET card_stage = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(stage), utc_now(), participant_id),
        )
