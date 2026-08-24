"""稳定物品目录与实例层。

武器、防具、行囊、标志物品、商店与制作统一引用稳定物品 ID；实例记录所有者、
数量、品质、耐久、充能、绑定、来源与容器。世界包不能携带脚本，目录只读。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
import re

ITEM_KINDS = frozenset(
    {
        "weapon", "armor", "gear", "consumable", "material",
        "relic", "container", "tool", "document", "currency",
    }
)
SLOT_KEYS = frozenset(
    {"hand_main", "hand_off", "two_hand", "body", "head", "accessory", "none"}
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip()[:maximum]


def _int(value: Any, default: int = 0, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def item_definitions(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从世界快照提取规范化物品目录。"""
    rules = _mapping(world.get("rules"))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    sources: list[tuple[list[Any], str]] = []

    objects = _mapping(rules.get("objects"))
    raw_definitions = objects.get("definitions", [])
    if isinstance(raw_definitions, Sequence) and not isinstance(raw_definitions, (str, bytes)):
        sources.append((list(raw_definitions), "objects.definitions"))
    catalog = rules.get("item_catalog")
    if isinstance(catalog, Sequence) and not isinstance(catalog, (str, bytes)):
        sources.append((list(catalog), "item_catalog"))
    catalog_map = rules.get("item_catalog")
    if isinstance(catalog_map, Mapping):
        raw_list = catalog_map.get("definitions", catalog_map.get("items", []))
        if isinstance(raw_list, Sequence) and not isinstance(raw_list, (str, bytes)):
            sources.append((list(raw_list), "item_catalog.definitions"))
    inventory_module = _mapping(rules.get("items_inventory"))
    raw_inventory = inventory_module.get(
        "definitions",
        inventory_module.get("items", []),
    )
    if isinstance(raw_inventory, Sequence) and not isinstance(
        raw_inventory,
        (str, bytes),
    ):
        sources.append(
            (list(raw_inventory), "items_inventory.definitions")
        )

    for raw_items, source in sources:
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item_id = _text(
                raw.get("item_id") or raw.get("id") or raw.get("object_id") or raw.get("name"),
                128,
            )
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            label = _text(raw.get("label") or raw.get("name") or item_id, 120)
            kind = _text(raw.get("kind") or raw.get("type") or "gear", 40).lower()
            if kind not in ITEM_KINDS:
                kind = "gear"
            slot = _text(raw.get("slot") or raw.get("equip_slot") or "none", 40).lower()
            if slot not in SLOT_KEYS:
                slot = "none"
            result.append(
                {
                    "item_id": item_id,
                    "label": label,
                    "kind": kind,
                    "slot": slot,
                    "hands": _text(raw.get("hands") or raw.get("handedness") or "", 20),
                    "weight": _text(raw.get("weight") or "", 40),
                    "durability_max": _int(raw.get("durability_max") or raw.get("durability"), 0, 0, 10000),
                    "charges_max": _int(raw.get("charges_max") or raw.get("charges"), 0, 0, 10000),
                    "tags": sorted({_text(tag, 80) for tag in _sequence(raw.get("tags")) if _text(tag, 80)}),
                    "description": _text(raw.get("description") or raw.get("summary") or "", 1000),
                    "effects": _sequence(raw.get("effects") or raw.get("provides")),
                    "source": source,
                }
            )
    return result


def item_definition(world: Mapping[str, Any], item_id: str) -> dict[str, Any]:
    item_id = _text(item_id, 128)
    for item in item_definitions(world):
        if item["item_id"] == item_id:
            return item
    return {}


def resolve_item_ref(world: Mapping[str, Any], ref: str) -> str:
    """把 item:xxx 或裸名称解析为稳定 ID；找不到时原样返回空。"""
    text = _text(ref, 128)
    if not text:
        return ""
    if item_definition(world, text):
        return text
    if text.startswith("item:"):
        candidate = text.removeprefix("item:")
        if item_definition(world, candidate):
            return candidate
        return ""
    # 允许按显示名解析
    lowered = text.casefold()
    for item in item_definitions(world):
        if item["label"].casefold() == lowered:
            return item["item_id"]
    # One deterministic local normalization pass handles harmless punctuation,
    # spacing and ``item`` prefix drift.  Ambiguous matches are rejected.
    token = re.sub(r"[\W_]+", "", lowered, flags=re.UNICODE)
    matches = [
        item["item_id"]
        for item in item_definitions(world)
        if token
        and token
        in {
            re.sub(
                r"[\W_]+",
                "",
                str(item["item_id"]).casefold(),
                flags=re.UNICODE,
            ),
            re.sub(
                r"[\W_]+",
                "",
                str(item["label"]).casefold(),
                flags=re.UNICODE,
            ),
        }
    ]
    if len(set(matches)) == 1:
        return matches[0]
    return ""


