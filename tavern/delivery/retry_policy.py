"""重试与租约策略（D1_PLAN 15 §6-7）。

退避阶梯：第 1 次立即、第 2 次 15 秒、第 3 次 1 分钟、第 4 次 5 分钟、
第 5 次 15 分钟，后续 30 分钟；上限按消息类型配置。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_SCHEDULE_SECONDS: tuple[float, ...] = (0.0, 15.0, 60.0, 300.0, 900.0, 1800.0)
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_LEASE_SECONDS = 120.0

# 消息类型级默认值；未列出的类型使用全局默认。
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "card_code": {"max_attempts": 6},
    "card_reminder": {"max_attempts": 8},
    "staged_supplement": {"max_attempts": 8},
    "dm_whisper": {"max_attempts": 10, "lease_seconds": 60.0},
    "death_confirm": {"max_attempts": 8},
    "vote_reminder": {"max_attempts": 8},
    "group_notice": {"max_attempts": 8},
    "webui_only": {"max_attempts": 4},
}


def next_retry_delay(attempts: int, kind: str = "notice") -> float:
    """第 ``attempts`` 次失败后的退避秒数（attempts 从 1 开始计数）。"""

    count = max(1, int(attempts or 0))
    index = min(count - 1, len(DEFAULT_SCHEDULE_SECONDS) - 1)
    return DEFAULT_SCHEDULE_SECONDS[index]


def max_attempts_for(kind: str = "notice") -> int:
    """该消息类型的最大尝试次数。"""

    return int((KIND_DEFAULTS.get(kind) or {}).get("max_attempts", DEFAULT_MAX_ATTEMPTS))


def lease_seconds_for(kind: str = "notice") -> float:
    """该消息类型的租约时长（秒）。"""

    return float((KIND_DEFAULTS.get(kind) or {}).get("lease_seconds", DEFAULT_LEASE_SECONDS))


def is_permanently_failed(attempts: int, kind: str = "notice") -> bool:
    """尝试次数达到上限后标记永久失败。"""

    return int(attempts or 0) >= max_attempts_for(kind)


def add_seconds(value: str, seconds: float) -> str:
    """ISO 时间加秒；无法解析时以当前 UTC 为基准。"""

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def next_retry_at(attempts: int, kind: str = "notice", *, now: str | None = None) -> str:
    """第 ``attempts`` 次失败后的下一次重试时间（ISO UTC）。"""

    if not now:
        from ..database_support import utc_now

        now = utc_now()
    return add_seconds(now, next_retry_delay(attempts, kind))


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_SCHEDULE_SECONDS",
    "KIND_DEFAULTS",
    "add_seconds",
    "is_permanently_failed",
    "lease_seconds_for",
    "max_attempts_for",
    "next_retry_at",
    "next_retry_delay",
]
