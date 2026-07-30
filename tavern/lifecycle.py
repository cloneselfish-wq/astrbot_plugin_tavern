from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from .security import clean_text


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
    "card_completion_timeout_seconds": 24 * 60 * 60,
    "preparation_timeout_seconds": 24 * 60 * 60,
    "ready_timeout_seconds": 30 * 60,
    "turn_timeout_seconds": 10 * 60,
    "turn_reminder_seconds": 3 * 60,
    "max_consecutive_timeouts": 2,
    "standby_timeout_seconds": 7 * 24 * 60 * 60,
    "delegation_ttl_seconds": 24 * 60 * 60,
    "check_timeout_seconds": 5 * 60,
    "vote_round_one_seconds": 10 * 60,
    "vote_round_two_seconds": 5 * 60,
    "vote_reminder_seconds": 2 * 60,
    "all_idle_pause_seconds": 10 * 60,
    "pause_stops_clock": True,
    "announce_timeouts": True,
    "turn_timeout_action": "skip",
    "card_timeout_action": "standby",
    "ready_timeout_action": "standby",
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
        "check_timeout_seconds",
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
        source.get("announce_timeouts", True)
    )
    timeout_action = str(
        source.get("turn_timeout_action", "skip")
    ).strip()
    result["turn_timeout_action"] = (
        timeout_action
        if timeout_action in {"skip", "hold"}
        else "skip"
    )
    for key, allowed, default in (
        (
            "card_timeout_action",
            {"standby", "release", "remind"},
            "standby",
        ),
        (
            "ready_timeout_action",
            {"standby", "remind"},
            "standby",
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
                        max_chars=50,
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
                        "integer"
                        if str(item.get("type") or "").lower() == "integer"
                        else "text"
                    ),
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
    stats_raw = raw.get("stats")
    stats_raw = stats_raw if isinstance(stats_raw, Mapping) else {}
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
                        max_chars=50,
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
    if not attributes:
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
    if not modifier_table:
        modifier_table = dict(DEFAULT_CARD_STATS["modifier_table"])
    budget = _bounded_int(
        stats_raw.get("budget"),
        int(DEFAULT_CARD_STATS["budget"]),
        0,
        2000,
    )
    for attribute in attributes:
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
                    max_chars=50,
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
            "budget": budget,
            "attributes": attributes,
            "modifier_table": modifier_table,
        },
    }


