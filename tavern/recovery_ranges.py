"""Strict semantics for persisted snapshot-recovery event exclusions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


MAX_EXCLUDED_EVENT_RANGES = 64


class RecoveryStateError(RuntimeError):
    """Persisted recovery state cannot be consumed without guessing."""

    code = "recovery.state_invalid"

    def __init__(self) -> None:
        super().__init__(
            "副本恢复状态损坏，已停止本次操作；"
            "请由管理员从健康备份恢复。"
        )


@dataclass(frozen=True, slots=True)
class RecoveryState:
    payload: Mapping[str, Any]
    excluded_event_ranges: tuple[tuple[int, int], ...]


def validate_recovery_state(value: object) -> RecoveryState:
    """Validate a decoded recovery object without coercing persisted values."""

    if not isinstance(value, Mapping):
        raise RecoveryStateError()
    ranges = value.get("excluded_event_ranges", [])
    if (
        not isinstance(ranges, (list, tuple))
        or len(ranges) > MAX_EXCLUDED_EVENT_RANGES
    ):
        raise RecoveryStateError()
    excluded: list[tuple[int, int]] = []
    for item in ranges:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise RecoveryStateError()
        start, end = item
        if (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end < start
        ):
            raise RecoveryStateError()
        excluded.append((start, end))
    return RecoveryState(
        payload=dict(value),
        excluded_event_ranges=tuple(excluded),
    )


def parse_recovery_json(value: object) -> RecoveryState:
    """Decode one SQLite/backup JSON cell and apply recovery semantics."""

    if not isinstance(value, str):
        raise RecoveryStateError()
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryStateError() from exc
    return validate_recovery_state(decoded)


__all__ = [
    "MAX_EXCLUDED_EVENT_RANGES",
    "RecoveryState",
    "RecoveryStateError",
    "parse_recovery_json",
    "validate_recovery_state",
]