def item_candidate_projection(
    world: Mapping[str, Any],
    session: Mapping[str, Any] | None,
    participant: Mapping[str, Any] | None,
    *,
    limit: int = 32,
) -> dict[str, Any]:
    """Bound the item catalog for model consumption.

    Ranking is deterministic: carried items, currently exposed shops, scene /
    quest mentions, then a small stable catalog tail.  The model receives only
    stable refs and allowed persistence operations.
    """

    session = _mapping(session)
    participant = _mapping(participant)
    definitions = item_definitions(world)
    by_id = {str(item["item_id"]): item for item in definitions}
    participant_tokens = {
        str(participant.get("id") or ""),
        str(participant.get("participant_id") or ""),
        str(participant.get("group_user_id") or ""),
    }
    inventory_ids: set[str] = set()
    for raw in _sequence(session.get("item_instances")):
        if not isinstance(raw, Mapping):
            continue
        owner_ref = str(raw.get("owner_ref") or "")
        if owner_ref and owner_ref not in participant_tokens:
            continue
        item_id = resolve_item_ref(
            world,
            str(raw.get("item_id") or raw.get("item_ref") or ""),
        )
        if item_id:
            inventory_ids.add(item_id)

    rules = _mapping(world.get("rules"))
    economy = _mapping(rules.get("economy"))
    shops = _sequence(economy.get("shops"))
    shop_ids: set[str] = set()
    for shop in shops:
        if not isinstance(shop, Mapping):
            continue
        for raw in _sequence(
            shop.get("offers")
            or shop.get("inventory")
            or shop.get("items")
        ):
            if not isinstance(raw, Mapping):
                continue
            item_id = resolve_item_ref(
                world,
                str(
                    raw.get("item_ref")
                    or raw.get("item_id")
                    or raw.get("item")
                    or ""
                ),
            )
            if item_id:
                shop_ids.add(item_id)

    context_parts = [
        json.dumps(
            session.get("world_state") or {},
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            session.get("story_ledger") or [],
            ensure_ascii=False,
            sort_keys=True,
        ),
        json.dumps(
            session.get("session_characters") or [],
            ensure_ascii=False,
            sort_keys=True,
        ),
    ]
    context = "\n".join(context_parts).casefold()
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item_id, item in by_id.items():
        reasons: list[str] = []
        score = 10
        if item_id in inventory_ids:
            score += 100
            reasons.append("carried_by_actor")
        if item_id in shop_ids:
            score += 80
            reasons.append("available_in_shop")
        searchable = [
            str(item.get("label") or ""),
            item_id,
            *[str(tag) for tag in item.get("tags") or []],
        ]
        if any(token and token.casefold() in context for token in searchable):
            score += 50
            reasons.append("scene_or_quest_match")
        if not reasons:
            reasons.append("catalog_tail")
        allowed_ops = ["grant"]
        if item_id in inventory_ids:
            allowed_ops.extend(["consume", "transfer"])
        ranked.append(
            (
                -score,
                item_id,
                {
                    "item_ref": (
                        item_id
                        if item_id.startswith("item:")
                        else f"item:{item_id}"
                    ),
                    "label": str(item.get("label") or item_id),
                    "kind": str(item.get("kind") or "gear"),
                    "allowed_ops": allowed_ops,
                    "reasons": reasons,
                },
            )
        )
    bounded = max(1, min(64, int(limit or 32)))
    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    items = [entry[2] for entry in ranked[:bounded]]
    return {
        "schema": "tavern-entity-candidates/1.0.0-rc10",
        "kind": "item",
        "limit": bounded,
        "catalog_count": len(definitions),
        "truncated": len(definitions) > len(items),
        "items": items,
        "narrative_prop_policy": (
            "未持久化的临时物件使用 prop 或 mention；"
            "grant/consume/transfer 只能引用本列表 item_ref。"
        ),
    }


def item_label(world: Mapping[str, Any], item_id: str) -> str:
    definition = item_definition(world, item_id)
    return definition.get("label") or item_id or item_id