def card_stat_allocation(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None = None,
    current_step: int | None = None,
) -> dict[str, Any]:
    """Return authoritative budget progress for sequential stat allocation."""

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
    result: dict[str, Any] = {
        "budget": budget,
        "used": used,
        "remaining": budget - used,
        "values": values,
        "stat_fields": stat_fields,
        "first_step": stat_fields[0]["step"] if stat_fields else len(definitions),
        "complete": bool(stat_fields)
        and all(item["field_key"] in values for item in stat_fields),
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
    if not isinstance(stats, Mapping):
        raise ValueError("角色卡模板必须包含 stats 对象")
    attributes = stats.get("attributes")
    if not isinstance(attributes, Sequence) or isinstance(
        attributes,
        (str, bytes),
    ) or not attributes:
        raise ValueError("角色卡模板必须包含 stats.attributes")
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
    if not minimum_budget <= budget <= maximum_budget:
        raise ValueError(
            f"属性预算必须介于 {minimum_budget} 与 {maximum_budget} 之间"
        )


def normalize_choices(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("行动选项必须是数组")
    by_key: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key") or "").strip().upper()
        if key not in CHOICE_KEYS or key in by_key:
            continue
        text = clean_text(item.get("text"), max_chars=240)
        if not text:
            continue
        raw_risk = str(item.get("risk") or "controlled").strip().lower()
        risk_aliases = {
            "low": "safe",
            "standard": "controlled",
            "high": "dangerous",
            "safe": "safe",
            "controlled": "controlled",
            "dangerous": "dangerous",
            "desperate": "desperate",
            "lethal": "lethal",
        }
        risk = risk_aliases.get(raw_risk, "controlled")
        known_consequences = clean_text(
            item.get("known_consequences"),
            max_chars=300,
        )
        by_key[key] = {
            "key": key,
            "text": text,
            "risk": risk,
            "requires_check": (
                True
                if risk in {"dangerous", "desperate", "lethal"}
                else bool(item.get("requires_check", False))
            ),
            "collective": bool(item.get("collective", False)),
            "check_type": (
                str(item.get("check_type") or "standard").strip().lower()
                if str(item.get("check_type") or "standard").strip().lower()
                in {"standard", "leader", "group", "resistance", "opposed"}
                else "standard"
            ),
            "check_stat": clean_text(item.get("check_stat"), max_chars=40),
            "difficulty": _bounded_int(
                item.get("difficulty"),
                12,
                5,
                25,
            ),
            "known_consequences": known_consequences,
            "advantage_sources": [
                clean_text(source, max_chars=120)
                for source in (
                    item.get("advantage_sources")
                    if isinstance(item.get("advantage_sources"), list)
                    else []
                )[:8]
                if clean_text(source, max_chars=120)
            ],
            "disadvantage_sources": [
                clean_text(source, max_chars=120)
                for source in (
                    item.get("disadvantage_sources")
                    if isinstance(item.get("disadvantage_sources"), list)
                    else []
                )[:8]
                if clean_text(source, max_chars=120)
            ],
        }
    if set(by_key) != set(CHOICE_KEYS):
        raise ValueError("每回合必须提供 A、B、C、D 四个有效选项")
    choices = [by_key[key] for key in CHOICE_KEYS]
    if not any(item["risk"] == "safe" for item in choices):
        raise ValueError("每组选项至少需要一个安全风险选项")
    return choices


def opening_choices(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    raw = rules.get("opening_choices")
    try:
        return normalize_choices(raw)
    except ValueError:
        return [dict(item) for item in DEFAULT_OPENING_CHOICES]


def fallback_choices(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    location = clean_text(state.get("location") or "当前地点", max_chars=80)
    summary = clean_text(
        state.get("scene_summary") or "眼前局势",
        max_chars=120,
    )
    return normalize_choices(
        [
            {
                "key": "A",
                "text": f"谨慎观察{location}，确认与“{summary}”有关的可见线索",
                "risk": "low",
                "requires_check": False,
            },
            {
                "key": "B",
                "text": "向在场角色询问公开信息，不作强迫或结果预设",
                "risk": "low",
                "requires_check": False,
            },
            {
                "key": "C",
                "text": "使用角色已经拥有的能力或物品作一次有限尝试",
                "risk": "standard",
                "requires_check": True,
            },
            {
                "key": "D",
                "text": "保持警戒并暂缓冒险行动，为下一步搜集更多信息",
                "risk": "low",
                "requires_check": False,
            },
        ]
    )


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


def format_choices(
    character_name: str,
    choices: Sequence[Mapping[str, Any]],
    *,
    rerolls_left: int = 1,
) -> str:
    lines = [f"【{character_name}的行动回合】"]
    for item in choices:
        risk = {
            "low": "安全",
            "standard": "可控",
            "high": "危险",
            "safe": "安全",
            "controlled": "可控",
            "dangerous": "危险",
            "desperate": "绝境",
            "lethal": "致命",
        }.get(str(item.get("risk")), "可控")
        check = " · 需检定" if item.get("requires_check") else ""
        consequence = (
            f" · {item.get('known_consequences')}"
            if item.get("known_consequences")
            else ""
        )
        lines.append(
            f"{item.get('key')}. {item.get('text')}（{risk}{check}{consequence}）"
        )
    lines.extend(
        [
            "",
            "发送：jg A",
            "也可：/酒馆 选择 A 语气尽量温和",
            f"本回合剩余重整次数：{max(0, rerolls_left)}",
        ]
    )
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
