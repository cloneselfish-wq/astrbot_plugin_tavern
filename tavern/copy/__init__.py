"""Structured, mobile-first user message rendering."""

from .candidate import (
    CandidateCopyError,
    CandidatePlayerCopy,
    DETAIL_BUDGET,
    FIELD_ENTITY_TYPES,
    HIDDEN_COPY_MARKERS,
    LABEL_BUDGET,
    MAX_ADVANTAGES,
    MAX_LIMITATIONS,
    MAX_STORY_HOOKS,
    SUMMARY_BUDGET,
    TECHNICAL_LABELS,
    candidate_player_copy,
    candidate_player_copies,
    copy_is_redundant,
    entity_type_for_field,
    player_copy_text,
    truncate_text,
)
from .document import MessageDocument, MessageSection
from .entities import EntityToken, decorate_entity
from .pagination import paginate_text
from .render import mobile_format_text, render_message

__all__ = [
    "CandidateCopyError",
    "CandidatePlayerCopy",
    "DETAIL_BUDGET",
    "EntityToken",
    "FIELD_ENTITY_TYPES",
    "HIDDEN_COPY_MARKERS",
    "LABEL_BUDGET",
    "MAX_ADVANTAGES",
    "MAX_LIMITATIONS",
    "MAX_STORY_HOOKS",
    "MessageDocument",
    "MessageSection",
    "SUMMARY_BUDGET",
    "TECHNICAL_LABELS",
    "candidate_player_copy",
    "candidate_player_copies",
    "copy_is_redundant",
    "decorate_entity",
    "entity_type_for_field",
    "mobile_format_text",
    "paginate_text",
    "player_copy_text",
    "render_message",
    "truncate_text",
]
