from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


Hook = Callable[[Mapping[str, Any]], Awaitable[None] | None]
SUPPORTED_EVENTS = frozenset(
    {
        "session_created",
        "character_approved",
        "turn_started",
        "option_selected",
        "check_completed",
        "vote_completed",
        "story_generated",
        "session_finished",
    }
)


class HookRegistry:
    """Read-only event fan-out for trusted Python extensions.

    Hooks observe committed events. They cannot receive a database connection
    and failures never roll back or interrupt the authoritative workflow.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = defaultdict(list)

    def subscribe(self, event: str, callback: Hook) -> Callable[[], None]:
        name = str(event or "").strip()
        if name not in SUPPORTED_EVENTS:
            raise ValueError(f"不支持的扩展事件：{name}")
        if not callable(callback):
            raise TypeError("事件回调必须可调用")
        self._hooks[name].append(callback)

        def unsubscribe() -> None:
            if callback in self._hooks[name]:
                self._hooks[name].remove(callback)

        return unsubscribe

    async def dispatch(self, event: str, payload: Mapping[str, Any]) -> list[str]:
        errors: list[str] = []
        for callback in tuple(self._hooks.get(event, ())):
            try:
                result = callback(dict(payload))
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=2.0)
            except Exception as exc:  # extensions are isolated by design
                errors.append(f"{callback!r}: {exc}")
        return errors
