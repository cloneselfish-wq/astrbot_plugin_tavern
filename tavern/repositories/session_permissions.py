from __future__ import annotations

from .sessions_support import *


class SessionPermissionsRepositoryMixin:
    async def migrate_session_world(
        self,
        session_id: str,
        candidate_world_ref: str,
        actor_id: str,
        *,
        expected_revision: int,
        operation_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if confirmation != "MIGRATE_FROZEN_WORLD":
            raise ValueError(
                "显式迁移确认无效；请先备份并重新确认迁移冻结世界。"
            )
        result = await self._run(
            self._migrate_session_world,
            session_id,
            candidate_world_ref,
            actor_id,
            int(expected_revision),
            clean_text(operation_id, max_chars=160),
        )
        if result.get("status") == "completed":
            try:
                result["economy_seeded_count"] = int(
                    await self.ensure_economy_currencies(session_id)
                )
            except Exception as exc:
                result["economy_seed_error"] = {
                    "code": "economy.seed_failed",
                    "message": clean_text(str(exc), max_chars=300),
                    "retryable": True,
                }
        result["capability"] = await self.economy_capability(session_id)
        return result

    def _migrate_session_world(
        self,
        session_id: str,
        candidate_world_ref: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        if not operation_id:
            raise ValueError("世界迁移必须提供幂等 operation_id")
        from ..world_migration import (
            compare_world_contracts,
            create_world_migration_backup,
        )

        with self._connect() as connection:
            replay = connection.execute(
                """
                SELECT result_json FROM operation_receipts
                WHERE operation_id=? AND operation_type='world.snapshot_migrate'
                  AND status='completed'
                """,
                (operation_id,),
            ).fetchone()
            if replay:
                return json_load(replay["result_json"], {})
            session = connection.execute(
                "SELECT * FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            config = connection.execute(
                "SELECT * FROM instance_configs WHERE session_id=?",
                (session_id,),
            ).fetchone()
            candidate = connection.execute(
                """
                SELECT * FROM worlds
                WHERE (id=? OR slug=?) AND archived=0
                """,
                (candidate_world_ref, candidate_world_ref),
            ).fetchone()
        if not session:
            raise DatabaseNotFoundError("待迁移副本不存在")
        if not config:
            raise DatabaseNotFoundError("待迁移副本缺少冻结世界快照")
        if not candidate:
            raise DatabaseNotFoundError("候选世界包不存在或已归档")
        if session["state"] in {SESSION_RUNNING, SESSION_FINISHED}:
            raise InvalidTransitionError(
                "运行中或已结束副本不能原地迁移；请先暂停，或克隆到新 revision。"
            )
        if int(session["revision"]) != int(expected_revision):
            raise DatabaseConflictError(
                "副本状态已变化；系统未执行迁移，请刷新后重新确认。"
            )
        frozen = json_load(config["world_snapshot_json"], {})
        candidate_payload = self._world(candidate)
        comparison = compare_world_contracts(frozen, candidate_payload)
        if not comparison["safe_for_clone"]:
            codes = ", ".join(
                str(item.get("code") or "unknown")
                for item in comparison["blockers"]
            )
            raise DatabaseConflictError(
                f"候选世界与当前角色契约不兼容，未执行迁移：{codes}"
            )

        backup = create_world_migration_backup(
            self.path,
            backup_dir=self.data_dir / "world_migration_backups",
            session_id=session_id,
            candidate_world_ref=str(candidate["id"]),
        )
        now = utc_now()
        frozen_hash = hashlib.sha256(
            json_dump(frozen).encode("utf-8")
        ).hexdigest()
        candidate_hash = hashlib.sha256(
            json_dump(candidate_payload).encode("utf-8")
        ).hexdigest()
        result = {
            "schema": "tavern-world-migration-result/1.0.0-rc10",
            "status": "completed",
            "session_ref": _actor_principal_ref(session_id),
            "from_world_revision": int(config["world_revision"]),
            "to_world_revision": int(candidate["revision"]),
            "from_content_version": str(frozen.get("content_version") or ""),
            "to_content_version": str(
                candidate_payload.get("content_version") or ""
            ),
            "backup_ref": backup["backup_ref"],
            "backup_sha256": backup["sha256"],
            "warnings": list(comparison["warnings"]),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                current_config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if (
                    not current
                    or not current_config
                    or int(current["revision"]) != int(expected_revision)
                    or hashlib.sha256(
                        str(current_config["world_snapshot_json"]).encode("utf-8")
                    ).hexdigest()
                    != hashlib.sha256(
                        str(config["world_snapshot_json"]).encode("utf-8")
                    ).hexdigest()
                ):
                    raise DatabaseConflictError(
                        "备份后副本状态发生变化；系统已保留备份但未执行迁移。"
                    )
                phase = json_load(current_config["phase_meta_json"], {})
                phase = dict(phase) if isinstance(phase, Mapping) else {}
                history = list(phase.get("world_migrations") or [])
                history.append(
                    {
                        "operation_id": operation_id,
                        "from_revision": int(current_config["world_revision"]),
                        "to_revision": int(candidate["revision"]),
                        "backup_ref": backup["backup_ref"],
                        "migrated_at": now,
                    }
                )
                phase["world_migrations"] = history[-20:]
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id=?, revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (candidate["id"], now, session_id),
                )
                connection.execute(
                    """
                    UPDATE instance_configs SET
                        world_revision=?, world_snapshot_json=?,
                        ui_profile_json=?, time_rules_json=?,
                        phase_meta_json=?, updated_at=?
                    WHERE session_id=?
                    """,
                    (
                        int(candidate["revision"]),
                        json_dump(candidate_payload),
                        candidate["ui_profile_json"],
                        json_dump(world_time_rules(candidate_payload)),
                        json_dump(phase),
                        now,
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'world.snapshot_migrate', ?, ?,
                              'completed', 'committed', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(
                            {
                                "candidate_world_ref": str(candidate["id"]),
                                "expected_revision": expected_revision,
                                "frozen_hash": frozen_hash,
                                "candidate_hash": candidate_hash,
                            }
                        ),
                        json_dump(result),
                        candidate_hash,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "world.snapshot_migrated",
                    operation_id,
                    result,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result
