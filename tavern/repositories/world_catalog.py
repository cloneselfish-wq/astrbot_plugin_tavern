from __future__ import annotations

from .worlds_support import *


class WorldCatalogRepositoryMixin:
    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_worlds, include_archived)

    def _list_worlds(self, include_archived: bool) -> list[dict[str, Any]]:
        with self._connect() as connection:
            condition = "" if include_archived else "WHERE archived = 0"
            rows = connection.execute(
                f"""
                SELECT * FROM worlds
                {condition}
                ORDER BY archived ASC, sort_order ASC, display_no ASC
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._world(row)
                count = connection.execute(
                    "SELECT COUNT(*) FROM characters WHERE world_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                item["character_count"] = count
                result.append(item)
            return result

    async def get_world(self, world_ref: str) -> dict[str, Any]:
        return await self._run(self._get_world, world_ref)

    def _get_world(self, world_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worlds WHERE id = ? OR slug = ?",
                (world_ref, world_ref),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("世界包不存在")
            world = self._world(row)
            character_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order ASC, name COLLATE NOCASE
                """,
                (row["id"],),
            ).fetchall()
            world["characters"] = [
                self._character(character) for character in character_rows
            ]
            return world

    async def save_world(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_world, dict(payload), actor_id)

    async def set_world_sort_order(
        self,
        world_id: str,
        sort_order: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_world_sort_order, world_id, int(sort_order), actor_id
        )

    def _set_world_sort_order(
        self, world_id: str, sort_order: int, actor_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    "UPDATE worlds SET sort_order=? WHERE id=?",
                    (max(1, sort_order), world_id),
                )
                self._insert_audit(
                    connection, "", actor_id, "world.reorder", world_id,
                    {"display_no": current["display_no"], "sort_order": max(1, sort_order)},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _allocate_world_display_no(connection: sqlite3.Connection) -> int:
        maximum = int(connection.execute(
            "SELECT COALESCE(MAX(display_no), 0) + 1 AS value FROM worlds"
        ).fetchone()["value"])
        row = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='next_world_display_no'"
        ).fetchone()
        try:
            counter = int(row["value"]) if row else 1
        except (TypeError, ValueError):
            counter = 1
        value = max(maximum, counter)
        connection.execute(
            """
            INSERT INTO tavern_meta(key, value) VALUES ('next_world_display_no', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (str(value + 1),),
        )
        return value

    def _persist_world_revision(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        world: Mapping[str, Any],
        now: str,
    ) -> None:
        revision = int(row["revision"])
        snapshot_hash = content_hash(world)
        connection.execute(
            """
            INSERT OR IGNORE INTO world_rule_revisions(
                id, world_id, world_revision, content_hash, rules_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_rule"), row["id"], revision, snapshot_hash,
                json_dump(world.get("rules") or {}), now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO world_snapshots(
                id, world_id, world_revision, content_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("world_snapshot"), row["id"], revision, snapshot_hash,
                json_dump(dict(world)), now,
            ),
        )
        features = enabled_feature_versions(world)
        required = {
            str(item).split("@", 1)[0]
            for item in world.get("required_features", [])
            if isinstance(item, str)
        }
        for feature, version in features.items():
            connection.execute(
                """
                INSERT OR REPLACE INTO world_feature_versions(
                    world_id, world_revision, feature_name, feature_version,
                    required, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], revision, feature, version, int(feature in required), now),
            )
        try:
            registry = EntityRegistry(world)
        except Exception:
            registry = None
        if registry is not None:
            for item in registry.export():
                connection.execute(
                    """
                    INSERT OR REPLACE INTO world_entity_registry(
                        world_id, world_revision, entity_ref, entity_type,
                        label, definition_json, content_hash, visibility, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"], revision, item["ref"], item["entity_type"],
                        item["label"], json_dump(item["definition"]),
                        content_hash(item["definition"]),
                        str(item["definition"].get("visibility") or "world"), now,
                    ),
                )

    def _save_world(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        world_id = str(payload.get("id") or "").strip()
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=400)
        if not name:
            raise ValueError("世界名称不能为空")
        description = clean_text(payload.get("description"), max_chars=20000)
        system_prompt = clean_text(
            payload.get("system_prompt"),
            max_chars=200000,
        )
        if not system_prompt:
            raise ValueError("世界设定不能为空")
        opening_scene = clean_text(
            payload.get("opening_scene"),
            max_chars=50000,
        )
        rules = payload.get("rules")
        initial_state = payload.get("initial_state")
        if not isinstance(rules, Mapping):
            raise ValueError("规则必须是 JSON 对象")
        if not isinstance(initial_state, Mapping):
            raise ValueError("初始状态必须是 JSON 对象")
        rules = dict(rules)
        if "internal_world_model_revision" in payload:
            rules["internal_world_model_revision"] = payload[
                "internal_world_model_revision"
            ]
        if "capabilities" in payload:
            rules["capabilities"] = payload["capabilities"]
        validate_world_contract({**payload, "rules": rules})
        known_fields = {
            "id", "slug", "name", "description", "system_prompt", "rules",
            "display_no", "sort_order",
            "opening_scene", "initial_state", "archived", "revision",
            "created_at", "updated_at", "internal_world_model_revision",
            "capabilities",
            "player_limits", "card_template", "time_rules", "choice_mode",
            "check_density",
            "source_package_id", "package_format", "content_version",
            "source_kind", "is_modified", "previous_content_version",
            "migration_status", "source_artifact_hash",
            "ui_schema", "ui_profile",
        }
        provided_extensions = {
            str(key): value
            for key, value in payload.items()
            if key not in known_fields
        }
        if "actor" in rules:
            validate_card_template_config(rules["actor"])
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if world_id:
                    current = connection.execute(
                        "SELECT * FROM worlds WHERE id = ?",
                        (world_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("世界包不存在")
                    current_extensions = json_load(
                        current["extensions_json"],
                        {},
                    )
                    extensions = (
                        {**dict(current_extensions), **provided_extensions}
                        if isinstance(current_extensions, Mapping)
                        else dict(provided_extensions)
                    )
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "世界包已被其他操作更新，请刷新后重试"
                        )
                    package_managed = str(actor_id).startswith(
                        ("system:", "package:")
                    )
                    ui_profile = (
                        dict(payload.get("ui_profile") or {})
                        if package_managed
                        else json_load(current["ui_profile_json"], {})
                    )
                    is_modified = (
                        int(bool(payload.get("is_modified")))
                        if package_managed and "is_modified" in payload
                        else int(
                            bool(current["is_modified"])
                            or str(current["source_kind"]) in {
                                "builtin",
                                "legacy_builtin",
                                "package",
                            }
                        )
                    )
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?, extensions_json = ?,
                            ui_profile_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            archived = ?, revision = revision + 1,
                            source_package_id = ?, package_format = ?,
                            content_version = ?, source_kind = ?,
                            is_modified = ?, previous_content_version = ?,
                            migration_status = ?, source_artifact_hash = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            slug,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(extensions),
                            json_dump(ui_profile),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            (
                                int(bool(payload["archived"]))
                                if "archived" in payload
                                else current["archived"]
                            ),
                            str(
                                payload.get(
                                    "source_package_id",
                                    current["source_package_id"],
                                )
                                or ""
                            ),
                            int(
                                payload.get(
                                    "package_format",
                                    current["package_format"],
                                )
                                or 0
                            ),
                            str(
                                payload.get(
                                    "content_version",
                                    current["content_version"],
                                )
                                or ""
                            ),
                            str(
                                payload.get("source_kind", current["source_kind"])
                                or "user"
                            ),
                            is_modified,
                            str(
                                payload.get(
                                    "previous_content_version",
                                    current["previous_content_version"],
                                )
                                or ""
                            ),
                            str(
                                payload.get(
                                    "migration_status",
                                    current["migration_status"],
                                )
                                or ""
                            ),
                            str(
                                payload.get(
                                    "source_artifact_hash",
                                    current["source_artifact_hash"],
                                )
                                or ""
                            ),
                            now,
                            world_id,
                        ),
                    )
                    action = "world.update"
                else:
                    world_id = new_id("world")
                    display_no = self._allocate_world_display_no(connection)
                    connection.execute(
                        """
                        INSERT INTO worlds(
                            id, slug, display_no, sort_order, name, description, system_prompt,
                            rules_json, extensions_json, ui_profile_json,
                            opening_scene, initial_state_json,
                            archived, revision, source_package_id, package_format,
                            content_version, source_kind, is_modified,
                            previous_content_version, migration_status,
                            source_artifact_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            world_id,
                            slug,
                            display_no,
                            display_no,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            json_dump(provided_extensions),
                            json_dump({}),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            str(payload.get("source_package_id") or ""),
                            int(payload.get("package_format", 0) or 0),
                            str(payload.get("content_version") or ""),
                            str(payload.get("source_kind") or "user"),
                            int(bool(payload.get("is_modified", False))),
                            str(payload.get("previous_content_version") or ""),
                            str(payload.get("migration_status") or ""),
                            str(payload.get("source_artifact_hash") or ""),
                            now,
                            now,
                        ),
                    )
                    action = "world.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    world_id,
                    {"slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                self._persist_world_revision(
                    connection, row, self._world(row), now
                )
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._archive_world, world_id, actor_id)

    def _archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                active_sessions = connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE world_id = ? AND state != 'closed'
                    """,
                    (world_id,),
                ).fetchone()[0]
                if active_sessions:
                    raise ValueError("仍有运行中的会话使用该世界，不能归档")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 1, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.archive",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._restore_world, world_id, actor_id)

    def _restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 0, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.restore",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise
