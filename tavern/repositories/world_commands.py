"""世界命令仓库。

把 TWP 纯函数命令层接入 SQLite：幂等键、预期会话修订、审计日志、
事件派发与 ``runtime`` 权威写入。
"""
from __future__ import annotations

import sqlite3
from ..database_support import *
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..twp.commands import (
    WorldCommandError,
    apply_command,
    list_commands,
    preview_command,
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _index_by(defs: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in _sequence(defs):
        if isinstance(raw, Mapping) and raw.get("id"):
            result[str(raw["id"])] = dict(raw)
    return result


class WorldCommandRepositoryMixin:
    async def world_command_catalog(self) -> list[dict[str, Any]]:
        return await self._run(self._world_command_catalog)

    def _world_command_catalog(self) -> list[dict[str, Any]]:
        from ..protocol.commands import command_catalog

        return command_catalog()

    async def world_runtime_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._world_runtime_state, session_id)

    def _world_runtime_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT world_id, world_state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本不存在")
            world_id = str(row["world_id"] or "")
            world = connection.execute(
                "SELECT * FROM worlds WHERE id = ?", (world_id,)
            ).fetchone()
            world_snapshot = (
                self._world(world) if world is not None else {}
            )
            state = json_load(row["world_state_json"], {})
            from ..protocol.projections import project_runtime
            from ..protocol.runtime import runtime_from_state

            runtime = runtime_from_state(state)
            projection = project_runtime(
                world_snapshot,
                state,
                viewer_role="dm",
                purpose="web",
            )
            return {
                "session_id": session_id,
                "world_slug": world_snapshot.get("slug", ""),
                "artifact_id": str(runtime.get("artifact_id") or world_snapshot.get("artifact_id") or ""),
                "runtime": projection,
                "projection": projection,
                "revision": int(runtime.get("revision", 0) or 0),
            }

    async def world_ending_readiness(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._world_ending_readiness, session_id)

    def _world_ending_readiness(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT world_id, world_state_json, state, turn_no
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本不存在")
            world = self._world_snapshot_for(connection, str(row["world_id"] or ""))
            state = json_load(row["world_state_json"], {})
            session_projection = {
                "id": session_id,
                "state": str(row["state"] or ""),
                "turn_no": int(row["turn_no"] or 0),
                "opening_committed": str(row["state"] or "") in {
                    "running",
                    "paused",
                },
            }
            party = self._ending_party_projection(connection, session_id)
            vote = self._ending_vote_projection(connection, session_id)
        from ..twp.endings import ending_readiness

        from ..protocol.runtime import flatten_runtime, runtime_from_state

        return ending_readiness(
            flatten_runtime(runtime_from_state(state)),
            world,
            session=session_projection,
            party=party,
            vote=vote,
        )

    def _ending_party_projection(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        """队伍结局投影：与 party_fate_aggregate 同口径（D1_PLAN 18 §6）。"""

        rows = connection.execute(
            """
            SELECT character_id, state, terminal, can_act
            FROM actor_fate_states
            WHERE session_id = ?
            ORDER BY updated_at, character_id
            """,
            (session_id,),
        ).fetchall()
        members = [
            {
                "ref": str(row["character_id"]),
                "state": str(row["state"] or ""),
                "terminal": bool(row["terminal"]),
                "can_act": bool(row["can_act"]),
            }
            for row in rows
        ]
        member_count = len(members)
        dead_count = sum(1 for member in members if member["terminal"])
        living_count = member_count - dead_count
        incapacitated_count = sum(
            1
            for member in members
            if not member["terminal"] and not member["can_act"]
        )
        return {
            "member_count": member_count,
            "living_count": living_count,
            "dead_count": dead_count,
            "incapacitated_count": incapacitated_count,
            "members": members,
        }

    def _ending_vote_projection(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        """表决结局投影：取最近的公开表决结果（无表决则空）。"""

        row = connection.execute(
            """
            SELECT winner_key FROM group_votes
            WHERE session_id = ? AND decision_status = 'decided'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT winner_key FROM group_votes
                WHERE session_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return {"choice": str(row["winner_key"] or "") if row else ""}

    async def world_command_preview(
        self,
        session_id: str,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._world_command_preview, session_id, command
        )

    def _world_command_preview(
        self,
        session_id: str,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT world_id, world_state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本不存在")
            world = self._world_snapshot_for(connection, str(row["world_id"] or ""))
            state = json_load(row["world_state_json"], {})
        preview = preview_command(world, state, dict(command))
        if preview.get("ok"):
            from ..protocol.commands import build_plan, normalize_envelope

            envelope = normalize_envelope(
                command,
                artifact_id=str(world.get("artifact_id") or ""),
            )
            preview["command_envelope"] = envelope.export()
            preview["plan"] = build_plan(world, state, envelope).export()
            from ..twp.cascades import cascade_preview

            preview["cascades"] = cascade_preview(world, preview.get("events") or [])
        return preview

    async def execute_world_command(
        self,
        session_id: str,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._execute_world_command, session_id, command
        )

    def _execute_world_command(
        self,
        session_id: str,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        command = dict(command)
        idempotency_key = str(command.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise WorldCommandError("世界命令缺少幂等键")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT world_id, world_state_json, revision
                    FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在")
                # D1-RUN-013：已归档副本拒绝一切世界命令写入。
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT result_json, status FROM operation_commits
                    WHERE operation_id = ? AND session_id = ?
                    """,
                    (idempotency_key, session_id),
                ).fetchone()
                if existing:
                    connection.execute("COMMIT")
                    payload = json_load(existing["result_json"], {})
                    payload["replayed"] = True
                    payload["status"] = existing["status"]
                    return payload
                world = self._world_snapshot_for(connection, str(row["world_id"] or ""))
                state = json_load(row["world_state_json"], {})
                state_before = deepcopy(state)
                result = apply_command(
                    world,
                    state,
                    command,
                    root_operation_id=idempotency_key,
                )
                # B2（A7）：成长里程碑解锁时按世界包声明真实授予能力/物品/知识。
                awards = self._apply_milestone_awards(
                    connection,
                    world,
                    result,
                    command,
                    idempotency_key,
                    session_id,
                )
                if awards:
                    result["state"] = result["state"]
                    result["awards"] = awards
                # B2（A9）：跨命令自动事件链——同一根操作 ID 下递归应用下游命令。
                from ..twp.cascades import apply_cascades

                cascade_result = apply_cascades(
                    world,
                    result["state"],
                    result["events"],
                    root_operation_id=idempotency_key,
                    operator=str(command.get("operator") or "system"),
                    cause=result["summary"],
                )
                result["state"] = cascade_result["state"]
                result["events"] = list(result["events"]) + cascade_result["events"]
                result["cascades"] = cascade_result["applied"]
                now = utc_now()
                # D1-RUN-005/007：真实回滚计划（逆操作可回放）随提交落库。
                rollback_plan = self._build_rollback_plan(
                    operation_id=idempotency_key,
                    command=command,
                    before_state=state_before,
                    after_state=result["state"],
                    changes=result["changes"],
                    events=result["events"],
                )
                connection.execute(
                    """
                    UPDATE sessions SET world_state_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(result["state"]), now, session_id),
                )
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
                    """,
                    (
                        idempotency_key,
                        session_id,
                        self._command_input_hash(command),
                        json_dump(
                            {
                                "ok": True, "session_id": session_id, "summary": result["summary"],
                                "revision": result["revision"],
                                "events": result["events"],
                                "cascades": result.get("cascades") or [],
                                "awards": result.get("awards") or [],
                            }
                        ),
                        json_dump(rollback_plan),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    str(command.get("operator") or "system"),
                    f"world_command.{command.get('domain')}.{command.get('action')}",
                    idempotency_key,
                    {
                        "targets": command.get("targets"),
                        "reason": command.get("reason"),
                        "summary": result["summary"],
                        "revision_after": result["revision"],
                        "events": [e.get("type") for e in result["events"]],
                    },
                )
                connection.execute("COMMIT")
                return {
                    "ok": True,
                    "session_id": session_id,
                    "summary": result["summary"],
                    "revision": result["revision"],
                    "events": result["events"],
                    "changes": result["changes"],
                    "affected": result["affected"],
                    "operation_id": idempotency_key,
                    "replayed": False,
                    "status": "completed",
                    "awards": result.get("awards") or [],
                    "cascades": result.get("cascades") or [],
                }
            except WorldCommandError:
                connection.execute("ROLLBACK")
                raise
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _apply_milestone_awards(
        self,
        connection: sqlite3.Connection,
        world: Mapping[str, Any],
        result: Mapping[str, Any],
        command: Mapping[str, Any],
        operation_id: str,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """B2（A7）：按世界包 progression.milestone_awards 真实授予。

        在 execute_world_command 的同一事务内执行；能力/物品写入实例表，
        知识写入运行态 revealed 列表；重复提交由同一 operation_id 幂等。
        """
        if str(command.get("domain") or "") != "progression" or str(command.get("action") or "") != "unlock_milestone":
            return []
        rules = _mapping(world.get("rules"))
        module = _mapping(rules.get("progression"))
        tracks = _index_by(module.get("tracks", []))
        target = str((command.get("targets") or [""])[0])
        track = tracks.get(target)
        if not track:
            return []
        milestone = str(
            (command.get("payload") or {}).get("milestone")
            or ((command.get("targets") or ["", ""])[1] if len(command.get("targets") or []) > 1 else "")
        )
        milestone_awards = _sequence(track.get("milestone_awards"))
        entry = next(
            (item for item in milestone_awards if str(item.get("milestone")) == milestone),
            None,
        )
        if not entry:
            return []
        owner = str((command.get("payload") or {}).get("owner") or "") or f"party:{session_id}"
        now = utc_now()
        applied: list[dict[str, Any]] = []
        state = dict(result.get("state") or {})
        from ..protocol.runtime import flatten_runtime, runtime_from_state

        runtime_root = runtime_from_state(state)
        runtime = flatten_runtime(runtime_root)
        for award in _sequence(entry.get("awards")):
            if not isinstance(award, Mapping):
                continue
            award_type = str(award.get("type") or "")
            ref = str(award.get("ref") or "")
            if not ref:
                continue
            if award_type == "capability":
                connection.execute(
                    """
                    INSERT OR IGNORE INTO actor_capability_instances(
                        id, session_id, actor_ref, capability_ref,
                        definition_version, source_ref, state_json,
                        persistence_scope, available, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, '{}', 'campaign', 1, ?, ?)
                    """,
                    (
                        new_id("capability_instance"), session_id, owner, ref,
                        f"award:{operation_id}", now, now,
                    ),
                )
                applied.append({"type": "capability", "ref": ref, "owner": owner})
            elif award_type == "item":
                quantity = max(1, int(award.get("qty", 1) or 1))
                connection.execute(
                    """
                    INSERT INTO item_instances(
                        id, session_id, owner_type, owner_ref, item_id,
                        quantity, quality, durability, charges, binding,
                        container, source, state_json, created_at, updated_at
                    ) VALUES (?, ?, 'party', ?, ?, ?, 'standard', 0, 0, 'none',
                              '', ?, '{}', ?, ?)
                    ON CONFLICT(session_id, owner_ref, item_id, container)
                    DO UPDATE SET quantity = quantity + excluded.quantity,
                                  updated_at = excluded.updated_at
                    """,
                    (
                        new_id("item_instance"), session_id, owner, ref, quantity,
                        f"award:{operation_id}", now, now,
                    ),
                )
                applied.append({"type": "item", "ref": ref, "quantity": quantity, "owner": owner})
            elif award_type == "fact":
                knowledge = dict(runtime.get("knowledge") or {})
                revealed = list(knowledge.get("revealed") or [])
                if ref not in revealed:
                    revealed.append(ref)
                knowledge["revealed"] = revealed
                runtime["knowledge"] = knowledge
                applied.append({"type": "fact", "ref": ref})
        if applied:
            from ..protocol.runtime import hydrate_runtime, store_runtime

            store_runtime(
                state,
                hydrate_runtime(
                    runtime,
                    artifact_id=str(runtime_root.get("artifact_id") or world.get("artifact_id") or ""),
                    enabled_modules=list(runtime_root.get("enabled_modules") or []),
                ),
            )
            result["state"] = state
        return applied

    def _world_snapshot_for(
        self,
        connection: sqlite3.Connection,
        world_id: str,
    ) -> dict[str, Any]:
        world = connection.execute(
            "SELECT * FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()
        return self._world(world) if world is not None else {}

    @staticmethod
    def _command_input_hash(command: Mapping[str, Any]) -> str:
        import hashlib
        import json as _json

        material = _json.dumps(
            command,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_rollback_plan(
        *,
        operation_id: str,
        command: Mapping[str, Any],
        before_state: Mapping[str, Any],
        after_state: Mapping[str, Any],
        changes: Sequence[Mapping[str, Any]],
        events: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """生成世界命令回滚计划（状态恢复式逆操作，D1-RUN-005/007）。

        每个运行态键记录 before/after 与确定性哈希；回滚时按逆序恢复
        before 值即可重放。事件与投递没有状态逆操作，列为
        irreversible_ops，由回执账本补偿而不是回滚领域状态。
        """

        import hashlib
        import json as _json

        def digest(value: Any) -> str:
            material = _json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            return hashlib.sha256(material.encode("utf-8")).hexdigest()

        from ..protocol.runtime import flatten_runtime, runtime_from_state

        before_flat = flatten_runtime(runtime_from_state(dict(before_state or {})))
        after_flat = flatten_runtime(runtime_from_state(dict(after_state or {})))
        entries: list[dict[str, Any]] = []
        for key in sorted(set(before_flat) | set(after_flat)):
            before_value = before_flat.get(key)
            after_value = after_flat.get(key)
            if before_value == after_value and key in before_flat and key in after_flat:
                continue
            if before_value == after_value:
                # 键仅在某一侧存在且值相同（如常量字段），无需逆操作。
                continue
            entries.append(
                {
                    "op": "restore_runtime_key",
                    "target_ref": key,
                    "before": before_value,
                    "after": after_value,
                }
            )
        change_rows = [
            dict(item)
            for item in changes
            if isinstance(item, Mapping)
        ]
        irreversible_ops = [
            {
                "index": index,
                "op": "emit_event",
                "target_ref": str(event.get("type") or ""),
                "reason": "事件与投递不可状态回滚，由回执账本补偿",
            }
            for index, event in enumerate(events)
            if isinstance(event, Mapping)
        ]
        return {
            "schema": "tavern-rollback-plan/1.0.0-rc10",
            "plan_id": f"rollback:{operation_id}",
            "operation_id": str(operation_id or ""),
            "command": {
                "domain": str(command.get("domain") or ""),
                "action": str(command.get("action") or ""),
            },
            "strategy": "state_restore",
            "reversible": bool(entries),
            "before_revision": int(before_flat.get("revision", 0) or 0),
            "after_revision": int(after_flat.get("revision", 0) or 0),
            "before_state_hash": digest(before_state),
            "after_state_hash": digest(after_state),
            "changes": change_rows,
            "entries": entries,
            "irreversible_ops": irreversible_ops,
        }


__all__ = ["WorldCommandRepositoryMixin"]
