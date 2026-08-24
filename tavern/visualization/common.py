"""Small, host-independent helpers shared by visual projections."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_INTERNAL_REF = re.compile(r"^[a-z][a-z0-9_.-]*:[a-z0-9_.:/-]+$", re.I)


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def text(value: Any, *, limit: int = 200, default: str = "") -> str:
    if value is None:
        return default
    result = str(value).strip()
    if len(result) > limit:
        return result[: max(0, limit - 1)].rstrip() + "…"
    return result


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def number_or_none(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        parsed = float(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return int(parsed) if parsed.is_integer() else parsed


def visible(visibility: Any, *, privileged: bool, self_owned: bool = False) -> bool:
    value = text(visibility, limit=30, default="public").lower()
    if value in {"", "public", "player", "party", "group"}:
        return True
    if privileged:
        return True
    return self_owned and value in {"private", "self", "character", "owner"}


def display_label(value: Any, *, fallback: str = "") -> str:
    candidate = text(value, limit=100)
    if not candidate or _INTERNAL_REF.fullmatch(candidate):
        return fallback
    return candidate


def latest_timestamp(*values: Any) -> str:
    texts = [text(value, limit=80) for value in values]
    return max((value for value in texts if value), default="")


def source_problem(code: str, message: str) -> dict[str, Any]:
    return {
        "code": str(code),
        "message": str(message),
        "recovery": "请重试当前板块；若仍失败，请联系管理员。",
        "retryable": True,
    }


def privacy_paths(value: Any, path: str = "$") -> list[str]:
    """Return forbidden public-contract fields for tests and release gates."""

    forbidden = {
        "session_id",
        "world_id",
        "actor_id",
        "actor_ref",
        "participant_id",
        "participant_ref",
        "provider_id",
        "preset_id",
        "trace_id",
        "correlation_id",
        "operation_id",
        "event_id",
        "command_id",
        "causation_id",
        "raw_payload",
        "raw_state",
        "prompt",
        "system_prompt",
        "technical",
        "technical_details",
    }
    result: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            child = f"{path}.{key}"
            if name in forbidden or name.endswith("_database_id"):
                result.append(child)
            result.extend(privacy_paths(item, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            result.extend(privacy_paths(item, f"{path}[{index}]"))
    return result


__all__ = [
    "display_label",
    "integer",
    "latest_timestamp",
    "mapping",
    "number_or_none",
    "privacy_paths",
    "sequence",
    "source_problem",
    "text",
    "visible",
]
