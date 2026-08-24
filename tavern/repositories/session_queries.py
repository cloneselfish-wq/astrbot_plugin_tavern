from __future__ import annotations

from .sessions_support import *


class SessionQueriesRepositoryMixin:
    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group,
            platform_id,
            group_id,
        )

    def _get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY
                    s.selected DESC,
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
            return self._session(row) if row else None

    async def get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group_ref,
            platform_id,
            group_id,
            instance_ref,
        )

    def _get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        reference = str(instance_ref or "").strip()
        if not reference:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                  AND (s.id = ? OR s.instance_slug = ?)
                LIMIT 1
                """,
                (platform_id, group_id, reference, reference.lower()),
            ).fetchone()
            return self._session(row) if row else None

    async def list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_group_sessions,
            platform_id,
            group_id,
        )

    def _list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    w.description AS world_description,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY s.selected DESC, s.updated_at DESC
                """,
                (platform_id, group_id),
            ).fetchall()
            return [self._session(row) for row in rows]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_session, session_id)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                       COALESCE(gr.remark, '') AS group_remark,
                       COALESCE(gr.revision, 1) AS group_revision,
                       COALESCE(ss.relative_path, '')
                           AS storage_relative_path,
                       COALESCE(ss.sync_status, 'pending')
                           AS storage_sync_status,
                       COALESCE(ss.last_error, '') AS storage_last_error,
                       COALESCE(ss.playthrough_no, 1) AS playthrough_no
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("会话不存在")
            return self._session(row)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._run(self._list_sessions)

    async def list_visible_sessions_page(
        self,
        *,
        viewer_id: str,
        viewer_participant_ref: str = "",
        is_admin: bool = False,
        query: str = "",
        state: str = "",
        offset: int = 0,
        page_size: int = 16,
    ) -> dict[str, Any]:
        """Return one SQL-paged session slice after principal cropping.

        Non-admin callers see only sessions where the viewer is a current
        participant/player, active human DM, or explicitly granted host.  The
        count is calculated from that already-cropped relation, so neither the
        page nor ``total`` leaks the existence of other sessions.
        """

        return await self._run(
            self._list_visible_sessions_page,
            str(viewer_id),
            str(viewer_participant_ref),
            bool(is_admin),
            str(query),
            str(state),
            int(offset),
            int(page_size),
        )

    def _list_visible_sessions_page(
        self,
        viewer_id: str,
        viewer_participant_ref: str,
        is_admin: bool,
        query: str,
        state: str,
        offset: int,
        page_size: int,
    ) -> dict[str, Any]:
        viewer_id = clean_text(viewer_id, max_chars=300)
        viewer_participant_ref = clean_text(
            viewer_participant_ref,
            max_chars=300,
        )
        if not is_admin and not viewer_id and not viewer_participant_ref:
            raise PermissionError("缺少可验证的副本成员身份")
        normalized_query = clean_text(query, max_chars=200).casefold()
        normalized_state = clean_text(state, max_chars=40).lower()
        allowed_states = {
            SESSION_CLOSED,
            SESSION_PREPARING,
            SESSION_RUNNING,
            SESSION_PAUSED,
            SESSION_FINISHED,
            SESSION_MAINTENANCE,
        }
        if normalized_state and normalized_state not in allowed_states:
            raise ValueError("副本状态筛选无效")
        normalized_offset = max(0, int(offset or 0))
        normalized_page_size = max(1, min(100, int(page_size or 16)))

        clauses: list[str] = []
        parameters: list[Any] = []
        if not is_admin:
            clauses.append(
                """
                (
                    EXISTS (
                        SELECT 1 FROM participants visible_pt
                        WHERE visible_pt.session_id = s.id
                          AND (
                              visible_pt.id = ?
                              OR visible_pt.group_user_id = ?
                              OR visible_pt.private_user_id = ?
                          )
                          AND visible_pt.participation_status NOT IN (
                              'retired', 'archived'
                          )
                    )
                    OR EXISTS (
                        SELECT 1 FROM players visible_player
                        WHERE visible_player.session_id = s.id
                          AND visible_player.user_id = ?
                          AND visible_player.enabled = 1
                    )
                    OR EXISTS (
                        SELECT 1 FROM dm_control_states visible_dm
                        WHERE visible_dm.session_id = s.id
                          AND visible_dm.mode = 'dm'
                          AND visible_dm.active_dm_user_id = ?
                    )
                    OR EXISTS (
                        SELECT 1 FROM permission_grants visible_grant
                        WHERE visible_grant.session_id = s.id
                          AND visible_grant.user_id = ?
                          AND visible_grant.role IN ('host', 'moderator')
                    )
                )
                """
            )
            parameters.extend(
                [
                    viewer_participant_ref,
                    viewer_id,
                    viewer_id,
                    viewer_id,
                    viewer_id,
                    viewer_id,
                ]
            )
        if normalized_query:
            clauses.append(
                """
                (
                    instr(lower(s.instance_name), ?) > 0
                    OR instr(lower(w.name), ?) > 0
                    OR instr(lower(COALESCE(gr.remark, '')), ?) > 0
                )
                """
            )
            parameters.extend([normalized_query] * 3)
        if normalized_state:
            clauses.append("s.state = ?")
            parameters.append(normalized_state)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        base = f"""
            FROM sessions s
            JOIN worlds w ON w.id = s.world_id
            LEFT JOIN group_registry gr
              ON gr.platform_id = s.platform_id
             AND gr.group_id = s.group_id
            {where}
        """
        order = """
            ORDER BY
                CASE s.state
                    WHEN 'running' THEN 0
                    WHEN 'preparing' THEN 1
                    WHEN 'paused' THEN 2
                    WHEN 'maintenance' THEN 3
                    WHEN 'finished' THEN 5
                    ELSE 4
                END,
                s.updated_at DESC,
                s.id DESC
        """
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {base}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            state_counts = {
                str(row["state"] or ""): int(row["count"] or 0)
                for row in connection.execute(
                    f"""
                    SELECT s.state, COUNT(*) AS count
                    {base}
                    GROUP BY s.state
                    """,
                    tuple(parameters),
                ).fetchall()
            }
            ids = [
                str(row["id"])
                for row in connection.execute(
                    f"""
                    SELECT s.id {base} {order}
                    LIMIT ? OFFSET ?
                    """,
                    (
                        *parameters,
                        normalized_page_size,
                        normalized_offset,
                    ),
                ).fetchall()
            ]
        loaded = self._list_sessions(ids)
        by_id = {str(item.get("id") or ""): item for item in loaded}
        items = [by_id[item] for item in ids if item in by_id]
        actor_names = self._list_turn_actor_names(ids)
        for item in items:
            turn = item.get("turn_state")
            turn = dict(turn) if isinstance(turn, Mapping) else {}
            current_ref = str(turn.get("current_user_id") or "")
            item["current_name"] = str(
                actor_names.get(str(item.get("id") or ""), {}).get(
                    current_ref,
                    "",
                )
            )
        return {
            "items": items,
            "total": total,
            "offset": normalized_offset,
            "page_size": normalized_page_size,
            "has_more": normalized_offset + len(items) < total,
            "state_counts": state_counts,
        }

    def _list_sessions(
        self,
        session_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        ids = (
            [str(item) for item in session_ids if str(item)]
            if session_ids is not None
            else None
        )
        if ids == []:
            return []
        where_clause = ""
        parameters: tuple[Any, ...] = ()
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            where_clause = f"WHERE s.id IN ({placeholders})"
            parameters = tuple(ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    COALESCE(gr.remark, '') AS group_remark,
                    COALESCE(gr.revision, 1) AS group_revision,
                    COALESCE(ss.relative_path, '')
                        AS storage_relative_path,
                    COALESCE(ss.sync_status, 'pending')
                        AS storage_sync_status,
                    COALESCE(ss.last_error, '') AS storage_last_error,
                    COALESCE(ss.playthrough_no, 1) AS playthrough_no,
                    srs.progress_json, srs.recovery_json,
                    sa.termination_type, sa.reason AS archive_reason,
                    sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                    COALESCE(sa.readonly, par.readonly, 0) AS readonly,
                    par.archive_schema AS protocol_archive_schema,
                    par.source_database_schema AS protocol_source_database_schema,
                    par.source_world_schema AS protocol_source_world_schema,
                    par.source_protocol AS protocol_source_protocol,
                    par.result_json AS protocol_archive_result_json,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count,
                    (
                        SELECT COUNT(*) FROM participants ready_pt
                        WHERE ready_pt.session_id = s.id
                          AND ready_pt.ready = 1
                          AND ready_pt.participation_status = 'active'
                    ) AS ready_count,
                    (
                        SELECT COUNT(*) FROM memories m
                        WHERE m.session_id = s.id
                    ) AS memory_count,
                    (
                        SELECT COUNT(*) FROM snapshots sn
                        WHERE sn.session_id = s.id
                    ) AS snapshot_count,
                    (
                        SELECT COUNT(*) FROM session_characters sc
                        WHERE sc.session_id = s.id
                          AND sc.lifecycle_status = 'active'
                    ) AS npc_count,
                    (
                        SELECT COUNT(*) FROM timer_instances ti
                        WHERE ti.session_id = s.id
                          AND ti.status IN ('active', 'paused')
                    ) AS active_timer_count,
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM group_votes gv
                            WHERE gv.session_id = s.id AND gv.status = 'open'
                        ) THEN 'vote'
                        WHEN EXISTS (
                            SELECT 1 FROM choice_sets cs
                            WHERE cs.session_id = s.id AND cs.status = 'active'
                        ) THEN 'choice'
                        WHEN s.state = 'preparing' THEN 'preparation'
                        WHEN s.state = 'paused' THEN 'admin'
                        ELSE ''
                    END AS waiting_for,
                    (
                        SELECT deadline_at FROM timer_instances due
                        WHERE due.session_id = s.id
                          AND due.status = 'active'
                        ORDER BY CASE due.timer_type
                            WHEN 'vote' THEN 0
                            WHEN 'turn' THEN 1
                            ELSE 2
                        END, due.created_at DESC
                        LIMIT 1
                    ) AS active_deadline_at
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                LEFT JOIN session_rule_states srs ON srs.session_id = s.id
                LEFT JOIN session_archives sa ON sa.session_id = s.id
                LEFT JOIN protocol_archive_receipts par
                  ON par.target_kind = 'session'
                 AND par.target_id = s.id
                {where_clause}
                ORDER BY
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                """,
                parameters,
            ).fetchall()
            return [self._session(row) for row in rows]

    async def search_sessions(
        self,
        query: str = "",
        scope: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self._run(
            self._search_sessions,
            query,
            scope,
            page,
            page_size,
        )

    def _search_sessions(
        self,
        query: str,
        scope: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "all").strip().lower()
        if normalized_scope not in {"all", "group", "story"}:
            raise ValueError("检索范围必须为 all、group 或 story")
        normalized_query = clean_text(query, max_chars=200).casefold()
        normalized_page = max(1, int(page or 1))
        normalized_page_size = min(100, max(5, int(page_size or 20)))
        group_match = """
            (
                instr(lower(COALESCE(gr.remark, '')), ?) > 0
                OR instr(lower(s.group_id), ?) > 0
                OR instr(lower(s.platform_id), ?) > 0
            )
        """
        story_match = """
            (
                instr(lower(s.instance_name), ?) > 0
                OR instr(lower(s.instance_slug), ?) > 0
                OR instr(lower(s.id), ?) > 0
                OR instr(lower(w.name), ?) > 0
                OR instr(lower(w.slug), ?) > 0
                OR EXISTS (
                    SELECT 1 FROM events e
                    WHERE e.session_id = s.id
                      AND instr(lower(e.content), ?) > 0
                )
            )
        """
        where = ""
        parameters: list[Any] = []
        if normalized_query:
            if normalized_scope == "group":
                where = f"WHERE {group_match}"
                parameters.extend([normalized_query] * 3)
            elif normalized_scope == "story":
                where = f"WHERE {story_match}"
                parameters.extend([normalized_query] * 6)
            else:
                where = f"WHERE ({group_match} OR {story_match})"
                parameters.extend([normalized_query] * 9)
        with self._connect() as connection:
            base = f"""
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                {where}
            """
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {base}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            pages = max(
                1,
                (total + normalized_page_size - 1)
                // normalized_page_size,
            )
            effective_page = min(normalized_page, pages)
            offset = (effective_page - 1) * normalized_page_size
            ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT s.id
                    {base}
                    ORDER BY
                        CASE s.state
                            WHEN 'running' THEN 0
                            WHEN 'preparing' THEN 1
                            WHEN 'paused' THEN 2
                            WHEN 'maintenance' THEN 3
                            WHEN 'finished' THEN 5
                            ELSE 4
                        END,
                        s.updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        *parameters,
                        normalized_page_size,
                        offset,
                    ),
                ).fetchall()
            ]
            group_rows = connection.execute(
                """
                SELECT s.platform_id, s.group_id,
                       COALESCE(gr.remark, '') AS remark,
                       COALESCE(gr.revision, 1) AS revision,
                       COUNT(*) AS story_count,
                       SUM(CASE WHEN s.state = 'running' THEN 1 ELSE 0 END)
                           AS running_count
                FROM sessions s
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                GROUP BY s.platform_id, s.group_id
                ORDER BY COALESCE(NULLIF(gr.remark, ''), s.group_id)
                """
            ).fetchall()
        items = self._list_sessions(ids)
        page_keys = {
            (item["platform_id"], item["group_id"]) for item in items
        }
        groups = [
            dict(row)
            for row in group_rows
            if (str(row["platform_id"]), str(row["group_id"])) in page_keys
        ]
        return {
            "items": items,
            "groups": groups,
            "query": normalized_query,
            "scope": normalized_scope,
            "page": effective_page,
            "page_size": normalized_page_size,
            "total": total,
            "pages": pages,
        }

    async def list_session_options(self) -> list[dict[str, Any]]:
        return await self._run(self._list_session_options)

    def _list_session_options(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                {
                    "id": str(row["id"]),
                    "platform_id": str(row["platform_id"]),
                    "group_id": str(row["group_id"]),
                    "group_remark": str(row["group_remark"] or ""),
                    "instance_name": str(row["instance_name"]),
                    "instance_slug": str(row["instance_slug"]),
                    "world_name": str(row["world_name"]),
                    "state": str(row["state"]),
                }
                for row in connection.execute(
                    """
                    SELECT s.id, s.platform_id, s.group_id,
                           s.instance_name, s.instance_slug, s.state,
                           w.name AS world_name,
                           COALESCE(gr.remark, '') AS group_remark
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN group_registry gr
                      ON gr.platform_id = s.platform_id
                     AND gr.group_id = s.group_id
                    ORDER BY
                        COALESCE(NULLIF(gr.remark, ''), s.group_id),
                        s.updated_at DESC
                    """
                ).fetchall()
            ]

    async def save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        result = await self._run(
            self._save_group_remark,
            platform_id,
            group_id,
            remark,
            actor_id,
            expected_revision,
        )
        await asyncio.to_thread(
            self.storage.sync_group,
            result["platform_id"],
            result["group_id"],
        )
        return result

    def _save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台 ID",
        )
        normalized_group = validate_platform_id(group_id, label="群 ID")
        normalized_remark = clean_text(remark, max_chars=120)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    LIMIT 1
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not exists:
                    raise DatabaseNotFoundError("群会话不存在")
                row = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not row:
                    registry_id = (
                        "group_"
                        + hashlib.sha256(
                            (
                                f"{normalized_platform}\0"
                                f"{normalized_group}"
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                    )
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO group_registry(
                            id, platform_id, group_id, remark, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            registry_id,
                            normalized_platform,
                            normalized_group,
                            normalized_remark,
                            now,
                            now,
                        ),
                    )
                else:
                    if (
                        int(expected_revision or 0) > 0
                        and int(row["revision"]) != int(expected_revision)
                    ):
                        raise DatabaseConflictError(
                            "群备注已被其他管理员更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE group_registry SET
                            remark = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (normalized_remark, utc_now(), row["id"]),
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "group.remark",
                    f"{normalized_platform}:{normalized_group}",
                    {
                        "remark": normalized_remark,
                        "revision": int(updated["revision"]),
                    },
                )
                connection.execute("COMMIT")
                return dict(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_storage_info(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.storage_info,
            session_id,
        )

    async def verify_storage(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.verify_instance,
            session_id,
        )

    def _write_session_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        turn_no: int,
        actor_id: str,
        content: str,
        meta: Mapping[str, Any],
    ) -> str:
        """副本事件权威写入器：终局/归档等系统事件只经本方法落库。"""

        event_id = new_id("event")
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', ?, '开团系统', ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                int(turn_no or 0),
                actor_id,
                str(content or ""),
                json_dump(dict(meta or {})),
                utc_now(),
            ),
        )
        return event_id
