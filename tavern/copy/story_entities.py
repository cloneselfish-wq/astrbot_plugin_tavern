from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .entities import ENTITY_MARKS, decorate_entity


_WORLD_GROUPS = {
    "characters": "character",
    "npcs": "character",
    "items": "item",
    "equipment": "item",
    "abilities": "ability",
    "skills": "ability",
    "quests": "quest",
    "factions": "faction",
    "organizations": "faction",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _add(
    result: dict[str, dict[str, str]],
    *,
    entity_type: str,
    ref: Any,
    label: Any,
    allow_short: bool = False,
) -> None:
    kind = _text(entity_type).casefold()
    stable_ref = _text(ref)
    name = _text(label)
    if kind not in ENTITY_MARKS or (len(name) < 2 and not allow_short):
        return
    row = {"type": kind, "ref": stable_ref or name, "label": name}
    for key in (stable_ref, name):
        if key and key not in result:
            result[key] = row


def build_story_entity_catalog(
    world: Mapping[str, Any],
    roster: Sequence[Mapping[str, Any]] = (),
    session_characters: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for item in roster:
        if not isinstance(item, Mapping):
            continue
        _add(
            result,
            entity_type="character",
            ref=item.get("id") or item.get("participant_id"),
            label=item.get("character_name") or item.get("display_name"),
            allow_short=True,
        )
    for item in session_characters:
        if not isinstance(item, Mapping):
            continue
        _add(
            result,
            entity_type="character",
            ref=item.get("id") or item.get("stable_key"),
            label=item.get("name"),
            allow_short=True,
        )
    for key, entity_type in _WORLD_GROUPS.items():
        rows = world.get(key)
        if not isinstance(rows, Sequence) or isinstance(
            rows, (str, bytes, bytearray)
        ):
            continue
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            _add(
                result,
                entity_type=entity_type,
                ref=item.get("id") or item.get("slug") or item.get("stable_key"),
                label=item.get("name") or item.get("label") or item.get("title"),
            )
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    item_catalog = rules.get("item_catalog")
    item_catalog = (
        item_catalog.values()
        if isinstance(item_catalog, Mapping)
        else item_catalog or ()
    )
    for item in item_catalog:
        if isinstance(item, Mapping):
            _add(
                result,
                entity_type="item",
                ref=item.get("id") or item.get("item_id"),
                label=item.get("label") or item.get("name"),
            )
    scene_graph = rules.get("scene_graph")
    scene_graph = scene_graph if isinstance(scene_graph, Mapping) else {}
    for item in scene_graph.get("nodes") or []:
        if isinstance(item, Mapping):
            _add(
                result,
                entity_type="location",
                ref=item.get("id"),
                label=item.get("label") or item.get("name") or item.get("title"),
            )
    quest_graph = rules.get("quest_graph")
    quest_graph = quest_graph if isinstance(quest_graph, Mapping) else {}
    for item in quest_graph.get("quests") or quest_graph.get("nodes") or []:
        if isinstance(item, Mapping):
            _add(
                result,
                entity_type="quest",
                ref=item.get("id"),
                label=item.get("label") or item.get("name") or item.get("title"),
            )
    return result


def normalize_entity_mentions(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return ()
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value[:64]:
        if not isinstance(raw, Mapping):
            continue
        entity_type = _text(raw.get("type")).casefold()
        ref = _text(raw.get("ref"))[:160]
        surface = _text(raw.get("surface"))[:160]
        key = (entity_type, ref, surface)
        if entity_type not in ENTITY_MARKS or not ref or not surface or key in seen:
            continue
        seen.add(key)
        result.append({"type": entity_type, "ref": ref, "surface": surface})
    return tuple(result)


def decorate_story_entities(
    narrative: str,
    *,
    mentions: Sequence[Mapping[str, Any]] = (),
    catalog: Mapping[str, Mapping[str, str]],
) -> str:
    """Decorate only catalog-backed exact surfaces; repeated calls are stable."""

    text = _text(narrative)
    if not text:
        return ""
    candidates: dict[str, str] = {}
    for mention in mentions:
        if not isinstance(mention, Mapping):
            continue
        ref = _text(mention.get("ref"))
        surface = _text(mention.get("surface"))
        row = catalog.get(ref) or catalog.get(surface)
        if not isinstance(row, Mapping):
            continue
        label = _text(row.get("label"))
        if surface not in {label, ref} or surface not in text:
            continue
        candidates[surface] = _text(row.get("type"))
    # Known player and world labels are authoritative even if a provider omitted
    # the optional mention list. Short one-character labels are intentionally
    # excluded by the catalog builder to avoid ordinary-word replacements.
    for row in catalog.values():
        label = _text(row.get("label"))
        if label and label in text:
            candidates.setdefault(label, _text(row.get("type")))
    for surface in sorted(candidates, key=lambda item: (-len(item), item)):
        marked = decorate_entity(candidates[surface], surface)
        if not marked or marked == surface:
            continue
        opening, closing = ENTITY_MARKS[candidates[surface]]
        pattern = re.compile(
            rf"(?<!{re.escape(opening)}){re.escape(surface)}(?!{re.escape(closing)})"
        )
        text = pattern.sub(marked, text)
    return text


__all__ = [
    "build_story_entity_catalog",
    "decorate_story_entities",
    "normalize_entity_mentions",
]
