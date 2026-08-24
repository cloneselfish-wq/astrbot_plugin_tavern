from __future__ import annotations

from ..database_support import *
from ..constants import PLUGIN_VERSION


class AuditRepositoryMixin:
    async def list_audit(
        self,
        session_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_audit,
            session_id,
            limit,
            offset,
        )

    def _list_audit(
        self,
        session_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        with self._connect() as connection:
            if session_id:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    WHERE session_id = ?
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "target": row["target"],
                    "detail": json_load(row["detail_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    async def write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        await self._run(
            self._write_audit,
            session_id,
            actor_id,
            action,
            target,
            dict(detail),
        )

    def _write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            self._insert_audit(
                connection,
                session_id,
                actor_id,
                action,
                target,
                detail,
            )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs(
                session_id, actor_id, action, target, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                actor_id,
                action,
                target,
                json_dump(dict(detail)),
                utc_now(),
            ),
        )
