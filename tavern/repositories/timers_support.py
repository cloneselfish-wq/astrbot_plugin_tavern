"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..resolution_receipts import content_hash
from .events import append_event


def timer_revision(value: Mapping[str, Any]) -> int:
    """Return a JS-safe CAS token for a timer row without exposing its id."""

    return int(
        content_hash(
            {
                "status": str(value.get("status") or ""),
                "deadline_at": str(value.get("deadline_at") or ""),
                "remaining_seconds": int(value.get("remaining_seconds") or 0),
                "reminder_at": str(value.get("reminder_at") or ""),
                "reminder_sent": int(value.get("reminder_sent") or 0),
                "updated_at": str(value.get("updated_at") or ""),
            }
        )[:13],
        16,
    )



__all__ = [name for name in globals() if not name.startswith('__')]
