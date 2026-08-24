from .common import *

class ActorStateMixin:

    # ── D1 Schema 20：角色命运（actor_fate@1.0） ───────────────────────

    async def set_actor_fate(
        self,
        *,
        session_id: str,
        character_id: str,
        state: str,
        state_label: str = "",
        can_act: bool = True,
        terminal: bool = False,
        reason: str = "",
        source: str = "",
        rescue_window_until: str = "",
        rescue_window_kind: str = "",
        transitioned_at: str | None = None,
    ) -> dict[str, Any]:
        """Reject the retired raw fate-state mutation surface."""
        raise PermissionError(
            "直接写入角色命运状态已停用。系统没有修改角色状态；"
            "请使用结构化命运后果或世界声明的救援操作。"
        )

    def _set_actor_fate(
        self,
        session_id: str,
        character_id: str,
        state: str,
        state_label: str,
        can_act: bool,
        terminal: bool,
        reason: str,
        source: str,
        rescue_window_until: str,
        rescue_window_kind: str,
        transitioned_at: str | None,
    ) -> dict[str, Any]:
        state = str(state or "").strip()
        if not state:
            raise ValueError("角色命运状态不能为空")
        now = transitioned_at or utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                character = connection.execute(
                    """
                    SELECT session_id FROM session_characters WHERE id = ?
                    """,
                    (str(character_id),),
                ).fetchone()
                if character is None:
                    raise DatabaseNotFoundError("角色不存在")
                if str(character["session_id"]) != str(session_id):
                    raise ValueError("角色不属于该副本")
                connection.execute(
                    """
                    INSERT INTO actor_fate_states(
                        character_id, session_id, state, state_label,
                        can_act, terminal, transitioned_at,
                        rescue_window_until, rescue_window_kind,
                        reason, source, revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(character_id) DO UPDATE SET
                        state = excluded.state,
                        state_label = excluded.state_label,
                        can_act = excluded.can_act,
                        terminal = excluded.terminal,
                        transitioned_at = excluded.transitioned_at,
                        rescue_window_until = excluded.rescue_window_until,
                        rescue_window_kind = excluded.rescue_window_kind,
                        reason = excluded.reason,
                        source = excluded.source,
                        revision = actor_fate_states.revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(character_id),
                        str(session_id),
                        state[:80],
                        str(state_label or "")[:120],
                        1 if can_act else 0,
                        1 if terminal else 0,
                        now,
                        str(rescue_window_until or "")[:64],
                        str(rescue_window_kind or "")[:64],
                        str(reason or "")[:500],
                        str(source or "")[:160],
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM actor_fate_states WHERE character_id = ?
                    """,
                    (str(character_id),),
                ).fetchone()
                # D1 Schema 20：命运状态携带救援窗口时，在同一事务内开启
                # rescue_windows 行（幂等）。终态不开启窗口。
                if terminal:
                    # 进入终态后未完成的救援窗口立即关闭（不可再救援）。
                    connection.execute(
                        """
                        UPDATE rescue_windows
                        SET status = 'cancelled', outcome = 'terminal',
                            completed_at = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE session_id = ? AND character_id = ?
                          AND status = 'open'
                        """,
                        (
                            now,
                            now,
                            str(session_id),
                            str(character_id),
                        ),
                    )
                elif rescue_window_kind and connection.execute(
                    """
                    SELECT 1 FROM rescue_windows
                    WHERE session_id = ? AND character_id = ?
                      AND kind = ? AND status = 'open'
                    """,
                    (
                        str(session_id),
                        str(character_id),
                        str(rescue_window_kind)[:64],
                    ),
                ).fetchone() is None:
                    connection.execute(
                        """
                        INSERT INTO rescue_windows(
                            id, session_id, character_id, kind, status,
                            opened_at, expires_on, allowed_rescue_commands_json,
                            success_transition_json, failure_transition_json,
                            command_labels_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'open', ?, ?, '[]', '[]', '[]',
                                  '{}', 1, ?, ?)
                        """,
                        (
                            new_id("rescue_window"),
                            str(session_id),
                            str(character_id),
                            str(rescue_window_kind)[:64],
                            now,
                            str(rescue_window_until or "")[:64],
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def get_actor_fate(
        self,
        session_id: str,
        character_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._get_actor_fate, session_id, character_id)

    def _get_actor_fate(
        self,
        session_id: str,
        character_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM actor_fate_states
                WHERE session_id = ? AND character_id = ?
                """,
                (str(session_id), str(character_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    async def record_fate_transition(
        self,
        *,
        session_id: str,
        character_id: str,
        from_state: str,
        to_state: str,
        reason: str = "",
        source: str = "",
        reversible: bool = False,
        rescue_window: bool = False,
        protection_consumed: bool = False,
        event_id: str = "",
    ) -> dict[str, Any]:
        """Reject standalone transition rows without an atomic fate write."""
        raise PermissionError(
            "单独追加命运转换记录已停用。系统没有写入审计记录；"
            "请使用原子命运结算服务。"
        )

    def _record_fate_transition(
        self,
        session_id: str,
        character_id: str,
        from_state: str,
        to_state: str,
        reason: str,
        source: str,
        reversible: bool,
        rescue_window: bool,
        protection_consumed: bool,
        event_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        item_id = new_id("fate")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO actor_fate_transitions(
                        id, session_id, character_id, from_state, to_state,
                        reason, source, reversible, rescue_window,
                        protection_consumed, event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        str(session_id),
                        str(character_id),
                        str(from_state or "")[:80],
                        str(to_state or "")[:80],
                        str(reason or "")[:500],
                        str(source or "")[:160],
                        1 if reversible else 0,
                        1 if rescue_window else 0,
                        1 if protection_consumed else 0,
                        str(event_id or "")[:160],
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM actor_fate_transitions WHERE id = ?",
                    (item_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    async def list_fate_transitions(
        self,
        session_id: str,
        character_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_fate_transitions,
            session_id,
            character_id,
            limit,
        )

    def _list_fate_transitions(
        self,
        session_id: str,
        character_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [str(session_id)]
        if character_id:
            clauses.append("character_id = ?")
            values.append(str(character_id))
        values.append(max(1, min(1000, int(limit or 100))))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM actor_fate_transitions
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]

    async def party_fate_aggregate(self, session_id: str) -> dict[str, int]:
        """队伍命运聚合（member/living/dead/incapacitated）。

        只统计已有 actor_fate_states 行的角色；成员资格（排除旁观者、
        NPC、召唤物、未开演席位等）由 fate_service 在创建命运行时决定。
        空队伍恒为全 0，不会误触发团灭终局（D1-RUN-012/18 §6）。
        """
        return await self._run(self._party_fate_aggregate, session_id)

    def _party_fate_aggregate(self, session_id: str) -> dict[str, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS member_count,
                    COALESCE(SUM(CASE WHEN afs.terminal = 0 THEN 1 ELSE 0 END), 0)
                        AS living_count,
                    COALESCE(SUM(CASE WHEN afs.terminal = 1 THEN 1 ELSE 0 END), 0)
                        AS dead_count,
                    COALESCE(SUM(
                        CASE WHEN afs.can_act = 0 AND afs.terminal = 0
                             THEN 1 ELSE 0 END
                    ), 0) AS incapacitated_count
                FROM actor_fate_states afs
                JOIN participants pt
                  ON pt.id = afs.character_id
                 AND pt.session_id = afs.session_id
                WHERE afs.session_id = ?
                  AND pt.card_status = 'approved'
                  AND pt.participation_status
                      IN ('active', 'standby', 'away')
                """,
                (str(session_id),),
            ).fetchone()
        if row is None:
            return {
                "member_count": 0,
                "living_count": 0,
                "dead_count": 0,
                "incapacitated_count": 0,
            }
        return {
            "member_count": int(row["member_count"] or 0),
            "living_count": int(row["living_count"] or 0),
            "dead_count": int(row["dead_count"] or 0),
            "incapacitated_count": int(row["incapacitated_count"] or 0),
        }

    # ── D1 Schema 20：角色能力与职业资源持久化（D1-DATA-005/006）───────

    async def list_character_capabilities(
        self,
        session_id: str,
        character_id: str = "",
        *,
        available_only: bool = True,
    ) -> list[dict[str, Any]]:
        """读取角色能力（建卡确认时写入的权威记录）。"""
        return await self._run(
            self._list_character_capabilities,
            session_id,
            character_id,
            bool(available_only),
        )

    def _list_character_capabilities(
        self,
        session_id: str,
        character_id: str,
        available_only: bool,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [str(session_id)]
        if character_id:
            clauses.append("character_id = ?")
            values.append(str(character_id))
        if available_only:
            clauses.append("available = 1")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM character_capabilities
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, capability_ref
                """,
                tuple(values),
            ).fetchall()
        return [
            {
                "capability_ref": str(row["capability_ref"]),
                "source_ref": str(row["source_ref"]),
                "state": json_load(row["state_json"], {}),
                "available": bool(row["available"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    async def get_character_capability(
        self,
        session_id: str,
        character_id: str,
        capability_ref: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_character_capability,
            session_id,
            character_id,
            capability_ref,
        )

    def _get_character_capability(
        self,
        session_id: str,
        character_id: str,
        capability_ref: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM character_capabilities
                WHERE session_id = ? AND character_id = ?
                  AND capability_ref = ?
                """,
                (
                    str(session_id),
                    str(character_id),
                    str(capability_ref),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    async def replace_character_capabilities(
        self,
        *,
        session_id: str,
        character_id: str,
        capabilities: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """事务性替换角色能力清单（建卡确认/重审时调用）。"""
        return await self._run(
            self._replace_character_capabilities,
            session_id,
            character_id,
            list(capabilities),
        )

    def _replace_character_capabilities(
        self,
        session_id: str,
        character_id: str,
        capabilities: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        session_id = str(session_id or "").strip()
        character_id = str(character_id or "").strip()
        if not session_id or not character_id:
            raise ValueError("能力记录必须包含副本与角色")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM character_capabilities
                    WHERE session_id = ? AND character_id = ?
                    """,
                    (session_id, character_id),
                )
                for item in capabilities:
                    capability_ref = str(
                        item.get("capability_ref") or item.get("ref") or ""
                    ).strip()
                    if not capability_ref:
                        continue
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO character_capabilities(
                            id, session_id, character_id, capability_ref,
                            source_ref, state_json, available,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("capability"),
                            session_id,
                            character_id,
                            capability_ref[:160],
                            str(item.get("source_ref") or "")[:160],
                            json_dump(
                                dict(item.get("state") or item.get("state_json") or {})
                            ),
                            0 if item.get("available") is False else 1,
                            now,
                            now,
                        ),
                    )
                rows = connection.execute(
                    """
                    SELECT * FROM character_capabilities
                    WHERE session_id = ? AND character_id = ?
                    ORDER BY created_at, capability_ref
                    """,
                    (session_id, character_id),
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [dict(row) for row in rows]

    async def list_character_resources(
        self,
        session_id: str,
        character_id: str = "",
    ) -> list[dict[str, Any]]:
        """读取角色职业资源（建卡确认写入 + 运行时调整）。"""
        return await self._run(
            self._list_character_resources,
            session_id,
            character_id,
        )

    def _list_character_resources(
        self,
        session_id: str,
        character_id: str,
    ) -> list[dict[str, Any]]:
        clauses = ["session_id = ?"]
        values: list[Any] = [str(session_id)]
        if character_id:
            clauses.append("character_id = ?")
            values.append(str(character_id))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM character_resources
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, resource_ref
                """,
                tuple(values),
            ).fetchall()
        return [
            {
                "resource_ref": str(row["resource_ref"]),
                "label": str(row["label"]),
                "current": int(row["current"] or 0),
                "maximum": int(row["maximum"] or 0),
                "state": json_load(row["state_json"], {}),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

    async def get_character_resource(
        self,
        session_id: str,
        character_id: str,
        resource_ref: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_character_resource,
            session_id,
            character_id,
            resource_ref,
        )

    def _get_character_resource(
        self,
        session_id: str,
        character_id: str,
        resource_ref: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM character_resources
                WHERE session_id = ? AND character_id = ?
                  AND resource_ref = ?
                """,
                (
                    str(session_id),
                    str(character_id),
                    str(resource_ref),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    async def replace_character_resources(
        self,
        *,
        session_id: str,
        character_id: str,
        resources: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        """事务性替换角色资源清单（建卡确认/重审时调用）。"""
        return await self._run(
            self._replace_character_resources,
            session_id,
            character_id,
            list(resources),
        )

    def _replace_character_resources(
        self,
        session_id: str,
        character_id: str,
        resources: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        session_id = str(session_id or "").strip()
        character_id = str(character_id or "").strip()
        if not session_id or not character_id:
            raise ValueError("资源记录必须包含副本与角色")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    DELETE FROM character_resources
                    WHERE session_id = ? AND character_id = ?
                    """,
                    (session_id, character_id),
                )
                for item in resources:
                    resource_ref = str(
                        item.get("resource_ref") or item.get("ref") or ""
                    ).strip()
                    if not resource_ref:
                        continue
                    current = max(0, int(item.get("current", 0) or 0))
                    maximum = max(0, int(item.get("maximum", 0) or 0))
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO character_resources(
                            id, session_id, character_id, resource_ref,
                            label, current, maximum, state_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_id("resource"),
                            session_id,
                            character_id,
                            resource_ref[:160],
                            str(item.get("label") or "")[:120],
                            current,
                            maximum if maximum else current,
                            json_dump(
                                dict(item.get("state") or item.get("state_json") or {})
                            ),
                            now,
                            now,
                        ),
                    )
                rows = connection.execute(
                    """
                    SELECT * FROM character_resources
                    WHERE session_id = ? AND character_id = ?
                    ORDER BY created_at, resource_ref
                    """,
                    (session_id, character_id),
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [dict(row) for row in rows]

    async def adjust_character_resource(
        self,
        *,
        session_id: str,
        character_id: str,
        resource_ref: str,
        delta: int,
        reason: str = "",
    ) -> dict[str, Any]:
        """运行时增减角色资源（花费/回复），当前值不低于 0。"""
        return await self._run(
            self._adjust_character_resource,
            session_id,
            character_id,
            resource_ref,
            int(delta or 0),
            reason,
        )

    def _adjust_character_resource(
        self,
        session_id: str,
        character_id: str,
        resource_ref: str,
        delta: int,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM character_resources
                    WHERE session_id = ? AND character_id = ?
                      AND resource_ref = ?
                    """,
                    (
                        str(session_id),
                        str(character_id),
                        str(resource_ref),
                    ),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError(
                        f"角色资源不存在：{resource_ref}"
                    )
                current = max(0, int(row["current"] or 0) + delta)
                connection.execute(
                    """
                    UPDATE character_resources
                    SET current = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (current, now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    str(session_id),
                    "system",
                    "character.resource.adjusted",
                    str(row["id"]),
                    {
                        "resource_ref": resource_ref,
                        "delta": delta,
                        "current": current,
                        "reason": str(reason or "")[:200],
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM character_resources WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)
