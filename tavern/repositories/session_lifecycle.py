from __future__ import annotations

from .sessions_support import *


class SessionLifecycleRepositoryMixin:
    async def ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str = "",
        instance_name: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_session,
            platform_id,
            group_id,
            unified_origin,
            world_ref,
            actor_id,
            instance_slug,
            instance_name,
        )

    async def clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        *,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str = "",
        candidate_world_ref: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._clone_session,
            source_session_id,
            actor_id,
            instance_slug,
            instance_name,
            snapshot_ref,
            candidate_world_ref,
            expected_revision,
            idempotency_key,
        )

    def _clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str,
        candidate_world_ref: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        slug = validate_slug(instance_slug)
        name = clean_text(instance_name, max_chars=100)
        if not name:
            raise ValueError("新副本名称不能为空")
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "source_session_id": clean_text(source_session_id, max_chars=240),
            "instance_slug": slug,
            "instance_name": name,
            "snapshot_ref": clean_text(snapshot_ref, max_chars=240),
            "candidate_world_ref": clean_text(
                candidate_world_ref, max_chars=240
            ),
            "expected_revision": expected_revision,
        }
        input_hash = hashlib.sha256(
            json_dump(request_payload).encode("utf-8")
        ).hexdigest()
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
                                "相同幂等键已用于另一份副本克隆请求"
                            )
                        if str(receipt["status"] or "") == "completed":
                            replay = json_load(receipt["result_json"], {})
                            replay["replayed"] = True
                            connection.execute("COMMIT")
                            return replay
                        raise DatabaseConflictError(
                            "副本克隆仍在处理中，请稍后重试"
                        )
                source = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (source_session_id,),
                ).fetchone()
                if not source:
                    raise DatabaseNotFoundError("源副本不存在")
                if (
                    expected_revision is not None
                    and int(source["revision"] or 0) != int(expected_revision)
                ):
                    raise DatabaseConflictError("源副本状态已经变化")
                candidate_world = None
                candidate_payload: dict[str, Any] | None = None
                if candidate_world_ref:
                    candidate_world = connection.execute(
                        """
                        SELECT * FROM worlds
                        WHERE (id=? OR slug=?) AND archived=0
                        """,
                        (candidate_world_ref, candidate_world_ref),
                    ).fetchone()
                    if not candidate_world:
                        raise DatabaseNotFoundError("候选世界包不存在或已归档")
                    source_config = connection.execute(
                        "SELECT * FROM instance_configs WHERE session_id=?",
                        (source_session_id,),
                    ).fetchone()
                    if not source_config:
                        raise DatabaseNotFoundError("源副本缺少冻结世界配置")
                    from ..world_migration import compare_world_contracts

                    candidate_payload = self._world(candidate_world)
                    migration = compare_world_contracts(
                        json_load(source_config["world_snapshot_json"], {}),
                        candidate_payload,
                    )
                    if not migration["safe_for_clone"]:
                        codes = ", ".join(
                            str(item.get("code") or "unknown")
                            for item in migration["blockers"]
                        )
                        raise DatabaseConflictError(
                            f"候选世界不能安全克隆应用：{codes}"
                        )
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND instance_slug = ?
                    """,
                    (
                        source["platform_id"],
                        source["group_id"],
                        slug,
                    ),
                ).fetchone()
                if duplicate:
                    raise DatabaseConflictError("当前群已存在同标识副本")
                snapshot = None
                if snapshot_ref:
                    snapshot = connection.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE session_id = ? AND (id = ? OR name = ?)
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (
                            source_session_id,
                            snapshot_ref,
                            snapshot_ref,
                        ),
                    ).fetchone()
                    if not snapshot:
                        raise DatabaseNotFoundError("指定的源存档不存在")
                if not snapshot:
                    archive = connection.execute(
                        """
                        SELECT final_snapshot_id FROM session_archives
                        WHERE session_id = ?
                        """,
                        (source_session_id,),
                    ).fetchone()
                    if archive:
                        snapshot = connection.execute(
                            "SELECT * FROM snapshots WHERE id = ?",
                            (archive["final_snapshot_id"],),
                        ).fetchone()
                state_json = (
                    snapshot["world_state_json"]
                    if snapshot
                    else source["world_state_json"]
                )
                turn_no = (
                    int(snapshot["turn_no"])
                    if snapshot
                    else int(source["turn_no"])
                )
                now = utc_now()
                target_id = new_id("session")
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'closed', ?, 1, ?,
                              0, ?, ?)
                    """,
                    (
                        target_id,
                        source["platform_id"],
                        source["group_id"],
                        source["unified_origin"],
                        slug,
                        name,
                        candidate_world["id"] if candidate_world else source["world_id"],
                        turn_no,
                        state_json,
                        now,
                        now,
                    ),
                )
                config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if config:
                    phase = json_load(config["phase_meta_json"], {})
                    phase = dict(phase) if isinstance(phase, Mapping) else {}
                    phase["branched_from_session_id"] = source_session_id
                    phase["branched_from_snapshot_id"] = (
                        snapshot["id"] if snapshot else ""
                    )
                    if candidate_world and candidate_payload:
                        phase["world_upgrade_from_revision"] = int(
                            config["world_revision"]
                        )
                        phase["world_upgrade_to_revision"] = int(
                            candidate_world["revision"]
                        )
                    connection.execute(
                        """
                        INSERT INTO instance_configs(
                            session_id, world_revision, world_snapshot_json,
                            ui_profile_json, time_rules_json, phase_meta_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            (
                                candidate_world["revision"]
                                if candidate_world else config["world_revision"]
                            ),
                            (
                                json_dump(candidate_payload)
                                if candidate_payload else config["world_snapshot_json"]
                            ),
                            (
                                candidate_world["ui_profile_json"]
                                if candidate_world else config["ui_profile_json"]
                            ),
                            (
                                json_dump(world_time_rules(candidate_payload))
                                if candidate_payload else config["time_rules_json"]
                            ),
                            json_dump(phase),
                            now,
                            now,
                        ),
                    )
                opening = connection.execute(
                    """
                    SELECT * FROM session_opening_decisions
                    WHERE session_id=?
                    """,
                    (source_session_id,),
                ).fetchone()
                if opening:
                    connection.execute(
                        """
                        INSERT INTO session_opening_decisions(
                            session_id, world_id, world_revision,
                            algorithm_version, seed, candidates_json,
                            selected_scene_ref, selected_reason,
                            selection_source,
                            overridden_by_principal_ref,
                            frozen, frozen_at, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                                  'cloned', '', ?, ?, 1, ?, ?)
                        """,
                        (
                            target_id,
                            (
                                candidate_world["id"]
                                if candidate_world
                                else opening["world_id"]
                            ),
                            (
                                int(candidate_world["revision"])
                                if candidate_world
                                else int(opening["world_revision"])
                            ),
                            opening["algorithm_version"],
                            opening["seed"],
                            opening["candidates_json"],
                            opening["selected_scene_ref"],
                            opening["selected_reason"],
                            int(opening["frozen"] or 0),
                            opening["frozen_at"],
                            now,
                            now,
                        ),
                    )
                rules = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if rules:
                    recovery = {
                        "state": "idle",
                        "message": "",
                        "operation_id": "",
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO session_rule_states(
                            session_id, progress_json,
                            content_boundaries_json, npc_policy_json,
                            context_budget_json, dice_rules_json,
                            recovery_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            target_id,
                            rules["progress_json"],
                            rules["content_boundaries_json"],
                            rules["npc_policy_json"],
                            rules["context_budget_json"],
                            rules["dice_rules_json"],
                            json_dump(recovery),
                            now,
                            now,
                        ),
                    )
                character_ids: dict[str, str] = {}
                for item in connection.execute(
                    """
                    SELECT sc.*, st.state_json
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall():
                    target_character_id = new_id("snpc")
                    character_ids[item["id"]] = target_character_id
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                  '', ?, ?, 1, ?, ?)
                        """,
                        (
                            target_character_id,
                            target_id,
                            item["stable_key"],
                            item["name"],
                            item["aliases_json"],
                            item["role_type"],
                            item["public_profile_json"],
                            item["known_facts_json"],
                            item["misconceptions_json"],
                            item["source"],
                            item["review_status"],
                            item["lifecycle_status"],
                            item["persistent"],
                            item["first_turn"],
                            item["last_turn"],
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            target_character_id,
                            item["state_json"] or "{}",
                            now,
                        ),
                    )
                for table, prefix, columns in (
                    (
                        "story_ledger",
                        "ledger",
                        (
                            "stable_key", "kind", "title", "description",
                            "status", "visibility",
                        ),
                    ),
                    (
                        "scene_clocks",
                        "clock",
                        (
                            "stable_key", "title", "segments",
                            "current_value", "visibility", "trigger_text",
                            "status",
                        ),
                    ),
                ):
                    for item in connection.execute(
                        f"SELECT * FROM {table} WHERE session_id = ?",
                        (source_session_id,),
                    ).fetchall():
                        target_row_id = new_id(prefix)
                        if table == "story_ledger":
                            connection.execute(
                                """
                                INSERT INTO story_ledger(
                                    id, session_id, stable_key, kind, title,
                                    description, status, visibility,
                                    source_event_id, completed_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO scene_clocks(
                                    id, session_id, stable_key, title,
                                    segments, current_value, visibility,
                                    trigger_text, status, triggered_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                memory_ids: dict[str, str] = {}
                memory_rows = connection.execute(
                    """
                    SELECT m.*, mg.visibility, mg.locked, mg.pinned,
                           mg.invalidated, mg.supersedes_id,
                           mg.conflict_status, mg.note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall()
                for item in memory_rows:
                    target_memory_id = new_id("memory")
                    memory_ids[item["id"]] = target_memory_id
                    fingerprint = memory_fingerprint(
                        target_id,
                        item["scope"],
                        item["scope_id"],
                        item["kind"],
                        item["content"],
                    )
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            target_id,
                            item["scope"],
                            item["scope_id"],
                            item["kind"],
                            item["content"],
                            item["importance"],
                            item["salience"],
                            item["tags_json"],
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                for item in memory_rows:
                    target_memory_id = memory_ids[item["id"]]
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            item["visibility"] or "public",
                            int(item["locked"] or 0),
                            int(item["pinned"] or 0),
                            int(item["invalidated"] or 0),
                            memory_ids.get(item["supersedes_id"], ""),
                            item["conflict_status"] or "clear",
                            item["note"] or "",
                            actor_id,
                            now,
                        ),
                    )
                event_id = append_event(
                    connection,
                    session_id=target_id,
                    turn_no=turn_no,
                    role="system",
                    actor_id=actor_id,
                    actor_name="开团系统",
                    content=(
                        f"已从副本「{source['instance_name']}」克隆分支。"
                    ),
                    meta={
                        "kind": "session_branch",
                        "source_session_id": source_session_id,
                        "source_snapshot_id": (
                            snapshot["id"] if snapshot else ""
                        ),
                    },
                    created_at=now,
                )
                target_row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (target_id,),
                ).fetchone()
                self._insert_snapshot(
                    connection,
                    target_row,
                    "branch-origin",
                    "manual",
                    actor_id,
                    replace=False,
                )
                self._insert_audit(
                    connection,
                    target_id,
                    actor_id,
                    "session.clone",
                    source_session_id,
                    {
                        "source_snapshot_id": (
                            snapshot["id"] if snapshot else ""
                        ),
                        "instance_slug": slug,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (target_id,),
                ).fetchone()
                result = self._session(row)
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
                            source_session_id,
                            input_hash,
                            json_dump(result),
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(platform_id, label="平台 ID")
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                world = connection.execute(
                    """
                    SELECT * FROM worlds
                    WHERE (id = ? OR slug = ?) AND archived = 0
                    """,
                    (world_ref, world_ref),
                ).fetchone()
                if not world:
                    raise DatabaseNotFoundError(
                        "指定世界包不存在或已归档"
                    )
                normalized_instance_slug = validate_slug(
                    instance_slug or world["slug"]
                )
                normalized_instance_name = clean_text(
                    instance_name or world["name"],
                    max_chars=100,
                )
                if not normalized_instance_name:
                    raise ValueError("副本名称不能为空")

                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.platform_id = ? AND s.group_id = ?
                      AND s.instance_slug = ?
                    """,
                    (
                        platform_id,
                        group_id,
                        normalized_instance_slug,
                    ),
                ).fetchone()
                if row:
                    if row["world_id"] != world["id"]:
                        raise DatabaseConflictError(
                            "该群中的副本标识已被其他世界使用"
                        )
                    if row["state"] == SESSION_FINISHED:
                        base = normalized_instance_slug[:45].rstrip("-_")
                        stamp = datetime.now().astimezone().strftime(
                            "%Y%m%d%H%M%S"
                        )
                        candidate = f"{base}-run-{stamp}"
                        serial = 2
                        while connection.execute(
                            """
                            SELECT 1 FROM sessions
                            WHERE platform_id = ? AND group_id = ?
                              AND instance_slug = ?
                            """,
                            (platform_id, group_id, candidate),
                        ).fetchone():
                            suffix = f"-{serial:02d}"
                            candidate = (
                                f"{base[:64 - len(suffix) - 19]}"
                                f"-run-{stamp}{suffix}"
                            )
                            serial += 1
                        normalized_instance_slug = validate_slug(candidate)
                        row = None
                if row:
                    if unified_origin and row["unified_origin"] != unified_origin:
                        connection.execute(
                            """
                            UPDATE sessions
                            SET unified_origin = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (unified_origin, utc_now(), row["id"]),
                        )
                        row = connection.execute(
                            """
                            SELECT s.*, w.name AS world_name,
                                   w.slug AS world_slug
                            FROM sessions s
                            JOIN worlds w ON w.id = s.world_id
                            WHERE s.id = ?
                            """,
                            (row["id"],),
                        ).fetchone()
                    connection.execute("COMMIT")
                    return self._session(row)

                has_selected = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ? AND selected = 1
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                session_id = new_id("session")
                now = utc_now()
                initial_state = public_world_state(
                    json_load(world["initial_state_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'closed', 0, 1, ?, 0, ?, ?
                    )
                    """,
                    (
                        session_id,
                        platform_id,
                        group_id,
                        unified_origin,
                        normalized_instance_slug,
                        normalized_instance_name,
                        0 if has_selected else 1,
                        world["id"],
                        json_dump(initial_state),
                        now,
                        now,
                    ),
                )
                world_payload = {
                    "id": world["id"],
                    "slug": world["slug"],
                    "name": world["name"],
                    "description": world["description"],
                    "system_prompt": world["system_prompt"],
                    "rules": json_load(world["rules_json"], {}),
                    "opening_scene": world["opening_scene"],
                    "initial_state": json_load(
                        world["initial_state_json"],
                        {},
                    ),
                    "revision": world["revision"],
                    "content_version": str(
                        world["content_version"] or ""
                    ),
                    "source_package_id": str(
                        world["source_package_id"] or ""
                    ),
                    "package_format": int(
                        world["package_format"] or 0
                    ),
                    "source_artifact_hash": str(
                        world["source_artifact_hash"] or ""
                    ),
                    "source_kind": str(
                        world["source_kind"] or ""
                    ),
                }
                # Package worlds store compiled top-level contracts such as
                # entity_index, capability_index, runtime_contract and artifact
                # metadata in extensions_json.  A frozen instance must retain
                # those contracts; otherwise the source world validates while
                # the running session loses its registry and cannot resolve
                # capabilities/resources during card confirmation.
                extensions = json_load(world["extensions_json"], {})
                if isinstance(extensions, Mapping):
                    for key, value in extensions.items():
                        if str(key) != "ui_schema":
                            world_payload.setdefault(str(key), value)
                connection.execute(
                    """
                    INSERT INTO instance_configs(
                        session_id, world_revision, world_snapshot_json,
                        ui_profile_json, time_rules_json, phase_meta_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        world["revision"],
                        json_dump(world_payload),
                        world["ui_profile_json"],
                        json_dump(world_time_rules(world_payload)),
                        json_dump({"resume_mode": False}),
                        now,
                        now,
                    ),
                )
                self._initialize_current_rows(connection)
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.create",
                    session_id,
                    {
                        "platform_id": platform_id,
                        "group_id": group_id,
                        "world_id": world["id"],
                        "instance_slug": normalized_instance_slug,
                        "instance_name": normalized_instance_name,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def session_lifecycle_context(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._session_lifecycle_context_for_id,
            session_id,
        )

    def _session_lifecycle_context_for_id(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            return self._session_lifecycle_context(connection, session_id)

    def _session_lifecycle_context(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        def count(sql: str, parameters: tuple[Any, ...] = ()) -> int:
            return int(connection.execute(sql, parameters).fetchone()[0])

        return {
            "active_card_drafts": count(
                """
                SELECT COUNT(*) FROM character_card_drafts
                WHERE participant_id IN (
                    SELECT id FROM participants WHERE session_id = ?
                ) AND status = 'active'
                """,
                (session_id,),
            ),
            "suspended_card_drafts": count(
                """
                SELECT COUNT(*) FROM character_card_drafts
                WHERE participant_id IN (
                    SELECT id FROM participants WHERE session_id = ?
                ) AND status = 'suspended'
                """,
                (session_id,),
            ),
            "active_participants": count(
                """
                SELECT COUNT(*) FROM participants
                WHERE session_id = ?
                  AND participation_status IN (
                    'reserved', 'active', 'standby', 'away'
                  )
                """,
                (session_id,),
            ),
            "pending_choices": count(
                """
                SELECT COUNT(*) FROM choice_sets
                WHERE session_id = ? AND status = 'active'
                """,
                (session_id,),
            ),
            "pending_votes": count(
                """
                SELECT COUNT(*) FROM group_votes
                WHERE session_id = ? AND status = 'open'
                """,
                (session_id,),
            ),
            "pending_timers": count(
                """
                SELECT COUNT(*) FROM timer_instances
                WHERE session_id = ? AND status IN ('active', 'paused')
                """,
                (session_id,),
            ),
            "pending_operations": count(
                """
                SELECT COUNT(*) FROM action_operations
                WHERE session_id = ? AND status = 'pending'
                """,
                (session_id,),
            ),
            "temporary_grants": count(
                """
                SELECT COUNT(*) FROM permission_grants
                WHERE session_id = ?
                """,
                (session_id,),
            ),
        }
