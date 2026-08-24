from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BackgroundTaskSupervisor:
    """Own background tasks so the AstrBot entrypoint does not manage races.

    ``start(..., restart="on_failure")`` restarts a task that exits with an
    exception after an exponential backoff (capped by ``max_backoff``).
    Cancellation and clean completion are never restarted.  The default
    ``restart="never"`` keeps the pre-C6 behaviour for existing callers.
    """

    def __init__(self, logger: Any) -> None:
        self.logger = logger
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    async def start(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        restart: str = "never",
        max_backoff: float = 60.0,
    ) -> asyncio.Task[None]:
        async with self._lock:
            if self._closing:
                raise RuntimeError("后台任务管理器正在关闭")
            current = self._tasks.get(name)
            if current is not None and not current.done():
                return current
            policy = "on_failure" if str(restart).strip() == "on_failure" else "never"
            meta = self._meta.setdefault(name, {})
            meta["restart"] = policy
            meta["max_backoff"] = max(1.0, float(max_backoff or 60.0))
            meta["factory"] = factory
            meta["state"] = "running"
            meta["last_started_at"] = _utc_now()
            # 保留最近一次失败信息，便于恢复后仍可观测历史故障。
            meta.setdefault("last_error_at", None)
            meta.setdefault("last_error_summary", None)
            meta["next_retry_at"] = None
            task = asyncio.create_task(factory(), name=name)
            self._tasks[name] = task
            self._restart_tasks.pop(name, None)
            task.add_done_callback(
                lambda completed, task_name=name: self._finished(
                    task_name, completed
                )
            )
            return task

    def _finished(self, name: str, task: asyncio.Task[None]) -> None:
        meta = self._meta.get(name) or {}
        if task.cancelled():
            meta["state"] = "cancelled"
            if self._tasks.get(name) is task:
                self._tasks.pop(name, None)
            return
        if self._closing:
            if self._tasks.get(name) is task:
                self._tasks.pop(name, None)
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)
        if error is not None:
            self.logger.error(
                "321开团后台任务意外退出：%s（%s）",
                name,
                error,
            )
            meta["state"] = "failed"
            meta["last_error_at"] = _utc_now()
            meta["last_error_summary"] = str(error)[:200]
            meta["restart_count"] = int(meta.get("restart_count") or 0) + 1
            if meta.get("restart") == "on_failure" and not self._closing:
                delay = min(
                    float(meta.get("max_backoff") or 60.0),
                    1.0 * (2 ** min(int(meta.get("restart_count") or 1) - 1, 6)),
                )
                meta["next_retry_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
                loop = asyncio.get_event_loop()
                self._restart_tasks[name] = loop.create_task(
                    self._restart_after(name, delay),
                    name=f"ai-tavern-restart-{name}",
                )
        else:
            meta["state"] = "stopped"
            meta["next_retry_at"] = None

    async def _restart_after(self, name: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                if self._closing:
                    return
                meta = self._meta.get(name) or {}
                factory = meta.get("factory")
                policy = str(meta.get("restart") or "never")
                max_backoff = float(meta.get("max_backoff") or 60.0)
            if factory is None:
                return
            await self.start(
                name,
                factory,
                restart=policy,
                max_backoff=max_backoff,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "321开团后台任务重启失败：%s",
                name,
            )

    def status(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for name in sorted(set(self._tasks) | set(self._meta)):
            meta = self._meta.get(name) or {}
            task = self._tasks.get(name)
            entry: dict[str, object] = {
                "name": name,
                "state": meta.get("state", "running" if task else "stopped"),
                "restart_count": int(meta.get("restart_count") or 0),
                "last_started_at": meta.get("last_started_at"),
                "last_error_at": meta.get("last_error_at"),
                "last_error_summary": meta.get("last_error_summary"),
                "next_retry_at": meta.get("next_retry_at"),
                # 兼容旧消费方
                "running": bool(task and not task.done()),
                "cancelled": bool(task and task.cancelled()),
            }
            result.append(entry)
        return result

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            restart_tasks = [
                task for task in self._restart_tasks.values() if not task.done()
            ]
            tasks = [task for task in self._tasks.values() if not task.done()]
            for task in restart_tasks:
                task.cancel()
            for task in tasks:
                task.cancel()
        if tasks or restart_tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*restart_tasks, return_exceptions=True)
        async with self._lock:
            self._tasks.clear()
            self._restart_tasks.clear()


__all__ = ["BackgroundTaskSupervisor"]
