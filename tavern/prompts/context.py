from .common import *
from .system import *

def _party(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in roster:
        if not isinstance(item, Mapping) or item.get(
            "participation_status"
        ) not in {"active", "standby", "away"}:
            continue
        profile = item.get("card_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        runtime = item.get("runtime_state")
        runtime = runtime if isinstance(runtime, Mapping) else {}
        # 非行动角色只暴露现场可观察信息，避免模型把其性格、秘密、
        # 专长或决定权混入下一位玩家的行动选项。
        result.append(
            {
                "participant_id": item.get("id"),
                "character_name": item.get("character_name"),
                "character_code": item.get("character_code"),
                "participation_status": item.get("participation_status"),
                "public_appearance": profile.get("appearance", ""),
                "visible_location": runtime.get("current_location", ""),
                "visible_statuses": runtime.get("statuses", []),
            }
        )
    return result


def _character_projection(
    value: Mapping[str, Any],
    world: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    profile = value.get("profile")
    if not isinstance(profile, Mapping):
        profile = value.get("card_profile")
    stats = value.get("stats")
    if not isinstance(stats, Mapping):
        stats = value.get("card_stats")
    runtime = value.get("runtime_state")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    compact_stats: dict[str, Any] = {}
    if isinstance(stats, Mapping):
        for key in (
            "profession",
            "primary",
            "secondary",
            "modifiers",
            "effective",
            "labels",
        ):
            value_at_key = stats.get(key)
            if value_at_key not in (None, "", [], {}):
                compact_stats[key] = value_at_key
        if not compact_stats:
            # Older cards expose only the effective attribute map as ``base``.
            base = stats.get("base")
            if isinstance(base, Mapping):
                compact_stats["attributes"] = dict(base)
    result = {
        "participant_id": value.get("participant_id") or value.get("id"),
        "character_name": value.get("character_name"),
        "display_name": value.get("display_name"),
        "stats": compact_stats,
        "runtime_state": {
            key: runtime.get(key)
            for key in ("statuses", "current_location", "resources", "reputation")
            if key in runtime
        },
        "participation_status": value.get("participation_status"),
    }
    if world is not None:
        actor_view = project_actor_view(
            world,
            profile if isinstance(profile, Mapping) else {},
            viewer_role="character",
        )
        visible_sections: list[dict[str, Any]] = []
        for section in actor_view.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            visible_items = []
            for item in section.get("items") or []:
                if not isinstance(item, Mapping):
                    continue
                display = item.get("display_value")
                if display in (None, "", [], {}):
                    continue
                visible_items.append(
                    {
                        "label": item.get("label"),
                        "value": display,
                        "role": item.get("role"),
                    }
                )
            if visible_items:
                visible_sections.append(
                    {
                        "label": section.get("label"),
                        "items": visible_items,
                    }
                )
        semantic_values = {
            str(key): item
            for key, item in (actor_view.get("semantic_values") or {}).items()
            if item not in (None, "", [], {})
        }
        result["actor_view"] = {
            key: actor_view.get(key)
            for key in ("title", "subtitle")
            if actor_view.get(key)
        }
        if semantic_values:
            result["actor_view"]["semantic_values"] = semantic_values
        if visible_sections:
            result["actor_view"]["sections"] = visible_sections
    else:
        result["profile"] = dict(profile) if isinstance(profile, Mapping) else {}
    return result


def compact_character(value: Mapping[str, Any]) -> dict[str, Any]:
    """Public helper for dedicated option prompts and context-size tests."""
    return _character_projection(value)


def _npc_projection(
    characters: Sequence[Mapping[str, Any]],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    presets: dict[str, Mapping[str, Any]] = {}
    for item in world.get("characters", []):
        if not isinstance(item, Mapping) or not item.get("enabled", True):
            continue
        for key in (item.get("id"), item.get("slug"), item.get("name")):
            if key:
                presets[str(key)] = item
    result: list[dict[str, Any]] = []
    for item in characters:
        if not isinstance(item, Mapping):
            continue
        preset = presets.get(str(item.get("stable_key") or "")) or presets.get(
            str(item.get("name") or "")
        )
        profile = item.get("public_profile")
        if not isinstance(profile, Mapping) and isinstance(preset, Mapping):
            profile = preset.get("profile")
        row = {
            "npc_id": item.get("id"),
            "stable_key": item.get("stable_key"),
            "name": item.get("name"),
            "aliases": item.get("aliases", []),
            "role_type": item.get("role_type"),
            "public_profile": dict(profile) if isinstance(profile, Mapping) else {},
            "known_facts": item.get("known_facts", []),
            "misconceptions": item.get("misconceptions", []),
            "runtime_state": item.get("state", {}),
        }
        if isinstance(preset, Mapping) and preset.get("prompt"):
            row["private_direction"] = preset.get("prompt")
        result.append(row)
    return result


def _memory_projection(memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "id",
        "scope",
        "scope_id",
        "kind",
        "content",
        "importance",
        "tags",
        "visibility",
        "locked",
        "pinned",
    )
    return [{key: item.get(key) for key in keys if key in item} for item in memories]


__all__ = [name for name in globals() if not name.startswith('__')]

