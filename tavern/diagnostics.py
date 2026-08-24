from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .constants import DATABASE_SCHEMA_VERSION, PLUGIN_VERSION
from .operations import recovery_summary
from .world_contract import world_contract


_SECRET_KEYS = re.compile(
    r"(?:token|secret|password|credential|cookie|authorization|bearer|"
    r"api[_-]?key|encryption[_-]?key|signing[_-]?key|private_origin|"
    r"unified_origin|system_prompt)",
    re.I,
)
_IDENTITY_KEYS = frozenset(
    {
        "actor_id",
        "actor_ref",
        "character_id",
        "controller_user_id",
        "group_id",
        "group_user_id",
        "idempotency_key",
        "operation_key",
        "owner_ref",
        "owner_user_id",
        "participant_id",
        "participant_ref",
        "private_user_id",
        "receipt_key",
        "request_key",
        "session_id",
        "transport_event_id",
        "user_id",
    }
)
_IDENTITY_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:actor|character|controller|group|owner|participant|private_user|"
    r"session|transport_event|user)_(?:id|ref)$",
    re.I,
)
_SAFE_CONTENT_ID_KEYS = frozenset(
    {
        "action_type_id",
        "action_type_ref",
        "attribute_id",
        "attribute_ref",
        "capability_id",
        "capability_ref",
        "challenge_id",
        "challenge_ref",
        "clock_id",
        "clock_ref",
        "clue_id",
        "clue_ref",
        "conflict_id",
        "conflict_ref",
        "conflict_template_id",
        "currency_id",
        "currency_ref",
        "effect_id",
        "effect_ref",
        "element_id",
        "element_ref",
        "ending_id",
        "ending_ref",
        "environment_id",
        "environment_ref",
        "faction_id",
        "faction_ref",
        "hazard_id",
        "hazard_ref",
        "item_id",
        "item_ref",
        "location_id",
        "location_ref",
        "module_id",
        "module_ref",
        "npc_id",
        "npc_ref",
        "objective_id",
        "objective_ref",
        "organization_id",
        "organization_ref",
        "package_id",
        "package_ref",
        "provider_id",
        "quest_id",
        "quest_ref",
        "recipe_id",
        "recipe_ref",
        "relationship_id",
        "relationship_ref",
        "resolution_method_id",
        "resolution_method_ref",
        "resource_id",
        "resource_ref",
        "runtime_effect_id",
        "runtime_effect_ref",
        "scene_id",
        "scene_ref",
        "site_id",
        "site_ref",
        "state_id",
        "state_ref",
        "stat_id",
        "stat_ref",
        "template_id",
        "template_ref",
        "threat_id",
        "threat_ref",
        "trait_id",
        "trait_ref",
        "world_id",
        "world_ref",
        "zone_id",
        "zone_ref",
    }
)
_CONTENT_REF_VALUE = re.compile(
    r"^(?:action_type|attribute|capability|challenge|clock|clue|conflict|currency|"
    r"effect|element|ending|environment|faction|hazard|item|location|module|npc|"
    r"objective|organization|package|provider|quest|recipe|relationship|"
    r"resolution_method|resource|runtime_effect|scene|site|state|stat|template|"
    r"threat|trait|world|zone):",
    re.I,
)
_NON_IDENTITY_CONTAINERS = frozenset(
    {
        "actions",
        "attributes",
        "choices",
        "modes",
        "options",
        "outcomes",
        "phases",
        "results",
        "states",
    }
)


def _singular_key(key: str) -> str:
    lowered = str(key or "").strip().lower()
    if lowered.endswith("_ids") or lowered.endswith("_refs"):
        return lowered[:-1]
    return lowered


def _is_identity_field(key: str, value: Any, path: tuple[str, ...]) -> bool:
    lowered = _singular_key(key)
    if path and path[0].lower() == "privacy":
        return False
    if lowered in _IDENTITY_KEYS or _IDENTITY_KEY_PATTERN.search(lowered):
        return True
    if lowered in _SAFE_CONTENT_ID_KEYS:
        return False
    if not (lowered in {"id", "ref"} or lowered.endswith(("_id", "_ref"))):
        return False
    if isinstance(value, str) and _CONTENT_REF_VALUE.match(value.strip()):
        return False
    if lowered in {"id", "ref"} and any(
        part.lower() in _NON_IDENTITY_CONTAINERS for part in path[:-1]
    ):
        return False
    return True