def normalize_item_instance(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _text(raw.get("id"), 160),
        "session_id": _text(raw.get("session_id"), 128),
        "owner_type": _text(raw.get("owner_type") or "character", 40),
        "owner_ref": _text(raw.get("owner_ref"), 128),
        "item_id": _text(raw.get("item_id"), 128),
        "quantity": _int(raw.get("quantity"), 1, 1, 1_000_000),
        "quality": _text(raw.get("quality") or "standard", 40),
        "durability": _int(raw.get("durability"), 0, 0, 10000),
        "charges": _int(raw.get("charges"), 0, 0, 10000),
        "binding": _text(raw.get("binding") or "none", 40),
        "container": _text(raw.get("container"), 120),
        "source": _text(raw.get("source"), 200),
        "state": _mapping(raw.get("state") or raw.get("state_json")),
    }


def card_item_grants(
    world: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Build the one structured grant plan used by preview and commit."""
    from .card_wizard import PRESET_REFS_KEY

    refs = fields.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    plan: dict[str, int] = {}
    structured: dict[
        tuple[str, str, str, str], dict[str, Any]
    ] = {}
    sources: list[str] = []
    unknown: list[dict[str, str]] = []

    def add_grant(
        raw: Mapping[str, Any],
        *,
        dimension: str,
        option_id: str,
    ) -> None:
        item_ref = (
            raw.get("item_ref")
            or raw.get("item")
            or raw.get("item_id")
            or raw.get("ref")
        )
        item_id = resolve_item_ref(world, str(item_ref or ""))
        if not item_id:
            unknown.append(
                {
                    "field": dimension,
                    "option_id": option_id,
                    "item_ref": str(item_ref or ""),
                }
            )
            return
        quantity = max(
            1, _int(raw.get("quantity", raw.get("qty", 1)), 1, 1, 1000)
        )
        owner_scope = str(raw.get("owner_scope") or "character").strip().lower()
        if owner_scope not in {"character", "party"}:
            raise ValueError(
                f"item_grants.owner_scope 必须是 character 或 party：{owner_scope}"
            )
        container = _text(raw.get("container"), 120)
        state = _mapping(raw.get("state") or raw.get("initial_state"))
        source_ref = f"card:{dimension}:{option_id}"
        state_token = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        identity = (owner_scope, item_id, container, state_token)
        current = structured.get(identity)
        if current:
            current["quantity"] = int(current["quantity"]) + quantity
            current["sources"].append(source_ref)
        else:
            structured[identity] = {
                "item_id": item_id,
                "item_ref": f"item:{item_id}",
                "item_label": item_label(world, item_id),
                "quantity": quantity,
                "owner_scope": owner_scope,
                "container": container,
                "state": state,
                "source": "character_card",
                "sources": [source_ref],
            }
        if owner_scope == "character" and not container and not state:
            plan[item_id] = plan.get(item_id, 0) + quantity
        sources.append(source_ref)

    for dimension, selected in refs.items():
        options = selected if isinstance(selected, list) else [selected]
        for option in options:
            if not isinstance(option, Mapping):
                continue
            snapshot = option.get("snapshot")
            snapshot = snapshot if isinstance(snapshot, Mapping) else {}
            # TWP candidate ``grants`` is the general capability/resource/track
            # effect contract.  Item seeding has its own explicit
            # ``item_grants`` field; conflating the two turns a capability grant
            # into an empty item reference and blocks otherwise complete cards.
            grants = snapshot.get("item_grants")
            option_id = str(option.get("id") or "")
            if isinstance(grants, Mapping):
                for item_ref, qty in grants.items():
                    add_grant(
                        {"item_ref": item_ref, "quantity": qty},
                        dimension=str(dimension),
                        option_id=option_id,
                    )
            elif isinstance(grants, Sequence) and not isinstance(grants, (str, bytes)):
                for raw in grants:
                    if not isinstance(raw, Mapping):
                        continue
                    add_grant(
                        raw,
                        dimension=str(dimension),
                        option_id=option_id,
                    )
    if strict and unknown:
        refs_text = "、".join(
            sorted({item["item_ref"] or "（空）" for item in unknown})
        )
        raise ValueError(f"角色卡物品授予引用了未注册物品：{refs_text}")
    grants_result = []
    for entry in structured.values():
        normalized = dict(entry)
        normalized["sources"] = sorted(set(normalized["sources"]))
        grants_result.append(normalized)
    grants_result.sort(
        key=lambda item: (
            str(item["owner_scope"]),
            str(item["item_id"]),
            str(item["container"]),
        )
    )
    return {
        "schema": "tavern-card-item-grant-plan/1.0.0-rc10",
        "grants": grants_result,
        "items": plan,
        "sources": sorted(set(sources)),
        "unknown": unknown,
    }


__all__ = [
    "ITEM_KINDS",
    "SLOT_KEYS",
    "card_item_grants",
    "item_definition",
    "item_definitions",
    "item_label",
    "item_candidate_projection",
    "normalize_item_instance",
    "resolve_item_ref",
]
