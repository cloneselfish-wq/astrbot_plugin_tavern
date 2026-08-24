from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from ..database_support import *
from ..item_catalog import normalize_item_instance


class ItemQueriesRepositoryMixin:
    async def list_item_instances(
        self,
        session_id: str,
        owner_ref: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_item_instances, session_id, owner_ref
        )

    def _list_item_instances(
        self,
        session_id: str,
        owner_ref: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM item_instances
                WHERE session_id = ? AND owner_ref = ?
                ORDER BY container, item_id
                """,
                (session_id, owner_ref),
            ).fetchall()
            return [
                normalize_item_instance(dict(row)) for row in rows
            ]

    async def list_session_item_instances(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Return all item instances for one session in one bounded query."""

        return await self._run(
            self._list_session_item_instances,
            session_id,
        )

    def _list_session_item_instances(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM item_instances
                WHERE session_id = ?
                ORDER BY owner_ref, container, item_id
                """,
                (session_id,),
            ).fetchall()
            return [
                normalize_item_instance(dict(row)) for row in rows
            ]

    async def page_item_instances(
        self,
        *,
        session_id: str,
        owner_ref: str = "",
        page: int = 1,
        page_size: int = 100,
    ) -> dict[str, Any]:
        """Read one deterministic inventory page without N+1 queries."""

        return await self._run(
            self._page_item_instances,
            session_id,
            owner_ref,
            page,
            page_size,
        )

    def _page_item_instances(
        self,
        session_id: str,
        owner_ref: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        page_size = max(1, min(100, int(page_size or 100)))
        where = ["session_id = ?"]
        values: list[Any] = [session_id]
        if owner_ref:
            where.append("owner_ref = ?")
            values.append(owner_ref)
        clause = " AND ".join(where)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM item_instances WHERE {clause}",
                    tuple(values),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM item_instances
                WHERE {clause}
                ORDER BY owner_ref, container, item_id
                LIMIT ? OFFSET ?
                """,
                (
                    *values,
                    page_size,
                    (page - 1) * page_size,
                ),
            ).fetchall()
        return {
            "items": [
                normalize_item_instance(dict(row)) for row in rows
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        }