def _mask(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return "anon_" + hashlib.sha256(
        ("tavern-diagnostic-identity\0" + text).encode("utf-8")
    ).hexdigest()


def _redact(
    value: Any,
    *,
    key: str,
    path: tuple[str, ...],
    force_identity: bool = False,
) -> Any:
    if _SECRET_KEYS.search(key):
        return "[REDACTED]"
    identity_field = force_identity or _is_identity_field(key, value, path)
    if isinstance(value, Mapping):
        return {
            str(nested_key): _redact(
                nested_value,
                key=str(nested_key),
                path=(*path, str(nested_key)),
            )
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _redact(
                item,
                key=key,
                path=(*path, "[]"),
                force_identity=identity_field,
            )
            for item in value
        ]
    if identity_field:
        return _mask(value)
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…[TRUNCATED]"
    return value


def redact(value: Any, key: str = "") -> Any:
    """Return one recursively redacted diagnostic value.

    Identity tokens are deterministic within and across diagnostic sections so
    support staff can correlate one actor or session without receiving the raw
    identifier.  Content references and rule enums remain readable.
    """

    initial_path = (str(key),) if key else ()
    return _redact(value, key=str(key), path=initial_path)


async def build_diagnostic_report(database: Any, session_id: str) -> dict[str, Any]:
    session = await database.get_session(session_id)
    session = dict(session)
    world_state = session.get("world_state") or {}
    session["world_state"] = {
        "location": world_state.get("location", ""),
        "time": world_state.get("time", ""),
        "scene_summary": world_state.get("scene_summary", ""),
        "fact_count": len(world_state.get("facts", []))
        if isinstance(world_state.get("facts"), list)
        else 0,
    }
    instance = await database.get_instance_config(session_id)
    events = await database.recent_events(session_id, 30)
    for item in events:
        if item.get("role") == "player":
            item["content"] = "[PLAYER INPUT REDACTED]"
        else:
            item["content"] = str(item.get("content") or "")[:1200]
    audit = await database.list_audit(session_id, 100, 0)
    for item in audit:
        if str(item.get("action") or "").startswith("operation."):
            item["target"] = _mask(item.get("target"))
    operations = await database.list_session_operations(session_id, 50)
    active_choices = await database.active_choice_set(session_id)
    active_vote = await database.active_vote(session_id)
    storage = await database.get_storage_info(session_id)
    providers = await database.list_provider_health()
    contract = world_contract(instance.get("world_snapshot") or {})
    receipt_table_rows = await database.execute_read(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name IN (
            'card_review_receipts',
            'supplement_action_receipts'
        )
        ORDER BY name
        """
    )
    receipt_tables = {str(item.get("name") or "") for item in receipt_table_rows}
    report = {
        "format": "astrbot-tavern-diagnostic",
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "plugin_version": PLUGIN_VERSION,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
        "database_contract": {
            "schema_version": DATABASE_SCHEMA_VERSION,
            "receipt_tables": {
                "card_review_receipts": "card_review_receipts" in receipt_tables,
                "supplement_action_receipts": (
                    "supplement_action_receipts" in receipt_tables
                ),
            },
        },
        "session": session,
        "contract_summary": {
            "version": contract.get("version"),
            "stats_mode": contract.get("stats", {}).get("mode"),
            "resolution_mode": contract.get("resolution", {}).get("mode"),
            "dice_system": contract.get("resolution", {}).get("dice_system"),
            "attribute_ids": [x.get("key") for x in contract.get("attributes", [])],
        },
        "workflow": {
            "active_choice_set": active_choices,
            "active_vote": active_vote,
            "operations": operations,
            "recovery": recovery_summary(
                operations,
                session_state=str(session.get("state") or ""),
                has_active_choices=bool(active_choices),
                has_active_vote=bool(active_vote),
            ),
        },
        "recent_events": events,
        "audit": audit,
        "provider_health": providers,
        "storage": storage,
        "privacy": {
            "user_ids": "hashed",
            "stable_identity_ids_and_refs": "consistently_hashed",
            "same_identity_is_correlatable": True,
            "world_content_refs_and_rule_enums": "preserved",
            "credentials": "removed",
            "system_prompt": "removed",
            "private_fields": "removed",
        },
    }
    return redact(report)
