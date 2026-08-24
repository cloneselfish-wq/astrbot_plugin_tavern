from __future__ import annotations

from .delivery_support import *


class TurnDeliveriesRepositoryMixin:
    async def prepare_turn_delivery(
        self,
        *,
        session_id: str,
        operation_id: str,
        actor_id: str,
        state_revision: str,
        origin: str,
        parts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist one synchronous ordered reply before any host send.

        Repeated preparation of the same operation returns the existing run. A
        changed part list for that operation is rejected instead of silently
        overwriting delivery history.
        """

        return await self._run(
            self._prepare_turn_delivery,
            session_id,
            operation_id,
            actor_id,
            state_revision,
            origin,
            [dict(part) for part in parts],
        )

    def _prepare_turn_delivery(
        self,
        session_id: str,
        operation_id: str,
        actor_id: str,
        state_revision: str,
        origin: str,
        parts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        operation_id = str(operation_id or "").strip()
        origin = str(origin or "").strip()
        if not session_id or not operation_id or not origin:
            raise ValueError("逐段投递必须包含会话、操作与发送目标")
        normalized: list[dict[str, Any]] = []
        seen_dedupes: set[str] = set()
        for index, raw in enumerate(parts):
            payload = raw.get("payload")
            if not isinstance(payload, Mapping):
                raise ValueError(f"第 {index + 1} 段消息缺少结构化载荷")
            dedupe_key = str(raw.get("dedupe_key") or "").strip()
            if not dedupe_key or dedupe_key in seen_dedupes:
                raise ValueError("逐段投递的去重键必须存在且互不重复")
            seen_dedupes.add(dedupe_key)
            normalized.append(
                {
                    "kind": str(raw.get("kind") or "notice")[:40],
                    "message_type": str(raw.get("message_type") or "")[:120],
                    "dedupe_key": dedupe_key[:180],
                    "payload_json": json_dump(dict(payload)),
                    "rendered_text": str(raw.get("rendered_text") or ""),
                }
            )
        if not normalized:
            raise ValueError("逐段投递至少需要一段消息")
        key_material = "\0".join((session_id, operation_id, origin))
        run_key = "turn-run:" + hashlib.sha256(
            key_material.encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE run_key=?",
                    (run_key,),
                ).fetchone()
                if row is not None:
                    existing_parts = connection.execute(
                        "SELECT * FROM turn_delivery_parts "
                        "WHERE run_id=? ORDER BY part_index",
                        (row["id"],),
                    ).fetchall()
                    expected = [
                        (
                            part["kind"],
                            part["message_type"],
                            part["dedupe_key"],
                            part["payload_json"],
                            part["rendered_text"],
                        )
                        for part in normalized
                    ]
                    actual = [
                        (
                            str(part["kind"]),
                            str(part["message_type"]),
                            str(part["dedupe_key"]),
                            str(part["payload_json"]),
                            str(part["rendered_text"]),
                        )
                        for part in existing_parts
                    ]
                    if expected != actual:
                        raise ValueError("同一回合操作的逐段内容发生变化，已拒绝覆盖历史")
                    connection.execute("COMMIT")
                    return _turn_run_record(row, existing_parts)
                run_id = new_id("turn_delivery")
                connection.execute(
                    """
                    INSERT INTO turn_delivery_runs(
                        id, run_key, session_id, operation_id, actor_id,
                        state_revision, origin, status, next_part_index,
                        total_parts, attempt_count, last_error, created_at,
                        updated_at, delivered_at, cancelled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, 0, '', ?, ?, '', '')
                    """,
                    (
                        run_id,
                        run_key,
                        session_id,
                        operation_id,
                        str(actor_id or ""),
                        str(state_revision or ""),
                        origin,
                        len(normalized),
                        now,
                        now,
                    ),
                )
                for index, part in enumerate(normalized):
                    connection.execute(
                        """
                        INSERT INTO turn_delivery_parts(
                            run_id, part_index, kind, message_type, dedupe_key,
                            payload_json, rendered_text, status, attempts,
                            last_error, created_at, updated_at, delivered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?, '')
                        """,
                        (
                            run_id,
                            index,
                            part["kind"],
                            part["message_type"],
                            part["dedupe_key"],
                            part["payload_json"],
                            part["rendered_text"],
                            now,
                            now,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE id=?", (run_id,)
                ).fetchone()
                stored_parts = connection.execute(
                    "SELECT * FROM turn_delivery_parts "
                    "WHERE run_id=? ORDER BY part_index",
                    (run_id,),
                ).fetchall()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _turn_run_record(row, stored_parts)

    async def mark_turn_delivery_sending(
        self, run_id: str, part_index: int
    ) -> dict[str, Any]:
        return await self._run(
            self._mark_turn_delivery_sending, run_id, part_index
        )

    def _mark_turn_delivery_sending(
        self, run_id: str, part_index: int
    ) -> dict[str, Any]:
        now = utc_now()
        index = max(0, int(part_index))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise ValueError("逐段投递记录不存在")
                part = connection.execute(
                    "SELECT * FROM turn_delivery_parts "
                    "WHERE run_id=? AND part_index=?",
                    (run_id, index),
                ).fetchone()
                if part is None:
                    raise ValueError("逐段投递分段不存在")
                if str(part["status"]) in {"delivered", "skipped"}:
                    connection.execute("COMMIT")
                    return _turn_part_record(part)
                blocked = connection.execute(
                    "SELECT COUNT(*) FROM turn_delivery_parts "
                    "WHERE run_id=? AND part_index<? "
                    "AND status NOT IN ('delivered','skipped')",
                    (run_id, index),
                ).fetchone()[0]
                if int(blocked or 0) > 0:
                    raise ValueError("前一段尚未确认送达，不能越序发送")
                connection.execute(
                    """
                    UPDATE turn_delivery_parts
                    SET status='sending', attempts=attempts+1,
                        last_error='', updated_at=?
                    WHERE run_id=? AND part_index=?
                    """,
                    (now, run_id, index),
                )
                connection.execute(
                    """
                    UPDATE turn_delivery_runs
                    SET status='sending', next_part_index=?,
                        attempt_count=attempt_count+1, last_error='', updated_at=?
                    WHERE id=?
                    """,
                    (index, now, run_id),
                )
                part = connection.execute(
                    "SELECT * FROM turn_delivery_parts "
                    "WHERE run_id=? AND part_index=?",
                    (run_id, index),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _turn_part_record(part)

    async def record_turn_delivery_receipt(
        self,
        run_id: str,
        part_index: int,
        status: str,
        *,
        error: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._record_turn_delivery_receipt,
            run_id,
            part_index,
            status,
            error,
        )

    def _record_turn_delivery_receipt(
        self,
        run_id: str,
        part_index: int,
        status: str,
        error: str,
    ) -> dict[str, Any]:
        status = str(status or "").strip().lower()
        if status not in {"sent", "deduped", "failed"}:
            raise ValueError("不支持的逐段投递回执")
        now = utc_now()
        index = max(0, int(part_index))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                part = connection.execute(
                    "SELECT * FROM turn_delivery_parts "
                    "WHERE run_id=? AND part_index=?",
                    (run_id, index),
                ).fetchone()
                if part is None:
                    raise ValueError("逐段投递分段不存在")
                if status in {"sent", "deduped"}:
                    connection.execute(
                        """
                        UPDATE turn_delivery_parts
                        SET status='delivered', last_error='', updated_at=?,
                            delivered_at=CASE WHEN delivered_at='' THEN ? ELSE delivered_at END
                        WHERE run_id=? AND part_index=?
                        """,
                        (now, now, run_id, index),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE turn_delivery_parts
                        SET status='failed', last_error=?, updated_at=?
                        WHERE run_id=? AND part_index=?
                        """,
                        (str(error or "发送未获平台确认")[:500], now, run_id, index),
                    )
                rows = connection.execute(
                    "SELECT * FROM turn_delivery_parts "
                    "WHERE run_id=? ORDER BY part_index",
                    (run_id,),
                ).fetchall()
                remaining = [
                    row
                    for row in rows
                    if str(row["status"]) not in {"delivered", "skipped"}
                ]
                delivered_count = len(rows) - len(remaining)
                if not remaining:
                    run_status = "delivered"
                    next_index = len(rows)
                    last_error = ""
                    delivered_at = now
                else:
                    next_index = int(remaining[0]["part_index"])
                    last_error = (
                        str(remaining[0]["last_error"] or "")
                        if str(remaining[0]["status"]) == "failed"
                        else ""
                    )
                    if status == "failed":
                        run_status = (
                            "partially_sent" if delivered_count else "retry_wait"
                        )
                    else:
                        run_status = "partially_sent"
                    delivered_at = ""
                connection.execute(
                    """
                    UPDATE turn_delivery_runs
                    SET status=?, next_part_index=?, last_error=?,
                        updated_at=?, delivered_at=?
                    WHERE id=?
                    """,
                    (
                        run_status,
                        next_index,
                        last_error,
                        now,
                        delivered_at,
                        run_id,
                    ),
                )
                run = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE id=?", (run_id,)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _turn_run_record(run, rows)

    async def get_turn_delivery_run(
        self, *, run_id: str = "", run_key: str = ""
    ) -> dict[str, Any] | None:
        return await self._run(self._get_turn_delivery_run, run_id, run_key)

    def _get_turn_delivery_run(
        self, run_id: str, run_key: str
    ) -> dict[str, Any] | None:
        if not str(run_id or "") and not str(run_key or ""):
            raise ValueError("需要逐段投递记录编号或幂等键")
        with self._connect() as connection:
            if run_id:
                row = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE id=?", (run_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM turn_delivery_runs WHERE run_key=?", (run_key,)
                ).fetchone()
            if row is None:
                return None
            parts = connection.execute(
                "SELECT * FROM turn_delivery_parts "
                "WHERE run_id=? ORDER BY part_index",
                (row["id"],),
            ).fetchall()
        return _turn_run_record(row, parts)

    async def list_turn_delivery_runs(
        self, session_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_turn_delivery_runs, session_id, limit
        )

    def _list_turn_delivery_runs(
        self, session_id: str, limit: int
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM turn_delivery_runs WHERE session_id=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (str(session_id or ""), max(1, min(200, int(limit or 50)))),
            ).fetchall()
            return [
                _turn_run_record(
                    row,
                    connection.execute(
                        "SELECT * FROM turn_delivery_parts "
                        "WHERE run_id=? ORDER BY part_index",
                        (row["id"],),
                    ).fetchall(),
                )
                for row in rows
            ]
