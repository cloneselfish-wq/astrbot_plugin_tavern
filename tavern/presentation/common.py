from __future__ import annotations


import re


from collections.abc import Sequence


from typing import Any, Mapping


from ..card_wizard import (
    field_visible,
    preset_options,
    resolve_current_wizard_step,
)


from ..constants import (
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)


from ..database import DatabaseNotFoundError


from ..copy.candidate import CandidateCopyError, candidate_player_copy


from ..copy.entities import decorate_entity


from ..lifecycle import (
    CARD_STAGE_A,
    CARD_STAGE_B,
    CARD_STAGE_C,
    attribute_maps,
    card_stage_state,
    card_stat_allocation,
    field_stage,
    resolve_profession_stats,
    semantic_field_key,
    stage_label,
    staged_creation,
    uses_profession_preset_stats,
)


from ..stat_generation import (
    calculate_preset_stack_stats,
    format_preset_stack_result,
    stat_generation_config,
    uses_preset_stack_stats,
)


INSTANCE_LIST_PAGE_SIZE = 5


INSTANCE_INTRO_MAX_CHARS = 220


REVIEW_LIST_PAGE_SIZE = 5


TIMER_POLL_INTERVAL_SECONDS = 15


TIMER_NOTICE_DEDUP_SECONDS = 25


TIMER_NOTICE_MIN_GAP_SECONDS = 2.0


PRIVATE_CARD_ACTIONS = frozenset(
    {
        "card",
        "card_fill",
        "card_stats_reset",
        "card_timer_notice",
        "card_preview",
        "card_confirm",
        "card_cancel",
    }
)


PRIVATE_ONLY_CARD_ACTIONS = PRIVATE_CARD_ACTIONS - {"card"}


_INSTANCE_PAGE_PATTERNS = (
    re.compile(r"^第\s*(\d{1,6})\s*页$"),
    re.compile(r"^页\s*(\d{1,6})$"),
    re.compile(r"^列表\s*(\d{1,6})$"),
)


from ..projections.character import project_actor_view


COMMON_DECLARED_EXPORTS = [
    "parse_instance_list_page",
    "_compact_instance_intro",
    "format_turn_status",
    "format_instance_list",
    "_instance_list_footer",
    "format_roster",
    "format_vote",
    "format_recovered_timer",
    "world_preset_brief",
    "_profession_preset_line",
    "_format_profession_step_prompt",
    "format_card_prompt",
    "format_card_preview",
    "format_card_stage_summary",
    "_review_reference",
    "_pending_review_cards",
    "_resolve_pending_review",
    "format_pending_reviews",
    "format_review_card",
    "_format_remaining_time",
    "_story_reply_parts",
]



__all__ = [name for name in globals() if not name.startswith('__')]

