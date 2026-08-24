"""EventTimeline projection built on the existing safe session-event view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..session_events import project_session_event
from ..contracts.narrative_document import legacy_text_fallback
from .common import integer, mapping, text
from .keys import OpaqueKeyFactory
from .realtime import visual_kinds_for_event


def project_history(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    latest_sequence: int,
    keys: OpaqueKeyFactory,
    page_size: int = 20,
    expose_latest: bool = False,
) -> dict[str, Any]:
    page_size = max(1, min(100, int(page_size)))
    projected: list[dict[str, Any]] = []
    raw_rows = [row for row in rows or () if isinstance(row, Mapping)]
    for index, raw in enumerate(raw_rows[:page_size]):
        # The visual timeline is an ordinary business surface even for admins;
        # technical event ids and raw payload remain in a separate diagnostic API.
        safe = project_session_event(raw, is_admin=False)
        sequence = integer(safe.get("seq"), 0)
        event_type = text(raw.get("type"), limit=80)
        item = {
                "key": keys.key("historyevent", f"{sequence}:{index}"),
                "sequence": sequence,
                "category": (
                    "story"
                    if event_type == "event:story_progress"
                    else text(safe.get("category"), limit=40, default="system")
                ),
                "title": text(safe.get("title"), limit=100),
                "summary": text(safe.get("summary"), limit=240),
                # Kind mapping uses the authoritative raw type on the server;
                # the response still contains only the resulting safe kinds.
                "visual_kinds": list(visual_kinds_for_event(raw)),
                "created_at": text(safe.get("created_at"), limit=80),
            }
        narrative_document = mapping(raw.get("narrative_document"))
        if narrative_document:
            item["narrative_document"] = narrative_document
        if not narrative_document and raw.get("legacy_record") is True:
            try:
                legacy = legacy_text_fallback(
                    raw.get("text"), legacy_record=True
                )
                item.update(legacy.to_dict())
            except Exception:
                item["narrative_problem"] = {
                    "code": "history.legacy_record_invalid",
                    "message": "明确标记的旧故事正文未通过安全检查。",
                    "recovery": "请由主持人从已验证的旧版备份恢复该记录。",
                    "retryable": False,
                }
        narrative_problem = mapping(raw.get("narrative_problem"))
        if narrative_problem:
            item["narrative_problem"] = {
                "code": text(narrative_problem.get("code"), limit=100),
                "message": text(narrative_problem.get("message"), limit=240),
                "recovery": text(
                    narrative_problem.get("recovery"), limit=240
                ),
                "retryable": bool(narrative_problem.get("retryable", True)),
            }
        projected.append(item)
    last_sequence = projected[-1]["sequence"] if projected else 0
    # The repository already returns page_size + 1 rows.  A global latest
    # sequence may belong only to hidden events; using it for pagination would
    # both leak their existence and make a player's cursor loop on empty pages.
    has_more = len(raw_rows) > page_size
    visible_latest = (
        max(0, int(latest_sequence)) if expose_latest else last_sequence
    )
    return {
        "items": projected,
        "next_cursor": (
            keys.cursor("historyseq", last_sequence) if has_more else ""
        ),
        "has_more": has_more,
        "page_size": page_size,
        "latest_sequence": visible_latest,
        "problems": [],
    }


__all__ = ["project_history"]
