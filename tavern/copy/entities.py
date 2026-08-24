from __future__ import annotations

from dataclasses import dataclass


ENTITY_MARKS = {
    "character": ("「", "」"),
    "person": ("「", "」"),
    "npc": ("「", "」"),
    "location": ("〔", "〕"),
    "scene": ("〔", "〕"),
    "item": ("『", "』"),
    "equipment": ("『", "』"),
    "ability": ("〈", "〉"),
    "skill": ("〈", "〉"),
    "specialty": ("〈", "〉"),
    "status": ("〈", "〉"),
    "quest": ("《", "》"),
    "chapter": ("《", "》"),
    "story": ("《", "》"),
    "faction": ("〖", "〗"),
    "organization": ("〖", "〗"),
}


@dataclass(frozen=True, slots=True)
class EntityToken:
    entity_type: str
    label: str
    entity_id: str = ""
    visibility: str = "public"


def decorate_entity(entity_type: str, label: str) -> str:
    text = str(label or "").strip()
    if not text:
        return ""
    marks = ENTITY_MARKS.get(str(entity_type or "").casefold())
    if not marks:
        return text
    if text.startswith(marks[0]) and text.endswith(marks[1]):
        return text
    return f"{marks[0]}{text}{marks[1]}"
