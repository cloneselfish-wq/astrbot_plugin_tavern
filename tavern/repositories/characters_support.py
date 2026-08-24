import logging

"""Domain repository methods extracted from the SQLite store."""

from ..card_wizard import (
    CANDIDATE_SNAPSHOTS_KEY,
    LAST_MESSAGE_KEY,
    archetype_packs,
    archetype_step,
    apply_archetype_pack_atomic,
    auto_fill_for_phase,
    auto_fill_remaining,
    choose_option,
    choose_options,
    clear_field_and_dependents,
    creation_flow,
    current_creation_mode,
    field_visible,
    mode_step,
    next_player_fillable_step,
    next_wizard_step,
    preset_only_guard,
    preset_options,
    revalidate_dependent_selections,
    store_preset_snapshot,
    store_preset_snapshots,
    wizard_completion_state,
)
from ..card_validation import validate_card_field
from ..database_support import *
from .events import append_event
from ..lifecycle import (
    CARD_STAGE_A,
    CARD_STAGES,
    card_stage_state,
    field_stage,
    stage_field_projection,
    staged_creation,
    stage_required_missing,
)
from ..capability_service import CapabilityService
from ..entity_registry import EntityRegistry, module_value
from ..resolution_receipts import content_hash
from ..idempotency import (
    replay_receipt,
    request_fingerprint,
    require_expected_revision,
    require_idempotency_key,
)
from ..runtime.turn_commit import stable_event_id
from ..display.currency import format_money
from ..item_catalog import card_item_grants
from .economy_support import _major_to_minor as _money_to_minor


logger = logging.getLogger(__name__)


_ACTOR_NAME_ROLE = "actor.identity.name"
_ACTOR_ALIAS_ROLE = "actor.identity.alias"
_ACTOR_PROFESSION_ROLE = "actor.identity.profession"
_ACTOR_PRIMARY_STAT_ROLE = "actor.stats.primary"
_ACTOR_SECONDARY_STAT_ROLE = "actor.stats.secondary"


def _lifecycle_copy_text(value: Any, *, character_name: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("text") or value.get("description") or ""
    return str(value or "").replace(
        "{character_name}",
        character_name,
    ).strip()


def _character_lifecycle_copy(
    world: Mapping[str, Any],
    *,
    character_name: str,
) -> tuple[str, dict[str, Any] | None]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    chat = rules.get("chat_experience")
    chat = chat if isinstance(chat, Mapping) else {}
    lifecycle = chat.get("character_lifecycle_copy")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    confirmed = _lifecycle_copy_text(
        lifecycle.get("confirmed_system_message"),
        character_name=character_name,
    ) or f"角色「{character_name}」已完成创建。"
    raw_entry = lifecycle.get("entry_beat")
    if not isinstance(raw_entry, Mapping) or not bool(
        raw_entry.get("enabled", False)
    ):
        return confirmed, None
    text = _lifecycle_copy_text(
        raw_entry,
        character_name=character_name,
    )
    if not text:
        return confirmed, None
    return confirmed, {
        "text": text,
        "visibility": str(raw_entry.get("visibility") or "group"),
        "text_id": str(raw_entry.get("text_id") or ""),
    }


def _field_for_semantic_role(
    template: Mapping[str, Any],
    semantic_role: str,
) -> Mapping[str, Any] | None:
    matches = [
        item
        for item in template.get("fields") or []
        if isinstance(item, Mapping)
        and str(item.get("semantic_role") or "").strip() == semantic_role
    ]
    if len(matches) > 1:
        raise ValueError(f"角色模板重复声明语义角色：{semantic_role}")
    return matches[0] if matches else None


def _field_key_for_semantic_role(
    template: Mapping[str, Any],
    semantic_role: str,
) -> str:
    definition = _field_for_semantic_role(template, semantic_role)
    return str(definition.get("key") or "") if definition else ""


def _semantic_field_value(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    semantic_role: str,
) -> Any:
    key = _field_key_for_semantic_role(template, semantic_role)
    return fields.get(key) if key else None


def _resolve_semantic_profession_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    """Bridge the declared actor semantics to the existing preset strategy.

    Profile storage remains package-defined.  The compatibility names below
    exist only in the temporary strategy input and are never persisted as
    actor fields.
    """

    profession_key = _field_key_for_semantic_role(
        template, _ACTOR_PROFESSION_ROLE
    )
    primary_key = _field_key_for_semantic_role(
        template, _ACTOR_PRIMARY_STAT_ROLE
    )
    secondary_key = _field_key_for_semantic_role(
        template, _ACTOR_SECONDARY_STAT_ROLE
    )
    missing_roles = [
        role
        for role, key in (
            (_ACTOR_PROFESSION_ROLE, profession_key),
            (_ACTOR_PRIMARY_STAT_ROLE, primary_key),
            (_ACTOR_SECONDARY_STAT_ROLE, secondary_key),
        )
        if not key
    ]
    if missing_roles:
        raise ValueError(
            "职业预设属性策略缺少语义字段：" + "、".join(missing_roles)
        )
    stats = dict(template.get("stats") or {})
    selector = dict(stats.get("preset_selector") or {})
    if profession_key:
        selector["field"] = profession_key
    stats["preset_selector"] = selector
    bonus_choices = []
    for raw in stats.get("bonus_choices") or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        declared_field = str(item.get("field") or "")
        if primary_key and declared_field == primary_key:
            item["field"] = "primary_attribute"
        elif secondary_key and declared_field == secondary_key:
            item["field"] = "secondary_attribute"
        bonus_choices.append(item)
    stats["bonus_choices"] = bonus_choices
    strategy_template = {**dict(template), "stats": stats}
    strategy_fields = dict(fields)
    if primary_key:
        strategy_fields["primary_attribute"] = fields.get(primary_key)
    if secondary_key:
        strategy_fields["secondary_attribute"] = fields.get(secondary_key)
    return resolve_profession_stats(
        strategy_template,
        strategy_fields,
        require_complete=require_complete,
    )


def _repair_semantic_profession_draft(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    current_step: int,
) -> tuple[dict[str, Any], int]:
    if not uses_profession_preset_stats(template):
        return fields, current_step
    profession_key = _field_key_for_semantic_role(
        template, _ACTOR_PROFESSION_ROLE
    )
    if not profession_key or not fields.get(profession_key):
        return fields, current_step
    resolved = _resolve_semantic_profession_stats(
        template, fields, require_complete=False
    )
    fields["profession_base_stats"] = resolved["base"]
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    repaired_step = next_fillable_card_step(
        template, template["fields"], current_step, fields
    )
    return fields, repaired_step



__all__ = [name for name in globals() if not name.startswith('__')]
