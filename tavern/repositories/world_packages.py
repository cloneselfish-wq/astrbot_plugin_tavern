from __future__ import annotations

from .worlds_support import *


class WorldPackagesRepositoryMixin:
    async def install_package_world(
        self,
        compiled_world: Mapping[str, Any],
        *,
        package: Mapping[str, Any],
        actor_id: str,
        builtin: bool = False,
        characters: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        return await self._run(
            self._install_package_world,
            dict(compiled_world),
            dict(package),
            actor_id,
            builtin,
            [dict(item) for item in characters],
        )

    async def install_builtin_world(
        self,
        compiled_world: Mapping[str, Any],
        *,
        package: Mapping[str, Any],
        actor_id: str,
        characters: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        return await self.install_package_world(
            compiled_world,
            package=package,
            actor_id=actor_id,
            builtin=True,
            characters=characters,
        )

    def _install_package_world(
        self,
        compiled_world: dict[str, Any],
        package: dict[str, Any],
        actor_id: str,
        builtin: bool,
        characters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        validate_world_contract(compiled_world)
        slug = validate_slug(compiled_world.get("slug"))
        package_id = str(package.get("id") or compiled_world.get("package_id") or "")
        content_version = str(
            package.get("content_version")
            or package.get("version")
            or compiled_world.get("content_version")
            or ""
        )
        package_format = int(compiled_world.get("package_format", 0) or 0)
        artifact_hash = str(
            package.get("artifact_hash")
            or compiled_world.get("artifact_hash")
            or ""
        )
        source_kind = "builtin" if builtin else "package"
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM worlds WHERE slug = ?",
                    (slug,),
                ).fetchone()
                package_owner = connection.execute(
                    """
                    SELECT * FROM worlds
                    WHERE source_package_id = ? AND slug <> ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (package_id, slug),
                ).fetchone()
                if builtin and package_owner:
                    raise BuiltinWorldConflictError(
                        "builtin_package_identity_conflict",
                        f"内置包 {package_id} 已绑定其他世界 slug",
                    )
                if (
                    builtin
                    and current
                    and str(current["source_package_id"]) != package_id
                ):
                    raise BuiltinWorldConflictError(
                        "world_slug_conflict",
                        f"世界 slug {slug} 已由其他包占用",
                    )
                if (
                    builtin
                    and current
                    and str(current["source_package_id"] or "") == package_id
                    and str(current["content_version"] or "") == content_version
                    and str(current["source_artifact_hash"] or "")
                    != artifact_hash
                ):
                    current_extensions = json_load(
                        current["extensions_json"],
                        {},
                    )
                    current_source_hash = (
                        str(current_extensions.get("source_hash") or "")
                        if isinstance(current_extensions, Mapping)
                        else ""
                    )
                    incoming_source_hash = str(
                        compiled_world.get("source_hash") or ""
                    )
                    compiler_only_refresh = (
                        not bool(current["is_modified"])
                        and str(current["source_kind"] or "")
                        in {"builtin", "legacy_builtin"}
                        and bool(current_source_hash)
                        and current_source_hash == incoming_source_hash
                    )
                    if not compiler_only_refresh and not bool(
                        current["is_modified"]
                    ):
                        raise BuiltinWorldConflictError(
                            "builtin_revision_collision",
                            f"内置包 {package_id}@{content_version} 的源码内容在未升版时发生变化",
                        )
                if (
                    current
                    and str(current["source_package_id"]) == package_id
                    and str(current["content_version"]) == content_version
                    and str(current["source_artifact_hash"]) == artifact_hash
                    and not bool(current["is_modified"])
                ):
                    connection.execute("COMMIT")
                    return {
                        "mode": "unchanged",
                        "item": self._world(current),
                        "preserved_world_id": "",
                    }

                preserved_world_id = ""
                if builtin and current and bool(current["is_modified"]):
                    preserved_world_id = str(current["id"])
                    suffix = preserved_world_id.rsplit("_", 1)[-1][-8:]
                    preserved_slug = f"{slug}-user-copy-{suffix}"
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, source_kind = 'user',
                            migration_status = 'preserved_custom',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            preserved_slug,
                            f"{current['name']}（升级前用户副本）",
                            now,
                            current["id"],
                        ),
                    )
                    current = None

                previous_version = (
                    str(current["content_version"] or "") if current else ""
                )
                known_fields = {
                    "slug", "name", "description", "system_prompt", "rules",
                    "opening_scene", "initial_state", "archived", "revision",
                    "created_at", "updated_at", "internal_world_model_revision",
                    "capabilities", "player_limits", "card_template",
                    "time_rules", "choice_mode", "check_density",
                    "ui_schema", "ui_profile",
                }
                ui_profile = dict(compiled_world.get("ui_profile") or {})
                extensions = {
                    str(key): value
                    for key, value in compiled_world.items()
                    if key not in known_fields
                }
                if current:
                    world_id = str(current["id"])
                    connection.execute(
                        """
                        UPDATE worlds SET
                            name = ?, description = ?, system_prompt = ?,
                            rules_json = ?, extensions_json = ?,
                            ui_profile_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            archived = 0, revision = revision + 1,
                            source_package_id = ?, package_format = ?,
                            content_version = ?, source_kind = ?,
                            is_modified = 0, previous_content_version = ?,
                            migration_status = 'installed',
                            source_artifact_hash = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            clean_text(compiled_world.get("name"), max_chars=400),
                            clean_text(
                                compiled_world.get("description"),
                                max_chars=20000,
                            ),
                            clean_text(
                                compiled_world.get("system_prompt"),
                                max_chars=200000,
                            ),
                            json_dump(dict(compiled_world.get("rules") or {})),
                            json_dump(extensions),
                            json_dump(ui_profile),
                            clean_text(
                                compiled_world.get("opening_scene"),
                                max_chars=50000,
                            ),
                            json_dump(
                                dict(compiled_world.get("initial_state") or {})
                            ),
                            package_id,
                            package_format,
                            content_version,
                            source_kind,
                            previous_version,
                            artifact_hash,
                            now,
                            world_id,
                        ),
                    )
                    mode = "updated"
                else:
                    world_id = new_id("world")
                    display_no = self._allocate_world_display_no(connection)
                    connection.execute(
                        """
                        INSERT INTO worlds(
                            id, slug, display_no, sort_order, name, description,
                            system_prompt, rules_json, extensions_json,
                            ui_profile_json,
                            opening_scene, initial_state_json, archived, revision,
                            source_package_id, package_format, content_version,
                            source_kind, is_modified, previous_content_version,
                            migration_status, source_artifact_hash,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, 0, ?, 'installed', ?, ?, ?)
                        """,
                        (
                            world_id,
                            slug,
                            display_no,
                            display_no,
                            clean_text(compiled_world.get("name"), max_chars=400),
                            clean_text(
                                compiled_world.get("description"),
                                max_chars=20000,
                            ),
                            clean_text(
                                compiled_world.get("system_prompt"),
                                max_chars=200000,
                            ),
                            json_dump(dict(compiled_world.get("rules") or {})),
                            json_dump(extensions),
                            json_dump(ui_profile),
                            clean_text(
                                compiled_world.get("opening_scene"),
                                max_chars=50000,
                            ),
                            json_dump(
                                dict(compiled_world.get("initial_state") or {})
                            ),
                            package_id,
                            package_format,
                            content_version,
                            source_kind,
                            previous_version,
                            artifact_hash,
                            now,
                            now,
                        ),
                    )
                    mode = "created" if not preserved_world_id else "preserved"

                existing_slugs = {
                    str(row["slug"])
                    for row in connection.execute(
                        "SELECT slug FROM characters WHERE world_id = ?",
                        (world_id,),
                    ).fetchall()
                }
                for index, character in enumerate(characters):
                    character_slug = validate_slug(character.get("slug"))
                    if character_slug in existing_slugs:
                        continue
                    connection.execute(
                        """
                        INSERT INTO characters(
                            id, world_id, slug, name, role, profile_json,
                            prompt, enabled, sort_order, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
                        """,
                        (
                            new_id("character"),
                            world_id,
                            character_slug,
                            clean_text(character.get("name"), max_chars=100),
                            clean_text(
                                character.get("role") or "npc",
                                max_chars=40,
                            ),
                            json_dump(dict(character.get("profile") or {})),
                            clean_text(
                                character.get("prompt"),
                                max_chars=20000,
                            ),
                            int(character.get("sort_order", index) or index),
                            now,
                            now,
                        ),
                    )
                    existing_slugs.add(character_slug)

                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                item = self._world(row)
                self._persist_world_revision(connection, row, item, now)
                receipt_id = (
                    f"builtin:{package_id}@{content_version}"
                    if builtin
                    else new_id("package_install")
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO migration_receipts(
                        id, migration_type, source_version, target_version,
                        world_id, operation_id, receipt_json,
                        confirmed_by, created_at
                    ) VALUES (?, 'world_package', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        previous_version,
                        content_version,
                        world_id,
                        receipt_id,
                        json_dump(
                            {
                                "mode": mode,
                                "package_id": package_id,
                                "package_format": package_format,
                                "preserved_world_id": preserved_world_id,
                            }
                        ),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.package_install",
                    world_id,
                    {
                        "mode": mode,
                        "package_id": package_id,
                        "content_version": content_version,
                        "preserved_world_id": preserved_world_id,
                    },
                )
                connection.execute("COMMIT")
                return {
                    "mode": mode,
                    "item": item,
                    "preserved_world_id": preserved_world_id,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def import_characters(
        self,
        world_id: str,
        items: Sequence[Mapping[str, Any]],
        actor_id: str,
        *,
        conflict_policy: str = "skip",
    ) -> dict[str, Any]:
        return await self._run(
            self._import_characters,
            world_id,
            [dict(item) for item in items],
            actor_id,
            conflict_policy,
        )

    def _import_characters(
        self,
        world_id: str,
        items: list[dict[str, Any]],
        actor_id: str,
        conflict_policy: str,
    ) -> dict[str, Any]:
        policy = str(conflict_policy or "skip").strip().lower()
        if policy not in {"skip", "overwrite", "error"}:
            raise ValueError("NPC 冲突策略必须是 skip、overwrite 或 error")
        now = utc_now()
        created = 0
        updated = 0
        skipped = 0
        output: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("世界包不存在")
                for payload in items:
                    slug = validate_slug(payload.get("slug"))
                    prior = connection.execute(
                        "SELECT * FROM characters WHERE world_id = ? AND slug = ?",
                        (world_id, slug),
                    ).fetchone()
                    if prior and policy == "skip":
                        skipped += 1
                        output.append(self._character(prior))
                        continue
                    if prior and policy == "error":
                        raise DatabaseConflictError(
                            f"常驻角色 slug 已存在：{slug}"
                        )
                    name = clean_text(payload.get("name"), max_chars=100)
                    if not name:
                        raise ValueError("角色名称不能为空")
                    role = clean_text(payload.get("role") or "npc", max_chars=40)
                    profile = payload.get("profile")
                    if not isinstance(profile, Mapping):
                        raise ValueError(f"角色「{name}」的 profile 必须是对象")
                    prompt = clean_text(payload.get("prompt"), max_chars=20000)
                    sort_order = max(
                        -10000,
                        min(10000, int(payload.get("sort_order", 0) or 0)),
                    )
                    enabled = int(bool(payload.get("enabled", True)))
                    if prior:
                        connection.execute(
                            """
                            UPDATE characters SET
                                name = ?, role = ?, profile_json = ?,
                                prompt = ?, enabled = ?, sort_order = ?,
                                revision = revision + 1, updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                name,
                                role,
                                json_dump(dict(profile)),
                                prompt,
                                enabled,
                                sort_order,
                                now,
                                prior["id"],
                            ),
                        )
                        character_id = str(prior["id"])
                        updated += 1
                        action = "character.import_overwrite"
                    else:
                        character_id = new_id("character")
                        connection.execute(
                            """
                            INSERT INTO characters(
                                id, world_id, slug, name, role, profile_json,
                                prompt, enabled, sort_order, revision,
                                created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """,
                            (
                                character_id,
                                world_id,
                                slug,
                                name,
                                role,
                                json_dump(dict(profile)),
                                prompt,
                                enabled,
                                sort_order,
                                now,
                                now,
                            ),
                        )
                        created += 1
                        action = "character.import_create"
                    self._insert_audit(
                        connection,
                        "",
                        actor_id,
                        action,
                        character_id,
                        {"world_id": world_id, "slug": slug, "name": name},
                    )
                    output.append(
                        self._character(
                            connection.execute(
                                "SELECT * FROM characters WHERE id = ?",
                                (character_id,),
                            ).fetchone()
                        )
                    )
                connection.execute("COMMIT")
                return {
                    "created": created,
                    "updated": updated,
                    "skipped": skipped,
                    "failed": 0,
                    "items": output,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
