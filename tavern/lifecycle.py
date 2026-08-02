from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .security import clean_text
from .world_contract import attribute_lookup, stats_mode, world_contract


CARD_UNCREATED = "uncreated"
CARD_DRAFT = "draft"
CARD_PENDING = "pending_review"
CARD_APPROVED = "approved"
CARD_REJECTED = "rejected"
CARD_STATUSES = {
    CARD_UNCREATED,
    CARD_DRAFT,
    CARD_PENDING,
    CARD_APPROVED,
    CARD_REJECTED,
}

PARTICIPANT_RESERVED = "reserved"
PARTICIPANT_ACTIVE = "active"
PARTICIPANT_STANDBY = "standby"
PARTICIPANT_AWAY = "away"
PARTICIPANT_RETIRED = "retired"
PARTICIPANT_ARCHIVED = "archived"
PARTICIPANT_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
    PARTICIPANT_RETIRED,
    PARTICIPANT_ARCHIVED,
}

SEAT_HOLDING_STATUSES = {
    PARTICIPANT_RESERVED,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_STANDBY,
    PARTICIPANT_AWAY,
}

CHOICE_KEYS = ("A", "B", "C", "D")

DEFAULT_TIME_RULES: dict[str, Any] = {
    "card_code_ttl_seconds": 30 * 60,
    "card_draft_ttl_seconds": 7 * 24 * 60 * 60,
    "card_completion_timeout_seconds": None,
    "preparation_timeout_seconds": None,
    "ready_timeout_seconds": None,
    "turn_timeout_seconds": None,
    "turn_reminder_seconds": None,
    "max_consecutive_timeouts": -1,
    "standby_timeout_seconds": None,
    "delegation_ttl_seconds": None,
    "vote_round_one_seconds": None,
    "vote_round_two_seconds": None,
    "vote_reminder_seconds": None,
    "all_idle_pause_seconds": None,
    "pause_stops_clock": True,
    "announce_timeouts": False,
    "turn_timeout_action": "hold",
    "card_timeout_action": "remind",
    "ready_timeout_action": "remind",
}

DEFAULT_CARD_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "key": "name",
        "label": "角色姓名",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "code",
        "label": "副本代号",
        "required": True,
        "private": False,
        "max_chars": 12,
    },
    {
        "key": "appearance",
        "label": "外貌特征",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "background",
        "label": "角色背景",
        "required": True,
        "private": False,
        "max_chars": 800,
    },
    {
        "key": "personality",
        "label": "性格与行事方式",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "goal",
        "label": "当前目标",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "belief",
        "label": "核心信念",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "bond",
        "label": "重要羁绊",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "specialties",
        "label": "专长标签（2—4 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 240,
    },
    {
        "key": "flaws",
        "label": "缺陷标签（1—2 个，以逗号分隔）",
        "required": True,
        "private": False,
        "max_chars": 200,
    },
    {
        "key": "weakness",
        "label": "弱点或限制",
        "required": True,
        "private": False,
        "max_chars": 300,
    },
    {
        "key": "knowledge_boundary",
        "label": "知识边界",
        "required": True,
        "private": False,
        "max_chars": 400,
    },
    {
        "key": "secret",
        "label": "私人秘密",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
    {
        "key": "content_boundaries",
        "label": "个人内容边界",
        "required": False,
        "private": True,
        "max_chars": 600,
    },
)

DEFAULT_CARD_STATS: dict[str, Any] = {
    "budget": 10,
    "attributes": [
        {
            "key": "body",
            "label": "体魄",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "agility",
            "label": "敏捷",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "will",
            "label": "意志",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
        {
            "key": "knowledge",
            "label": "学识",
            "minimum": 0,
            "maximum": 5,
            "default": 2,
        },
    ],
    "modifier_table": {
        "0": -3,
        "1": -2,
        "2": -1,
        "3": 0,
        "4": 1,
        "5": 2,
    },
}

DEFAULT_OPENING_CHOICES: tuple[dict[str, Any], ...] = (
    {
        "key": "A",
        "text": "先观察周围环境，确认眼前最明显的异常",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "B",
        "text": "与当前场景中最容易接触的人交谈，询问公开信息",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "C",
        "text": "检查自己能够合理接触的物品与随身资源",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
    {
        "key": "D",
        "text": "保持警戒，暂不冒进，等待局势显露更多线索",
        "risk": "low",
        "requires_check": False,
        "collective": False,
    },
)

DEFAULT_PROGRESS: dict[str, Any] = {
    "chapter": "序章",
    "current_objective": "等待剧情目标",
    "completed_milestones": 0,
    "total_milestones": 0,
}

DEFAULT_CONTENT_BOUNDARIES: dict[str, Any] = {
    "character_death": "ask",
    "player_conflict": "consent",
    "romance": "fade_to_black",
    "horror": "moderate",
    "sexual_content": "blocked",
}

DEFAULT_NPC_POLICY: dict[str, Any] = {
    "enabled": True,
    "auto_register": True,
    "max_new_per_turn": 3,
    "require_named_or_relevant": True,
    "generated_requires_review": True,
    "archive_after_inactive_rounds": 12,
}

DEFAULT_CONTEXT_BUDGET: dict[str, Any] = {
    "recent_turns": 12,
    "memories": 10,
    "active_npcs": 12,
    "ledger_items": 16,
    "locked_facts_always_include": True,
}

DEFAULT_DICE_RULES: dict[str, Any] = {
    "advantage": "2d20_keep_high",
    "disadvantage": "2d20_keep_low",
    "stacking": False,
    "opposites_cancel": True,
    "outcome_bands": True,
    "visibility": "public",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deadline_after(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))
    ).isoformat(timespec="seconds")


def _bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _optional_seconds(value: Any, default: int | None) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, str) and value.strip().lower() in {
        "",
        "unlimited",
        "none",
    }:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed == -1:
        return None
    if parsed == 0 or parsed < -1:
        raise ValueError("时间值必须大于 0，或使用 -1/留空表示不限时")
    return min(365 * 24 * 60 * 60, parsed)


