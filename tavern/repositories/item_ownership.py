from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ..database_support import *
from ..item_catalog import normalize_item_instance


class ItemOwnershipRepositoryMixin:
    @classmethod
    def _resolve_item_owner_locked(
        cls,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        owner_ref: str,
        owner_type: str = "",
    ) -> dict[str, Any]:
        """Resolve a public AI actor ref without exposing its database id."""

        ref = str(owner_ref or "").strip()
        kind = str(owner_type or "").strip().lower()
        if not ref:
            raise ValueError("物品所有者不能为空")
        if kind and kind not in {"character", "party", "actor"}:
            raise ValueError(f"物品 owner_type 无效：{kind}")
        is_actor_ref = ref.startswith("public:actor:")
        if kind == "actor" or is_actor_ref:
            if kind == "party":
                raise ValueError("AI 队友物品不能登记为队伍公共物品")
            rows = connection.execute(
                """
                SELECT a.id, a.session_id, a.status, i.status AS instance_status
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                ORDER BY a.created_at, a.id
                """,
                (session_id,),
            ).fetchall()
            matches = [
                row
                for row in rows
                if cls._public_ai_actor_ref(row["id"]) == ref
            ]
            if len(matches) > 1:
                raise DatabaseConflictError(
                    "AI 队友公开引用发生冲突，已停止物品操作"
                )
            if not matches:
                raise InvalidTransitionError(
                    "AI 队友不存在、已退役或不属于当前副本"
                )
            actor = matches[0]
            if (
                str(actor["status"]) in {"retired", "archived"}
                or str(actor["instance_status"]) == "retired"
            ):
                raise InvalidTransitionError(
                    "AI 队友不存在、已退役或不属于当前副本"
                )
            return {
                "owner_type": "actor",
                "owner_ref": cls._public_ai_actor_ref(actor["id"]),
                "actor_id": str(actor["id"]),
            }
        return {
            "owner_type": kind or "character",
            "owner_ref": ref,
            "actor_id": None,
        }

    def _assert_receiver_allowed(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        owner_ref: str,
    ) -> None:
        """转赠目标必须存在且允许接收（在场/暂离/等待返场）。"""
        if not str(owner_ref or "").strip():
            raise ValueError("转赠目标为空")
        if str(owner_ref).startswith("public:actor:"):
            self._resolve_item_owner_locked(
                connection,
                session_id=session_id,
                owner_ref=owner_ref,
                owner_type="actor",
            )
            return
        row = connection.execute(
            """
            SELECT 1 FROM participants
            WHERE session_id = ? AND participation_status IN (
                'active', 'standby', 'away'
            ) AND (id = ? OR group_user_id = ?)
            LIMIT 1
            """,
            (session_id, str(owner_ref), str(owner_ref)),
        ).fetchone()
        if not row:
            raise InvalidTransitionError("转赠目标不在场或不允许接收")

    async def transfer_item_instances(
        self,
        *,
        session_id: str,
        from_owner: str,
        to_owner: str,
        item_id: str,
        quantity: int = 1,
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._transfer_item_instances,
            session_id,
            from_owner,
            to_owner,
            item_id,
            quantity,
            reason,
            operation_id,
        )

    def _transfer_item_instances(
        self,
        session_id: str,
        from_owner: str,
        to_owner: str,
        item_id: str,
        quantity: int,
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._transfer_item_instances_locked(
                    connection,
                    session_id=session_id,
                    from_owner=from_owner,
                    to_owner=to_owner,
                    item_id=item_id,
                    quantity=quantity,
                    reason=reason,
                    operation_id=operation_id,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _transfer_item_instances_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        from_owner: str,
        to_owner: str,
        item_id: str,
        quantity: int,
        reason: str,
        operation_id: str,
    ) -> dict[str, Any]:
        """在调用方事务内转移结构化物品（含状态/接收方守卫）。"""
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("物品转移缺少幂等操作 ID")
        from_owner = str(from_owner or "").strip()
        to_owner = str(to_owner or "").strip()
        item_id = str(item_id or "").strip()
        if not from_owner or not to_owner:
            raise ValueError("转赠双方不能为空")
        if not item_id:
            raise ValueError("物品转移名称不能为空")
        if from_owner == to_owner:
            raise InvalidTransitionError("不能把道具转赠给自己")
        try:
            quantity = int(quantity)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("物品转移数量必须是正整数") from exc
        if quantity <= 0:
            raise ValueError("物品转移数量必须是正整数")
        input_hash = self._items_input_hash(
            {
                "transfer": True,
                "from": from_owner,
                "to": to_owner,
                "item": item_id,
                "qty": quantity,
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
                    "幂等操作 ID 已用于另一份物品转移计划"
                )
            payload = json_load(existing["result_json"], {})
            payload["replayed"] = True
            return payload
        self._require_running_asset_action(connection, session_id)
        from_owner_row = self._resolve_item_owner_locked(
            connection,
            session_id=session_id,
            owner_ref=from_owner,
            owner_type=(
                "actor" if from_owner.startswith("public:actor:") else "character"
            ),
        )
        to_owner_row = self._resolve_item_owner_locked(
            connection,
            session_id=session_id,
            owner_ref=to_owner,
            owner_type=(
                "actor" if to_owner.startswith("public:actor:") else "character"
            ),
        )
        from_owner = str(from_owner_row["owner_ref"])
        to_owner = str(to_owner_row["owner_ref"])
        self._assert_receiver_allowed(connection, session_id, to_owner)
        row = connection.execute(
            """
            SELECT * FROM item_instances
            WHERE session_id = ? AND owner_ref = ? AND item_id = ?
              AND container = ''
            """,
            (session_id, from_owner, item_id),
        ).fetchone()
        if not row or int(row["quantity"]) < quantity:
            raise ValueError(f"物品不足：{item_id}")
        now = utc_now()
        target = connection.execute(
            """
            SELECT * FROM item_instances
            WHERE session_id=? AND owner_ref=? AND item_id=? AND container=''
            """,
            (session_id, to_owner, item_id),
        ).fetchone()
        state_fields = (
            "quality", "durability", "charges", "binding", "source",
            "state_json",
        )
        if target is not None and any(
            target[field] != row[field] for field in state_fields
        ):
            raise InvalidTransitionError(
                "接收方同名物品的品质或状态不同，为避免丢失数据已停止合并"
            )
        remaining = int(row["quantity"]) - quantity
        if remaining <= 0:
            connection.execute(
                "DELETE FROM item_instances WHERE id = ?", (row["id"],)
            )
        else:
            connection.execute(
                """
                UPDATE item_instances SET quantity = ?, updated_at = ?
                WHERE id = ?
                """,
                (remaining, now, row["id"]),
            )
        connection.execute(
            """
            INSERT INTO item_instances(
                id, session_id, owner_type, owner_ref, actor_id, item_id,
                quantity, quality, durability, charges, binding,
                container, source, state_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?)
            ON CONFLICT(session_id, owner_ref, item_id, container)
            DO UPDATE SET quantity = quantity + excluded.quantity,
                          updated_at = excluded.updated_at
            """,
            (
                new_id("item_instance"), session_id,
                to_owner_row["owner_type"], to_owner,
                to_owner_row["actor_id"], item_id,
                quantity, row["quality"], row["durability"], row["charges"],
                row["binding"], row["source"], row["state_json"], now, now,
            ),
        )
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
                json_dump({"ok": True, "operation_id": operation_id}),
                now,
                now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            "system",
            "items.transfer",
            operation_id,
            {"from": from_owner, "to": to_owner, "item": item_id, "quantity": quantity, "reason": reason},
        )
        self._emit_item_visual_event_locked(
            connection,
            session_id=session_id,
            operation_id=operation_id,
            action="transfer",
            created_at=now,
        )
        self._enqueue_storage_sync(connection, [session_id], "sync")
        return {
            "ok": True,
            "operation_id": operation_id,
            "replayed": False,
        }
