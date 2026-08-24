"""B/C 阶段补充的触发策略（纯函数，无 I/O）。

对应 D1_PLAN/16 §2-§4：字段分组、补充时间点、谁来决定补充内容。
世界包可通过 ``content.staged_supplements``（或 ``rules.staged_supplements``）
覆盖默认轮次窗；未声明时使用内置默认值（Tier 1 闭环）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..lifecycle import (
    CARD_STAGE_B,
    CARD_STAGE_C,
    field_stage,
    stage_required_missing,
)
from ..story_context import evaluate_story_condition


SUPPLEMENT_KIND = "staged_supplement"
SUPPLEMENT_NOTICE_KIND = "group_notice"

#: 提议业务状态（存于 delivery_outbox.meta_json["state"]）。
OFFER_OPEN_STATES = frozenset({"offered", "postponed"})
OFFER_CLOSED_STATES = frozenset(
    {"confirmed", "cancelled", "expired", "rejected", "superseded"}
)

DEFAULT_CONFIG: dict[str, int] = {
    "first_offer_after_round": 3,
    "offer_interval_rounds": 3,
    "fallback_round": 12,
    "expires_after_rounds": 6,
    "reopen_after_rounds": 3,
    "max_active_offers": 2,
    "candidate_count": 3,
    "candidate_max": 5,
}


def supplement_config(world: Mapping[str, Any]) -> dict[str, int]:
    """合并世界声明的补充配置与内置默认值。"""

    raw: Any = None
    for path in (
        ("content", "staged_supplements"),
        ("rules", "staged_supplements"),
        ("staged_supplements",),
    ):
        node: Any = world
        found = True
        for key in path:
            if isinstance(node, Mapping) and key in node:
                node = node[key]
            else:
                found = False
                break
        if found and isinstance(node, Mapping):
            raw = node
            break
    config = dict(DEFAULT_CONFIG)
    if isinstance(raw, Mapping):
        declared = raw.get("defaults")
        if isinstance(declared, Mapping):
            for key in config:
                if key in declared:
                    try:
                        config[key] = int(declared[key])
                    except (TypeError, ValueError):
                        pass
    return config


def field_supplement_config(
    field: Mapping[str, Any],
    config: Mapping[str, int],
    world: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """合并单字段声明（``content.staged_supplements.fields[<key>]``）。"""

    merged = dict(config)
    world = world if isinstance(world, Mapping) else {}
    raw: Any = None
    for path in (
        ("content", "staged_supplements", "fields"),
        ("rules", "staged_supplements", "fields"),
    ):
        node: Any = world
        found = True
        for key in path:
            if isinstance(node, Mapping) and key in node:
                node = node[key]
            else:
                found = False
                break
        if found and isinstance(node, Mapping):
            raw = node
            break
    entry = raw.get(str(field.get("key") or "")) if isinstance(raw, Mapping) else None
    if isinstance(entry, Mapping):
        for key in ("candidate_count", "candidate_max", "reopen_after_rounds"):
            if key in entry:
                try:
                    merged[key] = int(entry[key])
                except (TypeError, ValueError):
                    pass
    return merged


def missing_bc_fields(
    template: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """按模板字段序返回仍缺失的 B/C 必填字段定义。"""

    return stage_required_missing(
        template,
        profile if isinstance(profile, Mapping) else {},
        stages=(CARD_STAGE_B, CARD_STAGE_C),
    )


def field_open_round(
    field: Mapping[str, Any],
    ordinal: int,
    config: Mapping[str, int],
) -> int:
    """B/C 组字段按组内序号开启（``ordinal`` 为同组内序号）。

    B 组按间隔在前期轮次开启；C 组自保底轮起按组内序号依次开启。
    """

    if field_stage(field) == CARD_STAGE_B:
        return int(config["first_offer_after_round"]) + max(
            0, int(ordinal)
        ) * int(config["offer_interval_rounds"])
    return int(config["fallback_round"]) + max(0, int(ordinal))


def fallback_due(
    turn_no: int | None,
    chapter: str = "",
    config: Mapping[str, int] | None = None,
) -> bool:
    """第一幕保底：章节越过 act:one，或轮次达到保底轮。"""

    config = config if isinstance(config, Mapping) else DEFAULT_CONFIG
    chapter = str(chapter or "").strip().lower()
    if chapter and chapter != "act:one":
        return True
    return int(turn_no or 0) >= int(config["fallback_round"])


def effective_trigger(
    trigger_source: str,
    *,
    turn_no: int | None,
    chapter: str = "",
    config: Mapping[str, int] | None = None,
) -> str:
    """把普通轮次扫描自动提升为第一幕保底扫描。"""

    source = str(trigger_source or "round_window").strip()
    if fallback_due(turn_no, chapter, config):
        return "act_end_fallback"
    return source if source in {"round_window", "act_end_fallback"} else "round_window"


def offer_expired(
    meta: Mapping[str, Any],
    turn_no: int | None,
    config: Mapping[str, int] | None = None,
) -> bool:
    """按轮次惰性判定提议是否过期（无墙钟依赖，可确定性测试）。"""

    config = config if isinstance(config, Mapping) else DEFAULT_CONFIG
    state = str(meta.get("state") or "offered")
    if state not in OFFER_OPEN_STATES:
        return False
    offer_round = int(meta.get("offer_round") or 0)
    expires_after = int(
        meta.get("expires_after_rounds") or config["expires_after_rounds"]
    )
    return int(turn_no or 0) >= offer_round + expires_after


def field_is_reofferable(
    meta: Mapping[str, Any] | None,
    turn_no: int | None,
    trigger_source: str,
    config: Mapping[str, int] | None = None,
) -> bool:
    """判断某字段当前提议是否允许再开新提议。

    - 已确认字段永不再开；
    - 未过期 offered 不再开；过期后随下次扫描重开；
    - postponed 在冷却轮数后可重开；
    - cancelled 仅第一幕保底重开；
    - 旧 rejected/superseded 行不再开（新行代表当前提议）。
    """

    config = config if isinstance(config, Mapping) else DEFAULT_CONFIG
    trigger = str(trigger_source or "round_window")
    if meta is None:
        return True
    state = str(meta.get("state") or "offered")
    if state == "confirmed":
        return False
    if state == "offered":
        return offer_expired(meta, turn_no, config)
    if state == "postponed":
        if offer_expired(meta, turn_no, config):
            return True
        offer_round = int(meta.get("offer_round") or 0)
        reopen_after = int(
            meta.get("reopen_after_rounds") or config["reopen_after_rounds"]
        )
        return int(turn_no or 0) >= offer_round + reopen_after
    if state == "cancelled":
        return trigger == "act_end_fallback"
    return False


def condition_matches(
    field: Mapping[str, Any],
    *,
    world: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> bool:
    """字段级剧情条件（``supplement_when``）求值；未声明视为通过。"""

    when = field.get("supplement_when")
    if not isinstance(when, Mapping) or not when:
        return True
    result = evaluate_story_condition(
        when,
        world=world,
        context=context if isinstance(context, Mapping) else {},
    )
    return bool(result.get("matched"))


__all__ = [
    "OFFER_CLOSED_STATES",
    "OFFER_OPEN_STATES",
    "SUPPLEMENT_KIND",
    "SUPPLEMENT_NOTICE_KIND",
    "condition_matches",
    "effective_trigger",
    "fallback_due",
    "field_is_reofferable",
    "field_open_round",
    "field_supplement_config",
    "missing_bc_fields",
    "offer_expired",
    "supplement_config",
]