def normalize_time_rules(value: Any = None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result = dict(DEFAULT_TIME_RULES)
    for key in (
        "card_code_ttl_seconds",
        "card_draft_ttl_seconds",
        "card_completion_timeout_seconds",
        "preparation_timeout_seconds",
        "ready_timeout_seconds",
        "turn_timeout_seconds",
        "turn_reminder_seconds",
        "standby_timeout_seconds",
        "delegation_ttl_seconds",
        "vote_round_one_seconds",
        "vote_round_two_seconds",
        "vote_reminder_seconds",
        "all_idle_pause_seconds",
    ):
        result[key] = _optional_seconds(source.get(key), result[key])
    raw_timeout_limit = source.get(
        "max_consecutive_timeouts",
        DEFAULT_TIME_RULES["max_consecutive_timeouts"],
    )
    try:
        timeout_limit = int(raw_timeout_limit)
    except (TypeError, ValueError):
        timeout_limit = int(DEFAULT_TIME_RULES["max_consecutive_timeouts"])
    if timeout_limit == 0 or timeout_limit < -1:
        raise ValueError("连续超时次数必须为 1—20，或 -1 表示永不自动转候补")
    result["max_consecutive_timeouts"] = (
        -1 if timeout_limit == -1 else min(20, max(1, timeout_limit))
    )
    result["pause_stops_clock"] = bool(
        source.get("pause_stops_clock", True)
    )
    result["announce_timeouts"] = bool(
        source.get("announce_timeouts", False)
    )
    timeout_action = str(
        source.get("turn_timeout_action", "hold")
    ).strip()
    result["turn_timeout_action"] = (
        timeout_action
        if timeout_action in {"skip", "hold"}
        else "hold"
    )
    for key, allowed, default in (
        (
            "card_timeout_action",
            {"standby", "release", "remind"},
            "remind",
        ),
        (
            "ready_timeout_action",
            {"standby", "remind"},
            "remind",
        ),
    ):
        action = str(source.get(key, default)).strip()
        result[key] = action if action in allowed else default

    turn_seconds = result["turn_timeout_seconds"]
    reminder = result["turn_reminder_seconds"]
    if (
        turn_seconds is not None
        and reminder is not None
        and reminder >= turn_seconds
    ):
        result["turn_reminder_seconds"] = max(1, turn_seconds // 3)
    return result


def world_time_rules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return normalize_time_rules({})
    return normalize_time_rules(rules.get("time_rules"))


def normalize_progress(value: Any = None) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    completed = _bounded_int(
        raw.get("completed_milestones"),
        0,
        0,
        1_000_000,
    )
    total = _bounded_int(
        raw.get("total_milestones"),
        0,
        0,
        1_000_000,
    )
    if total and completed > total:
        completed = total
    return {
        "chapter": clean_text(
            raw.get("chapter") or DEFAULT_PROGRESS["chapter"],
            max_chars=160,
        ),
        "current_objective": clean_text(
            raw.get("current_objective")
            or DEFAULT_PROGRESS["current_objective"],
            max_chars=400,
        ),
        "completed_milestones": completed,
        "total_milestones": total,
    }


def world_session_modules(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    state = world.get("initial_state")
    state = state if isinstance(state, Mapping) else {}
    progress = rules.get("progress")
    if not isinstance(progress, Mapping):
        progress = state.get("progress")
    content_boundaries = dict(DEFAULT_CONTENT_BOUNDARIES)
    if isinstance(rules.get("content_boundaries"), Mapping):
        content_boundaries.update(dict(rules["content_boundaries"]))
    npc_policy = dict(DEFAULT_NPC_POLICY)
    if isinstance(rules.get("npc_policy"), Mapping):
        npc_policy.update(dict(rules["npc_policy"]))
    npc_policy["max_new_per_turn"] = _bounded_int(
        npc_policy.get("max_new_per_turn"),
        3,
        0,
        3,
    )
    npc_policy["archive_after_inactive_rounds"] = _bounded_int(
        npc_policy.get("archive_after_inactive_rounds"),
        12,
        1,
        10_000,
    )
    context_budget = dict(DEFAULT_CONTEXT_BUDGET)
    if isinstance(rules.get("context_budget"), Mapping):
        context_budget.update(dict(rules["context_budget"]))
    for key, default, maximum in (
        ("recent_turns", 12, 50),
        ("memories", 10, 40),
        ("active_npcs", 12, 40),
        ("ledger_items", 16, 100),
    ):
        context_budget[key] = _bounded_int(
            context_budget.get(key),
            default,
            0,
            maximum,
        )
    dice_rules = dict(DEFAULT_DICE_RULES)
    if isinstance(rules.get("dice_rules"), Mapping):
        dice_rules.update(dict(rules["dice_rules"]))
    visibility = str(dice_rules.get("visibility") or "public").lower()
    dice_rules["visibility"] = (
        visibility
        if visibility in {"public", "immersive", "hidden"}
        else "public"
    )
    return {
        "progress": normalize_progress(progress),
        "content_boundaries": content_boundaries,
        "npc_policy": npc_policy,
        "context_budget": context_budget,
        "dice_rules": dice_rules,
        "recovery": {
            "state": "idle",
            "message": "",
            "operation_id": "",
            "updated_at": utc_now(),
        },
    }


def initial_character_runtime_state() -> dict[str, Any]:
    return {
        "inspiration": 1,
        "inspiration_max": 3,
        "statuses": [],
        "equipment": {},
        "known_clues": [],
        "npc_relationships": {},
        "temporary_traits": [],
        "reputation": {},
        "current_location": "",
    }


def player_limits(world: Mapping[str, Any]) -> dict[str, int]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("player_limits")
    raw = raw if isinstance(raw, Mapping) else {}
    maximum = _bounded_int(raw.get("maximum"), 4, 1, 32)
    minimum = _bounded_int(raw.get("minimum_start"), 2, 1, maximum)
    recommended_min = _bounded_int(
        raw.get("recommended_min"),
        min(2, maximum),
        1,
        maximum,
    )
    recommended_max = _bounded_int(
        raw.get("recommended_max"),
        maximum,
        recommended_min,
        maximum,
    )
    return {
        "minimum_start": minimum,
        "maximum": maximum,
        "recommended_min": recommended_min,
        "recommended_max": recommended_max,
    }


def card_template(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("character_card")
    raw = raw if isinstance(raw, Mapping) else {}
    fields_raw = raw.get("fields")
    fields: list[dict[str, Any]] = []
    if isinstance(fields_raw, Sequence) and not isinstance(
        fields_raw, (str, bytes)
    ):
        seen: set[str] = set()
        for item in fields_raw[:30]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or key in seen:
                continue
            seen.add(key)
            fields.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "required": bool(item.get("required", True)),
                    "private": bool(item.get("private", False)),
                    "max_chars": _bounded_int(
                        item.get("max_chars"),
                        500,
                        10,
                        4000,
                    ),
                    "type": (
                        str(item.get("type") or "text").lower()
                        if str(item.get("type") or "text").lower()
                        in {"text", "textarea", "integer", "select",
                            "multi_select", "boolean", "derived"}
                        else "text"
                    ),
                    **({"options": list(item.get("options") or [])}
                       if item.get("options") else {}),
                    **({"options_source": str(item.get("options_source"))}
                       if item.get("options_source") else {}),
                    **({"value_field": str(item.get("value_field"))}
                       if item.get("value_field") else {}),
                    **({"label_field": str(item.get("label_field"))}
                       if item.get("label_field") else {}),
                }
            )
    if not fields:
        fields = [dict(item) for item in DEFAULT_CARD_FIELDS]
    for field in fields:
        if str(field.get("key") or "") in {"name", "code"}:
            field["max_chars"] = min(
                12,
                int(field.get("max_chars", 12) or 12),
            )
    char_limit = _bounded_int(raw.get("field_char_limit"), 0, 0, 4000)
    if char_limit:
        for field in fields:
            key = str(field.get("key") or "")
            if key in {"name", "code"}:
                continue
            if str(field.get("type") or "") == "integer":
                continue
            field["max_chars"] = min(
                int(field.get("max_chars", 4000) or 4000),
                char_limit,
            )
    stats_raw = raw.get("stats")
    stats_raw = stats_raw if isinstance(stats_raw, Mapping) else {}
    mode = stats_mode(stats_raw)
    attributes_raw = stats_raw.get("attributes")
    attributes: list[dict[str, Any]] = []
    if isinstance(attributes_raw, Sequence) and not isinstance(
        attributes_raw,
        (str, bytes),
    ):
        for item in attributes_raw[:20]:
            if not isinstance(item, Mapping):
                continue
            key = re.sub(
                r"[^a-zA-Z0-9_-]",
                "",
                str(item.get("key") or "").strip(),
            )[:40]
            if not key or any(entry["key"] == key for entry in attributes):
                continue
            minimum = _bounded_int(item.get("minimum"), 0, -100, 100)
            maximum = _bounded_int(
                item.get("maximum"),
                5,
                minimum,
                100,
            )
            attributes.append(
                {
                    "key": key,
                    "label": clean_text(
                        item.get("label") or key,
                        max_chars=4000,
                    ),
                    "minimum": minimum,
                    "maximum": maximum,
                    "default": _bounded_int(
                        item.get("default"),
                        minimum,
                        minimum,
                        maximum,
                    ),
                }
            )
    if not attributes and mode != "none":
        attributes = [
            dict(item)
            for item in DEFAULT_CARD_STATS["attributes"]
        ]
    table_raw = stats_raw.get("modifier_table")
    table_raw = table_raw if isinstance(table_raw, Mapping) else {}
    modifier_table: dict[str, int] = {}
    for raw_value, raw_modifier in table_raw.items():
        try:
            value_key = str(int(raw_value))
            modifier = int(raw_modifier)
        except (TypeError, ValueError):
            continue
        modifier_table[value_key] = max(-10, min(10, modifier))
    if not modifier_table and mode != "none":
        modifier_table = dict(DEFAULT_CARD_STATS["modifier_table"])
    budget = _bounded_int(
        stats_raw.get("budget"),
        int(DEFAULT_CARD_STATS["budget"]),
        0,
        2000,
    )
    profession_mode = mode == "preset"
    # Profession-preset mode: keep the 10 attributes for checks/preview but do
    # NOT generate 10 manual stat-entry questions (doc §4.2).
    for attribute in (attributes if mode == "manual" else []):
        field_key = f"stat_{attribute['key']}"
        existing_field = next(
            (
                item
                for item in fields
                if str(item.get("key") or "") == field_key
            ),
            None,
        )
        if existing_field is not None:
            fields.remove(existing_field)
        fields.append(
            {
                "key": field_key,
                "label": clean_text(
                    (
                        existing_field.get("label")
                        if existing_field is not None
                        else ""
                    )
                    or (
                        f"{attribute['label']}数值"
                        f"（{attribute['minimum']}—{attribute['maximum']}，"
                        f"总预算 {budget}）"
                    ),
                    max_chars=4000,
                ),
                "required": True,
                "private": False,
                "max_chars": 12,
                "type": "integer",
                "minimum": attribute["minimum"],
                "maximum": attribute["maximum"],
                "default": attribute["default"],
                "stat_key": attribute["key"],
            }
        )
    return {
        "version": _bounded_int(raw.get("version"), 1, 1, 100000),
        "auto_approve": bool(raw.get("auto_approve", False)),
        "edit_requires_review": bool(
            raw.get("edit_requires_review", True)
        ),
        "fields": fields,
        "stats": {
            "mode": mode,
            "budget": budget,
            "attributes": attributes,
            "modifier_table": modifier_table,
            "input_mode": str(stats_raw.get("input_mode") or ""),
            "allocation_mode": str(
                stats_raw.get("allocation_mode") or ""
            ),
            "primary_bonus": _bounded_int(
                stats_raw.get("primary_bonus"), 7, 0, 100
            ),
            "secondary_bonus": _bounded_int(
                stats_raw.get("secondary_bonus"), 3, 0, 100
            ),
            "allocation": dict(stats_raw.get("allocation") or {}),
            "total_validation": dict(stats_raw.get("total_validation") or {}),
            "preset_selector": dict(stats_raw.get("preset_selector") or {}),
            "bonus_choices": list(stats_raw.get("bonus_choices") or []),
        },
        "profession_presets": list(raw.get("profession_presets") or []),
        "profession_mode": profession_mode,
    }


def card_stat_allocation(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
    current_step: int | None = None,
) -> dict[str, Any]:
    """Return authoritative progress for none, manual, or preset stats."""

    stats_config = template.get("stats") or {}
    mode = stats_mode(stats_config)
    if mode == "none":
        return {"mode": "none", "stat_fields": [], "current": None, "values": {}, "used": 0, "budget": 0, "remaining": 0, "complete": True}
    if uses_profession_preset_stats(template):
        # Profession-preset mode: stats are derived, never manually allocated.
        safe_fields = fields if isinstance(fields, Mapping) else {}
        try:
            resolved = resolve_profession_stats(
                template, safe_fields, require_complete=False
            )
        except ValueError:
            resolved = None
        total_validation = stats_config.get("total_validation") or {}
        final_total = int(total_validation.get("final_total", stats_config.get("budget", 0)))
        return {
            "mode": "preset",
            "stat_fields": [],
            "current": None,
            "values": resolved["raw"] if resolved else {},
            "base_values": resolved["base"] if resolved else {},
            "used": resolved["effective_total"] if resolved else 0,
            "budget": final_total,
            "remaining": (
                max(0, final_total - resolved["effective_total"])
                if resolved else final_total
            ),
            "resolved": resolved,
        }

    field_values = fields if isinstance(fields, Mapping) else {}
    definitions = template.get("fields")
    definitions = (
        list(definitions)
        if isinstance(definitions, Sequence)
        and not isinstance(definitions, (str, bytes))
        else []
    )
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    attributes_raw = stats.get("attributes")
    attributes = (
        list(attributes_raw)
        if isinstance(attributes_raw, Sequence)
        and not isinstance(attributes_raw, (str, bytes))
        else []
    )
    attributes_by_key = {
        str(item.get("key") or ""): item
        for item in attributes
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }
    stat_fields: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, Mapping):
            continue
        stat_key = str(definition.get("stat_key") or "")
        attribute = attributes_by_key.get(stat_key)
        if not stat_key or not isinstance(attribute, Mapping):
            continue
        stat_fields.append(
            {
                "step": index,
                "field_key": str(definition.get("key") or f"stat_{stat_key}"),
                "stat_key": stat_key,
                "label": str(attribute.get("label") or stat_key),
                "description": str(attribute.get("description") or ""),
                "minimum": int(attribute.get("minimum", 0)),
                "maximum": int(attribute.get("maximum", 0)),
                "default": int(attribute.get("default", 0)),
            }
        )

    values: dict[str, int] = {}
    for item in stat_fields:
        field_key = item["field_key"]
        if field_key not in field_values:
            continue
        try:
            values[field_key] = int(field_values[field_key])
        except (TypeError, ValueError):
            continue

    budget = int(stats.get("budget", 0) or 0)
    used = sum(values.values())
    allocation = stats.get("allocation") or {}
    rule = str(allocation.get("rule") or "maximum")
    target = int(allocation.get("total", budget))
    total_ok = (True if rule == "none" else used <= target if rule == "maximum" else used == target if rule == "exact" else int(allocation.get("minimum_total", 0)) <= used <= int(allocation.get("maximum_total", budget)))
    result: dict[str, Any] = {
        "mode": "manual",
        "budget": budget,
        "used": used,
        "remaining": budget - used,
        "values": values,
        "stat_fields": stat_fields,
        "first_step": stat_fields[0]["step"] if stat_fields else len(definitions),
        "complete": bool(stat_fields)
        and all(item["field_key"] in values for item in stat_fields)
        and total_ok,
        "total_ok": total_ok,
        "allocation_rule": rule,
        "current": None,
    }

    if current_step is None:
        return result
    current = next(
        (item for item in stat_fields if item["step"] == int(current_step)),
        None,
    )
    if not current:
        return result

    current_value = values.get(current["field_key"])
    used_before = used - (current_value if current_value is not None else 0)
    reserved_minimum = sum(
        item["minimum"]
        for item in stat_fields
        if item["step"] > current["step"]
        and item["field_key"] not in values
    )
    effective_maximum = min(
        current["maximum"],
        budget - used_before - reserved_minimum,
    )
    current_position = next(
        index
        for index, item in enumerate(stat_fields, start=1)
        if item["step"] == current["step"]
    )
    result["current"] = {
        **current,
        "position": current_position,
        "total": len(stat_fields),
        "used_before": used_before,
        "remaining_before": budget - used_before,
        "reserved_minimum": reserved_minimum,
        "effective_maximum": effective_maximum,
    }
    return result


PROFESSION_PRESET_STAT_MODE = (
    "automatic_profession_base_plus_two_fixed_bonus_choices"
)


def uses_profession_preset_stats(
    template: Mapping[str, Any],
) -> bool:
    """Return True when the card template opts into the profession-preset
    stat mode (fixed 50 base + primary +7 / secondary +3 = 60)."""
    stats = template.get("stats")
    if not isinstance(stats, Mapping):
        return False
    return bool(
        stats_mode(stats) == "preset"
        or stats.get("input_mode") == PROFESSION_PRESET_STAT_MODE
        or stats.get("allocation_mode")
        == "profession_base_plus_primary7_secondary3"
    )


def find_profession_preset(template: Mapping[str, Any], profession_ref: str) -> Mapping[str, Any]:
    reference = str(profession_ref or "").strip().casefold()
    for preset in template.get("profession_presets") or []:
        if not isinstance(preset, Mapping):
            continue
        aliases = preset.get("aliases")
        aliases = aliases if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)) else []
        candidates = {str(preset.get("id") or "").strip().casefold(), str(preset.get("key") or "").strip().casefold(), str(preset.get("name") or "").strip().casefold(), *(str(item).strip().casefold() for item in aliases)}
        if reference and reference in candidates:
            return preset
    raise ValueError(f"不存在预设“{profession_ref}”，请从提示中的可选项选择")


