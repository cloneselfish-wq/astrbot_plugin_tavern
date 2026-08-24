"""B/C 分阶段建卡补充域：纯函数策略、候选、回复解析与公开投影。"""

from .candidates import (
    DEFER_OPTION,
    FALLBACK_OPTIONS,
    REDUCE_OPTION,
    apply_selection,
    build_candidates,
    diff_item_grant_plans,
    field_value_kind,
    option_view,
)
from .policy import (
    OFFER_CLOSED_STATES,
    OFFER_OPEN_STATES,
    SUPPLEMENT_KIND,
    SUPPLEMENT_NOTICE_KIND,
    condition_matches,
    effective_trigger,
    fallback_due,
    field_is_reofferable,
    field_open_round,
    field_supplement_config,
    missing_bc_fields,
    offer_expired,
    supplement_config,
)
from .presentation import (
    confirm_group_projection,
    offer_group_hint,
    offer_private_text,
    supplement_list_line,
)
from .reply import parse_supplement_reply

__all__ = [
    "DEFER_OPTION",
    "FALLBACK_OPTIONS",
    "OFFER_CLOSED_STATES",
    "OFFER_OPEN_STATES",
    "REDUCE_OPTION",
    "SUPPLEMENT_KIND",
    "SUPPLEMENT_NOTICE_KIND",
    "apply_selection",
    "build_candidates",
    "condition_matches",
    "confirm_group_projection",
    "diff_item_grant_plans",
    "effective_trigger",
    "fallback_due",
    "field_is_reofferable",
    "field_open_round",
    "field_supplement_config",
    "field_value_kind",
    "missing_bc_fields",
    "offer_expired",
    "offer_group_hint",
    "offer_private_text",
    "option_view",
    "parse_supplement_reply",
    "supplement_config",
    "supplement_list_line",
]
