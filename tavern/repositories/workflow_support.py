"""Domain repository methods extracted from the SQLite store."""

from ..database_support import *
from ..lifecycle import resolve_card_stage
from ..story_pacing import compute_turn_progress_indicators
from ..resolution_receipts import content_hash
from ..story_context import (
    project_opening_scene,
    recommend_opening_scenarios,
    select_opening_scenario,
)
from .events import append_event
from .story_support import _owner_tuple_locked


def participant_revision(
    value: Mapping[str, Any],
    session_revision: int,
) -> int:
    """Return a JS-safe revision for a roster participant and its session."""

    return int(
        content_hash(
            {
                "participation_status": str(
                    value.get("participation_status") or ""
                ),
                "card_status": str(value.get("card_status") or ""),
                "ready": bool(value.get("ready")),
                "action_locked": bool(value.get("action_locked")),
                "updated_at": str(value.get("updated_at") or ""),
                "session_revision": int(session_revision or 0),
            }
        )[:13],
        16,
    )



__all__ = [name for name in globals() if not name.startswith('__')]
