from __future__ import annotations

from ..database_support import *
from ..constants import PLUGIN_VERSION


class AdminOverviewRepositoryMixin:
    async def restore_owner_control(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
    ) -> int:
        """管理员/DM 恢复角色原玩家控制权（撤销全部活跃委托）。"""
        return await self._run(
            self._restore_owner_control, session_id, participant_id, actor_id
        )

    def _restore_owner_control(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.restore_owner",
                    participant_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def overview(self) -> dict[str, Any]:
        return await self._run(self._overview)

    async def global_token_usage(self, window_seconds: int) -> int:
        """全副本滚动窗口内的已完成 Token 用量合计（0.12.0-A3）。"""
        return await self._run(self._global_token_usage, window_seconds)

    def _global_token_usage(self, window_seconds: int) -> int:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=max(1, int(window_seconds)))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(total_tokens), 0)
                    FROM token_usage
                    WHERE status = 'completed' AND created_at >= ?
                    """,
                    (cutoff,),
                ).fetchone()[0]
            )

    def _overview(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                "worlds": connection.execute(
                    "SELECT COUNT(*) FROM worlds WHERE archived = 0"
                ).fetchone()[0],
                "sessions": connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                "running": connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE state = 'running'"
                ).fetchone()[0],
                "players": connection.execute(
                    """
                    SELECT COUNT(*) FROM participants
                    WHERE participation_status IN (
                        'reserved', 'active', 'standby', 'away'
                    )
                    """
                ).fetchone()[0],
                "memories": connection.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0],
                "snapshots": connection.execute(
                    "SELECT COUNT(*) FROM snapshots"
                ).fetchone()[0],
                "preparing": connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions WHERE state = 'preparing'
                    """
                ).fetchone()[0],
                "open_votes": connection.execute(
                    """
                    SELECT COUNT(*) FROM group_votes WHERE status = 'open'
                    """
                ).fetchone()[0],
                "active_timers": connection.execute(
                    """
                    SELECT COUNT(*) FROM timer_instances
                    WHERE status = 'active'
                    """
                ).fetchone()[0],
                "pending_deliveries": connection.execute(
                    "SELECT COUNT(*) FROM delivery_outbox WHERE status='pending'"
                ).fetchone()[0],
            }
            catalog_size = (
                self.path.stat().st_size if self.path.exists() else 0
            )
            instance_paths = list(
                (self.data_dir / "groups").glob(
                    "*/stories/*/instance.sqlite3"
                )
            )
            instance_size = sum(
                item.stat().st_size
                for item in instance_paths
                if item.is_file()
            )
            storage_errors = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM story_storage
                    WHERE sync_status = 'error'
                    """
                ).fetchone()[0]
            )
            # 0.12.0-A3：近 24h 审计统计（供总览「完整性 / 群内指令」）。
            cutoff_24h = (
                datetime.now(timezone.utc) - timedelta(hours=24)
            ).isoformat(timespec="seconds")
            audit_total = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs WHERE created_at >= ?
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            audit_failed = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ?
                      AND (
                          action LIKE '%.failed'
                          OR action LIKE '%denied'
                          OR detail_json LIKE '%"error"%'
                      )
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            relaxed_hits = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ? AND action LIKE '%relaxed%'
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            invalid_transitions = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM audit_logs
                    WHERE created_at >= ?
                      AND action IN ('state.failed', 'transition.failed')
                    """,
                    (cutoff_24h,),
                ).fetchone()[0]
            )
            database_ok = bool(
                connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()[0]
                == "ok"
            ) and storage_errors == 0
            return {
                "counts": counts,
                "database_size": catalog_size + instance_size,
                "catalog_size": catalog_size,
                "instance_size": instance_size,
                "instance_database_count": len(instance_paths),
                "storage_errors": storage_errors,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "database_ok": database_ok,
                # 0.12.0-A3：运行完整性（WebUI 总览「运行完整性」卡片）。
                "integrity": {
                    "schema_version": DATABASE_SCHEMA_VERSION,
                    "database_ok": database_ok,
                    "storage_errors": storage_errors,
                    "recovery_points": counts["snapshots"],
                    "failed_operations_24h": audit_failed,
                    "invalid_transitions_24h": invalid_transitions,
                },
                # 0.12.0-A3：群内指令统计（WebUI 总览「群内指令」卡片）。
                "commands": {
                    "command_count_24h": audit_total,
                    "success_rate": (
                        round(
                            (1 - audit_failed / audit_total) * 100, 1
                        )
                        if audit_total
                        else 100.0
                    ),
                    "relaxed_parse_hits_24h": relaxed_hits,
                },
            }
