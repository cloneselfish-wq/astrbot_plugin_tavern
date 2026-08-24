from __future__ import annotations

from .delivery_support import *


class DeliveryResultsRepositoryMixin:
    async def complete(
        self,
        delivery_id: str,
        token: str,
        *,
        sent_parts: int,
        delivered_at: str,
    ) -> dict[str, Any] | None:
        """租约持有者标记全部送达；非持有者返回 None（不修改）。"""
        return await self._run(
            self._complete_delivery,
            delivery_id,
            token,
            sent_parts,
            delivered_at,
        )

    def _complete_delivery(
        self,
        delivery_id: str,
        token: str,
        sent_parts: int,
        delivered_at: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status = 'delivered', delivered_at = ?,
                        next_part_index = ?, next_retry_at = '',
                        last_error = '', last_error_code = '',
                        lease_owner = '', leased_at = '', updated_at = ?
                    WHERE id = ? AND lease_owner = ?
                      AND status IN ('leased', 'partially_sent')
                    """,
                    (
                        str(delivered_at or now),
                        max(0, int(sent_parts or 0)),
                        now,
                        str(delivery_id),
                        str(token or ""),
                    ),
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
                    status="delivered",
                    progress="",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _delivery_record(row)

    async def mark_partial(
        self,
        delivery_id: str,
        token: str,
        *,
        next_part_index: int,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        """分片部分送达：保存游标并释放租约，等待继续发送。"""
        return await self._run(
            self._mark_partial_delivery,
            delivery_id,
            token,
            next_part_index,
            attempts,
            next_retry_at,
            last_error_code,
            last_error_message,
        )

    def _mark_partial_delivery(
        self,
        delivery_id: str,
        token: str,
        next_part_index: int,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        return self._mark_delivery_state(
            delivery_id,
            token,
            status="partially_sent",
            next_part_index=next_part_index,
            attempts=attempts,
            next_retry_at=next_retry_at,
            last_error_code=last_error_code,
            last_error_message=last_error_message,
        )

    async def mark_retry(
        self,
        delivery_id: str,
        token: str,
        *,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        """投递失败：按退避进入 retry_wait，释放租约。"""
        return await self._run(
            self._mark_retry_delivery,
            delivery_id,
            token,
            attempts,
            next_retry_at,
            last_error_code,
            last_error_message,
        )

    def _mark_retry_delivery(
        self,
        delivery_id: str,
        token: str,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        return self._mark_delivery_state(
            delivery_id,
            token,
            status="retry_wait",
            next_part_index=None,
            attempts=attempts,
            next_retry_at=next_retry_at,
            last_error_code=last_error_code,
            last_error_message=last_error_message,
        )

    async def mark_failed(
        self,
        delivery_id: str,
        token: str,
        *,
        attempts: int,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        """尝试次数达到上限：永久失败并释放租约。"""
        return await self._run(
            self._mark_failed_delivery,
            delivery_id,
            token,
            attempts,
            last_error_code,
            last_error_message,
        )

    def _mark_failed_delivery(
        self,
        delivery_id: str,
        token: str,
        attempts: int,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None:
        return self._mark_delivery_state(
            delivery_id,
            token,
            status="permanently_failed",
            next_part_index=None,
            attempts=attempts,
            next_retry_at="",
            last_error_code=last_error_code,
            last_error_message=last_error_message,
        )

    async def cancel(
        self,
        delivery_id: str,
        *,
        actor: str,
        reason: str,
        token: str = "",
    ) -> dict[str, Any] | None:
        """取消一条消息；持有租约时可指定 token 精确取消。"""
        return await self._run(
            self._cancel_protocol_delivery,
            delivery_id,
            actor,
            reason,
            token,
        )

    def _cancel_protocol_delivery(
        self,
        delivery_id: str,
        actor: str,
        reason: str,
        token: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?",
                    (str(delivery_id),),
                ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                if str(row["status"] or "") in {
                    "delivered",
                    "permanently_failed",
                }:
                    connection.execute("COMMIT")
                    return _delivery_record(row)
                if token:
                    cursor = connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status = 'cancelled', cancelled_at = ?,
                            last_error = ?, lease_owner = '', leased_at = '',
                            next_retry_at = '', updated_at = ?
                        WHERE id = ? AND lease_owner = ?
                        """,
                        (
                            now,
                            str(reason or "")[:500],
                            now,
                            str(delivery_id),
                            str(token),
                        ),
                    )
                    if cursor.rowcount == 0:
                        connection.execute("COMMIT")
                        return None
                else:
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status = 'cancelled', cancelled_at = ?,
                            last_error = ?, lease_owner = '', leased_at = '',
                            next_retry_at = '', updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            now,
                            str(reason or "")[:500],
                            now,
                            str(delivery_id),
                        ),
                    )
                self._insert_audit(
                    connection,
                    str(row["session_id"] or ""),
                    str(actor or "system"),
                    "delivery.cancelled",
                    str(delivery_id),
                    {"reason": reason},
                )
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id = ?",
                    (str(delivery_id),),
                ).fetchone()
                self._emit_delivery_updated(
                    connection,
                    updated,
                    status="cancelled",
                    progress="",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _delivery_record(updated)

    async def finish_delivery(
        self,
        delivery_id: str,
        *,
        success: bool,
        error: str = "",
        error_code: str = "",
        delivered_on_reply: bool = False,
    ) -> dict[str, Any]:
        """结束一次投递尝试。

        成功 -> delivered（或 delivered_on_reply）；失败 -> retry_wait
        （按退避计算 next_retry_at），达到 max_attempts -> permanently_failed。
        平台发送失败只更新投递状态，不重放领域 Effect（D1-RUN-007）。
        """
        return await self._run(
            self._finish_delivery,
            delivery_id,
            success,
            error,
            error_code,
            delivered_on_reply,
        )

    def _finish_delivery(
        self,
        delivery_id: str,
        success: bool,
        error: str,
        error_code: str,
        delivered_on_reply: bool,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                if success:
                    status = (
                        "delivered_on_reply"
                        if delivered_on_reply
                        else "delivered"
                    )
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status=?, attempts=attempts+1, last_error='',
                            last_error_code='', delivered_at=?,
                            lease_owner='', leased_at='', updated_at=?
                        WHERE id=?
                        """,
                        (status, now, now, delivery_id),
                    )
                else:
                    attempts = int(row["attempts"] or 0) + 1
                    max_attempts = max(
                        1,
                        int(
                            row["max_attempts"]
                            or DELIVERY_DEFAULT_MAX_ATTEMPTS
                        ),
                    )
                    if attempts >= max_attempts:
                        status = "permanently_failed"
                        next_retry_at = ""
                    else:
                        status = "retry_wait"
                        next_retry_at = retry_backoff_after(attempts, now)
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET status=?, attempts=?, last_error=?,
                            last_error_code=?, next_retry_at=?,
                            lease_owner='', leased_at='', updated_at=?
                        WHERE id=?
                        """,
                        (
                            status,
                            attempts,
                            str(error or "发送失败")[:500],
                            str(error_code or "")[:80],
                            next_retry_at,
                            now,
                            delivery_id,
                        ),
                    )
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                progress: str | int = (
                    str(updated["attempts"] or 0)
                    if status in {"retry_wait", "permanently_failed"}
                    else ""
                )
                self._emit_delivery_updated(
                    connection,
                    updated,
                    status=status,
                    progress=progress,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

    async def dismiss_delivery(
        self,
        delivery_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._dismiss_delivery, delivery_id, actor_id)

    def _dismiss_delivery(
        self,
        delivery_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status='dismissed', updated_at=?
                    WHERE id=?
                    """,
                    (now, delivery_id),
                )
                self._insert_audit(
                    connection,
                    str(row["session_id"] or ""),
                    actor_id,
                    "delivery.dismissed",
                    delivery_id,
                    {},
                )
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                self._emit_delivery_updated(
                    connection,
                    updated,
                    status="cancelled",
                    progress="",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

    async def cancel_delivery(
        self,
        delivery_id: str,
        actor_id: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        """取消一条尚未送达的消息；取消后后台任务不得再发送。"""
        return await self._run(
            self._cancel_delivery,
            delivery_id,
            actor_id,
            reason,
        )

    def _cancel_delivery(
        self,
        delivery_id: str,
        actor_id: str,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                if str(row["status"] or "") in {
                    "delivered",
                    "permanently_failed",
                }:
                    raise ValueError("该通知已结束，不能取消")
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET status='cancelled', cancelled_at=?, last_error=?,
                        lease_owner='', leased_at='', next_retry_at='',
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, str(reason or "")[:500], now, delivery_id),
                )
                self._insert_audit(
                    connection,
                    str(row["session_id"] or ""),
                    actor_id,
                    "delivery.cancelled",
                    delivery_id,
                    {"reason": reason},
                )
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

    async def advance_delivery_part(
        self,
        delivery_id: str,
        worker_id: str,
        *,
        next_part_index: int,
        total_parts: int | None = None,
        shard_cursor: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """推进物理分片游标；未发完时保持可领取状态。"""
        return await self._run(
            self._advance_delivery_part,
            delivery_id,
            worker_id,
            next_part_index,
            total_parts,
            dict(shard_cursor) if shard_cursor else None,
        )

    def _advance_delivery_part(
        self,
        delivery_id: str,
        worker_id: str,
        next_part_index: int,
        total_parts: int | None,
        shard_cursor: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                if not row:
                    raise ValueError("待投递通知不存在")
                current_total = (
                    int(total_parts or 0)
                    if total_parts is not None
                    else int(row["total_parts"] or 1)
                )
                current_total = max(1, current_total)
                next_index = max(0, int(next_part_index or 0))
                status = (
                    "partially_sent"
                    if next_index < current_total
                    else "leased"
                )
                cursor = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET next_part_index=?, total_parts=?,
                        shard_cursor_json=?, status=?, updated_at=?
                    WHERE id=? AND lease_owner=? AND status IN (
                        'leased', 'partially_sent'
                    )
                    """,
                    (
                        next_index,
                        current_total,
                        json_dump(dict(shard_cursor) if shard_cursor else {}),
                        status,
                        now,
                        delivery_id,
                        worker_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise ValueError("只有持有租约的后台任务可以推进分片")
                updated = connection.execute(
                    "SELECT * FROM delivery_outbox WHERE id=?",
                    (delivery_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

