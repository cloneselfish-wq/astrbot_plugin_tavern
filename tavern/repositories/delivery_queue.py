from __future__ import annotations

from .delivery_support import *


class DeliveryQueueRepositoryMixin:
    def _recover_interrupted_turn_deliveries(self) -> int:
        """Move uncertain in-flight parts to manual/reply-triggered resume state."""

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    "SELECT run_id, part_index FROM turn_delivery_parts "
                    "WHERE status='sending'"
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE turn_delivery_parts
                        SET status='failed', last_error=?, updated_at=?
                        WHERE run_id=? AND part_index=? AND status='sending'
                        """,
                        (
                            "进程在平台确认前中断；为避免越序，等待同一回合重试",
                            now,
                            row["run_id"],
                            row["part_index"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE turn_delivery_runs
                        SET status=CASE WHEN next_part_index>0
                            THEN 'partially_sent' ELSE 'retry_wait' END,
                            last_error=?, updated_at=?
                        WHERE id=? AND status='sending'
                        """,
                        (
                            "进程在平台确认前中断；等待同一回合重试",
                            now,
                            row["run_id"],
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return len(rows)

    def _emit_delivery_updated(
        self,
        connection: Any,
        row: Any,
        *,
        status: str,
        progress: str | int,
    ) -> None:
        """状态真实迁移落库后，在同一事务写幂等 ``event:delivery.updated``。

        事件 ID 对同一 delivery + 状态 + 进度稳定，重试不重复领域事件
        （WP-11）；payload 只含安全状态中文语义与 affected_modules，
        不含 UMO / 用户 ID / 目标快照。``private_owner`` 投递为 private，
        其余为 public。
        """
        label = _DELIVERY_STATUS_LABELS.get(str(status or ""))
        if label is None or row is None:
            return
        session_id = str(row["session_id"] or "")
        if not session_id:
            return
        audience = str(row["audience"] or "group")
        insert_session_event(
            connection,
            session_id=session_id,
            event_id=(
                f"delivery.updated:{row['id']}:{status}:{progress}"
            ),
            type_="event:delivery.updated",
            actor_ref="system",
            payload={
                "status": label,
                "affected_modules": ["deliveries"],
            },
            visibility=(
                "private"
                if audience == AUDIENCE_PRIVATE_OWNER
                else "public"
            ),
        )

    async def queue_delivery(
        self,
        *,
        session_id: str,
        origin: str,
        kind: str,
        text: str,
        reason: str,
        dedupe_key: str = "",
        priority: int = 100,
        audience: str = "player",
        target_snapshot: Mapping[str, Any] | None = None,
        projection_snapshot: str = "",
        rendered_parts: Sequence[Mapping[str, Any]] | None = None,
        next_part_index: int = 0,
        total_parts: int = 1,
        shard_cursor: Mapping[str, Any] | None = None,
        max_attempts: int = DELIVERY_DEFAULT_MAX_ATTEMPTS,
        next_retry_at: str = "",
        lease_owner: str = "",
        leased_at: str = "",
        status: str = "pending",
    ) -> dict[str, Any]:
        """把一条待投递消息写入 outbox（领域事务只写队列，不等待网络）。"""

        from ..messaging.player import render_player_text

        text = render_player_text(text, default_title="酒馆通知")
        return await self._run(
            self._queue_delivery,
            session_id,
            origin,
            kind,
            text,
            reason,
            dedupe_key,
            priority,
            audience,
            dict(target_snapshot) if target_snapshot else None,
            projection_snapshot,
            [dict(part) for part in rendered_parts]
            if rendered_parts
            else None,
            next_part_index,
            total_parts,
            dict(shard_cursor) if shard_cursor else None,
            max_attempts,
            next_retry_at,
            lease_owner,
            leased_at,
            status,
        )

    def _queue_delivery(
        self,
        session_id: str,
        origin: str,
        kind: str,
        text: str,
        reason: str,
        dedupe_key: str,
        priority: int,
        audience: str,
        target_snapshot: Mapping[str, Any] | None,
        projection_snapshot: str,
        rendered_parts: Sequence[Mapping[str, Any]] | None,
        next_part_index: int,
        total_parts: int,
        shard_cursor: Mapping[str, Any] | None,
        max_attempts: int,
        next_retry_at: str,
        lease_owner: str,
        leased_at: str,
        status: str,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        origin = str(origin or "").strip()
        text = str(text or "").strip()
        if not origin or not text:
            raise ValueError("待投递通知必须包含会话来源与正文")
        status = str(status or "pending").strip().lower()
        if status not in DELIVERY_ACTIVE_STATUSES | {"webui_only"}:
            status = "pending"
        now = utc_now()
        item_id = new_id("delivery")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if dedupe_key:
                    existing = connection.execute(
                        """
                        SELECT * FROM delivery_outbox
                        WHERE dedupe_key = ? AND status IN (
                            'pending', 'leased', 'partially_sent',
                            'retry_wait'
                        )
                        """,
                        (str(dedupe_key),),
                    ).fetchone()
                    if existing:
                        connection.execute("COMMIT")
                        return dict(existing)
                connection.execute(
                    """
                    INSERT INTO delivery_outbox(
                        id, session_id, origin, kind, text, status, attempts,
                        last_error, last_error_code, dedupe_key, priority,
                        audience, target_snapshot_json, projection_snapshot,
                        rendered_parts_json, next_part_index, total_parts,
                        shard_cursor_json, lease_owner, leased_at,
                        next_retry_at, max_attempts, created_at, updated_at,
                        delivered_at, cancelled_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 0, ?, '', ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, '', ''
                    )
                    """,
                    (
                        item_id,
                        session_id,
                        origin,
                        str(kind or "notice")[:40],
                        text,
                        status,
                        str(reason or "")[:500],
                        str(dedupe_key or "")[:180],
                        max(0, int(priority or 100)),
                        str(audience or "player")[:40],
                        json_dump(dict(target_snapshot) if target_snapshot else {}),
                        str(projection_snapshot or ""),
                        json_dump(
                            list(rendered_parts) if rendered_parts else []
                        ),
                        max(0, int(next_part_index or 0)),
                        max(1, int(total_parts or 1)),
                        json_dump(dict(shard_cursor) if shard_cursor else {}),
                        str(lease_owner or "")[:120],
                        str(leased_at or ""),
                        str(next_retry_at or ""),
                        max(1, min(100, int(max_attempts or 8))),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    "system",
                    "delivery.queued",
                    item_id,
                    {
                        "kind": kind,
                        "origin": origin,
                        "reason": reason,
                        "priority": priority,
                        "audience": audience,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?", (item_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row)

    # ── DeliveryOutboxRepository 协议（tavern.delivery.DeliveryService /
    # OutboxWorker 使用）───────────────────────────────────────────────
    # 协议记录键见 service.DeliveryOutboxRepository 文档；DB 映射：
    # id→delivery_id、kind→message_type、lease_owner→lease_token、
    # leased_at→lease_until、last_error→last_error_message、
    # target_snapshot_json/rendered_parts_json/projection_snapshot/
    # meta_json 均为 JSON 列。旧 queue_delivery/finish_delivery 保持可用。

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        """按协议记录新建一条投递（delivery_id 与活动 dedupe_key 幂等）。"""
        return await self._run(self._create_delivery, dict(record))

    def _create_delivery(self, record: dict[str, Any]) -> dict[str, Any]:
        record = dict(record or {})
        delivery_id = str(record.get("delivery_id") or "").strip() or new_id(
            "delivery"
        )
        session_id = str(record.get("session_id") or "").strip()
        dedupe_key = str(record.get("dedupe_key") or "")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?",
                    (delivery_id,),
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return _delivery_record(existing)
                if dedupe_key:
                    dup = connection.execute(
                        """
                        SELECT * FROM delivery_outbox
                        WHERE dedupe_key = ? AND status IN (
                            'pending', 'leased', 'partially_sent',
                            'retry_wait', 'webui_only'
                        )
                        """,
                        (dedupe_key,),
                    ).fetchone()
                    if dup is not None:
                        connection.execute("COMMIT")
                        return _delivery_record(dup)
                created = self._create_delivery_locked(connection, record)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return created

    def _create_delivery_locked(
        self,
        connection: Any,
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """在已开启 ``BEGIN IMMEDIATE`` 的现有 connection 上插入 outbox 行。

        D1-DEL-010：普通 ``create``（自开事务）与密语等原子路径（领域事件
        + outbox 同事务）复用本实现；本方法不负责事务边界与去重，由调用方
        保证在事务内调用并在失败时回滚。
        """
        record = dict(record or {})
        delivery_id = str(record.get("delivery_id") or "").strip() or new_id(
            "delivery"
        )
        session_id = str(record.get("session_id") or "").strip()
        target_snapshot = record.get("target_snapshot") or {}
        if not isinstance(target_snapshot, Mapping):
            target_snapshot = {}
        rendered_parts = record.get("rendered_parts") or []
        if not isinstance(rendered_parts, (list, tuple)):
            rendered_parts = [str(rendered_parts)]
        rendered_parts = [
            str(part or "").strip()
            for part in rendered_parts
            if str(part or "").strip()
        ]
        text = str(record.get("text") or "")
        if not text:
            text = "\n".join(rendered_parts)
        origin = str(
            (target_snapshot.get("unified_origin") if isinstance(target_snapshot, Mapping) else "")
            or (
                f"{target_snapshot.get('platform_instance_id', '')}:"
                f"{target_snapshot.get('target_id', '')}"
                if isinstance(target_snapshot, Mapping)
                else ""
            )
            or record.get("origin")
            or ""
        ).strip()
        if not origin or not text:
            raise ValueError("待投递通知必须包含会话来源与正文")
        kind = str(record.get("message_type") or "notice")[:40]
        status = str(record.get("status") or "pending").strip().lower()
        if status not in DELIVERY_ACTIVE_STATUSES | {"webui_only"}:
            status = "pending"
        now = str(record.get("created_at") or "") or utc_now()
        updated_at = str(record.get("updated_at") or "") or now
        dedupe_key = str(record.get("dedupe_key") or "")
        projection = record.get("projection_snapshot")
        meta = record.get("meta") or {}
        if not isinstance(meta, Mapping):
            meta = {}
        total_parts = max(
            1,
            int(
                record.get("total_parts")
                or len(rendered_parts)
                or 1
            ),
        )
        connection.execute(
            """
            INSERT INTO delivery_outbox(
                id, session_id, origin, kind, text, status, attempts,
                last_error, last_error_code, dedupe_key, priority,
                audience, target_snapshot_json, projection_snapshot,
                rendered_parts_json, next_part_index, total_parts,
                shard_cursor_json, lease_owner, leased_at,
                next_retry_at, max_attempts, meta_json,
                created_at, updated_at, delivered_at, cancelled_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, '', ''
            )
            """,
            (
                delivery_id,
                session_id,
                origin,
                kind,
                text,
                status,
                max(0, int(record.get("attempts") or 0)),
                str(record.get("last_error_message") or "")[:500],
                str(record.get("last_error_code") or "")[:80],
                dedupe_key[:180],
                max(0, int(record.get("priority") or 100)),
                str(record.get("audience") or "player")[:40],
                json_dump(dict(target_snapshot)),
                (
                    json_dump(dict(projection))
                    if isinstance(projection, Mapping)
                    else str(projection or "")
                ),
                json_dump(list(rendered_parts)),
                max(0, int(record.get("next_part_index") or 0)),
                total_parts,
                json_dump({}),
                str(record.get("lease_token") or "")[:120],
                str(record.get("lease_until") or ""),
                str(record.get("next_retry_at") or ""),
                max(
                    1,
                    min(
                        100,
                        int(record.get("max_attempts") or 8),
                    ),
                ),
                json_dump(dict(meta)),
                now,
                updated_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM delivery_outbox WHERE id = ?",
            (delivery_id,),
        ).fetchone()
        return _delivery_record(row)

    async def get(self, delivery_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_delivery, delivery_id)

    def _get_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE id = ?",
                (str(delivery_id),),
            ).fetchone()
        return _delivery_record(row) if row is not None else None

    async def dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        return await self._run(self._dedupe_delivery, dedupe_key)

    def _dedupe_delivery(self, dedupe_key: str) -> dict[str, Any] | None:
        dedupe_key = str(dedupe_key or "").strip()
        if not dedupe_key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE dedupe_key = ? AND status IN (
                    'pending', 'leased', 'partially_sent', 'retry_wait',
                    'webui_only'
                )
                ORDER BY created_at LIMIT 1
                """,
                (dedupe_key,),
            ).fetchone()
        return _delivery_record(row) if row is not None else None

    async def list_due(
        self,
        *,
        limit: int,
        now: str,
    ) -> list[dict[str, Any]]:
        """扫描到期待投递与租约已过期的 leased 记录（worker 单轮）。"""
        return await self._run(self._list_due, limit, now)

    def _list_due(self, limit: int, now: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            maintenance = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='maintenance_mode'"
            ).fetchone()
            if maintenance is not None and str(maintenance["value"] or "") == "1":
                return []
            rows = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE (
                    status IN ('pending', 'retry_wait', 'partially_sent')
                    AND (next_retry_at = '' OR next_retry_at <= ?)
                ) OR (
                    status = 'leased' AND lease_owner <> ''
                    AND leased_at <> '' AND leased_at <= ?
                )
                ORDER BY priority ASC, created_at ASC
                LIMIT ?
                """,
                (str(now), str(now), max(1, min(100, int(limit or 20)))),
            ).fetchall()
        return [_delivery_record(row) for row in rows]

    def _mark_delivery_state(
        self,
        delivery_id: str,
        token: str,
        *,
        status: str,
        next_part_index: int | None,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                sets = [
                    "status = ?",
                    "attempts = ?",
                    "next_retry_at = ?",
                    "last_error_code = ?",
                    "last_error = ?",
                    "lease_owner = ''",
                    "leased_at = ''",
                    "updated_at = ?",
                ]
                values: list[Any] = [
                    status,
                    max(0, int(attempts or 0)),
                    str(next_retry_at or ""),
                    str(last_error_code or "")[:80],
                    str(last_error_message or "")[:500],
                    now,
                ]
                if next_part_index is not None:
                    sets.append("next_part_index = ?")
                    values.append(max(0, int(next_part_index or 0)))
                values.extend([str(delivery_id), str(token or "")])
                cursor = connection.execute(
                    "UPDATE delivery_outbox SET "
                    + ", ".join(sets)
                    + " WHERE id = ? AND lease_owner = ? "
                    + "AND status IN ('leased', 'partially_sent')",
                    tuple(values),
                )
                if cursor.rowcount == 0:
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?",
                    (str(delivery_id),),
                ).fetchone()
                self._emit_delivery_updated(
                    connection,
                    row,
                    status=status,
                    progress=(
                        max(0, int(next_part_index or 0))
                        if status == "partially_sent"
                        else max(0, int(attempts or 0))
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _delivery_record(row)

    async def list_status(
        self,
        session_id: str,
        *,
        viewer: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """WebUI 投递状态源：按副本列出（服务层按观众裁剪隐私）。"""
        return await self._run(self._list_status, session_id, viewer, limit)

    def _list_status(
        self,
        session_id: str,
        viewer: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM delivery_outbox
                WHERE session_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (
                    str(session_id),
                    max(1, min(500, int(limit or 100))),
                ),
            ).fetchall()
        return [_delivery_record(row) for row in rows]

    async def list_deliveries(
        self,
        *,
        session_id: str = "",
        status: str = "pending",
        origin: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_deliveries, session_id, status, origin, limit
        )

    def _list_deliveries(
        self,
        session_id: str,
        status: str,
        origin: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id=?")
            values.append(session_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        if origin:
            clauses.append("origin=?")
            values.append(origin)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(500, int(limit or 100))))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM delivery_outbox{where} ORDER BY created_at ASC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]
