"""Domain repository methods extracted from the SQLite store."""

import hashlib

from ..database_support import *
from ..story_pacing import compute_turn_progress_indicators
from .events import append_event


def _owner_tuple_locked(
    owner_type: Any,
    owner_ref: Any,
) -> tuple[str, str] | None:
    """把经济操作里的 owner 字段解析为 (type, ref)；缺任一项视为无。"""
    otype = str(owner_type or "").strip()
    oref = str(owner_ref or "").strip()
    if not otype or not oref:
        return None
    return (otype, oref)


def memory_revision(value: Mapping[str, Any]) -> int:
    """Return a JS-safe CAS revision for one memory governance projection."""

    canonical = json_dump(
        {
            "updated_at": str(value.get("updated_at") or ""),
            "content": str(value.get("content") or ""),
            "importance": value.get("importance"),
            "tags": list(value.get("tags") or []),
            "visibility": str(value.get("visibility") or "public"),
            "locked": bool(value.get("locked")),
            "pinned": bool(value.get("pinned")),
            "invalidated": bool(value.get("invalidated")),
            "conflict_status": str(value.get("conflict_status") or "clear"),
            "governance_note": str(value.get("governance_note") or ""),
        }
    )
    return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:13], 16)



__all__ = [name for name in globals() if not name.startswith('__')]
