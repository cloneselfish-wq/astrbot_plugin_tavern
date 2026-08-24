"""Lease-based catalog-to-instance storage synchronization worker."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any


class StorageSyncWorker:
    def __init__(
        self,
        repository: Any,
        *,
        worker_id: str = "",
        poll_seconds: float = 1.0,
        batch_size: int = 20,
        lease_seconds: int = 120,
    ) -> None:
        self.repository = repository
        self.worker_id = worker_id or f"storage-worker:{uuid.uuid4().hex}"
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.batch_size = max(1, min(100, int(batch_size)))
        self.lease_seconds = max(10, int(lease_seconds))

    def _sync(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        session_id = str(record.get("session_id") or "")
        kind = str(record.get("kind") or "")
        payload = record.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        storage = self.repository.storage
        if kind == "sync":
            return storage.sync_session(session_id)
        if kind in {"archive_save", "archive_backup"}:
            return {
                "archive": str(
                    storage.create_archive(
                        session_id,
                        kind="save" if kind == "archive_save" else "backup",
                        reason=str(
                            payload.get("reason")
                            or (
                                "手动命名存档"
                                if kind == "archive_save"
                                else "回合自动安全备份"
                            )
                        ),
                        refresh=False,
                    )
                )
            }
        raise ValueError(f"未知存储同步类型：{kind}")

    async def run_once(self) -> int:
        records = await self.repository.claim_storage_outbox(
            self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        for record in records:
            session_id = str(record.get("session_id") or "")
            kind = str(record.get("kind") or "")
            try:
                result = await asyncio.to_thread(self._sync, record)
                if bool(result.get("queued")):
                    raise RuntimeError(
                        "复制期间 catalog revision 已变化，已安全重新排队"
                    )
            except Exception as exc:
                await self.repository.finish_storage_outbox(
                    session_id,
                    kind,
                    self.worker_id,
                    delivered=False,
                    error_code="storage.sync_failed",
                    error_message=str(exc),
                )
            else:
                await self.repository.finish_storage_outbox(
                    session_id,
                    kind,
                    self.worker_id,
                    delivered=True,
                )
        return len(records)

    async def run(self) -> None:
        while True:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            if not processed:
                await asyncio.sleep(self.poll_seconds)


__all__ = ["StorageSyncWorker"]
