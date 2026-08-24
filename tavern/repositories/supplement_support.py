"""B/C 分阶段建卡补充域仓储（D1_PLAN/16 §3-§6）。

零新表：提议状态存于 ``delivery_outbox`` 的 ``meta_json``，确认结果复用
``character_card_versions`` / ``character_cards`` / ``participants.card_stage``。
所有写路径在同一 ``BEGIN IMMEDIATE`` 内完成，任一失败整体回滚。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..card_lifecycle import validate_card_revision
from ..card_wizard import preset_only_guard
from ..constants import SESSION_FINISHED
from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    json_dump,
    json_load,
    new_id,
    utc_now,
)
from ..delivery.target import (
    TARGET_KIND_GROUP,
    TARGET_KIND_PRIVATE,
    TARGET_KIND_WEBUI_ONLY,
    DeliveryTarget,
)
from ..item_catalog import card_item_grants
from ..idempotency import (
    replay_receipt,
    request_fingerprint,
    require_expected_revision,
    require_idempotency_key,
)
from ..runtime.turn_commit import stable_event_id
from .events import append_event
from ..lifecycle import (
    card_stage_state,
    card_template,
    field_stage,
    stage_label,
    stage_lock_field,
    staged_creation,
)
from ..presets import resolve_character_presets, validate_preset_selection
from ..supplement import (
    OFFER_OPEN_STATES,
    SUPPLEMENT_KIND,
    SUPPLEMENT_NOTICE_KIND,
    apply_selection,
    build_candidates,
    condition_matches,
    confirm_group_projection,
    diff_item_grant_plans,
    effective_trigger,
    field_is_reofferable,
    field_open_round,
    field_supplement_config,
    missing_bc_fields,
    offer_expired,
    offer_group_hint,
    offer_private_text,
    option_view,
    supplement_config,
)


_ACTIVE_DELIVERY_STATUSES = frozenset(
    {"pending", "leased", "partially_sent", "retry_wait"}
)



__all__ = [name for name in globals() if not name.startswith('__')]
