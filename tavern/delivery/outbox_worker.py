"""独立后台投递 worker（D1_PLAN 15 §6-7、16.3）。

独立于玩家入站消息重试：扫描到期 pending/partially_sent/retry_wait 与
租约过期的 leased 记录 → 原子领取租约 → 逐条投递 → 成功标记 delivered、
失败按退避进入 retry_wait、超上限进入 permanently_failed。进程重启后通过
过期的 ``lease_until`` 恢复未完成投递。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from .retry_policy import lease_seconds_for
from .service import DeliveryOutboxRepository, DeliveryService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerRunSummary:
    reminders_claimed: int = 0
    scanned: int = 0
    leased: int = 0
    delivered: int = 0
    retry_wait: int = 0
    partially_sent: int = 0
    permanently_failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reminders_claimed": self.reminders_claimed,
            "scanned": self.scanned,
            "leased": self.leased,
            "delivered": self.delivered,
            "retry_wait": self.retry_wait,
            "partially_sent": self.partially_sent,
            "permanently_failed": self.permanently_failed,
            "cancelled": self.cancelled,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


class OutboxWorker:
    def __init__(
        self,
        *,
        service: DeliveryService,
        repository: DeliveryOutboxRepository | None = None,
        poll_interval: float = 1.0,
        max_per_cycle: int = 20,
        now_fn=None,
        monotonic_fn=None,
    ) -> None:
        self.service = service
        self.repository = repository or service.repository
        self.poll_interval = max(0.5, float(poll_interval or 1.0))
        self.max_per_cycle = max(1, int(max_per_cycle or 20))
        self.now_fn = now_fn or _default_now
        self.monotonic_fn = monotonic_fn or time.monotonic
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(self) -> WorkerRunSummary:
        """执行一轮扫描与投递；单条失败不中断整轮。"""

        if self.repository is None:
            return WorkerRunSummary(errors=("未配置待投递队列",))
        now = self.now_fn()
        reminder_claim = getattr(
            self.repository,
            "claim_due_generation_reminders",
            None,
        )
        reminders_claimed = 0
        reminder_errors: tuple[str, ...] = ()
        if callable(reminder_claim):
            try:
                claimed = await reminder_claim(
                    now=now,
                    now_monotonic=self.monotonic_fn(),
                    limit=self.max_per_cycle,
                )
                reminders_claimed = int(
                    (claimed or {}).get("claimed") or 0
                )
                reminder_errors = tuple(
                    "generation_reminder_problem:"
                    + str(item.get("code") or "unknown")
                    for item in (claimed or {}).get("problems", ())
                    if isinstance(item, dict)
                )
            except Exception as exc:  # noqa: BLE001
                reminder_errors = (
                    "generation_reminder_claim:"
                    + type(exc).__name__,
                )
        if reminder_errors:
            counts: dict[str, int] = {}
            for code in reminder_errors:
                counts[code] = counts.get(code, 0) + 1
            logger.warning(
                "321开团故事提醒扫描出现安全停止：count=%d codes=%s",
                len(reminder_errors),
                ",".join(
                    f"{code}={counts[code]}" for code in sorted(counts)
                ),
            )
        due = await self.repository.list_due(limit=self.max_per_cycle, now=now)
        summary = WorkerRunSummary(
            reminders_claimed=reminders_claimed,
            scanned=len(due),
            errors=reminder_errors,
        )
        for record in due:
            delivery_id = str(record.get("delivery_id") or "")
            if not delivery_id:
                summary = replace(summary, skipped=summary.skipped + 1)
                continue
            kind = str(record.get("message_type") or "notice")
            token = uuid.uuid4().hex
            lease_until = _add_seconds(now, lease_seconds_for(kind))
            leased = await self.repository.lease(delivery_id, token, lease_until)
            if leased is None:
                summary = replace(summary, skipped=summary.skipped + 1)
                continue
            summary = replace(summary, leased=summary.leased + 1)
            try:
                outcome = await self.service.deliver_leased(
                    delivery_id,
                    token,
                    record=leased,
                )
            except Exception as exc:  # noqa: BLE001
                summary = replace(
                    summary,
                    errors=summary.errors + (f"{delivery_id}:{type(exc).__name__}",),
                )
                continue
            outcome_status = outcome.status
            if outcome_status == "sent":
                summary = replace(summary, delivered=summary.delivered + 1)
            elif outcome_status == "retry_wait":
                summary = replace(summary, retry_wait=summary.retry_wait + 1)
            elif outcome_status == "partially_sent":
                summary = replace(summary, partially_sent=summary.partially_sent + 1)
            elif outcome_status == "permanently_failed":
                summary = replace(summary, permanently_failed=summary.permanently_failed + 1)
            elif outcome_status == "cancelled":
                summary = replace(summary, cancelled=summary.cancelled + 1)
            else:
                summary = replace(summary, skipped=summary.skipped + 1)
        return summary

    async def start(self) -> None:
        """启动后台循环（幂等）。"""

        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run_loop(),
            name="d1-outbox-worker",
        )

    async def stop(self) -> None:
        """停止后台循环并等待当前任务退出。"""

        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=self.poll_interval + 2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()

    async def _run_loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                pass  # 单轮扫描失败不退出循环
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                continue


def _default_now() -> str:
    from ..database_support import utc_now

    return utc_now()


def _add_seconds(value: str, seconds: float) -> str:
    from .retry_policy import add_seconds

    return add_seconds(value, seconds)


__all__ = ["OutboxWorker", "WorkerRunSummary"]