def attribute_maps(
    template: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    attributes = template.get("stats", {}).get("attributes", [])
    label_to_key: dict[str, str] = {}
    key_to_label: dict[str, str] = {}
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            continue
        key = str(attribute.get("key") or "")
        label = str(attribute.get("label") or key)
        if not key:
            continue
        label_to_key[label] = key
        label_to_key[key] = key
        key_to_label[key] = label
    return label_to_key, key_to_label


def resolve_profession_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Compute final preset stats from the frozen world contract.

    The preset base total, choice bonuses and final total are authoritative
    world-package data. Preview, confirmation and audit paths reuse this
    resolver so persisted values cannot drift from that declaration.
    """
    stats_config = template.get("stats") or {}
    selector = stats_config.get("preset_selector") or {}
    selector_field = str(selector.get("field") or "profession")
    profession_ref = str(fields.get(selector_field) or "")
    if not profession_ref:
        raise ValueError("请先选择预设")
    preset = find_profession_preset(template, profession_ref)
    profession_name = str(preset.get("name") or profession_ref)
    base_source = (
        preset.get("base_attributes")
        or preset.get("attributes")
        or {}
    )
    attribute_defs = template["stats"]["attributes"]
    attribute_keys = [str(item["key"]) for item in attribute_defs]
    base: dict[str, int] = {}
    for key in attribute_keys:
        if key not in base_source:
            raise ValueError(
                f"职业“{profession_name}”缺少属性：{key}"
            )
        base[key] = int(base_source[key])
    validation = stats_config.get("total_validation") or {}
    base_total = int(validation.get("base_total", stats_config.get("base_budget", 50)))
    final_total = int(validation.get("final_total", stats_config.get("budget", base_total)))
    bonus_choices = stats_config.get("bonus_choices") or []
    primary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == "primary_attribute"), {})
    secondary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == "secondary_attribute"), {})
    primary_bonus = int(primary_cfg.get("bonus", stats_config.get("primary_bonus", 7)))
    secondary_bonus = int(secondary_cfg.get("bonus", stats_config.get("secondary_bonus", 3)))
    if sum(base.values()) != base_total:
        raise ValueError(f"预设“{profession_name}”基础属性总和不是{base_total}")
    label_to_key, key_to_label = attribute_maps(template)
    primary_label = str(fields.get("primary_attribute") or "")
    secondary_label = str(fields.get("secondary_attribute") or "")
    if require_complete and not primary_label:
        raise ValueError("尚未选择主属性")
    if require_complete and not secondary_label:
        raise ValueError("尚未选择副属性")
    primary_key = (
        label_to_key.get(primary_label) if primary_label else None
    )
    secondary_key = (
        label_to_key.get(secondary_label) if secondary_label else None
    )
    if primary_label and primary_key is None:
        raise ValueError("主属性不在可选属性列表中")
    if secondary_label and secondary_key is None:
        raise ValueError("副属性不在可选属性列表中")
    if (
        primary_key is not None
        and secondary_key is not None
        and primary_key == secondary_key
    ):
        raise ValueError("主属性与副属性不能相同")
    effective = dict(base)
    if primary_key:
        effective[primary_key] += primary_bonus
    if secondary_key:
        effective[secondary_key] += secondary_bonus
    if require_complete and sum(effective.values()) != final_total:
        raise ValueError(f"最终属性总和必须为{final_total}")
    attribute_definitions = {
        str(item["key"]): item for item in attribute_defs
    }
    for key, value in effective.items():
        definition = attribute_definitions[key]
        minimum = int(definition["minimum"])
        maximum = int(definition["maximum"])
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{definition['label']}最终值{value}"
                f"超出允许范围{minimum}—{maximum}"
            )
    modifier_table = template["stats"].get("modifier_table", {})
    modifiers = {
        key: int(modifier_table.get(str(value), 0))
        for key, value in effective.items()
    }
    return {
        "mode": "preset",
        "profession_id": str(preset.get("id") or preset.get("key") or profession_ref),
        "profession": profession_name,
        "base": base,
        "raw": effective,
        "labels": key_to_label,
        "modifiers": modifiers,
        "primary": {
            "attribute": primary_key or "",
            "label": primary_label,
            "bonus": primary_bonus if primary_key else 0,
        },
        "secondary": {
            "attribute": secondary_key or "",
            "label": secondary_label,
            "bonus": secondary_bonus if secondary_key else 0,
        },
        "base_total": base_total,
        "bonus_total": ((primary_bonus if primary_key else 0) + (secondary_bonus if secondary_key else 0)),
        "effective_total": sum(effective.values()),
        "modifier_table": dict(modifier_table),
    }


def next_fillable_card_step(
    template: Mapping[str, Any],
    fields_def: list[Mapping[str, Any]],
    start_step: int,
) -> int:
    step = start_step
    while step < len(fields_def):
        definition = fields_def[step]
        if not isinstance(definition, Mapping):
            step += 1
            continue
        key = str(definition.get("key") or "")
        if (
            uses_profession_preset_stats(template)
            and (
                key.startswith("stat_")
                or definition.get("skip_manual_prompt")
            )
        ):
            step += 1
            continue
        break
    return step


def repair_profession_preset_draft(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    current_step: int,
) -> tuple[dict[str, Any], int]:
    """Recompute profession-preset stat fields for a legacy/partial draft.

    Reused when an old draft already carries hand-filled ``stat_*`` fields so
    the values are overwritten with the formula-derived ones and the cursor is
    moved to the first non-attribute field.
    """
    if not uses_profession_preset_stats(template):
        return fields, current_step
    profession = fields.get("profession")
    if not profession:
        return fields, current_step
    resolved = resolve_profession_stats(
        template, fields, require_complete=False
    )
    fields["profession_base_stats"] = resolved["base"]
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    fields_def = template["fields"]
    repaired_step = next_fillable_card_step(
        template, fields_def, current_step
    )
    return fields, repaired_step


def validate_card_template_config(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("玩家角色卡模板必须是 JSON 对象")
    try:
        version = int(value.get("version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数") from exc
    if version < 1:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数")
    fields = value.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ValueError("角色卡模板必须包含 fields 数组")
    keys: list[str] = []
    for item in fields:
        if not isinstance(item, Mapping):
            raise ValueError("角色卡 fields 的每一项都必须是对象")
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("角色卡字段 key 不能为空")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"角色卡字段 key 非法：{key}")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("角色卡字段 key 不能重复")
    for required in ("name", "code"):
        if required not in keys:
            raise ValueError(f"角色卡缺少必需字段 {required}")
    for item in fields:
        key = str(item.get("key") or "").strip()
        if key not in {"name", "code"}:
            continue
        try:
            max_chars = int(item.get("max_chars", 12))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "角色姓名与副本代号 max_chars 必须是整数"
            ) from exc
        if max_chars > 12:
            raise ValueError(
                "角色姓名与副本代号最多只能设置为 12 个字符"
            )

    stats = value.get("stats")
    stats = stats if isinstance(stats, Mapping) else {"mode": "none"}
    mode = stats_mode(stats)
    attributes = stats.get("attributes")
    if not isinstance(attributes, Sequence) or isinstance(
        attributes,
        (str, bytes),
    ) or (mode != "none" and not attributes):
        raise ValueError("启用数值时必须包含 stats.attributes")
    if mode == "none":
        if attributes:
            raise ValueError("stats.mode=none 时不得声明角色属性")
        return
    attribute_keys: set[str] = set()
    minimum_budget = 0
    maximum_budget = 0
    for item in attributes:
        if not isinstance(item, Mapping):
            raise ValueError("属性定义必须是 JSON 对象")
        key = str(item.get("key") or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"属性 key 非法：{key or '空'}")
        if key in attribute_keys:
            raise ValueError("属性 key 不能重复")
        attribute_keys.add(key)
        try:
            minimum = int(item.get("minimum"))
            maximum = int(item.get("maximum"))
            default = int(item.get("default"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"属性 {key} 的范围必须是整数") from exc
        if minimum > maximum or not minimum <= default <= maximum:
            raise ValueError(f"属性 {key} 的默认值不在合法范围内")
        minimum_budget += minimum
        maximum_budget += maximum
    try:
        budget = int(stats.get("budget"))
    except (TypeError, ValueError) as exc:
        raise ValueError("属性预算必须是整数") from exc
    if mode == "manual" and not minimum_budget <= budget <= maximum_budget:
        raise ValueError(
            f"属性预算必须介于 {minimum_budget} 与 {maximum_budget} 之间"
        )

    # Preset stat mode. Totals and bonuses come from the world package.
    if mode == "preset":
        field_keys = {
            str(item.get("key") or "")
            for item in fields
            if isinstance(item, Mapping)
        }
        for required_field in (
            "profession",
            "primary_attribute",
            "secondary_attribute",
        ):
            if required_field not in field_keys:
                raise ValueError(
                    f"职业预设模式必须包含“{required_field}”字段"
                )
        presets = value.get("profession_presets")
        presets = presets if isinstance(presets, list) else []
        if not presets:
            raise ValueError("职业预设模式至少需要一个职业预设")
        attr_index = {
            str(item.get("key") or ""): item
            for item in attributes
            if isinstance(item, Mapping)
        }
        for preset in presets:
            if not isinstance(preset, Mapping):
                raise ValueError("职业预设必须是 JSON 对象")
            name = str(preset.get("name") or "")
            if not name:
                raise ValueError("职业预设缺少名称")
            base_source = (
                preset.get("base_attributes")
                or preset.get("attributes")
                or {}
            )
            if not isinstance(base_source, Mapping):
                raise ValueError(f"职业“{name}”缺少基础属性")
            for key, definition in attr_index.items():
                if key not in base_source:
                    raise ValueError(
                        f"职业“{name}”缺少属性：{key}"
                    )
                try:
                    value_int = int(base_source[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 必须是整数"
                    ) from exc
                amin = int(definition["minimum"])
                amax = int(definition["maximum"])
                if not amin <= value_int <= amax:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 超出允许范围"
                        f"{amin}—{amax}"
                    )
            if sum(int(v) for v in base_source.values()) != int((stats.get("total_validation") or {}).get("base_total", stats.get("base_budget", 50))):
                raise ValueError(
                    f"职业“{name}”基础属性总和不符合 total_validation.base_total"
                )
        configured_bonus_ceiling = max(
            (
                max(0, int(item.get("bonus", 0)))
                for item in (stats.get("bonus_choices") or [])
                if isinstance(item, Mapping)
            ),
            default=0,
        )
        if not configured_bonus_ceiling:
            configured_bonus_ceiling = max(
                0,
                int(stats.get("primary_bonus", 7)),
            )
        for key, definition in attr_index.items():
            amax = int(definition["maximum"])
            maximum_base = max(
                int(
                    (preset.get("base_attributes") or {}).get(key, 0)
                )
                for preset in presets
                if isinstance(preset, Mapping)
            )
            if maximum_base + configured_bonus_ceiling > amax:
                raise ValueError(
                    f"属性 {definition.get('label', key)} 最大值 {amax}"
                    "不足以容纳世界包声明的预设加成"
                )


def normalize_choices(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("行动选项必须是数组")
    contract = world_contract(world) if world is not None else None
    aliases = {"low":"safe","standard":"controlled","high":"dangerous","safe":"safe","controlled":"controlled","dangerous":"dangerous","desperate":"desperate","lethal":"lethal"}
    by_key = {}
    for item in value:
        if not isinstance(item, Mapping): continue
        key = str(item.get("key") or "").strip().upper()
        if key not in CHOICE_KEYS or key in by_key: continue
        raw_text = str(item.get("text") or "").strip()
        if len(raw_text) > 50: raise ValueError("每个行动选项正文不得超过 50 字")
        text = clean_text(raw_text, max_chars=50)
        if not text: continue
        risk = aliases.get(str(item.get("danger_id") or item.get("risk") or "controlled").lower(), str(item.get("danger_id") or item.get("risk") or "controlled").lower())
        check = item.get("check"); check = check if isinstance(check, Mapping) else {}
        required = bool(check.get("required", item.get("requires_check", False)))
        stat = clean_text(check.get("attribute_id", item.get("check_stat")), max_chars=40)
        label = stat
        consequence = clean_text(check.get("known_consequences", item.get("known_consequences")), max_chars=300)
        risk_label = {"safe":"安全","controlled":"可控","dangerous":"危险","desperate":"绝境","lethal":"致命"}.get(risk, risk)
        if contract is not None:
            danger_map = {str(x.get("id")):str(x.get("label")) for x in contract["danger_levels"]}
            if risk not in danger_map: raise ValueError(f"世界包不允许危险度：{risk}")
            risk_label = danger_map[risk]
            mode = contract["resolution"]["mode"]
            if mode in {"none","narrative"}: required=False; stat=label=""
            elif required and mode == "attribute":
                matched = attribute_lookup(contract, stat)
                if matched is None:
                    generic=contract["resolution"]["generic_check"]
                    if not generic.get("enabled",False): raise ValueError("需要属性检定时必须使用当前世界声明的属性 ID")
                    stat=""; label=str(generic.get("label") or "通用")
                else:
                    stat,label=matched
                    if stat not in contract["resolution"]["allowed_attributes"]: raise ValueError(f"世界包不允许属性检定：{stat}")
            elif required and mode == "dice_only": stat=label=""
        check_type=str(check.get("type",item.get("check_type","standard")) or "standard").lower()
        if check_type not in {"standard","leader","group","resistance","opposed"}: check_type="standard"
        difficulty=_bounded_int(check.get("difficulty",item.get("difficulty")),12,5,25)
        adv=check.get("advantage_sources",item.get("advantage_sources")); dis=check.get("disadvantage_sources",item.get("disadvantage_sources"))
        row={"key":key,"text":text,"actor_id":clean_text(item.get("actor_id"),max_chars=128),"risk":risk,"danger_id":risk,"risk_label":risk_label,"requires_check":required,"collective":bool(item.get("collective",False)),"check_type":check_type,"check_stat":stat,"check_label":label,"difficulty":difficulty,"known_consequences":consequence,"advantage_sources":[clean_text(x,max_chars=120) for x in (adv if isinstance(adv,list) else [])[:8] if clean_text(x,max_chars=120)],"disadvantage_sources":[clean_text(x,max_chars=120) for x in (dis if isinstance(dis,list) else [])[:8] if clean_text(x,max_chars=120)]}
        row["check"]={"required":True,"attribute_id":stat,"type":check_type,"difficulty":difficulty,"known_consequences":consequence} if required else None
        by_key[key]=row
    if set(by_key)!=set(CHOICE_KEYS): raise ValueError("每回合必须提供 A、B、C、D 四个有效选项")
    result=[by_key[k] for k in CHOICE_KEYS]
    if not any(x["risk"]=="safe" for x in result): raise ValueError("每组选项至少需要一个安全风险选项")
    return result


def normalize_choices_compat(value: Any, world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value,(str,bytes)): raise ValueError("行动选项必须是数组")
    items=[dict(x) for x in value if isinstance(x,Mapping)]
    if len(items)!=len(value): raise ValueError("行动选项必须全部是对象")
    assign=len(items)==4 and not any(str(x.get("key") or "").strip() for x in items)
    for i,item in enumerate(items):
        raw=chr(ord("A")+i) if assign else str(item.get("key") or "")
        match=re.fullmatch(r"(?:选项\s*)?([ABCD])(?:\s*[.、:：)）])?",unicodedata.normalize("NFKC",raw).strip().upper())
        if match: item["key"]=match.group(1)
    return normalize_choices(items,world)


def opening_choices(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules=world.get("rules"); rules=rules if isinstance(rules,Mapping) else {}
    try: return normalize_choices(rules.get("opening_choices"),world)
    except ValueError: return normalize_choices([dict(x) for x in DEFAULT_OPENING_CHOICES],world)


def fallback_choices(state: Mapping[str, Any], world: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    location=clean_text(str(state.get("location") or "当前地点")[:12],max_chars=12); summary=clean_text(str(state.get("scene_summary") or "眼前局势")[:16],max_chars=16)
    return normalize_choices([{"key":"A","text":f"谨慎观察{location}，确认与“{summary}”有关的可见线索","risk":"safe","requires_check":False},{"key":"B","text":"向在场角色询问公开信息，不作强迫或结果预设","risk":"safe","requires_check":False},{"key":"C","text":"使用角色已经拥有的能力或物品作一次有限尝试","risk":"controlled","requires_check":False},{"key":"D","text":"保持警戒并暂缓冒险行动，为下一步搜集更多信息","risk":"safe","requires_check":False}],world)


def parse_choice_input(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    match = re.fullmatch(r"([A-Da-d])(?:\s+(.+))?", text, re.S)
    if not match:
        raise ValueError("请选择 A、B、C 或 D，可在字母后补充简短演绎")
    key = match.group(1).upper()
    flavor = clean_text(match.group(2) or "", max_chars=160)
    return key, flavor


_DURATION_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*"
    r"(秒|s|sec|分钟|分|min|m|小时|时|h|天|d)\s*$",
    re.I,
)


def parse_duration(value: str, *, maximum_days: int = 365) -> int:
    match = _DURATION_PATTERN.fullmatch(str(value or ""))
    if not match:
        raise ValueError("时间格式示例：30分钟、2小时、1天")
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {
        "秒": 1,
        "s": 1,
        "sec": 1,
        "分钟": 60,
        "分": 60,
        "min": 60,
        "m": 60,
        "小时": 3600,
        "时": 3600,
        "h": 3600,
        "天": 86400,
        "d": 86400,
    }[unit]
    seconds = int(amount * multiplier)
    if seconds <= 0:
        raise ValueError("时间必须大于 0")
    return min(maximum_days * 86400, seconds)


def safe_exit_narrative(
    world: Mapping[str, Any],
    character_name: str,
    *,
    forced: bool = False,
) -> str:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    templates = rules.get("safe_exit_templates")
    if isinstance(templates, Sequence) and not isinstance(
        templates, (str, bytes)
    ):
        usable = [
            clean_text(item, max_chars=300)
            for item in templates
            if clean_text(item, max_chars=300)
        ]
        if usable:
            return usable[0].replace("{character}", character_name)
    if forced:
        return (
            f"{character_name}暂时离开了队伍，去处理一件无法拖延的私事。"
            "其余人保留了对方留下的联络线索，未来仍可能在合理时机重逢。"
        )
    return (
        f"{character_name}在确认眼前局势暂时稳定后，与众人作了简短告别。"
        "这段同行经历被完整保留，若条件允许，仍可循着旧线索重新会合。"
    )


# 选项字母对应的圆形字母 emoji，用于行动回合的视觉分区
_CHOICE_LETTER_EMOJI = {
    "A": "🅰️",
    "B": "🅱️",
    "C": "🅲️",
    "D": "🅳️",
    "E": "🅴️",
    "F": "🅵️",
}


def format_choices(character_name: str, choices: Sequence[Mapping[str, Any]], *, rerolls_left: int = 1) -> str:
    lines=[f"🎯 【{character_name}的行动回合】"]
    defaults={"safe":"安全","controlled":"可控","dangerous":"危险","desperate":"绝境","lethal":"致命"}
    for item in choices:
        annotations=[str(item.get("risk_label") or defaults.get(str(item.get("risk")),"可控"))]
        if item.get("requires_check"):
            label=str(item.get("check_label") or item.get("check_stat") or "").strip(); annotations.append(f'需“{label}”检定' if label else "需检定")
        key=str(item.get("key") or ""); letter=_CHOICE_LETTER_EMOJI.get(key.upper(),key)
        lines.extend(["",f"{letter} {item.get('text')}（{' · '.join(annotations)}）"] )
        if str(item.get("risk"))=="lethal" and item.get("known_consequences"): lines.append(f"⚠️ 已知后果：{item.get('known_consequences')}")
    lines.extend(["","","💬 发送：jg A","📝 也可：/酒馆 选择 A 语气尽量温和",f"♻️ 本回合剩余重整次数：{max(0,rerolls_left)}"] )
    return "\n".join(lines)


def vote_result(
    *,
    eligible_count: int,
    ballots: Sequence[Mapping[str, Any]],
    option_keys: Sequence[str],
) -> dict[str, Any]:
    counts = {str(key): 0 for key in option_keys}
    for ballot in ballots:
        key = str(ballot.get("option_key") or "")
        if key in counts:
            counts[key] += 1
    cast_count = sum(counts.values())
    quorum = cast_count > eligible_count / 2
    winners = [
        key for key, count in counts.items()
        if cast_count and count > cast_count / 2
    ]
    return {
        "counts": counts,
        "cast_count": cast_count,
        "eligible_count": eligible_count,
        "quorum": quorum,
        "winner": winners[0] if quorum and len(winners) == 1 else "",
        "all_voted": cast_count >= eligible_count,
    }
