from .common import *
from ..repositories.configuration_revision_security import (
    sanitize_configuration_revisions,
)

class DatabaseMaintenanceMixin:
    @staticmethod
    def _sanitize_configuration_revisions(
        connection: sqlite3.Connection,
    ) -> dict[str, int]:
        return sanitize_configuration_revisions(
            connection,
            abandon_reserved=True,
        )

    @staticmethod
    def _ensure_storage_outbox_generations(
        connection: sqlite3.Connection,
    ) -> None:
        """幂等补齐 storage_sync_outbox 的代际列。

        旧 Schema 21 数据库升级时表已存在，``CREATE TABLE IF NOT EXISTS``
        不会补列；这里按列名探测后增量 ``ALTER TABLE``，保持 schema_version
        不变、向前兼容。
        """
        existing = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(storage_sync_outbox)"
            ).fetchall()
        }
        additions = (
            ("desired_generation", "INTEGER NOT NULL DEFAULT 1"),
            ("leased_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("completed_generation", "INTEGER NOT NULL DEFAULT 0"),
        )
        for name, definition in additions:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE storage_sync_outbox ADD COLUMN {name} {definition}"
                )

    @staticmethod
    def _stable_key(value: Any, fallback: str = "") -> str:
        text = " ".join(str(value or "").strip().casefold().split())
        if text:
            return text[:160]
        return str(fallback or uuid.uuid4().hex)[:160]

    def _backfill_world_revisions(self, connection: sqlite3.Connection) -> None:
        """Give pre-v0.11 worlds an immutable first rule revision and snapshot."""

        now = utc_now()
        for row in connection.execute(
            "SELECT * FROM worlds ORDER BY display_no ASC, id ASC"
        ).fetchall():
            exists = connection.execute(
                """
                SELECT 1 FROM world_snapshots
                WHERE world_id=? AND world_revision=?
                """,
                (row["id"], row["revision"]),
            ).fetchone()
            if exists:
                continue
            self._persist_world_revision(
                connection, row, self._world(row), now
            )

    @staticmethod
    def _candidate_session_values(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) == "session_id" and item:
                    result.add(str(item))
                result.update(DatabaseMaintenanceMixin._candidate_session_values(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                result.update(DatabaseMaintenanceMixin._candidate_session_values(item))
        elif isinstance(value, str) and value.startswith("session_"):
            result.add(value)
        return result

    def _all_session_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM sessions"
                ).fetchall()
            }

    def _candidate_session_ids_from_args(
        self,
        args: Sequence[Any],
    ) -> set[str]:
        """从调用参数中提取候选 session id（不查询数据库，仅启发式）。"""
        candidates: set[str] = set()
        if args and isinstance(args[0], str):
            candidates.add(args[0])
        if len(args) >= 2 and isinstance(args[1], str) and (
            args[1].startswith("session_") or args[1] in candidates
        ):
            candidates.add(args[1])
        for value in args:
            candidates.update(self._candidate_session_values(value))
        return candidates

    def _resolve_written_session_ids(
        self,
        written: set[str],
        args: Sequence[Any],
        result: Any,
    ) -> set[str]:
        """根据写事务触碰的表解析受影响 session id。

        不再依赖 Python 方法名：表命中即触发，session id 从参数/结果候选
        或按表反查（实体 id → session_id）得到；无候选时保守同步全部分副本。
        """
        if not written & self._SYNC_TRIGGER_TABLES:
            return set()
        candidates: set[str] = set()
        for value in (*args, result):
            candidates.update(self._candidate_session_values(value))
        if args and isinstance(args[0], str):
            candidates.add(args[0])
        with self._connect() as connection:
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                resolved = {
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT id FROM sessions
                        WHERE id IN ({placeholders})
                        """,
                        tuple(candidates),
                    ).fetchall()
                }
                if resolved:
                    return resolved
            if args:
                entity_lookups = (
                    ("players", "id"),
                    ("memories", "id"),
                    ("snapshots", "id"),
                    ("timer_instances", "id"),
                )
                for table, column in entity_lookups:
                    if table not in written:
                        continue
                    row = connection.execute(
                        f"""
                        SELECT session_id FROM {table}
                        WHERE {column} = ?
                        """,
                        (str(args[0]),),
                    ).fetchone()
                    if row:
                        return {str(row["session_id"])}
                if "character_card_drafts" in written:
                    row = connection.execute(
                        """
                        SELECT pt.session_id
                        FROM character_card_drafts draft
                        JOIN participants pt ON pt.id = draft.participant_id
                        WHERE pt.private_origin = ?
                        ORDER BY draft.created_at DESC LIMIT 1
                        """,
                        (str(args[0]),),
                    ).fetchone()
                    if row:
                        return {str(row["session_id"])}
            # 写了会话表却无法从参数解析出副本：保守同步全部分副本
            # （覆盖定时器轮询、批量清理、导入等整库写路径）。
            return self._all_session_ids()

    def _enqueue_storage_sync(
        self,
        connection: sqlite3.Connection,
        session_ids: Sequence[str],
        kind: str = "sync",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        """在当前（调用方）事务内写入存储同步出站行，随事务一起提交。"""
        if not session_ids:
            return
        if kind not in {"sync", "archive_save", "archive_backup"}:
            raise ValueError(f"未知同步出站类型：{kind}")
        now = utc_now()
        for session_id in session_ids:
            connection.execute(
                """
                INSERT INTO storage_sync_outbox(
                    session_id, kind, payload_json, status,
                    desired_generation, leased_generation,
                    completed_generation, attempts, max_attempts,
                    lease_owner, leased_at, lease_expires_at,
                    next_retry_at, last_error_code, last_error,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, ?, 'pending', 1, 0, 0, 0, 8,
                    '', '', '', '', '', '', ?, ?
                )
                ON CONFLICT(session_id, kind) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    desired_generation = storage_sync_outbox.desired_generation + 1,
                    status = CASE
                        WHEN storage_sync_outbox.status = 'leased'
                        THEN storage_sync_outbox.status
                        ELSE 'pending'
                    END,
                    lease_owner = CASE
                        WHEN storage_sync_outbox.status = 'leased'
                        THEN storage_sync_outbox.lease_owner
                        ELSE ''
                    END,
                    leased_at = CASE
                        WHEN storage_sync_outbox.status = 'leased'
                        THEN storage_sync_outbox.leased_at
                        ELSE ''
                    END,
                    lease_expires_at = CASE
                        WHEN storage_sync_outbox.status = 'leased'
                        THEN storage_sync_outbox.lease_expires_at
                        ELSE ''
                    END,
                    next_retry_at = '',
                    last_error_code = '',
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (
                    str(session_id),
                    kind,
                    json_dump(dict(payload or {})),
                    now,
                    now,
                ),
            )

    def _mark_storage_pending(self, session_ids: Sequence[str]) -> None:
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE story_storage SET
                    sync_status = 'pending', last_error = '',
                    updated_at = ?
                WHERE session_id IN ({placeholders})
                """,
                (utc_now(), *session_ids),
            )

    def _drain_storage_outbox(self, limit: int = 64) -> list[dict[str, Any]]:
        """消费 storage_sync_outbox：每行执行对应同步/归档动作。

        单趟处理（失败行保留，由下一次 drain/bootstrap 重试），
        避免一次性死循环。调用方需保证不持有会话写锁。
        """
        if not hasattr(self, "storage"):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM storage_sync_outbox
                WHERE status IN ('pending', 'retry_wait')
                  AND (next_retry_at = '' OR next_retry_at <= ?)
                ORDER BY created_at, session_id LIMIT ?
                """,
                (utc_now(), max(1, int(limit))),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            session_id = str(row["session_id"])
            kind = str(row["kind"])
            payload = json_load(row["payload_json"], {})
            if not isinstance(payload, Mapping):
                payload = {}
            try:
                if kind == "sync":
                    result = self.storage.sync_session(session_id)
                elif kind == "archive_save":
                    result = self.storage.create_archive(
                        session_id,
                        kind="save",
                        reason=str(
                            payload.get("reason") or "手动命名存档"
                        ),
                        refresh=False,
                    )
                elif kind == "archive_backup":
                    result = self.storage.create_archive(
                        session_id,
                        kind="backup",
                        reason=str(
                            payload.get("reason")
                            or "回合自动安全备份"
                        ),
                        refresh=False,
                    )
                else:
                    continue
                results.append(
                    {
                        "session_id": session_id,
                        "kind": kind,
                        **(
                            dict(result)
                            if isinstance(result, Mapping)
                            else {}
                        ),
                    }
                )
                with self._connect() as connection:
                    connection.execute(
                        """
                        DELETE FROM storage_sync_outbox
                        WHERE session_id = ? AND kind = ?
                        """,
                        (session_id, kind),
                    )
            except Exception as exc:  # noqa: BLE001 - 单个副本失败不阻断其余
                with self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE storage_sync_outbox SET
                            attempts = attempts + 1,
                            status = CASE
                                WHEN attempts + 1 >= max_attempts
                                THEN 'permanently_failed'
                                ELSE 'retry_wait'
                            END,
                            next_retry_at = CASE
                                WHEN attempts + 1 >= max_attempts THEN ''
                                ELSE ?
                            END,
                            last_error_code = 'storage.sync_failed',
                            last_error = ?, updated_at = ?
                        WHERE session_id = ? AND kind = ?
                        """,
                        (
                            (
                                datetime.now(timezone.utc)
                                + timedelta(
                                    seconds=min(
                                        3600,
                                        2 ** min(10, int(row["attempts"]) + 1),
                                    )
                                )
                            ).isoformat(timespec="seconds"),
                            str(exc)[:1000],
                            utc_now(),
                            session_id,
                            kind,
                        ),
                    )
                results.append(
                    {"session_id": session_id, "kind": kind, "error": str(exc)[:1000]}
                )
        return results

    def pending_storage_syncs(self) -> list[dict[str, Any]]:
        """返回当前出站队列（测试/运维观察用）。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM storage_sync_outbox
                ORDER BY created_at, session_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def invoke() -> T:
            method_name = fn.__name__
            _THREAD_WRITTEN_TABLES.stack.append(set())
            try:
                result = fn(*args)
            finally:
                written = _THREAD_WRITTEN_TABLES.stack.pop()
            if written and hasattr(self, "storage"):
                session_ids = self._resolve_written_session_ids(
                    written,
                    args,
                    result,
                )
                if session_ids:
                    kind = self._LEGACY_ARCHIVE_KINDS.get(
                        method_name,
                        "sync",
                    )
                    self._mark_storage_pending(sorted(session_ids))
                    # 写事务已提交；出站行与提交存在极小窗口（崩溃时由
                    # bootstrap 的 pending 状态兜底重试）。
                    with self._connect() as connection:
                        self._enqueue_storage_sync(
                            connection,
                            sorted(session_ids),
                            kind=kind,
                            payload={
                                "reason": (
                                    "副本最终存档"
                                    if method_name == "_finalize_session"
                                    else "手动命名存档"
                                )
                            }
                            if kind == "archive_save"
                            else None,
                        )
                    if not bool(self.defer_storage_sync):
                        self._drain_storage_outbox()
            return result

        return await asyncio.to_thread(invoke)
