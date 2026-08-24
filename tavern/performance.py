"""Bounded, privacy-safe request stage profiling."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


ALLOWED_STAGES = frozenset(
    {
        "parse",
        "authorize",
        "database",
        "context_compile",
        "provider_wait",
        "validation",
        "platform_delivery",
        "application",
    }
)


@dataclass(slots=True)
class RequestProfiler:
    correlation_id: str
    route: str
    started_at: float = field(default_factory=time.perf_counter)
    durations_ms: dict[str, float] = field(default_factory=dict)
    cache: dict[str, bool] = field(default_factory=dict)
    provider_attempts: int = 0
    provider_timeout: bool = False

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        normalized = str(name or "").strip()
        if normalized not in ALLOWED_STAGES:
            raise ValueError(f"不支持的性能阶段：{normalized}")
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.durations_ms[normalized] = round(
                self.durations_ms.get(normalized, 0.0) + elapsed,
                3,
            )

    def set_cache(self, name: str, hit: bool) -> None:
        key = str(name or "").strip()
        if key in {"context_hit", "rule_runtime_hit"}:
            self.cache[key] = bool(hit)

    def record_provider(self, *, timeout: bool = False) -> None:
        self.provider_attempts += 1
        self.provider_timeout = self.provider_timeout or bool(timeout)

    def export(self) -> dict[str, Any]:
        return {
            "schema": "tavern-performance-event/1.0.0-rc10",
            "correlation_id": str(self.correlation_id or "")[:160],
            "route": str(self.route or "unknown")[:120],
            "durations_ms": {
                **dict(sorted(self.durations_ms.items())),
                "total": round(
                    (time.perf_counter() - self.started_at) * 1000,
                    3,
                ),
            },
            "cache": dict(sorted(self.cache.items())),
            "provider": {
                "attempts": int(self.provider_attempts),
                "timeout": bool(self.provider_timeout),
            },
        }

    def json(self) -> str:
        return json.dumps(
            self.export(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


__all__ = ["ALLOWED_STAGES", "RequestProfiler"]

