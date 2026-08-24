from __future__ import annotations

from .worlds_support import *


class WorldAuthoringQueriesRepositoryMixin:
    def _save_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        character_id = str(payload.get("id") or "").strip()
        world_id = validate_platform_id(
            payload.get("world_id"),
            label="世界 ID",
        )
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=100)
        if not name:
            raise ValueError("角色名称不能为空")
        role = clean_text(payload.get("role") or "npc", max_chars=40)
        prompt = clean_text(payload.get("prompt"), max_chars=20000)
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("角色资料必须是 JSON 对象")
        try:
            sort_order = max(-10000, min(10000, int(payload.get("sort_order", 0))))
        except (TypeError, ValueError):
            sort_order = 0
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("世界包不存在")
                if character_id:
                    current = connection.execute(
                        "SELECT * FROM characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("角色不存在")
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "角色已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE characters SET
                            world_id = ?, slug = ?, name = ?, role = ?,
                            profile_json = ?, prompt = ?, enabled = ?,
                            sort_order = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            character_id,
                        ),
                    )
                    action = "character.update"
                else:
                    character_id = new_id("char")
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
                    action = "character.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    character_id,
                    {"world_id": world_id, "slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_character, character_id, actor_id)

    def _delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色不存在")
                connection.execute(
                    "DELETE FROM characters WHERE id = ?",
                    (character_id,),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "character.delete",
                    character_id,
                    {"name": row["name"], "world_id": row["world_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
