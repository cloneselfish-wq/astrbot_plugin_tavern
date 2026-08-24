from __future__ import annotations

from .sessions_support import *


class ParticipantsRepositoryMixin:
    async def list_turn_actor_names(
        self,
        session_ids: Sequence[str],
    ) -> dict[str, dict[str, str]]:
        """Return one batched actor-name directory for session list views."""

        return await self._run(self._list_turn_actor_names, session_ids)

    def _list_turn_actor_names(
        self,
        session_ids: Sequence[str],
    ) -> dict[str, dict[str, str]]:
        ids = list(
            dict.fromkeys(
                str(item).strip()
                for item in session_ids
                if str(item).strip()
            )
        )
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.session_id,
                    'human' AS actor_kind,
                    p.user_id AS actor_id,
                    COALESCE(
                        NULLIF(p.character_name, ''),
                        NULLIF(p.display_name, ''),
                        ''
                    ) AS actor_name
                FROM players p
                WHERE p.enabled = 1
                  AND p.session_id IN ({placeholders})
                UNION ALL
                SELECT
                    a.session_id,
                    'ai_companion' AS actor_kind,
                    a.id AS actor_id,
                    COALESCE(NULLIF(a.display_name, ''), '') AS actor_name
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id = a.id
                WHERE a.actor_kind = 'ai_companion'
                  AND a.status = 'active'
                  AND i.status <> 'retired'
                  AND a.session_id IN ({placeholders})
                """,
                (*ids, *ids),
            ).fetchall()
        result: dict[str, dict[str, str]] = {item: {} for item in ids}
        for row in rows:
            session_id = str(row["session_id"] or "")
            actor_id = str(row["actor_id"] or "")
            if not session_id or not actor_id:
                continue
            actor_ref = (
                _actor_principal_ref(actor_id)
                if str(row["actor_kind"] or "") == "ai_companion"
                else actor_id
            )
            result.setdefault(session_id, {})[actor_ref] = str(
                row["actor_name"] or ""
            )
        return result

    async def ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_player,
            session_id,
            user_id,
            display_name,
        )

    def _ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100)
        if not display_name:
            raise ValueError("加入失败：平台没有提供可公开显示的名称")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO players(
                    id, session_id, user_id, display_name, character_name,
                    profile_json, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                ON CONFLICT(session_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id("player"),
                    session_id,
                    user_id,
                    display_name,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            return self._player(row)

    async def list_players(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_players, session_id)

    def _list_players(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ?
                ORDER BY enabled DESC, updated_at DESC
                """,
                (session_id,),
            ).fetchall()
            return [self._player(row) for row in rows]

    @staticmethod
    def _turn_status_for(
        connection: sqlite3.Connection,
        session_id: str,
        stored_world_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stored_world_state is None:
            session = connection.execute(
                "SELECT world_state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            stored_world_state = json_load(session["world_state_json"], {})

        rows = connection.execute(
            """
            SELECT * FROM players
            WHERE session_id = ? AND enabled = 1
            """,
            (session_id,),
        ).fetchall()
        actors: dict[str, dict[str, Any]] = {
            str(row["user_id"]): {
                "player_id": str(row["id"]),
                "user_id": str(row["user_id"]),
                "actor_ref": "",
                "actor_kind": "human",
                "display_name": str(row["display_name"] or ""),
                "character_name": str(row["character_name"] or ""),
            }
            for row in rows
        }
        ai_rows = connection.execute(
            """
            SELECT a.id, a.display_name, a.status, i.mode
            FROM actors a
            JOIN ai_companion_instances i ON i.actor_id=a.id
            WHERE a.session_id=? AND a.actor_kind='ai_companion'
              AND a.status='active' AND i.status<>'retired'
            ORDER BY a.created_at, a.id
            """,
            (session_id,),
        ).fetchall()
        for row in ai_rows:
            actor_ref = _actor_principal_ref(row["id"])
            actors[actor_ref] = {
                "player_id": "",
                "user_id": actor_ref,
                "actor_ref": actor_ref,
                "actor_kind": "ai_companion",
                "display_name": str(row["display_name"] or ""),
                "character_name": str(row["display_name"] or ""),
                "mode": str(row["mode"] or ""),
            }
        state = turn_state_from_world(
            stored_world_state,
            allowed_user_ids=actors,
        )
        order = []
        for position, user_id in enumerate(state["order"], start=1):
            row = actors[user_id]
            order.append(
                {
                    "position": position,
                    "player_id": row["player_id"],
                    "user_id": user_id,
                    "actor_ref": row["actor_ref"],
                    "actor_kind": row["actor_kind"],
                    "display_name": row["display_name"],
                    "character_name": row["character_name"],
                    "name": row["character_name"] or row["display_name"],
                }
            )
        current = next(
            (
                item
                for item in order
                if item["user_id"] == state["current_user_id"]
            ),
            None,
        )
        return {
            "round_no": state["round_no"],
            "current_user_id": state["current_user_id"],
            "current_name": current["name"] if current else "",
            "order": order,
        }

    @staticmethod
    def _enabled_turn_actor_refs(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> set[str]:
        refs = {
            str(row["user_id"])
            for row in connection.execute(
                """
                SELECT user_id FROM players
                WHERE session_id = ? AND enabled = 1
                """,
                (session_id,),
            ).fetchall()
        }
        refs.update(
            _actor_principal_ref(row["id"])
            for row in connection.execute(
                """
                SELECT a.id
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status='active' AND i.status<>'retired'
                """,
                (session_id,),
            ).fetchall()
        )
        return refs

    async def get_turn_status(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_turn_status, session_id)

    def _get_turn_status(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._turn_status_for(connection, session_id)

    async def join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._join_turn_order,
            session_id,
            user_id,
            display_name,
            actor_id,
        )

    def _join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100)
        if not display_name:
            raise ValueError("加入回合队列失败：角色缺少可公开显示的名称")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name, character_name,
                        profile_json, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("player"),
                        session_id,
                        user_id,
                        display_name,
                        now,
                        now,
                    ),
                )
                player_row = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player_row["enabled"]:
                    raise InvalidTransitionError("你的玩家身份当前不可用")

                enabled_ids = self._enabled_turn_actor_refs(
                    connection,
                    session_id,
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state, joined = join_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), now, session_id),
                    )
                if joined:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.join",
                        user_id,
                        {"position": turn_state["order"].index(user_id) + 1},
                    )
                session_row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {
                    "joined": joined,
                    "player": self._player(player_row),
                    "session": self._session(session_row),
                    "turn": status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._leave_turn_order,
            session_id,
            user_id,
            actor_id,
        )

    def _leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = self._enabled_turn_actor_refs(
                    connection,
                    session_id,
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state, removed = leave_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), utc_now(), session_id),
                    )
                if removed:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.leave",
                        user_id,
                        {},
                    )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {"removed": removed, "turn": status}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._skip_turn,
            session_id,
            requester_id,
            actor_id,
            force,
        )

    def _skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        force: bool,
    ) -> dict[str, Any]:
        requester_id = validate_platform_id(requester_id, label="用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = self._enabled_turn_actor_refs(
                    connection,
                    session_id,
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                current_user_id = turn_state["current_user_id"]
                if not current_user_id:
                    raise InvalidTransitionError("回合队列为空")
                if not force and requester_id != current_user_id:
                    current = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    current_name = str(current.get("current_name") or "").strip()
                    if not current_name:
                        raise InvalidTransitionError(
                            "跳过回合失败：当前行动者缺少可公开显示的名称；"
                            "系统没有推进回合，请主持人先修复阵容名称"
                        )
                    raise InvalidTransitionError(f"当前轮到「{current_name}」")
                turn_state = advance_turn(turn_state, current_user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.force_skip" if force else "turn_order.skip",
                    current_user_id,
                    {"round_no": turn_state["round_no"]},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_turn_order(
        self,
        session_id: str,
        order: Sequence[str],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_turn_order,
            session_id,
            list(order),
            actor_id,
        )

    def _set_turn_order(
        self,
        session_id: str,
        order: list[str],
        actor_id: str,
    ) -> dict[str, Any]:
        if len(order) > 100:
            raise ValueError("回合队列最多 100 人")
        normalized_order = [
            validate_platform_id(item, label="用户 ID") for item in order
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = self._enabled_turn_actor_refs(
                    connection,
                    session_id,
                )
                unknown = [
                    item for item in normalized_order if item not in enabled_ids
                ]
                if unknown:
                    raise ValueError("回合顺序包含不存在或已停用的玩家")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state = replace_turn_order(
                    turn_state,
                    normalized_order,
                )
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.set",
                    session_id,
                    {"order": normalized_order},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def designate_turn(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._designate_turn,
            session_id,
            user_id,
            actor_id,
        )

    def _designate_turn(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                      AND participation_status = 'active'
                      AND card_status = 'approved'
                    """,
                    (session_id, user_id),
                ).fetchone()
                ai_actor = None
                if participant is None:
                    for candidate in connection.execute(
                        """
                        SELECT a.id FROM actors a
                        JOIN ai_companion_instances i ON i.actor_id=a.id
                        WHERE a.session_id=?
                          AND a.actor_kind='ai_companion'
                          AND a.status='active'
                          AND i.status<>'retired'
                        """,
                        (session_id,),
                    ).fetchall():
                        if _actor_principal_ref(candidate["id"]) == user_id:
                            ai_actor = candidate
                            break
                if participant is None and ai_actor is None:
                    raise ValueError("指定角色当前不在有效行动阵容中")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=self._enabled_turn_actor_refs(
                        connection,
                        session_id,
                    ),
                )
                if user_id not in turn_state["order"]:
                    raise ValueError("指定角色当前不在回合队列中")
                turn_state["current_user_id"] = user_id
                now = utc_now()
                new_revision = int(session["revision"]) + 1
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(embed_turn_state(stored_state, turn_state)),
                        now,
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'turn'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                choice_id = new_id("choices")
                choices = fallback_choices(stored_state)
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, actor_id, round_no,
                        session_revision, choices_json, status, reroll_count,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                    """,
                    (
                        choice_id,
                        session_id,
                        participant["id"] if participant is not None else None,
                        ai_actor["id"] if ai_actor is not None else None,
                        turn_state["round_no"],
                        new_revision,
                        json_dump(choices),
                        f"designate:{session_id}:{new_revision}",
                        now,
                        now,
                    ),
                )
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
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=(
                        participant["id"]
                        if participant is not None
                        else ""
                    ),
                    timer_type="turn",
                    timeout_seconds=rules["turn_timeout_seconds"],
                    reminder_seconds=rules["turn_reminder_seconds"],
                    action={
                        "choice_set_id": choice_id,
                        "user_id": user_id,
                        "actor_ref": user_id if ai_actor is not None else "",
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn.designate",
                    (
                        participant["id"]
                        if participant is not None
                        else user_id
                    ),
                    {
                        "user_id": (
                            user_id if participant is not None else ""
                        ),
                        "actor_ref": (
                            user_id if ai_actor is not None else ""
                        ),
                    },
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    embed_turn_state(stored_state, turn_state),
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _remove_turn_member(
        connection: sqlite3.Connection,
        session_id: str,
        user_id: str,
        *,
        updated_at: str,
    ) -> bool:
        session = connection.execute(
            "SELECT world_state_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise DatabaseNotFoundError("会话不存在")
        stored_state = json_load(session["world_state_json"], {})
        turn_state, removed = leave_turn(
            turn_state_from_world(stored_state),
            user_id,
        )
        enabled_ids = self._enabled_turn_actor_refs(
            connection,
            session_id,
        )
        turn_state = normalize_turn_state(
            turn_state,
            allowed_user_ids=enabled_ids,
        )
        updated_state = embed_turn_state(stored_state, turn_state)
        if json_dump(updated_state) != json_dump(stored_state):
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (json_dump(updated_state), updated_at, session_id),
            )
        return removed

    async def save_player(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_player, dict(payload), actor_id)
