from __future__ import annotations

from .workflow_support import *
from .choice_narratives import ChoiceNarrativesRepositoryMixin


class ChoicesRepositoryMixin(ChoiceNarrativesRepositoryMixin):
    @staticmethod
    def _opening_option_ref(scene_ref: object) -> str:
        return (
            "opening_"
            + hashlib.sha256(
                f"opening-option/1:{scene_ref}".encode("utf-8")
            ).hexdigest()[:16]
        )

    def _opening_public_view(
        self,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidates = json_load(row.get("candidates_json", "[]"), [])
        items = [
            {
                "option_ref": self._opening_option_ref(
                    item.get("scene_ref")
                ),
                "name": str(item.get("scene_label") or "开局候选"),
                "reason": (
                    "与当前队伍画像更契合"
                    if int(item.get("score") or 0) > 0
                    else "该世界提供的稳定开局"
                ),
            }
            for item in candidates
            if isinstance(item, Mapping)
        ]
        selected_ref = self._opening_option_ref(
            row.get("selected_scene_ref")
        )
        selected = next(
            (item for item in items if item["option_ref"] == selected_ref),
            {
                "option_ref": selected_ref,
                "name": "当前开局",
                "reason": str(row.get("selected_reason") or ""),
            },
        )
        return {
            "schema": "tavern-opening-decision/1.0.0-rc10",
            "selected": selected,
            "options": items,
            "selection_source": str(row.get("selection_source") or ""),
            "frozen": bool(row.get("frozen")),
            "revision": int(row.get("revision") or 0),
        }

    async def opening_decision(self, session_id: str) -> dict[str, Any] | None:
        return await self._run(self._opening_decision, session_id)

    def _opening_decision(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_opening_decisions WHERE session_id=?",
                (str(session_id),),
            ).fetchone()
        if row is None:
            return None
        return self._opening_public_view(dict(row))

    async def prepare_opening_decision(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._prepare_opening_decision, session_id)

    def _prepare_opening_decision(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM session_opening_decisions
                    WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    return self._opening_public_view(dict(existing))
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if str(session["state"]) not in {
                    SESSION_CLOSED,
                    SESSION_PREPARING,
                }:
                    raise InvalidTransitionError(
                        "故事开演后不能重新选择开局"
                    )
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                if config is None:
                    raise DatabaseNotFoundError("副本缺少冻结世界配置")
                world = json_load(config["world_snapshot_json"], {})
                participants = connection.execute(
                    """
                    SELECT pt.*, cv.profile_json AS card_profile_json
                    FROM participants pt
                    LEFT JOIN character_card_versions cv
                      ON cv.id=pt.character_version_id
                    WHERE pt.session_id=?
                      AND pt.card_status='approved'
                      AND pt.participation_status='active'
                    ORDER BY pt.created_at, pt.id
                    """,
                    (session_id,),
                ).fetchall()
                squad = [
                    {
                        **self._participant(item),
                        "card_profile": json_load(
                            item["card_profile_json"]
                            if "card_profile_json" in item.keys()
                            else "",
                            {},
                        ),
                    }
                    for item in participants
                ]
                seed = int(
                    hashlib.sha256(
                        str(session_id).encode("utf-8")
                    ).hexdigest()[:16],
                    16,
                )
                selected = select_opening_scenario(
                    world,
                    squad,
                    seed=seed,
                )
                candidates = recommend_opening_scenarios(world, squad)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO session_opening_decisions(
                        session_id, world_id, world_revision,
                        algorithm_version, seed, candidates_json,
                        selected_scene_ref, selected_reason,
                        selection_source, frozen, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'profile-score-v1', ?, ?, ?, ?,
                              ?, 0, 1, ?, ?)
                    """,
                    (
                        session_id,
                        session["world_id"],
                        int(config["world_revision"] or 1),
                        str(seed),
                        json_dump(candidates),
                        str(selected.get("opening_scene_ref") or ""),
                        "；".join(
                            str(item)
                            for item in selected.get(
                                "opening_selection_reasons"
                            )
                            or []
                        ),
                        "recommended" if candidates else "defaulted",
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM session_opening_decisions
                    WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._opening_public_view(dict(row))
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def override_opening_decision(
        self,
        session_id: str,
                scene_ref: str,
        principal_ref: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._override_opening_decision,
            session_id,
            scene_ref,
            principal_ref,
            expected_revision,
        )

    def _override_opening_decision(
        self,
        session_id: str,
        scene_ref: str,
        principal_ref: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT state FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if str(session["state"]) not in {SESSION_CLOSED, SESSION_PREPARING}:
                    raise InvalidTransitionError("开演后开局已经冻结，不能再修改")
                row = connection.execute(
                    "SELECT * FROM session_opening_decisions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("开局建议尚未生成")
                if int(row["frozen"] or 0):
                    raise InvalidTransitionError("开局已经冻结，不能再修改")
                if int(row["revision"] or 0) != int(expected_revision):
                    raise DatabaseConflictError("开局状态已更新，请刷新后重试")
                candidates = json_load(row["candidates_json"], [])
                candidate = next(
                    (
                        item
                        for item in candidates
                        if (
                            str(item.get("scene_ref") or "")
                            == str(scene_ref)
                            or self._opening_option_ref(
                                item.get("scene_ref")
                            )
                            == str(scene_ref)
                        )
                    ),
                    None,
                )
                if candidate is None:
                    raise ValueError("请选择当前世界提供的开局")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE session_opening_decisions SET
                        selected_scene_ref=?, selected_reason=?,
                        selection_source='admin_override',
                        overridden_by_principal_ref=?,
                        revision=revision+1, updated_at=?
                    WHERE session_id=? AND revision=? AND frozen=0
                    """,
                    (
                        str(candidate.get("scene_ref") or ""),
                        "管理员在开演前根据队伍讨论调整了开局。",
                        str(principal_ref),
                        now,
                        session_id,
                        expected_revision,
                    ),
                )
                updated = connection.execute(
                    "SELECT * FROM session_opening_decisions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._opening_public_view(dict(updated))

    async def opening_preflight(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._opening_preflight, session_id)

    def _opening_preflight(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            config = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not config:
                raise DatabaseNotFoundError("副本配置不存在")
            world = json_load(config["world_snapshot_json"], {})
            limits = player_limits(world)
            template = card_template(world)
            rows = connection.execute(
                """
                SELECT pt.*, cv.profile_json AS card_profile_json
                FROM participants pt
                LEFT JOIN character_card_versions cv
                  ON cv.id = pt.character_version_id
                WHERE pt.session_id = ?
                  AND pt.participation_status IN (
                      'reserved', 'active', 'standby', 'away'
                  )
                ORDER BY pt.created_at
                """,
                (session_id,),
            ).fetchall()
            blockers: list[dict[str, Any]] = []
            active: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            seen_codes: set[str] = set()
            for row in rows:
                item = self._participant(row)
                # D1：副本准备检查只要求 A 组完成（core_ready）；B/C 组
                # 待补充不产生 blocker。阶段优先读取已持久化列，未合入时派生。
                item["card_stage"] = resolve_card_stage(
                    template,
                    item.get("card_profile") or {},
                    row=row,
                )
                label = (
                    item["character_name"]
                    or item["display_name"]
                    or item["group_user_id"]
                )
                if item["participation_status"] == PARTICIPANT_AWAY:
                    continue
                if item["card_status"] != CARD_APPROVED:
                    status_meta = {
                        CARD_UNCREATED: (
                            "participant.card_uncreated",
                            "尚未完成建卡",
                            "请玩家在私聊中完成角色卡。",
                        ),
                        CARD_DRAFT: (
                            "participant.card_draft",
                            "角色卡仍在填写",
                            "请玩家继续私聊建卡并提交审核。",
                        ),
                        CARD_PENDING: (
                            "participant.card_pending_review",
                            "角色卡等待审核",
                            "请管理员审核通过或驳回后继续。",
                        ),
                        CARD_REJECTED: (
                            "participant.card_rejected",
                            "角色卡未通过审核",
                            "请玩家按驳回说明修改后重新提交。",
                        ),
                    }
                    code, message, resolution = status_meta.get(
                        item["card_status"],
                        (
                            "participant.card_invalid",
                            "角色卡状态无效",
                            "请管理员检查角色卡状态。",
                        ),
                    )
                    blockers.append(
                        {
                            "code": code,
                            "participant_id": item["id"],
                            "character_name": label,
                            "message": message,
                            "resolution": resolution,
                        }
                    )
                    continue
                if item["participation_status"] == PARTICIPANT_STANDBY:
                    continue
                if not item["ready"]:
                    blockers.append(
                        {
                            "code": "participant.not_ready",
                            "participant_id": item["id"],
                            "character_name": label,
                            "message": "尚未确认准备",
                            "resolution": "请玩家确认准备，或由管理员强制准备合格角色。",
                        }
                    )
                name_key = item["character_name"].casefold()
                code_key = item["character_code"].casefold()
                if name_key in seen_names:
                    blockers.append(
                        {
                            "code": "participant.duplicate_character_name",
                            "participant_id": item["id"],
                            "character_name": label,
                            "message": "角色名与其他出场角色重复",
                            "resolution": "请修改角色名后重新审核。",
                        }
                    )
                if code_key in seen_codes:
                    blockers.append(
                        {
                            "code": "participant.duplicate_character_code",
                            "participant_id": item["id"],
                            "character_name": label,
                            "message": "昵称或副本代号与其他出场角色重复",
                            "resolution": "请修改昵称或副本代号后重新审核。",
                        }
                    )
                seen_names.add(name_key)
                seen_codes.add(code_key)
                active.append(item)
            if len(active) < limits["minimum_start"]:
                blockers.append(
                    {
                        "code": "session.minimum_players",
                        "participant_id": "",
                        "character_name": "",
                        "message": (
                            f"有效出场人数不足：{len(active)}/"
                            f"{limits['minimum_start']}"
                        ),
                        "resolution": "请增加通过审核并占用出场席位的角色。",
                    }
                )
            if len(active) > limits["maximum"]:
                blockers.append(
                    {
                        "code": "session.maximum_players",
                        "participant_id": "",
                        "character_name": "",
                        "message": (
                            f"有效出场人数超过上限：{len(active)}/"
                            f"{limits['maximum']}"
                        ),
                        "resolution": "请将超额角色调整为候补或离场。",
                    }
                )
            eligible_force_ready = sum(
                1
                for item in active
                if not item["ready"]
                and item["participation_status"] == PARTICIPANT_ACTIVE
            )
            return {
                "ok": not blockers,
                "blocker_count": len(blockers),
                "blockers": blockers,
                "blocker_messages": [
                    (
                        f"{item['character_name']}：{item['message']}"
                        if item.get("character_name")
                        else str(item["message"])
                    )
                    for item in blockers
                ],
                "eligible_force_ready": eligible_force_ready,
                "participants": active,
                "limits": limits,
                "resume_mode": bool(session["turn_no"]),
                "state": session["state"],
            }

    async def active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_choice_set, session_id)

    def _active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cs.* FROM choice_sets cs
                WHERE cs.session_id = ? AND cs.status = 'active'
                ORDER BY cs.created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._choice_set(row)
            snapshot_row = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if snapshot_row:
                world_snapshot = json_load(
                    snapshot_row["world_snapshot_json"],
                    {},
                )
                try:
                    result["choices"] = normalize_choices(
                        json_load(row["choices_json"], []),
                        world_snapshot,
                    )
                except ValueError:
                    # Keep the structurally valid stored projection if the
                    # frozen snapshot is unavailable or was externally damaged.
                    pass
            participant = connection.execute(
                "SELECT * FROM participants WHERE id = ?",
                (row["participant_id"],),
            ).fetchone()
            result["participant"] = (
                self._participant(participant) if participant else None
            )
            actor = (
                connection.execute(
                    """
                    SELECT a.*, i.mode, i.status AS instance_status,
                           i.frozen_profile_json
                    FROM actors a
                    LEFT JOIN ai_companion_instances i ON i.actor_id=a.id
                    WHERE a.id=?
                    """,
                    (row["actor_id"],),
                ).fetchone()
                if row["actor_id"]
                else None
            )
            if actor is None and participant is not None:
                actor = connection.execute(
                    """
                    SELECT * FROM actors
                    WHERE session_id=? AND participant_id=?
                    """,
                    (session_id, participant["id"]),
                ).fetchone()
            if actor is not None and str(actor["actor_kind"]) == "ai_companion":
                result["actor"] = self._ai_turn_actor(dict(actor))
            elif participant is not None:
                result["actor"] = {
                    **self._participant(participant),
                    "actor_kind": "human",
                    "actor_ref": "",
                }
            else:
                result["actor"] = None
            return result

    async def replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: Sequence[Mapping[str, Any]],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_choices(choices)
        return await self._run(
            self._replace_active_choices,
            session_id,
            participant_id,
            normalized,
            actor_id,
        )

    def _replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("当前没有可重整的选项")
                if current["participant_id"] != participant_id:
                    raise PermissionError("只能重整自己当前回合的选项")
                if int(current["reroll_count"]) >= 1:
                    raise ValueError("本回合的免费重整次数已经用完")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, current["id"]),
                )
                new_id_value = new_id("choices")
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status,
                        reroll_count, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (
                        new_id_value,
                        session_id,
                        participant_id,
                        current["round_no"],
                        session["revision"],
                        json_dump(choices),
                        f"reroll:{current['id']}",
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "choice.reroll",
                    new_id_value,
                    {"previous_choice_set_id": current["id"]},
                )
                row = connection.execute(
                    "SELECT * FROM choice_sets WHERE id = ?",
                    (new_id_value,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._choice_set(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def emergency_replace_choices(
        self,
        session_id: str,
        choices: Sequence[Mapping[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._emergency_replace_choices,
            session_id,
            [dict(item) for item in choices],
            actor_id,
        )

    def _emergency_replace_choices(
        self,
        session_id: str,
        choices: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("当前没有可编辑的活动选项")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                now = utc_now()
                connection.execute(
                    "UPDATE choice_sets SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, current["id"]),
                )
                choice_id = new_id("choices")
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status,
                        reroll_count, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        choice_id,
                        session_id,
                        current["participant_id"],
                        current["round_no"],
                        session["revision"],
                        json_dump(choices),
                        current["reroll_count"],
                        f"admin-rescue:{choice_id}",
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "rescue.replace_choices",
                    choice_id,
                    {"previous_choice_set_id": current["id"]},
                )
                row = connection.execute(
                    "SELECT * FROM choice_sets WHERE id = ?", (choice_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._choice_set(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _insert_fallback_choices(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        participant: sqlite3.Row,
        now: str,
        idempotency_key: str,
    ) -> str:
        """为行动玩家落库一组 A—D 兜底选项并挂行动计时器。

        供 `_resume_after_vote`（表决未通过 / 暂缓通过）与
        `restore_actor_choices`（表决推进失败恢复）复用。
        """
        state = json_load(session["world_state_json"], {})
        choices = fallback_choices(state)
        choice_id = new_id("choices")
        turn = turn_state_from_world(state)
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
                participant["id"],
                turn["round_no"],
                session["revision"],
                json_dump(choices),
                idempotency_key,
                now,
                now,
            ),
        )
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
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": str(participant["group_user_id"]),
            },
        )
        return choice_id

    async def restore_actor_choices(
        self,
        session_id: str,
        user_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        """表决/推进失败后的通用兜底。

        仅在没有 active 选项集时，为指定玩家幂等创建一组 A—D 兜底选项；
        已有 active 选项或玩家不可行动时不做任何修改。
        """
        return await self._run(
            self._restore_actor_choices,
            session_id,
            str(user_id or "").strip(),
            clean_text(reason, max_chars=200),
        )
