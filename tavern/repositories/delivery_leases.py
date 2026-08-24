from __future__ import annotations

from .delivery_support import *


class DeliveryLeasesRepositoryMixin:
    async def lease(
        self,
        delivery_id: str,
        token: str,
        lease_until: str,
    ) -> dict[str, Any] | None:
        """原子领取/续租；租约已过期（lease_until 早于当前）可被接管。"""
        return await self._run(self._lease_delivery, delivery_id, token, lease_until)

    def _lease_delivery(
        self,
        delivery_id: str,
        token: str,
        lease_until: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                maintenance = connection.execute(
                    "SELECT value FROM tavern_meta WHERE key='maintenance_mode'"
                ).fetchone()
                if (
                    maintenance is not None
                    and str(maintenance["value"] or "") == "1"
                ):
                    connection.execute("COMMIT")
                    return None
                cursor = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'leased', lease_owner = ?, leased_at = ?,
                        updated_at = ?
                    WHERE id = ? AND (
                        status IN ('pending', 'retry_wait')
                        OR (
                            status = 'partially_sent'
                            AND (
                                lease_owner = ''
                                OR leased_at = ''
                                OR leased_at <= ?
                            )
                        )
                        OR (
                            status = 'leased' AND lease_owner <> ''
                            AND (leased_at = '' OR leased_at <= ?)
                        )
                    )
                    """,
                    (
                        str(token or "")[:120],
                        str(lease_until or now),
                        now,
                        str(delivery_id),
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 0:
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?",
                    (str(delivery_id),),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _delivery_record(row)

    async def claim_deliveries(
        self,
        worker_id: str,
        *,
        limit: int = 10,
        now: str | None = None,
    ) -> list[dict[str, Any]]:
        """后台 worker 原子领取到期待投递（租约防重，D1-DEL-006）。"""
        return await self._run(
            self._claim_deliveries,
            worker_id,
            limit,
            now,
        )

    def _claim_deliveries(
        self,
        worker_id: str,
        limit: int,
        now: str | None,
    ) -> list[dict[str, Any]]:
        worker_id = str(worker_id or "").strip() or "worker_unknown"
        now = now or utc_now()
        claimed: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM delivery_outbox
                    WHERE (
                        status IN ('pending', 'retry_wait')
                        AND (next_retry_at = '' OR next_retry_at <= ?)
                    ) OR (
                        status = 'partially_sent'
                        AND (next_retry_at = '' OR next_retry_at <= ?)
                        AND (
                            lease_owner = ''
                            OR leased_at = ''
                            OR leased_at <= ?
                        )
                    )
                    ORDER BY priority ASC, created_at ASC
                    LIMIT ?
                    """,
                    (
                        now,
                        now,
                        now,
                        max(1, min(100, int(limit or 10))),
                    ),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status='leased', lease_owner=?, leased_at=?,
                            updated_at=?
                        WHERE id=?
                        """,
                        (worker_id, now, now, row["id"]),
                    )
                    claimed.append(dict(row))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    async def recover_expired_leases(
        self,
        *,
        now: str | None = None,
        ttl_seconds: int = DELIVERY_LEASE_TTL_SECONDS,
    ) -> list[dict[str, Any]]:
        """回收超时租约（进程崩溃/停机恢复），回到可重试队列。"""
        return await self._run(
            self._recover_expired_leases,
            now,
            ttl_seconds,
        )

    def _recover_expired_leases(
        self,
        now: str | None,
        ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        now = now or utc_now()
        import datetime

        cutoff = ""
        try:
            base = datetime.datetime.fromisoformat(now)
            if base.tzinfo is None:
                base = base.replace(tzinfo=datetime.timezone.utc)
            cutoff = (
                base
                - datetime.timedelta(seconds=max(1, int(ttl_seconds or 300)))
            ).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            cutoff = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if cutoff:
                    rows = connection.execute(
                        """
                        SELECT * FROM delivery_outbox
                        WHERE status = 'leased'
                          AND leased_at <> ''
                          AND leased_at <= ?
                        """,
                        (cutoff,),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM delivery_outbox
                        WHERE status = 'leased' AND leased_at <> ''
                        """
                    ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status='retry_wait', lease_owner='', leased_at='',
                            next_retry_at=?, last_error=?,
                            last_error_code='lease_expired', updated_at=?
                        WHERE id=?
                        """,
                        (now, "后台任务租约超时，已回收等待重试", now, row["id"]),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return [dict(row) for row in rows]

    async def release_delivery(
        self,
        delivery_id: str,
        worker_id: str,
        *,
        next_retry_at: str = "",
        error: str = "",
    ) -> dict[str, Any] | None:
        """worker 主动释放租约（如分片发送中断），回到重试队列；非租约持有者返回 None。"""
        return await self._run(
            self._release_delivery,
            delivery_id,
            worker_id,
            next_retry_at,
            error,
        )

    def _release_delivery(
        self,
        delivery_id: str,
        worker_id: str,
        next_retry_at: str,
        error: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status='retry_wait', lease_owner='', leased_at='',
                        next_retry_at=?, last_error=?, updated_at=?
                    WHERE id=? AND lease_owner=? AND status='leased'
                    """,
                    (
                        str(next_retry_at or now),
                        str(error or "")[:500],
                        now,
                        delivery_id,
                        worker_id,
                    ),
                )
                if cursor.rowcount == 0:
                    connection.execute("COMMIT")
                    return None
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(row) if row is not None else None
