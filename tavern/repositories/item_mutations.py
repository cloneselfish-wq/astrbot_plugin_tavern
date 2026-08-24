from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ..database_support import *
from ..item_catalog import normalize_item_instance


class ItemMutationsRepositoryMixin:
    @staticmethod
    def _public_ai_actor_ref(actor_id: object) -> str:
        digest = hashlib.sha256(
            str(actor_id or "").encode("utf-8")
        ).hexdigest()[:12].upper()
        return f"public:actor:{digest}"

    @staticmethod
    def _emit_item_visual_event_locked(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        operation_id: str,
        action: str,
        created_at: str,
    ) -> dict[str, Any]:
        digest = hashlib.sha256(
            f"{operation_id}:{action}:party".encode("utf-8")
        ).hexdigest()[:32]
        revision = next_session_event_seq(connection, session_id)
        return insert_session_event(
            connection,
            session_id=session_id,
            event_id=f"event:item.inventory:{digest}",
            type_="event:item.inventory_changed",
            payload={
                "title": "小队背包更新",
                "summary": "一名队友的可见背包已经更新。",
                "affected_modules": ["party"],
                "kind": "party",
                "revision": revision,
            },
            visibility="public",
            created_at=created_at,
        )

    def _apply_item_ops_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        item_ops: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Apply one normalized item/status plan inside the caller transaction."""

        effects: dict[str, list[Any]] = {
            "consumed": [],
            "granted": [],
            "transferred": [],
            "status_removed": [],
        }
        for op in item_ops:
            if not isinstance(op, Mapping):
                raise ValueError("道具操作格式无效")
            op_kind = str(op.get("op") or "")
            if op_kind == "consume":
                result = self._consume_item_instances_locked(
                    connection,
                    session_id=session_id,
                    owner_ref=str(op.get("owner_ref") or ""),
                    items=dict(op.get("items") or {}),
                    reason=str(op.get("reason") or "回合使用"),
                    operation_id=str(op.get("operation_id") or ""),
                )
                effects["consumed"].extend(
                    list(result.get("consumed") or [])
                )
            elif op_kind == "transfer":
                result = self._transfer_item_instances_locked(
                    connection,
                    session_id=session_id,
                    from_owner=str(op.get("from_owner") or ""),
                    to_owner=str(op.get("to_owner") or ""),
                    item_id=str(op.get("item_id") or ""),
                    quantity=int(op.get("quantity", 1) or 1),
                    reason=str(op.get("reason") or "回合转赠"),
                    operation_id=str(op.get("operation_id") or ""),
                )
                effects["transferred"].append(result)
            elif op_kind == "grant":
                result = self._grant_item_instances_locked(
                    connection,
                    session_id=session_id,
                    grants=[dict(op.get("grant") or {})],
                    operation_id=str(op.get("operation_id") or ""),
                    actor_id=str(op.get("actor_id") or "system"),
                    audit_action="items.grant",
                )
                effects["granted"].extend(
                    list(result.get("granted") or [])
                )
            elif op_kind == "remove_status":
                result = self._remove_participant_status_locked(
                    connection,
                    session_id=session_id,
                    target_ref=str(op.get("target_ref") or ""),
                    keywords=tuple(
                        str(keyword)
                        for keyword in (op.get("keywords") or ())
                    ),
                    actor_id=str(op.get("actor_id") or "system"),
                    reason=str(op.get("reason") or "道具治疗"),
                )
                effects["status_removed"].extend(
                    list(result.get("removed") or [])
                )
            else:
                raise ValueError(f"未知道具操作：{op_kind}")
        return effects

    async def grant_item_instances(
        self,
        *,
        session_id: str,
        owner_ref: str,
        items: Mapping[str, int],
        source: str,
        operation_id: str,
        owner_type: str = "character",
    ) -> dict[str, Any]:
        return await self._run(
            self._grant_item_instances,
            session_id,
            owner_ref,
            dict(items),
            source,
            operation_id,
            owner_type,
        )

    def _grant_item_instances(
        self,
        session_id: str,
        owner_ref: str,
        items: Mapping[str, int],
        source: str,
        operation_id: str,
        owner_type: str,
    ) -> dict[str, Any]:
        normalized_items: dict[str, int] = {}
        for key, value in (items or {}).items():
            item_id = str(key or "").strip()
            try:
                quantity = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("物品授予数量必须是正整数") from exc
            if not item_id or quantity <= 0:
                raise ValueError("物品授予必须包含名称与正整数数量")
            normalized_items[item_id] = quantity
        items = normalized_items
        grants = [
            {
                "owner_type": owner_type,
                "owner_ref": owner_ref,
                "item_id": item_id,
                "quantity": quantity,
                "container": "",
                "state": {},
                "source": source,
            }
            for item_id, quantity in sorted(items.items())
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._grant_item_instances_locked(
                    connection,
                    session_id=session_id,
                    grants=grants,
                    operation_id=operation_id,
                    actor_id="system",
                    audit_action="items.grant",
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _grant_item_instances_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        grants: Sequence[Mapping[str, Any]],
        operation_id: str,
        actor_id: str,
        audit_action: str,
    ) -> dict[str, Any]:
        """Grant structured items using the caller's existing transaction."""

        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("物品授予缺少幂等操作 ID")
        request_grants: list[dict[str, Any]] = []
        for raw in grants:
            if not isinstance(raw, Mapping):
                raise ValueError("物品授予计划格式无效")
            item_id = str(raw.get("item_id") or "").strip()
            owner_ref = str(raw.get("owner_ref") or "").strip()
            quantity = int(raw.get("quantity", 0) or 0)
            if not item_id or not owner_ref or quantity <= 0:
                raise ValueError("物品授予必须包含所有者、名称与正整数数量")
            requested_owner_type = str(
                raw.get("owner_type") or (
                    "actor" if owner_ref.startswith("public:actor:")
                    else "character"
                )
            ).strip().lower()
            request_grants.append(
                {
                    "owner_type": requested_owner_type,
                    "owner_ref": owner_ref,
                    "item_id": item_id,
                    "quantity": quantity,
                    "quality": str(raw.get("quality") or "standard"),
                    "durability": max(0, int(raw.get("durability", 0) or 0)),
                    "charges": max(0, int(raw.get("charges", 0) or 0)),
                    "binding": str(raw.get("binding") or "none"),
                    "container": str(raw.get("container") or ""),
                    "source": str(raw.get("source") or "grant"),
                    "state": dict(raw.get("state") or {})
                    if isinstance(raw.get("state"), Mapping)
                    else {},
                }
            )
        request = {
            "grant": True,
            "session_id": session_id,
            "grants": request_grants,
        }
        input_hash = self._items_input_hash(request)
        existing = connection.execute(
            """
            SELECT input_hash, result_json FROM operation_commits
            WHERE operation_id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        ).fetchone()
        if existing:
            if str(existing["input_hash"] or "") != input_hash:
                raise DatabaseConflictError(
                    "幂等操作 ID 已用于另一份物品授予计划"
                )
            payload = json_load(existing["result_json"], {})
            payload["replayed"] = True
            return payload
        self._assert_session_writable(connection, session_id)
        normalized: list[dict[str, Any]] = []
        for item in request_grants:
            owner = self._resolve_item_owner_locked(
                connection,
                session_id=session_id,
                owner_ref=str(item["owner_ref"]),
                owner_type=str(item["owner_type"]),
            )
            normalized.append(
                {
                    **item,
                    "owner_type": owner["owner_type"],
                    "owner_ref": owner["owner_ref"],
                    "actor_id": owner["actor_id"],
                }
            )
        if not request_grants:
            result = {
                "ok": True,
                "granted": [],
                "operation_id": operation_id,
                "replayed": False,
            }
            now = utc_now()
            connection.execute(
                """
                INSERT INTO operation_commits(
                    operation_id, session_id, input_hash, status,
                    result_json, rollback_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                """,
                (
                    operation_id, session_id, input_hash,
                    json_dump(result), now, now,
                ),
            )
            return result
        now = utc_now()
        granted: list[dict[str, Any]] = []
        for item in normalized:
            connection.execute(
                """
                INSERT INTO item_instances(
                    id, session_id, owner_type, owner_ref, actor_id, item_id,
                    quantity, quality, durability, charges, binding,
                    container, source, state_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, owner_ref, item_id, container)
                DO UPDATE SET quantity = quantity + excluded.quantity,
                              updated_at = excluded.updated_at
                """,
                (
                    new_id("item_instance"),
                    session_id,
                    item["owner_type"],
                    item["owner_ref"],
                    item["actor_id"],
                    item["item_id"],
                    item["quantity"],
                    item["quality"],
                    item["durability"],
                    item["charges"],
                    item["binding"],
                    item["container"],
                    item["source"],
                    json_dump(item["state"]),
                    now,
                    now,
                ),
            )
            granted.append(
                {
                    key: value
                    for key, value in item.items()
                    if key != "actor_id"
                }
            )
        result = {
            "ok": True,
            "granted": granted,
            "operation_id": operation_id,
            "replayed": False,
        }
        connection.execute(
            """
            INSERT INTO operation_commits(
                operation_id, session_id, input_hash, status,
                result_json, rollback_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
            """,
            (
                operation_id,
                session_id,
                input_hash,
                json_dump(result),
                now,
                now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id,
            audit_action,
            operation_id,
            {"grants": normalized},
        )
        self._emit_item_visual_event_locked(
            connection,
            session_id=session_id,
            operation_id=operation_id,
            action="grant",
            created_at=now,
        )
        self._enqueue_storage_sync(connection, [session_id], "sync")
        return result

    def _require_running_asset_action(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        """玩家资产动作（使用/赠予/购买）只允许在运行态执行。"""
        self._assert_session_writable(connection, session_id)
        row = connection.execute(
            "SELECT state FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise DatabaseNotFoundError("会话不存在")
        if str(row["state"]) != "running":
            raise InvalidTransitionError(
                "副本不在运行状态，无法操作道具"
            )

    async def consume_item_instances(
        self,
        *,
        session_id: str,
        owner_ref: str,
        items: Mapping[str, int],
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._consume_item_instances,
            session_id,
            owner_ref,
            dict(items),
            reason,
            operation_id,
        )

    def _consume_item_instances(
        self,
        session_id: str,
        owner_ref: str,
        items: Mapping[str, int],
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._consume_item_instances_locked(
                    connection,
                    session_id=session_id,
                    owner_ref=owner_ref,
                    items=items,
                    reason=reason,
                    operation_id=operation_id,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _consume_item_instances_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        owner_ref: str,
        items: Mapping[str, int],
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """在调用方事务内消耗结构化物品（与回合提交共用同一事务）。"""
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("物品消耗缺少幂等操作 ID")
        owner_ref = str(owner_ref or "").strip()
        if not owner_ref:
            raise ValueError("物品消耗所有者不能为空")
        normalized_items: dict[str, int] = {}
        for key, value in (items or {}).items():
            item_id = str(key or "").strip()
            try:
                quantity = int(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("物品消耗数量必须是正整数") from exc
            if not item_id or quantity <= 0:
                raise ValueError("物品消耗必须包含名称与正整数数量")
            normalized_items[item_id] = quantity
        items = normalized_items
        if not items:
            raise ValueError("物品消耗计划不能为空")
        input_hash = self._items_input_hash(
            {
                "consume": True,
                "owner": owner_ref,
                "items": items,
                "reason": reason,
            }
        )
        existing = connection.execute(
            """
            SELECT input_hash, result_json FROM operation_commits
            WHERE operation_id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        ).fetchone()
        if existing:
            if str(existing["input_hash"] or "") != input_hash:
                raise DatabaseConflictError(
                    "幂等操作 ID 已用于另一份物品消耗计划"
                )
            payload = json_load(existing["result_json"], {})
            payload["replayed"] = True
            return payload
        self._require_running_asset_action(connection, session_id)
        owner = self._resolve_item_owner_locked(
            connection,
            session_id=session_id,
            owner_ref=owner_ref,
            owner_type=(
                "actor" if owner_ref.startswith("public:actor:") else "character"
            ),
        )
        owner_ref = str(owner["owner_ref"])
        now = utc_now()
        consumed: list[str] = []
        for item_id, quantity in sorted(items.items()):
            row = connection.execute(
                """
                SELECT id, quantity FROM item_instances
                WHERE session_id = ? AND owner_ref = ? AND item_id = ?
                  AND container = ''
                """,
                (session_id, owner_ref, item_id),
            ).fetchone()
            if not row or int(row["quantity"]) < quantity:
                raise ValueError(f"物品不足：{item_id}")
            remaining = int(row["quantity"]) - quantity
            if remaining <= 0:
                connection.execute(
                    "DELETE FROM item_instances WHERE id = ?",
                    (row["id"],),
                )
            else:
                connection.execute(
                    """
                    UPDATE item_instances SET quantity = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (remaining, now, row["id"]),
                )
            consumed.append(f"{item_id} x{quantity}")
        connection.execute(
            """
            INSERT INTO operation_commits(
                operation_id, session_id, input_hash, status,
                result_json, rollback_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
            """,
            (
                operation_id,
                session_id,
                input_hash,
                json_dump({"ok": True, "consumed": consumed, "operation_id": operation_id}),
                now,
                now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            "system",
            "items.consume",
            operation_id,
            {"owner": owner_ref, "items": items, "reason": reason},
        )
        self._emit_item_visual_event_locked(
            connection,
            session_id=session_id,
            operation_id=operation_id,
            action="consume",
            created_at=now,
        )
        self._enqueue_storage_sync(connection, [session_id], "sync")
        return {
            "ok": True,
            "consumed": consumed,
            "operation_id": operation_id,
            "replayed": False,
        }

    @staticmethod
    def _items_input_hash(payload: Mapping[str, Any]) -> str:
        import hashlib
        import json as _json

        material = _json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
