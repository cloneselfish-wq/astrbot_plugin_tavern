from .common import *

def parse_instance_list_page(
    argument: str,
    *,
    allow_bare_number: bool = False,
) -> int | None:
    """Parse an explicit list page without confusing it with an instance ref."""

    text = str(argument or "").strip()
    if not text:
        return 1
    if allow_bare_number and re.fullmatch(r"\d{1,6}", text):
        return max(1, int(text))
    for pattern in _INSTANCE_PAGE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return max(1, int(match.group(1)))
    return None


def _compact_instance_intro(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "暂无简介"
    if len(text) <= INSTANCE_INTRO_MAX_CHARS:
        return text
    return text[: INSTANCE_INTRO_MAX_CHARS - 1].rstrip() + "…"


def format_turn_status(turn: Mapping[str, Any]) -> str:
    order = turn.get("order")
    if not isinstance(order, list) or not order:
        return "【回合顺序】尚无玩家，请先发送 /团 加入。"
    current_id = str(turn.get("current_user_id") or "")
    lines = []
    for item in order:
        marker = "▶" if str(item.get("user_id") or "") == current_id else "·"
        lines.append(
            f"{marker} {item.get('position', '?')}. "
            f"{item.get('name') or '角色资料缺失，请联系主持人'}"
        )
    return (
        f"【回合顺序】第 {turn.get('round_no', 1)} 轮\n"
        + "\n".join(lines)
    )


def format_instance_list(
    instances: list[Mapping[str, Any]],
    worlds: list[Mapping[str, Any]] | None = None,
    *,
    page: int = 1,
) -> str:
    source = instances if instances else list(worlds or [])
    total = len(source)
    pages = max(1, (total + INSTANCE_LIST_PAGE_SIZE - 1) // INSTANCE_LIST_PAGE_SIZE)
    effective_page = min(max(1, int(page or 1)), pages)
    start = (effective_page - 1) * INSTANCE_LIST_PAGE_SIZE
    page_items = source[start : start + INSTANCE_LIST_PAGE_SIZE]

    if instances:
        state_labels = {
            SESSION_CLOSED: "已关闭",
            SESSION_PREPARING: "准备中",
            SESSION_RUNNING: "运行中",
            SESSION_PAUSED: "已暂停",
            SESSION_FINISHED: "已完结",
            SESSION_MAINTENANCE: "维护中",
        }
        lines = [
            f"【请选择开团副本｜第 {effective_page}/{pages} 页"
            f"｜共 {total} 个】"
        ]
        for index, item in enumerate(page_items, start=start + 1):
            marker = "▶" if item.get("selected") else "·"
            lines.append(
                f"{marker} {index}. 《{item.get('instance_name')}》"
                f" · {item.get('world_name')}"
                f" · {state_labels.get(item.get('state'), '状态异常')}"
                f" · 第 {item.get('turn_no', 0)} 回合"
            )
            lines.append(
                "   简介："
                + _compact_instance_intro(
                    item.get("world_description")
                    or item.get("description")
                )
            )
        lines.extend(
            _instance_list_footer(
                effective_page,
                pages,
                selection_label="发送：/团 开启 <序号或完整副本名>",
            )
        )
        return "\n".join(lines)

    lines = [
        f"【本群还没有开团副本｜可用世界第 {effective_page}/{pages} 页"
        f"｜共 {total} 个】"
    ]
    for index, item in enumerate(page_items, start=start + 1):
        lines.append(f"· {index}. 《{item.get('name')}》")
        lines.append(
            "   简介："
            + _compact_instance_intro(item.get("description"))
        )
    if not page_items:
        lines.append("当前没有可用世界包")
    else:
        lines.extend(
            _instance_list_footer(
                effective_page,
                pages,
                selection_label=(
                    "选择一个世界建立首个副本："
                    "/团 开启 <序号或完整世界名>"
                ),
            )
        )
    return "\n".join(lines)


def _instance_list_footer(
    page: int,
    pages: int,
    *,
    selection_label: str,
) -> list[str]:
    lines = ["", selection_label]
    navigation = []
    if page > 1:
        navigation.append(f"上一页：/团 开启 第{page - 1}页")
    if page < pages:
        navigation.append(f"下一页：/团 开启 第{page + 1}页")
    if navigation:
        lines.append("｜".join(navigation))
    return lines


def format_roster(roster: list[Mapping[str, Any]]) -> str:
    if not roster:
        return "【当前阵容】尚无玩家加入。"
    card_labels = {
        "uncreated": "未建卡",
        "draft": "建卡中",
        "pending_review": "待审核",
        "approved": "已通过",
        "rejected": "未通过",
    }
    participation_labels = {
        "reserved": "占位",
        "active": "出场",
        "standby": "候补",
        "away": "暂离",
        "retired": "已退场",
        "archived": "已归档",
    }
    lines = ["【当前阵容】"]
    for item in roster:
        name = (
            item.get("character_name")
            or item.get("display_name")
            or "角色资料缺失，请联系主持人"
        )
        ready = "已准备" if item.get("ready") else "未准备"
        lines.append(
            f"· {name}"
            f"（{item.get('character_code') or '无代号'}）"
            f" · {card_labels.get(item.get('card_status'), '状态异常')}"
            f" · {ready}"
            f" · {participation_labels.get(item.get('participation_status'), '状态异常')}"
        )
    return "\n".join(lines)


def format_vote(vote: Mapping[str, Any]) -> str:
    lines = [
        f"【集体决策 · 第 {vote.get('stage', 1)} 轮】",
        str(vote.get("question") or ""),
    ]
    lines.extend(
        f"{item.get('key')}. {item.get('text')}"
        for item in vote.get("options", [])
    )
    lines.extend(
        [
            "",
            f"有效成员：{len(vote.get('eligible_user_ids', []))} 人",
            f"截止：{vote.get('deadline_at') or '不限时'}",
            "发送：/团 投票 A",
        ]
    )
    return "\n".join(lines)


def format_recovered_timer(
    timers: Sequence[Mapping[str, Any]],
    *,
    vote_active: bool,
) -> str:
    timer_type = "vote" if vote_active else "turn"
    timer = next(
        (
            item
            for item in timers
            if item.get("timer_type") == timer_type
            and item.get("status") == "active"
        ),
        None,
    )
    if not timer:
        return "⏳ 【恢复计时】当前流程不限时或倒计时已关闭"
    try:
        remaining = max(0, int(timer.get("remaining_seconds") or 0))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    minutes, seconds = divmod(remaining, 60)
    label = "投票" if vote_active else "行动回合"
    if minutes:
        text = f"{minutes} 分 {seconds} 秒"
    else:
        text = f"{seconds} 秒"
    return f"⏳ 【恢复计时】{label}剩余 {text}"


def world_preset_brief(world: Mapping[str, Any], focus: str = "") -> str:
    """Build a compact summary of the world's preset content (professions,
    factions, regions) to show when a player starts creating a character."""
    if not isinstance(world, Mapping):
        return ""
    rules = world.get("rules")
    if not isinstance(rules, Mapping):
        return ""
    professions = rules.get("professions")
    professions = professions if isinstance(professions, list) else []
    modules = rules.get("setting_modules")
    modules = modules if isinstance(modules, Mapping) else {}
    stat_rules = rules.get("actor") or {}
    stat_defs = (stat_rules.get("stats") or {}).get("attributes") or []
    stat_labels: dict[str, str] = {}
    for _attr in stat_defs:
        if isinstance(_attr, Mapping) and _attr.get("key"):
            stat_labels[str(_attr["key"])] = str(
                _attr.get("label") or _attr["key"]
            )
    if not professions:
        _pp = stat_rules.get("profession_presets")
        if isinstance(_pp, list):
            professions = _pp
    _stats_raw = stat_rules.get("stats") or {}
    _profession_mode = bool(
        isinstance(_stats_raw, Mapping)
        and (
            _stats_raw.get("mode") == "preset"
            or _stats_raw.get("input_mode")
            == "automatic_profession_base_plus_two_fixed_bonus_choices"
            or _stats_raw.get("allocation_mode")
            == "profession_base_plus_primary7_secondary3"
        )
    )
    _bonus_note = (
        f"（基础属性合计 {_stats_raw.get('base_budget', 50)} 点已锁定，随后选主属性 +{_stats_raw.get('primary_bonus', 7)}、副属性 +{_stats_raw.get('secondary_bonus', 3)}，最终固定 {_stats_raw.get('budget', 60)} 点）"
        if _profession_mode
        else ""
    )
    lines: list[str] = []
    if professions:
        if focus == "profession":
            lines.append(
                "【可选预设职业】填写以下任一名称即可自动套用其基础数值："
                + _bonus_note
            )
        else:
            lines.append(
                "【本世界预设职业】（建卡时在「预设职业」一栏填写其一，"
                "将自动套用基础数值）"
                + _bonus_note
            )
        for item in professions:
            if not isinstance(item, Mapping):
                continue
            disp = item.get("label") or item.get("name") or item.get("key") or "?"
            base = item.get("base_stats")
            if not isinstance(base, Mapping):
                base = item.get("attributes")
            if not isinstance(base, Mapping):
                base = item.get("base_attributes")
            base = base if isinstance(base, Mapping) else {}
            if base:
                bs_text = "、".join(
                    f"{stat_labels.get(str(k), k)}{v}"
                    for k, v in base.items()
                )
            else:
                bs_text = "数值自定"
            free = item.get("free_points")
            free_text = (
                f" · 可分配 {free} 点" if isinstance(free, int) else ""
            )
            desc = item.get("description")
            desc_text = f" — {desc}" if desc else ""
            lines.append(f"· {disp}：{bs_text}{free_text}{desc_text}")
    if focus != "profession":
        factions = modules.get("factions")
        factions = factions if isinstance(factions, list) else []
        if factions:
            names = [
                f.get("name", "")
                for f in factions[:5]
                if isinstance(f, Mapping)
            ]
            if names:
                lines.append("【主要势力】" + "、".join(names))
        regions = modules.get("regions")
        regions = regions if isinstance(regions, list) else []
        if regions:
            names = [
                r.get("name", "")
                for r in regions[:5]
                if isinstance(r, Mapping)
            ]
            if names:
                lines.append("【主要地点】" + "、".join(names))
    return "\n".join(lines)


__all__ = [name for name in globals() if not name.startswith('__')]

