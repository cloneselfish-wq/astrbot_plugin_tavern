from .common import *

class DatabaseCoreMixin:
    """SQLite persistence with short, explicit transactions.

    A fresh connection is used for each operation so methods can safely run in
    worker threads. Per-session async locks in the engine serialize story turns;
    optimistic revisions provide a second line of defense.
    """

    # C6：同步触发不再使用方法名白名单，改为「写事务实际触碰到的表」。
    # 以下为玩家可见会话状态表；命中后进入 storage_sync_outbox。
    _SESSION_TABLES = frozenset(
        {
            "sessions",
            "players",
            "participants",
            "events",
            "memories",
            "memory_governance",
            "snapshots",
            "session_characters",
            "character_card_drafts",
            "character_runtime_states",
            "item_instances",
            "economy_state",
            "economy_currencies",
            "economy_wallets",
            "economy_transactions",
            "choice_sets",
            "group_votes",
            "rolls",
            "session_rule_states",
            "story_ledger",
            "scene_clocks",
            "timer_instances",
            "timer_policies",
            "token_usage",
            "story_storage",
            "session_archives",
            "delivery_outbox",
            # D1 Schema 20：副本事件、增量投影与主动投递/终局状态表。
            "session_events",
            "projection_checkpoints",
            "delivery_targets",
            "character_capabilities",
            "character_resources",
            "actor_fate_states",
            "actor_fate_transitions",
            "rescue_windows",
            "terminal_receipts",
            "session_finalizations",
            "player_tendency_evidence",
            "player_tendency_profiles",
            "npc_knowledge_evidence",
            "actors",
            "ai_companion_instances",
            "ai_companion_decision_receipts",
            "session_opening_decisions",
            "principal_bindings",
            "room_invites",
            "choice_recovery_receipts",
            "world_module_runtime_status",
            "session_narrative_styles",
            "gameplay_states",
        }
    )
    # 引擎内部簿记表不进入玩家可见副本同步（与 C5 行为一致，
    # 避免每回合多次回执更新触发整库拷贝）。
    _BOOKKEEPING_TABLES = frozenset(
        {
            "audit_logs",
            "operation_receipts",
            "operation_commits",
            "storage_sync_outbox",
            "event_outbox",
            "author_jobs",
            "world_analysis_artifacts",
            "turn_delivery_runs",
            "turn_delivery_parts",
            "gameplay_receipts",
        }
    )
    _SYNC_TRIGGER_TABLES = (
        _SESSION_TABLES - _BOOKKEEPING_TABLES
    )
    # 少量旧路径（快照存档/副本完结）在写事务后额外生成保存档；
    # 这里是行为映射而非同步白名单，同步触发仍由表驱动。
    _LEGACY_ARCHIVE_KINDS = {
        "_create_snapshot": "archive_save",
        "_finalize_session": "archive_save",
    }

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Tests, migrations and startup recovery retain immediate drain.
        # The AstrBot lifecycle flips this after initialization, when the
        # lease-based storage worker is running.
        self.defer_storage_sync = False
        # 沿用 catalog_d1.sqlite3，按版本顺序执行受控迁移。
        self.path = self.data_dir / "catalog_d1.sqlite3"
        self.last_schema_migration = None
        self.legacy_reset_result = LegacyResetResult(status="clean")
        self._prepare_database()
        self._schema_lock = threading.Lock()
        self._initialize()
        try:
            self._recover_interrupted_turn_deliveries()
        except Exception:
            pass
        self.storage = InstanceStorage(
            data_dir=self.data_dir,
            catalog_path=self.path,
            connect_catalog=self._connect,
            schema_version=DATABASE_SCHEMA_VERSION,
        )
        self.storage.bootstrap()
        # C6：启动时回收过期操作租约并消费同步出站队列（崩溃/停机恢复）。
        try:
            self._recover_expired_operations(utc_now())
        except Exception:
            pass
        try:
            self._drain_storage_outbox()
        except Exception:
            pass

    def _prepare_database(self) -> None:
        if not self.path.exists():
            if (self.data_dir / "groups").exists():
                try:
                    self.legacy_reset_result = backup_and_remove_legacy(
                        self.data_dir, self.path
                    )
                except LegacyResetError as exc:
                    raise RuntimeError(str(exc)) from exc
            return
        try:
            current = _read_schema_version(self.path)
            if current == DATABASE_SCHEMA_VERSION:
                return
            if current == 29 and DATABASE_SCHEMA_VERSION == 30:
                from .schema_rc10 import migrate_schema_29_to_30

                self.last_schema_migration = migrate_schema_29_to_30(
                    self.data_dir,
                    self.path,
                )
                return
            if current > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库来自更新版本（Schema {current}），当前版本只接受 "
                    f"Schema {DATABASE_SCHEMA_VERSION}。系统未修改数据库。"
                )
        except RuntimeError as exc:
            if current == 29 and DATABASE_SCHEMA_VERSION == 30:
                raise RuntimeError(
                    "Schema 29 到 Schema 30 的只读归档迁移失败；"
                    "系统保留原数据库且未启动新版本。"
                ) from exc
            if "更新版本" in str(exc):
                raise
            current = 0
        except (sqlite3.Error, OSError, TypeError, ValueError):
            current = 0

        try:
            self.legacy_reset_result = backup_and_remove_legacy(self.data_dir, self.path)
        except LegacyResetError as exc:
            raise RuntimeError(str(exc)) from exc

    def _connect(self) -> _ManagedConnection:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
            factory=_TrackingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return _ManagedConnection(connection)

    # ── v1.0-A2（结构优化）：统一 DB 访问助手 ──────────────────────────
    # 新增只读聚合模块优先使用这两个助手，
    # 收敛「自行 with self._connect()」的散点写法；语义与既有 _run 一致：
    # 在 worker 线程执行、读操作退出即提交（无副作用）、写操作显式提交。
    async def execute_read(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        """执行只读 SQL 并返回 dict 行列表（在 worker 线程中运行）。"""
        return await self._run(self._execute_read, sql, tuple(params))

    def _execute_read(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(str(sql), params).fetchall()
        return [dict(row) for row in rows]

    async def execute_write(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> int:
        """执行单条写入 SQL（隐式提交）；返回受影响行数。"""
        return await self._run(self._execute_write, sql, tuple(params))

    def _execute_write(
        self,
        sql: str,
        params: Sequence[Any],
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(str(sql), params)
        return cursor.rowcount if cursor.rowcount >= 0 else 0
