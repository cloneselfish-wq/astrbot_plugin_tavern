from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any


EventPredicate = Callable[[Mapping[str, Any]], bool]


class EventBroker:
    """Small in-process fan-out used by the WebUI activity stream."""

    def __init__(self, hooks: Any = None) -> None:
        self._subscribers: dict[
            asyncio.Queue[dict[str, Any]],
            tuple[EventPredicate | None, bool],
        ] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._hooks = hooks
        self._tasks: set[asyncio.Task[Any]] = set()
        self.recent_errors: list[str] = []

    async def publish(self, event: Mapping[str, Any]) -> None:
        if self._closed:
            return
        payload = dict(event)
        hook_name = str(payload.get("hook") or "")
        if hook_name and self._hooks is not None:
            await self._hooks.dispatch(hook_name, payload)
        async with self._lock:
            subscribers = tuple(self._subscribers.items())
        for queue, (predicate, notify_gaps) in subscribers:
            if predicate is not None:
                try:
                    if not bool(predicate(payload)):
                        continue
                except Exception as exc:  # noqa: BLE001 - subscriber isolation
                    self.recent_errors.append(
                        f"subscriber filter {type(exc).__name__}: {exc}"
                    )
                    self.recent_errors = self.recent_errors[-20:]
                    continue
            queued_payload = payload
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                # Console consumers never expose this internal marker. It tells a
                # filtered stream to reconcile against the durable event log
                # after the broker had to evict an in-memory notification.
                if notify_gaps:
                    queued_payload = dict(payload)
                    queued_payload["_broker_gap"] = True
            try:
                queue.put_nowait(queued_payload)
            except asyncio.QueueFull:
                pass

    def schedule(self, event: Mapping[str, Any]) -> None:
        """非阻塞投递：调用方在业务事务提交后调用。

        hook 分发在事件循环后台执行，不阻塞回合提交路径；
        失败只记录到 recent_errors，绝不改变已提交的玩家回合。
        """
        if self._closed:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def dispatch() -> None:
            try:
                await self.publish(event)
            except Exception as exc:  # noqa: BLE001 - hook 隔离
                self.recent_errors.append(f"{type(exc).__name__}: {exc}")
                self.recent_errors = self.recent_errors[-20:]

        task = asyncio.create_task(dispatch())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def subscribe(
        self,
        predicate: EventPredicate | None = None,
        *,
        notify_gaps: bool = False,
        timeout_seconds: float = 25.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to broker notifications.

        ``predicate`` is evaluated before an item enters the subscriber queue,
        so a session-scoped consumer never receives another session's raw
        notification even transiently.  The optional arguments are backwards
        compatible: legacy callers retain the original global stream and
        25-second keepalive behavior.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        try:
            timeout = max(0.001, float(timeout_seconds))
        except (TypeError, ValueError, OverflowError):
            timeout = 25.0
        async with self._lock:
            self._subscribers[queue] = (predicate, bool(notify_gaps))
        try:
            yield {"type": "ready"}
            while not self._closed:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    yield {"type": "keepalive"}
                    continue
                yield item
        finally:
            async with self._lock:
                self._subscribers.pop(queue, None)

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        self._tasks.clear()
        async with self._lock:
            self._subscribers.clear()
