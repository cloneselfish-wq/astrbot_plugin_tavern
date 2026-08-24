"""D1 player fate, rescue-window, and automatic terminal orchestration."""

from __future__ import annotations

import re

from ..database_support import *
from ..contracts.actor_fate import (
    parse_actor_fate,
    parse_terminal_conditions,
)
from ..idempotency import request_fingerprint
from ..protocol.runtime import flatten_runtime, runtime_from_state
from ..runtime.fate_service import (
    apply_transition,
    find_transition,
    project_party_summary,
    resolve_structured_consequence,
    state_definition,
)
from ..runtime.terminal_service import (
    arbitrate_terminal_conditions,
    build_terminal_context,
    evaluate_terminal_conditions,
)
from .events import append_event


_TURN_EXPIRY_RE = re.compile(r"^turn:(\d+)$")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _transition_pair(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        return (
            str(value.get("from") or "").strip(),
            str(value.get("to") or "").strip(),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = list(value)
        if len(items) == 2:
            return str(items[0]).strip(), str(items[1]).strip()
    text = str(value or "").strip()
    for delimiter in ("->", "→"):
        if delimiter in text:
            left, right = text.split(delimiter, 1)
            return left.strip(), right.strip()
    return "", ""


def _window_definition(
    contract: Mapping[str, Any],
    kind: str,
) -> dict[str, Any]:
    for item in _sequence(contract.get("rescue_windows")):
        if isinstance(item, Mapping) and str(item.get("kind") or "") == kind:
            return dict(item)
    return {}



__all__ = [name for name in globals() if not name.startswith('__')]
