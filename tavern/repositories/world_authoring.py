from __future__ import annotations

from .worlds_support import *


class WorldAuthoringRepositoryMixin:
    async def prepare_world_write_intent(
        self,
        world_id: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Reserve an external+SQLite world write before touching package files."""
        return await self._run(
            self._prepare_world_write_intent,
            str(world_id),
            str(actor_id),
            int(expected_revision),
            str(idempotency_key),
            str(operation_type),
            dict(request_payload),
        )

    def _prepare_world_write_intent(
        self,
        world_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        world_id = clean_text(world_id, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        operation_type = clean_text(operation_type, max_chars=120)
        if not world_id:
            raise ValueError("世界写入缺少世界引用")
        if expected_revision < 1:
            raise ValueError("世界写入缺少有效的修订号")
        if not request_key:
            raise ValueError("世界写入缺少幂等键")
        if operation_type != "world.module_toggle":
            raise ValueError("世界写入操作类型未登记")
        request = {
            "operation_type": operation_type,
            "world_id": world_id,
            "expected_revision": expected_revision,
            "input": request_payload,
        }
        input_hash = content_hash(request)
        operation_id = _world_write_operation_id(world_id, request_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同幂等键已用于另一份世界写入"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result.update(
                            {
                                "state": "completed",
                                "replayed": True,
                            }
                        )
                        connection.execute("COMMIT")
                        return result
                    if str(receipt["status"] or "") not in {
                        "reserved",
                        "failed_retryable",
                    }:
                        raise DatabaseConflictError(
                            "世界写入不能从当前阶段继续，请刷新后重试"
                        )
                current = connection.execute(
                    "SELECT revision FROM worlds WHERE id=?",
                    (world_id,),
                ).fetchone()
                if current is None:
                    raise DatabaseNotFoundError("世界包不存在")
                if int(current["revision"]) != expected_revision:
                    raise DatabaseConflictError(
                        "世界草稿已经变化；已保留你的输入，请刷新比较后重试"
                    )
                now = utc_now()
                if receipt is None:
                    connection.execute(
                        """
                        INSERT INTO operation_receipts(
                            operation_id, session_id, operation_type,
                            request_json, result_json, status, phase,
                            input_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '{}', 'reserved',
                                  'external_pending', ?, ?, ?)
                        """,
                        (
                            operation_id,
                            f"world:{world_id}",
                            operation_type,
                            json_dump(request),
                            input_hash,
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE operation_receipts
                        SET status='reserved', phase='external_pending',
                            last_error_code='', updated_at=?
                        WHERE operation_id=?
                        """,
                        (now, operation_id),
                    )
                connection.execute("COMMIT")
                return {
                    "state": "prepared",
                    "operation_id": operation_id,
                    "input_hash": input_hash,
                    "replayed": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def commit_world_write_intent(
        self,
        world: Mapping[str, Any],
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_world_write_intent,
            dict(world),
            str(actor_id),
            int(expected_revision),
            str(idempotency_key),
            str(operation_type),
            dict(request_payload),
        )

    def _commit_world_write_intent(
        self,
        world: dict[str, Any],
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        world_id = clean_text(world.get("id"), max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        request = {
            "operation_type": operation_type,
            "world_id": world_id,
            "expected_revision": expected_revision,
            "input": request_payload,
        }
        input_hash = content_hash(request)
        operation_id = _world_write_operation_id(world_id, request_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is None:
                    raise DatabaseConflictError(
                        "世界写入预留已失效，请重新提交"
                    )
                if str(receipt["input_hash"] or "") != input_hash:
                    raise DatabaseConflictError(
                        "相同幂等键已用于另一份世界写入"
                    )
                if str(receipt["status"] or "") == "completed":
                    result = json_load(receipt["result_json"], {})
                    result = dict(result) if isinstance(result, Mapping) else {}
                    result["replayed"] = True
                    connection.execute("COMMIT")
                    return result
                if str(receipt["status"] or "") not in {
                    "reserved",
                    "failed_retryable",
                }:
                    raise DatabaseConflictError(
                        "世界写入不能从当前阶段提交"
                    )
                world["id"] = world_id
                world["revision"] = expected_revision
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET status='pending', phase='database_commit', updated_at=?
                    WHERE operation_id=?
                    """,
                    (utc_now(), operation_id),
                )
                updated = self._save_world_inline(
                    connection,
                    world,
                    actor_id,
                    expected_revision=expected_revision,
                    audit_action=operation_type,
                )
                item = self._world(updated)
                now = utc_now()
                self._persist_world_revision(
                    connection,
                    updated,
                    item,
                    now,
                )
                result = {"item": item, "replayed": False}
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json=?, status='completed', phase='committed',
                        committed_revision=?, updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        json_dump(result),
                        int(updated["revision"]),
                        now,
                        operation_id,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fail_world_write_intent(
        self,
        world_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
        error_code: str,
        retryable: bool,
    ) -> None:
        await self._run(
            self._fail_world_write_intent,
            str(world_id),
            int(expected_revision),
            str(idempotency_key),
            str(operation_type),
            dict(request_payload),
            str(error_code),
            bool(retryable),
        )

    def _fail_world_write_intent(
        self,
        world_id: str,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: dict[str, Any],
        error_code: str,
        retryable: bool,
    ) -> None:
        request = {
            "operation_type": operation_type,
            "world_id": world_id,
            "expected_revision": expected_revision,
            "input": request_payload,
        }
        input_hash = content_hash(request)
        operation_id = _world_write_operation_id(world_id, idempotency_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is None or str(receipt["input_hash"] or "") != input_hash:
                    raise DatabaseConflictError("世界写入回执不匹配")
                if str(receipt["status"] or "") != "completed":
                    connection.execute(
                        """
                        UPDATE operation_receipts
                        SET status=?, phase='external_failed',
                            last_error_code=?, updated_at=?
                        WHERE operation_id=?
                        """,
                        (
                            "failed_retryable" if retryable else "failed",
                            clean_text(error_code, max_chars=120),
                            utc_now(),
                            operation_id,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def archive_world_intent(
        self,
        world_id: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._archive_world_intent,
            str(world_id),
            str(actor_id),
            int(expected_revision),
            str(idempotency_key),
        )

    def _archive_world_intent(
        self,
        world_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        world_id = clean_text(world_id, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        if not world_id:
            raise ValueError("归档世界缺少世界引用")
        if expected_revision < 1:
            raise ValueError("归档世界缺少有效的修订号")
        if not request_key:
            raise ValueError("归档世界缺少幂等键")
        request = {
            "operation_type": "world.archive",
            "world_id": world_id,
            "expected_revision": expected_revision,
            "input": {},
        }
        input_hash = content_hash(request)
        operation_id = _world_write_operation_id(world_id, request_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同幂等键已用于另一份世界写入"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError("世界归档仍在处理中")
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id=?",
                    (world_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("世界包不存在")
                if int(row["revision"]) != expected_revision:
                    raise DatabaseConflictError(
                        "世界状态已经变化；请刷新后重新确认归档"
                    )
                if bool(row["archived"]):
                    raise DatabaseConflictError("世界已经归档，请刷新世界库")
                active_sessions = connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE world_id=? AND state!='closed'
                    """,
                    (world_id,),
                ).fetchone()[0]
                if active_sessions:
                    raise ValueError("仍有运行中的会话使用该世界，不能归档")
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'world.archive', ?, '{}',
                              'pending', 'database_commit', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        f"world:{world_id}",
                        json_dump(request),
                        input_hash,
                        now,
                        now,
                    ),
                )
                changed = connection.execute(
                    """
                    UPDATE worlds
                    SET archived=1, revision=revision+1, updated_at=?
                    WHERE id=? AND revision=? AND archived=0
                    """,
                    (now, world_id, expected_revision),
                )
                if changed.rowcount != 1:
                    raise DatabaseConflictError(
                        "世界状态已经变化；请刷新后重新确认归档"
                    )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.archive",
                    world_id,
                    {
                        "slug": row["slug"],
                        "revision_before": expected_revision,
                        "revision_after": expected_revision + 1,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id=?",
                    (world_id,),
                ).fetchone()
                item = self._world(updated)
                self._persist_world_revision(
                    connection,
                    updated,
                    item,
                    now,
                )
                result = {"item": item, "replayed": False}
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json=?, status='completed', phase='committed',
                        committed_revision=?, updated_at=?
                    WHERE operation_id=?
                    """,
                    (
                        json_dump(result),
                        int(updated["revision"]),
                        now,
                        operation_id,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_characters(
        self,
        world_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_characters, world_id)

    def _list_characters(self, world_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if world_id:
                rows = connection.execute(
                    """
                    SELECT * FROM characters WHERE world_id = ?
                    ORDER BY sort_order ASC, name COLLATE NOCASE
                    """,
                    (world_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM characters
                    ORDER BY world_id, sort_order ASC, name COLLATE NOCASE
                    """
                ).fetchall()
            return [self._character(row) for row in rows]

    async def save_character_intent(
        self,
        world_id: str,
        actor_id: str,
        *,
        character_id: str = "",
        values: Mapping[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create/update one resident character with CAS and durable replay."""

        return await self._run(
            self._save_character_intent,
            str(world_id),
            str(actor_id),
            str(character_id),
            dict(values),
            int(expected_revision),
            str(idempotency_key),
        )

    def _save_character_intent(
        self,
        world_id: str,
        actor_id: str,
        character_id: str,
        values: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        world_id = clean_text(world_id, max_chars=160)
        character_id = clean_text(character_id, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        if not world_id or not request_key or expected_revision < 1:
            raise ValueError("常驻角色写入缺少世界、修订号或防重复凭证")
        name = clean_text(values.get("name"), max_chars=100)
        role = clean_text(values.get("role") or "npc", max_chars=40)
        description_supplied = "description" in values
        private_direction_supplied = "private_direction" in values
        description = clean_text(values.get("description"), max_chars=4000)
        private_direction = clean_text(
            values.get("private_direction"), max_chars=12000
        )
        enabled = bool(values.get("enabled", True))
        if not name:
            raise ValueError("常驻角色名称不能为空")
        request = {
            "operation_type": (
                "character.update" if character_id else "character.create"
            ),
            "world_id": world_id,
            "character_id": character_id,
            "expected_revision": expected_revision,
            "input": {
                "name": name,
                "role": role,
                "enabled": enabled,
                **({"description": description} if description_supplied else {}),
                **(
                    {"private_direction": private_direction}
                    if private_direction_supplied
                    else {}
                ),
            },
        }
        input_hash = content_hash(request)
        operation_id = _character_write_operation_id(
            world_id, actor_id, request_key
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同防重复凭证已用于另一份常驻角色修改"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError("常驻角色修改仍在处理中")
                world = connection.execute(
                    "SELECT * FROM worlds WHERE id=?", (world_id,)
                ).fetchone()
                if world is None:
                    raise DatabaseNotFoundError("世界包不存在")
                if bool(world["archived"]):
                    raise DatabaseConflictError("世界已经归档，不能继续编辑角色")
                now = utc_now()
                if character_id:
                    current = connection.execute(
                        "SELECT * FROM characters WHERE id=? AND world_id=?",
                        (character_id, world_id),
                    ).fetchone()
                    if current is None:
                        raise DatabaseNotFoundError("常驻角色不存在")
                    if int(current["revision"]) != expected_revision:
                        raise DatabaseConflictError(
                            "常驻角色已经变化；请刷新后重新提交"
                        )
                    profile = json_load(current["profile_json"], {})
                    profile = dict(profile) if isinstance(profile, Mapping) else {}
                    if description_supplied:
                        profile["description"] = description
                    if private_direction_supplied:
                        if private_direction:
                            profile["private_direction"] = private_direction
                        else:
                            profile.pop("private_direction", None)
                    stored_prompt = (
                        private_direction
                        if private_direction_supplied
                        else str(current["prompt"] or "")
                    )
                    changed = connection.execute(
                        """
                        UPDATE characters SET
                            name=?, role=?, profile_json=?, prompt=?, enabled=?,
                            revision=revision+1, updated_at=?
                        WHERE id=? AND world_id=? AND revision=?
                        """,
                        (
                            name,
                            role,
                            json_dump(profile),
                            stored_prompt,
                            int(enabled),
                            now,
                            character_id,
                            world_id,
                            expected_revision,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise DatabaseConflictError(
                            "常驻角色已经变化；请刷新后重新提交"
                        )
                    audit_action = "character.update"
                else:
                    if int(world["revision"]) != expected_revision:
                        raise DatabaseConflictError(
                            "世界草稿已经变化；请刷新后重新创建角色"
                        )
                    character_id = new_id("char")
                    slug = validate_slug(new_id("npc"))
                    profile = {"description": description}
                    if private_direction:
                        profile["private_direction"] = private_direction
                    connection.execute(
                        """
                        INSERT INTO characters(
                            id, world_id, slug, name, role, profile_json,
                            prompt, enabled, sort_order, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                        """,
                        (
                            character_id,
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(profile),
                            private_direction,
                            int(enabled),
                            now,
                            now,
                        ),
                    )
                    audit_action = "character.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    audit_action,
                    character_id,
                    {"world_id": world_id, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM characters WHERE id=?", (character_id,)
                ).fetchone()
                item = self._character(row)
                result = {"item": item, "replayed": False}
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase, input_hash,
                        committed_revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'completed', 'committed', ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        f"world:{world_id}",
                        audit_action,
                        json_dump(request),
                        json_dump(result),
                        input_hash,
                        int(row["revision"]),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def retire_character_intent(
        self,
        world_id: str,
        character_id: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        """Disable a resident character without deleting references/history."""

        return await self._run(
            self._retire_character_intent,
            str(world_id),
            str(character_id),
            str(actor_id),
            int(expected_revision),
            str(idempotency_key),
            str(reason),
        )

    def _retire_character_intent(
        self,
        world_id: str,
        character_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
    ) -> dict[str, Any]:
        world_id = clean_text(world_id, max_chars=160)
        character_id = clean_text(character_id, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        reason = clean_text(reason, max_chars=1000)
        if not world_id or not character_id or not request_key or expected_revision < 1:
            raise ValueError("常驻角色退役缺少目标、修订号或防重复凭证")
        if not reason:
            raise ValueError("请说明常驻角色退役原因")
        request = {
            "operation_type": "character.retire",
            "world_id": world_id,
            "character_id": character_id,
            "expected_revision": expected_revision,
            "input": {"reason": reason},
        }
        input_hash = content_hash(request)
        operation_id = _character_write_operation_id(
            world_id, actor_id, request_key
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同防重复凭证已用于另一份常驻角色修改"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError("常驻角色退役仍在处理中")
                row = connection.execute(
                    "SELECT * FROM characters WHERE id=? AND world_id=?",
                    (character_id, world_id),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("常驻角色不存在")
                if int(row["revision"]) != expected_revision:
                    raise DatabaseConflictError(
                        "常驻角色已经变化；请刷新后重新确认退役"
                    )
                if not bool(row["enabled"]):
                    raise DatabaseConflictError("常驻角色已经退役，请刷新内容树")
                now = utc_now()
                changed = connection.execute(
                    """
                    UPDATE characters
                    SET enabled=0, revision=revision+1, updated_at=?
                    WHERE id=? AND world_id=? AND revision=? AND enabled=1
                    """,
                    (now, character_id, world_id, expected_revision),
                )
                if changed.rowcount != 1:
                    raise DatabaseConflictError(
                        "常驻角色已经变化；请刷新后重新确认退役"
                    )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "character.retire",
                    character_id,
                    {"world_id": world_id, "name": row["name"], "reason": reason},
                )
                updated = connection.execute(
                    "SELECT * FROM characters WHERE id=?", (character_id,)
                ).fetchone()
                item = self._character(updated)
                result = {"item": item, "replayed": False}
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase, input_hash,
                        committed_revision, created_at, updated_at
                    ) VALUES (?, ?, 'character.retire', ?, ?,
                              'completed', 'committed', ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        f"world:{world_id}",
                        json_dump(request),
                        json_dump(result),
                        input_hash,
                        int(updated["revision"]),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def save_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_character,
            dict(payload),
            actor_id,
        )
