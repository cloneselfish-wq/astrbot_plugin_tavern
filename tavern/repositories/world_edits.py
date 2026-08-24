"""B2（A3）世界作者编辑仓库：保存前体检 + 可回滚撤销。

每次可视化编辑先记录当前世界为撤销快照，再体检候选并保存；revert 恢复最近一次撤销。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from ..database_support import *
from ..resolution_receipts import content_hash


DESIGNER_OPERATION_TYPES = frozenset(
    {
        "designer.field_save",
        "designer.preset_save",
    }
)


def _world_write_operation_id(world_id: str, idempotency_key: str) -> str:
    digest = content_hash(
        {
            "scope": "world.write",
            "world_id": str(world_id),
            "idempotency_key": str(idempotency_key),
        }
    )
    return f"console-world-write:{digest[:40]}"


class WorldEditRepositoryMixin:
    async def save_world_edit(
        self,
        world: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        """保存一次作者编辑：先记录撤销快照，再保存新世界。"""
        return await self._run(self._save_world_edit, dict(world), actor_id)

    def _save_world_edit(
        self,
        world: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        world_id = str(world.get("id") or "").strip()
        if not world_id:
            raise ValueError("需要世界 id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?", (world_id,)
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("世界包不存在")
                self._record_world_edit_undo(connection, current)
                updated = self._save_world_inline(
                    connection,
                    world,
                    actor_id,
                    expected_revision=(
                        int(world["revision"])
                        if world.get("revision") is not None
                        else None
                    ),
                    audit_action="world.designer_edit",
                )
                item = self._world(updated)
                self._persist_world_revision(
                    connection,
                    updated,
                    item,
                    utc_now(),
                )
                self._prune_world_edit_undo(connection, world_id)
                connection.execute("COMMIT")
                return item
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def apply_world_edit_intent(
        self,
        world_id: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: Mapping[str, Any],
        transform: Callable[[dict[str, Any]], Mapping[str, Any]],
        validate: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Apply one designer mutation with durable CAS/replay semantics."""
        return await self._run(
            self._apply_world_edit_intent,
            str(world_id),
            str(actor_id),
            int(expected_revision),
            str(idempotency_key),
            str(operation_type),
            dict(request_payload),
            transform,
            validate,
        )

    def _apply_world_edit_intent(
        self,
        world_id: str,
        actor_id: str,
        expected_revision: int,
        idempotency_key: str,
        operation_type: str,
        request_payload: dict[str, Any],
        transform: Callable[[dict[str, Any]], Mapping[str, Any]],
        validate: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    ) -> dict[str, Any]:
        world_id = clean_text(world_id, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=200)
        operation_type = clean_text(operation_type, max_chars=120)
        if not world_id:
            raise ValueError("作者编辑缺少世界引用")
        if expected_revision < 1:
            raise ValueError("作者编辑缺少有效的世界修订号")
        if not request_key:
            raise ValueError("作者编辑缺少幂等键")
        if operation_type not in DESIGNER_OPERATION_TYPES:
            raise ValueError("作者编辑操作类型未登记")
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
                            "相同幂等键已用于另一份世界编辑"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result = dict(result) if isinstance(result, Mapping) else {}
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError(
                        "世界编辑仍在处理中，请稍后重试"
                    )

                current = connection.execute(
                    "SELECT * FROM worlds WHERE id=?",
                    (world_id,),
                ).fetchone()
                if current is None:
                    raise DatabaseNotFoundError("世界包不存在")
                if int(current["revision"]) != expected_revision:
                    raise DatabaseConflictError(
                        "世界草稿已经变化；已保留你的输入，请刷新比较后重试"
                    )
                candidate = transform(self._world(current))
                if not isinstance(candidate, Mapping):
                    raise ValueError("作者编辑没有生成有效的世界候选")
                candidate = dict(candidate)
                candidate["id"] = world_id
                candidate["revision"] = expected_revision
                report: dict[str, Any] = {}
                if validate is not None:
                    checked = validate(candidate)
                    report = dict(checked) if isinstance(checked, Mapping) else {}
                    if not bool(report.get("compatible")):
                        messages = [
                            str(item.get("message") or "")
                            for item in (report.get("errors") or [])[:5]
                            if isinstance(item, Mapping)
                        ]
                        raise ValueError(
                            "编辑后模板体检未通过："
                            + ("；".join(filter(None, messages)) or "存在未通过项")
                        )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '{}', 'pending', 'validate', ?, ?, ?)
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
                self._record_world_edit_undo(connection, current)
                updated = self._save_world_inline(
                    connection,
                    candidate,
                    actor_id,
                    expected_revision=expected_revision,
                    audit_action=operation_type,
                )
                item = self._world(updated)
                self._persist_world_revision(
                    connection,
                    updated,
                    item,
                    now,
                )
                self._prune_world_edit_undo(connection, world_id)
                result = {
                    "item": item,
                    "report": report,
                    "replayed": False,
                }
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
                        utc_now(),
                        operation_id,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _record_world_edit_undo(
        self,
        connection: sqlite3.Connection,
        current: sqlite3.Row,
    ) -> None:
        connection.execute(
            """
            INSERT INTO world_edit_undo(
                id, world_id, revision, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                new_id("weu"),
                str(current["id"]),
                int(current["revision"]),
                json_dump(self._world(current)),
                utc_now(),
            ),
        )

    @staticmethod
    def _prune_world_edit_undo(
        connection: sqlite3.Connection,
        world_id: str,
    ) -> None:
        connection.execute(
            """
            DELETE FROM world_edit_undo
            WHERE world_id = ? AND id NOT IN (
                SELECT id FROM world_edit_undo WHERE world_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 20
            )
            """,
            (world_id, world_id),
        )

    async def revert_world_edit(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._revert_world_edit, world_id, actor_id)

    def _revert_world_edit(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                undo = connection.execute(
                    """
                    SELECT * FROM world_edit_undo
                    WHERE world_id = ? ORDER BY created_at DESC LIMIT 1
                    """,
                    (world_id,),
                ).fetchone()
                if not undo:
                    raise DatabaseNotFoundError("没有可回滚的作者编辑")
                payload = json_load(undo["payload_json"], {})
                updated = self._save_world_inline(
                    connection,
                    payload,
                    actor_id,
                    audit_action="world.edit_revert",
                )
                self._persist_world_revision(
                    connection,
                    updated,
                    self._world(updated),
                    utc_now(),
                )
                connection.execute(
                    "DELETE FROM world_edit_undo WHERE id = ?", (undo["id"],)
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?", (world_id,)
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _save_world_inline(
        self,
        connection: sqlite3.Connection,
        payload: Mapping[str, Any],
        actor_id: str,
        *,
        expected_revision: int | None = None,
        audit_action: str = "world.edit_revert",
    ) -> sqlite3.Row:
        """在既有事务内复用 worlds 表的完整更新逻辑（与 _save_world 一致）。"""
        from ..world_contract import validate_world_contract
        from ..lifecycle import validate_card_template_config

        world_id = str(payload.get("id") or "")
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=400)
        if not name:
            raise ValueError("世界名称不能为空")
        description = clean_text(payload.get("description"), max_chars=20000)
        system_prompt = clean_text(payload.get("system_prompt"), max_chars=200000)
        if not system_prompt:
            raise ValueError("世界设定不能为空")
        opening_scene = clean_text(payload.get("opening_scene"), max_chars=50000)
        rules = payload.get("rules")
        initial_state = payload.get("initial_state")
        if not isinstance(rules, Mapping) or not isinstance(initial_state, Mapping):
            raise ValueError("规则与初始状态必须是 JSON 对象")
        rules = dict(rules)
        if "internal_world_model_revision" in payload:
            rules["internal_world_model_revision"] = payload[
                "internal_world_model_revision"
            ]
        if "capabilities" in payload:
            rules["capabilities"] = payload["capabilities"]
        validate_world_contract({**payload, "rules": rules})
        if "actor" in rules:
            validate_card_template_config(rules["actor"])
        now = utc_now()
        existing = connection.execute(
            "SELECT * FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
        if not existing:
            raise DatabaseNotFoundError("世界包不存在")
        if (
            expected_revision is not None
            and int(existing["revision"]) != int(expected_revision)
        ):
            raise DatabaseConflictError(
                "世界草稿已经变化；已保留你的输入，请刷新比较后重试"
            )
        known_fields = {
            "id", "slug", "name", "description", "system_prompt", "rules",
            "display_no", "sort_order", "opening_scene", "initial_state",
            "archived", "revision", "created_at", "updated_at",
            "internal_world_model_revision", "capabilities", "player_limits",
            "card_template", "time_rules", "choice_mode", "check_density",
            "source_package_id", "package_format", "content_version",
            "source_kind", "is_modified", "previous_content_version",
            "migration_status", "source_artifact_hash", "characters",
        }
        extension_keys = set(payload.keys()) - known_fields
        extensions = json_load(existing["extensions_json"], {})
        if isinstance(extensions, Mapping):
            extensions = {**dict(extensions), **{k: payload[k] for k in extension_keys if k in payload}}
        else:
            extensions = {k: payload[k] for k in extension_keys if k in payload}
        package_managed = str(actor_id).startswith(("system:", "package:"))
        is_modified = (
            int(bool(payload.get("is_modified")))
            if package_managed and "is_modified" in payload
            else int(
                bool(existing["is_modified"])
                or str(existing["source_kind"]) in {
                    "builtin",
                    "legacy_builtin",
                    "package",
                }
            )
        )
        connection.execute(
            """
            UPDATE worlds SET
                slug = ?, name = ?, description = ?, system_prompt = ?,
                rules_json = ?, opening_scene = ?, initial_state_json = ?,
                extensions_json = ?, archived = ?, revision = revision + 1,
                source_package_id = ?, package_format = ?,
                content_version = ?, source_kind = ?, is_modified = ?,
                previous_content_version = ?, migration_status = ?,
                source_artifact_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                slug,
                name,
                description,
                system_prompt,
                json_dump(rules),
                opening_scene,
                json_dump(initial_state),
                json_dump(extensions),
                (
                    int(bool(payload["archived"]))
                    if "archived" in payload
                    else int(existing["archived"])
                ),
                str(payload.get("source_package_id", existing["source_package_id"]) or ""),
                int(payload.get("package_format", existing["package_format"]) or 0),
                str(payload.get("content_version", existing["content_version"]) or ""),
                str(payload.get("source_kind", existing["source_kind"]) or "user"),
                is_modified,
                str(payload.get("previous_content_version", existing["previous_content_version"]) or ""),
                str(payload.get("migration_status", existing["migration_status"]) or ""),
                str(payload.get("source_artifact_hash", existing["source_artifact_hash"]) or ""),
                now,
                world_id,
            ),
        )
        if connection.execute("SELECT changes() AS value").fetchone()["value"] != 1:
            raise DatabaseConflictError("世界草稿更新失败，请刷新后重试")
        connection.execute(
            """
            INSERT INTO audit_logs(session_id, actor_id, action, target, detail_json, created_at)
            VALUES ('', ?, ?, ?, ?, ?)
            """,
            (
                actor_id,
                audit_action,
                world_id,
                json_dump(
                    {
                        "slug": slug,
                        "revision_before": int(existing["revision"]),
                        "revision_after": int(existing["revision"]) + 1,
                    }
                ),
                now,
            ),
        )
        return connection.execute(
            "SELECT * FROM worlds WHERE id=?",
            (world_id,),
        ).fetchone()


__all__ = ["WorldEditRepositoryMixin"]
