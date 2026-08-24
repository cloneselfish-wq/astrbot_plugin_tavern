"""Lease-based committed-event hook dispatcher."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import Any


class EventOutboxWorker:
    def __init__(
        self,
        repository: Any,
        hooks: Any,
        *,
        worker_id: str = "",
        poll_seconds: float = 1.0,
        batch_size: int = 20,
        lease_seconds: int = 120,
    ) -> None:
        self.repository = repository
        self.hooks = hooks
        self.worker_id = worker_id or f"event-worker:{uuid.uuid4().hex}"
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.batch_size = max(1, min(100, int(batch_size)))
        self.lease_seconds = max(10, int(lease_seconds))

    async def run_once(self) -> int:
        records = await self.repository.claim_event_outbox(
            self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.lease_seconds,
        )
        for record in records:
            try:
                topic = str(record.get("topic") or "").strip()
                payload = record.get("payload")
                payload = dict(payload) if isinstance(payload, Mapping) else {}
                errors = await self.hooks.dispatch(topic, payload)
                if errors:
                    raise RuntimeError("; ".join(str(item) for item in errors))
            except Exception as exc:  # committed state is never rolled back
                await self.repository.finish_event_outbox(
                    str(record.get("id") or ""),
                    self.worker_id,
                    delivered=False,
                    error_code="hook.dispatch_failed",
                    error_message=str(exc),
                )
            else:
                await self.repository.finish_event_outbox(
                    str(record.get("id") or ""),
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


__all__ = ["EventOutboxWorker"]
