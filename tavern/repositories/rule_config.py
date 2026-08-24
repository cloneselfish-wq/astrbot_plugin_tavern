from __future__ import annotations

from .rules_support import *


class RuleConfigRepositoryMixin:
    async def get_instance_config(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_instance_config, session_id)

    def _get_instance_config(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本配置不存在")
            world_snapshot = json_load(
                row["world_snapshot_json"],
                {},
            )
            return {
                "session_id": row["session_id"],
                "world_revision": row["world_revision"],
                "world_snapshot": world_snapshot,
                "ui_profile": json_load(row["ui_profile_json"], {}),
                "character_card_template": card_template(world_snapshot),
                "time_rules": normalize_time_rules(
                    json_load(row["time_rules_json"], {})
                ),
                "phase_meta": json_load(row["phase_meta_json"], {}),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._get_session_archive, session_id)

    def _get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_archives WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "session_id": row["session_id"],
                "termination_type": row["termination_type"],
                "reason": row["reason"],
                "final_snapshot_id": row["final_snapshot_id"],
                "ended_by": row["ended_by"],
                "ended_at": row["ended_at"],
                "readonly": bool(row["readonly"]),
            }

    async def get_session_archive_view(
        self,
        session_id: str,
        *,
        viewer_role: str = "player",
        include_technical_refs: bool = False,
        ending_label: str = "",
        reason_extra: str = "",
    ) -> dict[str, Any] | None:
        """D1-UX-013：终局视图（正常/失败/强制终止 + 永久归档）。

        普通视图不包含 final_snapshot_id / ended_by 等内部字段。
        """
        return await self._run(
            self._get_session_archive_view,
            session_id,
            viewer_role,
            include_technical_refs,
            ending_label,
            reason_extra,
        )

    def _get_session_archive_view(
        self,
        session_id: str,
        viewer_role: str,
        include_technical_refs: bool,
        ending_label: str,
        reason_extra: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_archives WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            archive = {
                "session_id": row["session_id"],
                "termination_type": row["termination_type"],
                "reason": row["reason"],
                "final_snapshot_id": row["final_snapshot_id"],
                "ended_by": row["ended_by"],
                "ended_at": row["ended_at"],
                "readonly": bool(row["readonly"]),
            }
        from ..projections.delivery import project_terminal_view

        return project_terminal_view(
            archive,
            ending_label=ending_label,
            reason_extra=reason_extra,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )

    async def get_session_rule_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_session_rule_state, session_id)

    def _get_session_rule_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_rule_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                self._initialize_current_rows(connection)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本规则状态不存在")
            return {
                "session_id": row["session_id"],
                "progress": normalize_progress(
                    json_load(row["progress_json"], {})
                ),
                "content_boundaries": json_load(
                    row["content_boundaries_json"],
                    {},
                ),
                "npc_policy": json_load(row["npc_policy_json"], {}),
                "context_budget": json_load(
                    row["context_budget_json"],
                    {},
                ),
                "dice_rules": json_load(row["dice_rules_json"], {}),
                "recovery": json_load(row["recovery_json"], {}),
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def save_session_rule_state(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_rule_state,
            session_id,
            dict(payload),
            actor_id,
        )

    def _save_session_rule_state(
        self,
        session_id: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    self._initialize_current_rows(connection)
                    row = connection.execute(
                        "SELECT * FROM session_rule_states WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本规则状态不存在")
                expected = payload.get("revision")
                if expected not in {None, ""} and int(expected) != int(
                    row["revision"]
                ):
                    raise DatabaseConflictError("副本规则状态已被其他操作更新")
                progress = (
                    normalize_progress(payload["progress"])
                    if "progress" in payload
                    else normalize_progress(json_load(row["progress_json"], {}))
                )
                boundaries = (
                    dict(payload["content_boundaries"])
                    if isinstance(payload.get("content_boundaries"), Mapping)
                    else json_load(row["content_boundaries_json"], {})
                )
                npc_policy = (
                    dict(payload["npc_policy"])
                    if isinstance(payload.get("npc_policy"), Mapping)
                    else json_load(row["npc_policy_json"], {})
                )
                npc_policy["max_new_per_turn"] = bounded_int(
                    npc_policy.get("max_new_per_turn"),
                    3,
                    0,
                    3,
                )
                context_budget = (
                    dict(payload["context_budget"])
                    if isinstance(payload.get("context_budget"), Mapping)
                    else json_load(row["context_budget_json"], {})
                )
                dice_rules = (
                    dict(payload["dice_rules"])
                    if isinstance(payload.get("dice_rules"), Mapping)
                    else json_load(row["dice_rules_json"], {})
                )
                visibility = str(
                    dice_rules.get("visibility") or "public"
                ).lower()
                dice_rules["visibility"] = (
                    visibility
                    if visibility in {"public", "immersive", "hidden"}
                    else "public"
                )
                recovery = (
                    dict(payload["recovery"])
                    if isinstance(payload.get("recovery"), Mapping)
                    else json_load(row["recovery_json"], {})
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE session_rule_states SET
                        progress_json = ?, content_boundaries_json = ?,
                        npc_policy_json = ?, context_budget_json = ?,
                        dice_rules_json = ?, recovery_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json_dump(progress),
                        json_dump(boundaries),
                        json_dump(npc_policy),
                        json_dump(context_budget),
                        json_dump(dice_rules),
                        json_dump(recovery),
                        now,
                        session_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.rules.update",
                    session_id,
                    {
                        "progress": progress,
                        "npc_policy": npc_policy,
                        "dice_visibility": dice_rules["visibility"],
                    },
                )
                connection.execute("COMMIT")
                return self._get_session_rule_state(session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_session_characters(
        self,
        session_id: str,
        *,
        include_archived: bool = True,
        context_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_session_characters,
            session_id,
            include_archived,
            context_only,
        )

    def _list_session_characters(
        self,
        session_id: str,
        include_archived: bool,
        context_only: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            limit = 500
            if context_only:
                rules = connection.execute(
                    """
                    SELECT context_budget_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                budget = json_load(
                    rules["context_budget_json"] if rules else "",
                    {},
                )
                limit = bounded_int(budget.get("active_npcs"), 6, 0, 40)
            # session_characters 同时承载 D1 玩家命运外键身份；NPC 列表、
            # 模型上下文和 NPC 管理页不得把玩家镜像误当作 NPC。
            clauses = ["sc.session_id = ?", "sc.role_type <> 'player'"]
            params: list[Any] = [session_id]
            if not include_archived or context_only:
                clauses.append("sc.lifecycle_status = 'active'")
            if context_only:
                clauses.append("sc.review_status <> 'rejected'")
            rows = connection.execute(
                f"""
                SELECT sc.*, st.state_json,
                       st.revision AS state_revision
                FROM session_characters sc
                LEFT JOIN session_character_states st
                  ON st.character_id = sc.id
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE sc.source WHEN 'world_preset' THEN 0 ELSE 1 END,
                    sc.last_turn DESC, sc.updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._session_character(row) for row in rows]

    async def save_session_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_character,
            dict(payload),
            actor_id,
        )

    def _save_session_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        character_id = str(payload.get("id") or "").strip()
        name = clean_text(payload.get("name"), max_chars=80)
        if not session_id or not name:
            raise ValueError("副本 ID 与 NPC 名称不能为空")
        aliases = [
            clean_text(item, max_chars=80)
            for item in (
                payload.get("aliases")
                if isinstance(payload.get("aliases"), list)
                else []
            )[:12]
            if clean_text(item, max_chars=80)
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current = (
                    connection.execute(
                        "SELECT * FROM session_characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if character_id
                    else None
                )
                if character_id and not current:
                    raise DatabaseNotFoundError("副本 NPC 不存在")
                lowered_names = {
                    self._stable_key(name),
                    *(self._stable_key(item) for item in aliases),
                }
                candidates = connection.execute(
                    """
                    SELECT id, name, aliases_json FROM session_characters
                    WHERE session_id = ? AND id <> ?
                      AND lifecycle_status <> 'archived'
                    """,
                    (session_id, character_id),
                ).fetchall()
                for candidate in candidates:
                    candidate_names = {
                        self._stable_key(candidate["name"]),
                        *(
                            self._stable_key(item)
                            for item in json_load(
                                candidate["aliases_json"],
                                [],
                            )
                        ),
                    }
                    if lowered_names & candidate_names:
                        raise DatabaseConflictError(
                            f"NPC 名称或别名与「{candidate['name']}」重复"
                        )
                now = utc_now()
                profile = (
                    dict(payload.get("public_profile"))
                    if isinstance(payload.get("public_profile"), Mapping)
                    else {}
                )
                known_facts = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("known_facts")
                        if isinstance(payload.get("known_facts"), list)
                        else []
                    )[:30]
                    if clean_text(item, max_chars=400)
                ]
                misconceptions = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("misconceptions")
                        if isinstance(payload.get("misconceptions"), list)
                        else []
                    )[:20]
                    if clean_text(item, max_chars=400)
                ]
                state = (
                    dict(payload.get("state"))
                    if isinstance(payload.get("state"), Mapping)
                    else {}
                )
                role_type = clean_text(
                    payload.get("role_type") or "npc",
                    max_chars=40,
                )
                review_status = str(
                    payload.get("review_status") or "approved"
                ).lower()
                if review_status not in {
                    "pending",
                    "approved",
                    "rejected",
                    "duplicate",
                }:
                    review_status = "approved"
                lifecycle_status = str(
                    payload.get("lifecycle_status") or "active"
                ).lower()
                if lifecycle_status not in {
                    "active",
                    "departed",
                    "dead",
                    "archived",
                }:
                    lifecycle_status = "active"
                if current:
                    expected = payload.get("revision")
                    if expected not in {None, ""} and int(expected) != int(
                        current["revision"]
                    ):
                        raise DatabaseConflictError("NPC 已被其他操作更新")
                    connection.execute(
                        """
                        UPDATE session_characters SET
                            name = ?, aliases_json = ?, role_type = ?,
                            public_profile_json = ?, known_facts_json = ?,
                            misconceptions_json = ?, review_status = ?,
                            lifecycle_status = ?, persistent = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            character_id,
                        ),
                    )
                    action = "session_npc.update"
                else:
                    character_id = new_id("snpc")
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_turn,
                            last_turn, revision, created_at, updated_at
                        ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?,
                                 ?, ?, turn_no, turn_no, 1, ?, ?
                          FROM sessions WHERE id = ?
                        """,
                        (
                            character_id,
                            session_id,
                            f"admin:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            now,
                            session_id,
                        ),
                    )
                    action = "session_npc.create"
                connection.execute(
                    """
                    INSERT INTO session_character_states(
                        character_id, state_json, revision, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(character_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (character_id, json_dump(state), now),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    character_id,
                    {"name": name, "review_status": review_status},
                )
                row = connection.execute(
                    """
                    SELECT sc.*, st.state_json,
                           st.revision AS state_revision
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.id = ?
                    """,
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session_character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_story_ledger(
        self,
        session_id: str,
        *,
        include_host: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_story_ledger,
            session_id,
            include_host,
        )

    def _list_story_ledger(
        self,
        session_id: str,
        include_host: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM story_ledger
                WHERE session_id = ?
                  AND (? = 1 OR visibility = 'public')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    CASE kind WHEN 'main' THEN 0 WHEN 'objective' THEN 1
                              WHEN 'side' THEN 2 ELSE 3 END,
                    updated_at DESC
                """,
                (session_id, int(include_host)),
            ).fetchall()
            return [self._ledger_entry(row) for row in rows]

    async def list_scene_clocks(
        self,
        session_id: str,
        *,
        include_hidden: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_scene_clocks,
            session_id,
            include_hidden,
        )

    def _list_scene_clocks(
        self,
        session_id: str,
        include_hidden: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scene_clocks
                WHERE session_id = ?
                  AND (? = 1 OR visibility <> 'hidden')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    updated_at DESC
                """,
                (session_id, int(include_hidden)),
            ).fetchall()
            return [self._scene_clock(row) for row in rows]

    async def advance_scene_clock(
        self,
        session_id: str,
        clock_id: str,
        segments: int,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        """推进场景时钟（A14 通用接口）：校验、落库、审计。"""
        return await self._run(
            self._advance_scene_clock,
            session_id,
            clock_id,
            segments,
            actor_id,
            note,
        )

    def _advance_scene_clock(
        self,
        session_id: str,
        clock_id: str,
        segments: int,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        segments = max(-9999, min(9999, int(segments)))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM scene_clocks WHERE id = ? AND session_id = ?",
                    (clock_id, session_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("场景时钟不存在")
                current = int(row["current_value"]) + segments
                cap = int(row["segments"])
                if current < 0:
                    current = 0
                completed = current >= cap
                if completed:
                    current = cap
                connection.execute(
                    "UPDATE scene_clocks SET current_value = ?, updated_at = ? WHERE id = ?",
                    (current, now, clock_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "story.clock.advance",
                    clock_id,
                    {
                        "delta": segments,
                        "current": current,
                        "completed": completed,
                        "note": str(note)[:200],
                    },
                )
                connection.execute("COMMIT")
                return {
                    "id": clock_id,
                    "session_id": session_id,
                    "title": row["title"],
                    "current_value": current,
                    "segments": cap,
                    "completed": completed,
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._inspiration_status,
            session_id,
            user_id,
        )

    def _inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, crs.state_json
                FROM participants pt
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                actor = self._ai_check_actor_locked(
                    connection,
                    session_id,
                    user_id,
                )
                if actor is None:
                    raise DatabaseNotFoundError("当前行动角色不存在")
                state = json_load(actor["state_json"], {})
                participant_id = ""
                character_name = str(actor["display_name"] or "AI 队友")
            else:
                state = json_load(row["state_json"], {})
                participant_id = str(row["participant_id"])
                character_name = str(
                    row["character_name"] or row["display_name"]
                )
            state = dict(state) if isinstance(state, Mapping) else {}
            return {
                "participant_id": participant_id,
                "character_name": character_name,
                "balance": bounded_int(
                    state.get("inspiration"),
                    1,
                    0,
                    3,
                ),
                "maximum": bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                ),
            }
