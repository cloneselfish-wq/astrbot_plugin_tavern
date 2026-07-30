from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any, Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .tavern.config import TavernConfig
from .tavern.constants import (
    PLAYER_ACTIONS,
    PLUGIN_NAME,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from .tavern.database import (
    DatabaseNotFoundError,
    InvalidTransitionError,
    TavernDatabase,
)
from .tavern.engine import (
    TavernBusyError,
    TavernEngine,
    TavernEngineError,
    TavernPlayerDisabledError,
    TavernTurnOrderError,
)
from .tavern.events import EventBroker
from .tavern.lifecycle import (
    card_stat_allocation,
    format_choices,
    normalize_time_rules,
    parse_choice_input,
    parse_duration,
    player_limits,
)
from .tavern.security import (
    ParsedCommand,
    parse_story_trigger,
    parse_tavern_command,
    validate_platform_id,
)
from .tavern.web_console import TavernWebConsole


INSTANCE_LIST_PAGE_SIZE = 5
INSTANCE_INTRO_MAX_CHARS = 220
REVIEW_LIST_PAGE_SIZE = 5
PRIVATE_CARD_ACTIONS = frozenset(
    {
        "card",
        "card_fill",
        "card_stats_reset",
        "card_preview",
        "card_confirm",
        "card_cancel",
    }
)
PRIVATE_ONLY_CARD_ACTIONS = PRIVATE_CARD_ACTIONS - {"card"}
_INSTANCE_PAGE_PATTERNS = (
    re.compile(r"^第\s*(\d{1,6})\s*页$"),
    re.compile(r"^页\s*(\d{1,6})$"),
    re.compile(r"^列表\s*(\d{1,6})$"),
)


HELP_TEXT = """\
【AI 酒馆 v0.5.2 Alpha｜多人跑团与独立存档】
主持：/酒馆 开启 <副本> → /酒馆 开演
恢复：/酒馆 暂停 → /酒馆 继续 → 全员准备 → /酒馆 继续
玩家：/酒馆 加入｜角色｜准备｜阵容｜暂离｜返回队列｜退出
建卡：加入后私聊 Bot 发送 /酒馆 建卡 <验证码>｜重填数值
回合：jg A｜/酒馆 选择 A｜/酒馆 重整选项
裁定：/酒馆 灵感｜/酒馆 灵感 A 优势｜/酒馆 灵感重投 A
集体：/酒馆 投票 A（不消耗个人行动）
记录：/酒馆 回顾｜存档列表｜存档 <名称>｜读档 <名称>｜回滚
管理：审核｜跳过｜移至｜指定｜封禁｜解封｜黑名单｜延时
安全：任一出场玩家可发送 /酒馆 安全暂停
结束：/酒馆 关闭｜/酒馆 完结 确认｜/酒馆 强制终止 确认 <原因>

普通群聊完全旁路；剧情只接受 A/B/C/D，不接受自由改写世界。"""


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
        return "【回合顺序】尚无玩家，请先发送 /酒馆 加入。"
    current_id = str(turn.get("current_user_id") or "")
    lines = []
    for item in order:
        marker = "▶" if str(item.get("user_id") or "") == current_id else "·"
        lines.append(
            f"{marker} {item.get('position', '?')}. "
            f"{item.get('name') or item.get('user_id')}"
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
            f"【请选择酒馆副本｜第 {effective_page}/{pages} 页"
            f"｜共 {total} 个】"
        ]
        for index, item in enumerate(page_items, start=start + 1):
            marker = "▶" if item.get("selected") else "·"
            lines.append(
                f"{marker} {index}. {item.get('instance_name')} "
                f"（{item.get('instance_slug')}）"
                f" · {item.get('world_name')}"
                f" · {state_labels.get(item.get('state'), item.get('state'))}"
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
                selection_label="发送：/酒馆 开启 <副本标识>",
            )
        )
        return "\n".join(lines)

    lines = [
        f"【本群还没有酒馆副本｜可用世界第 {effective_page}/{pages} 页"
        f"｜共 {total} 个】"
    ]
    for index, item in enumerate(page_items, start=start + 1):
        lines.append(f"· {index}. {item.get('name')}（{item.get('slug')}）")
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
                    "/酒馆 开启 <世界标识>"
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
        navigation.append(f"上一页：/酒馆 开启 第{page - 1}页")
    if page < pages:
        navigation.append(f"下一页：/酒馆 开启 第{page + 1}页")
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
            or item.get("group_user_id")
        )
        ready = "已准备" if item.get("ready") else "未准备"
        lines.append(
            f"· {name}"
            f"（{item.get('character_code') or '无代号'}）"
            f" · {card_labels.get(item.get('card_status'), item.get('card_status'))}"
            f" · {ready}"
            f" · {participation_labels.get(item.get('participation_status'), item.get('participation_status'))}"
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
            "发送：/酒馆 投票 A",
        ]
    )
    return "\n".join(lines)


def format_card_prompt(draft: Mapping[str, Any]) -> str:
    template = draft.get("template") or {}
    fields = template.get("fields") or []
    values = draft.get("fields")
    values = values if isinstance(values, Mapping) else {}
    step = int(
        draft.get("current_step", draft.get("draft_step", 0)) or 0
    )
    if step >= len(fields):
        allocation = card_stat_allocation(template, values)
        lines = ["【角色卡字段已填写完成】"]
        if allocation["stat_fields"]:
            lines.append(
                f"角色数值：已使用 {allocation['used']}"
                f"/{allocation['budget']} 点"
                f" · 剩余 {allocation['remaining']} 点"
            )
            lines.append(
                "只重新分配数值：/酒馆 重填数值"
            )
        lines.append(
            "发送 /酒馆 预览 检查内容，"
            "确认无误后发送 /酒馆 确认建卡。"
        )
        return "\n".join(lines)
    field = fields[step]
    allocation = card_stat_allocation(template, values, step)
    current_stat = allocation.get("current")
    if isinstance(current_stat, Mapping):
        lines = []
        if (
            int(current_stat["position"]) == 1
            and not allocation["values"]
        ):
            lines.extend(
                [
                    "【接下来开始填写角色数值】",
                    "前面的角色资料已经保存；"
                    "数值会按世界模板的总预算依次分配。",
                ]
            )
        lines.extend(
            [
                (
                    f"【角色数值 {current_stat['position']}"
                    f"/{current_stat['total']}】"
                    f"{current_stat['label']}"
                ),
                (
                    f"当前可填：{current_stat['minimum']}"
                    f"—{current_stat['effective_maximum']}"
                ),
                (
                    f"总预算：{allocation['budget']} 点"
                    f" · 已使用：{current_stat['used_before']} 点"
                    f" · 当前剩余："
                    f"{current_stat['remaining_before']} 点"
                ),
            ]
        )
        if int(current_stat["reserved_minimum"]) > 0:
            lines.append(
                f"已为后续属性预留最低 "
                f"{current_stat['reserved_minimum']} 点。"
            )
        lines.extend(
            [
                "后续属性的可填上限会随剩余预算自动递减。",
                "直接回复整数，或发送：/酒馆 填写 <整数>",
                "重新分配全部数值：/酒馆 重填数值",
            ]
        )
        return "\n".join(lines)
    required = "必填" if field.get("required") else "选填"
    return (
        f"【角色卡 {step + 1}/{len(fields)}】"
        f"{field.get('label')}（{required}，最多 {field.get('max_chars')} 字）\n"
        "字段内容不得包含空格、全角空格、换行或制表符。\n"
        "直接回复内容，或发送：/酒馆 填写 <内容>"
    )


def format_card_preview(draft: Mapping[str, Any]) -> str:
    template = draft.get("template") or {}
    fields = draft.get("fields") or {}
    lines = ["【角色卡预览】"]
    for definition in template.get("fields") or []:
        if definition.get("stat_key"):
            continue
        value = str(fields.get(definition.get("key"), "") or "")
        if definition.get("private"):
            value = "（私密字段已保存）" if value else "（未填写）"
        lines.append(f"· {definition.get('label')}：{value or '（未填写）'}")
    allocation = card_stat_allocation(template, fields)
    if allocation["stat_fields"]:
        modifier_table = (template.get("stats") or {}).get(
            "modifier_table"
        ) or {}
        lines.append("")
        lines.append("【角色数值】")
        for item in allocation["stat_fields"]:
            value = allocation["values"].get(item["field_key"])
            if value is None:
                lines.append(f"· {item['label']}：（未填写）")
                continue
            modifier = int(modifier_table.get(str(value), 0))
            lines.append(
                f"· {item['label']}：{value}"
                f"（检定修正 {modifier:+d}）"
            )
        lines.append(
            f"· 预算：已使用 {allocation['used']}"
            f"/{allocation['budget']} 点"
            f" · 剩余 {allocation['remaining']} 点"
        )
    lines.extend(
        [
            "",
            "只重新分配数值：/酒馆 重填数值",
            "确认：/酒馆 确认建卡",
            "放弃并释放席位：/酒馆 取消建卡",
        ]
    )
    return "\n".join(lines)


def _review_reference(participant: Mapping[str, Any]) -> str:
    raw = str(participant.get("id") or "").split("_", 1)[-1]
    token = re.sub(r"[^a-zA-Z0-9]", "", raw).upper()
    return f"R-{(token or 'UNKNOWN')[:8]}"


def _pending_review_cards(
    roster: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [
        item
        for item in roster
        if item.get("card_status") == "pending_review"
        and item.get("character_version_id")
    ]


def _resolve_pending_review(
    pending: list[Mapping[str, Any]],
    reference: str,
) -> Mapping[str, Any]:
    normalized = str(reference or "").strip()
    ordinal = normalized.removeprefix("#")
    if ordinal.isdigit():
        index = int(ordinal)
        if 1 <= index <= len(pending):
            return pending[index - 1]
        raise DatabaseNotFoundError(
            "待审核序号不存在，请发送 /酒馆 审核 刷新名单"
        )

    lowered = normalized.casefold()
    matches = []
    for item in pending:
        aliases = item.get("aliases")
        aliases = aliases if isinstance(aliases, list) else []
        candidates = {
            str(item.get("id") or ""),
            _review_reference(item),
            str(item.get("character_name") or ""),
            str(item.get("character_code") or ""),
            str(item.get("display_name") or ""),
            *(str(value) for value in aliases),
        }
        if any(
            candidate and candidate.casefold() == lowered
            for candidate in candidates
        ):
            matches.append(item)
    if not matches:
        raise DatabaseNotFoundError(
            "未找到对应的待审核角色，请发送 /酒馆 审核 刷新名单"
        )
    if len(matches) > 1:
        raise ValueError("角色标识不唯一，请改用名单中的审核号")
    return matches[0]


def format_pending_reviews(
    pending: list[Mapping[str, Any]],
    *,
    page: int = 1,
) -> str:
    if not pending:
        return "【待审核角色卡】当前没有待审核玩家。"
    pages = max(
        1,
        (len(pending) + REVIEW_LIST_PAGE_SIZE - 1)
        // REVIEW_LIST_PAGE_SIZE,
    )
    effective_page = min(max(1, int(page or 1)), pages)
    start = (effective_page - 1) * REVIEW_LIST_PAGE_SIZE
    items = pending[start : start + REVIEW_LIST_PAGE_SIZE]
    lines = [
        f"【待审核角色卡｜第 {effective_page}/{pages} 页"
        f"｜共 {len(pending)} 人】"
    ]
    for index, item in enumerate(items, start=start + 1):
        lines.append(
            f"{index}. "
            f"{item.get('character_name') or item.get('display_name')}"
            f"（{item.get('character_code') or '无代号'}）"
            f" · 玩家：{item.get('display_name')}"
            f" · 审核号：{_review_reference(item)}"
        )
    lines.extend(
        [
            "",
            "查看角色卡：/酒馆 审核 <序号或审核号>",
            "通过：/酒馆 审核 <序号或审核号> 通过 [备注]",
            "驳回：/酒馆 审核 <序号或审核号> 驳回 [原因]",
        ]
    )
    navigation = []
    if effective_page > 1:
        navigation.append(
            f"上一页：/酒馆 审核 第{effective_page - 1}页"
        )
    if effective_page < pages:
        navigation.append(
            f"下一页：/酒馆 审核 第{effective_page + 1}页"
        )
    if navigation:
        lines.append("｜".join(navigation))
    return "\n".join(lines)


def format_review_card(
    participant: Mapping[str, Any],
    template: Mapping[str, Any],
) -> str:
    profile = participant.get("card_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    stats = participant.get("card_stats")
    stats = stats if isinstance(stats, Mapping) else {}
    lines = [
        f"【角色卡审核详情｜{_review_reference(participant)}】",
        (
            f"玩家：{participant.get('display_name')}"
            f" · 角色："
            f"{participant.get('character_name') or '未命名'}"
            f"（{participant.get('character_code') or '无代号'}）"
        ),
        (
            f"角色卡版本：{participant.get('card_version_no') or 1}"
            f" · 模板版本："
            f"{participant.get('card_template_version') or 1}"
        ),
        "",
        "【角色资料】",
    ]
    for definition in template.get("fields") or []:
        if definition.get("stat_key"):
            continue
        value = profile.get(definition.get("key"), "")
        if definition.get("private"):
            value_text = (
                "（已填写，群聊中隐藏）"
                if str(value or "").strip()
                else "（未填写）"
            )
        else:
            value_text = str(value or "").strip() or "（未填写）"
        lines.append(f"· {definition.get('label')}：{value_text}")

    attributes = (template.get("stats") or {}).get("attributes") or []
    raw_stats = stats.get("raw")
    raw_stats = raw_stats if isinstance(raw_stats, Mapping) else {}
    modifiers = stats.get("modifiers")
    modifiers = modifiers if isinstance(modifiers, Mapping) else {}
    budget = int(
        stats.get("budget")
        or (template.get("stats") or {}).get("budget")
        or 0
    )
    used = 0
    lines.extend(["", "【角色数值】"])
    for attribute in attributes:
        key = str(attribute.get("key") or "")
        fallback = profile.get(f"stat_{key}", attribute.get("default", 0))
        value = int(raw_stats.get(key, fallback))
        modifier = int(modifiers.get(key, 0))
        used += value
        lines.append(
            f"· {attribute.get('label') or key}：{value}"
            f"（检定修正 {modifier:+d}）"
        )
    lines.extend(
        [
            f"· 预算：已使用 {used}/{budget} 点 · 剩余 {budget - used} 点",
            "",
            "标为私密的字段不会在群聊展开；"
            "完整内容仍可在后台“准备与角色”查看。",
            (
                f"通过：/酒馆 审核 {_review_reference(participant)}"
                " 通过 [备注]"
            ),
            (
                f"驳回：/酒馆 审核 {_review_reference(participant)}"
                " 驳回 [原因]"
            ),
        ]
    )
    return "\n".join(lines)


def _format_remaining_time(value: Any) -> str:
    try:
        remaining = max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    days, remaining = divmod(remaining, 24 * 60 * 60)
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


class TavernPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.plugin_config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.database = TavernDatabase(self.data_dir)
        self.broker = EventBroker()
        self._config_lock = asyncio.Lock()
        self.engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=self.runtime_config,
            broker=self.broker,
        )
        self.web_console = TavernWebConsole(
            context=context,
            plugin_config=config,
            database=self.database,
            broker=self.broker,
            data_dir=self.data_dir,
            logger=logger,
            allow_group=self._allow_group,
            config_lock=self._config_lock,
        )
        self._timer_task: asyncio.Task[None] | None = None

    def runtime_config(self) -> TavernConfig:
        return TavernConfig.from_mapping(self.plugin_config)

    async def _allow_group(
        self,
        *,
        group_id: str,
        platform_id: str,
        actor_id: str,
        source: str,
    ) -> bool:
        """Persist an explicitly authorized group binding.

        The caller has already authenticated either a configured group
        administrator or an AstrBot Dashboard user.
        """

        normalized_group = validate_platform_id(group_id, label="群 ID")
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        async with self._config_lock:
            current = self.runtime_config()
            if normalized_group in current.allowed_group_ids:
                return False

            previous_security = self.plugin_config.get("security")
            security: dict[str, Any]
            if isinstance(previous_security, Mapping):
                security = dict(previous_security)
            else:
                security = {}
            security["allowed_group_ids"] = sorted(
                {*current.allowed_group_ids, normalized_group}
            )
            self.plugin_config["security"] = security

            try:
                save_async = getattr(
                    self.plugin_config,
                    "save_config_async",
                    None,
                )
                if callable(save_async):
                    await save_async()
                else:
                    save = getattr(self.plugin_config, "save_config", None)
                    if callable(save):
                        save()
            except Exception:
                if previous_security is None:
                    self.plugin_config.pop("security", None)
                else:
                    self.plugin_config["security"] = previous_security
                raise

        try:
            await self.database.write_audit(
                "",
                actor_id,
                "security.group_auto_allowed",
                normalized_group,
                {
                    "platform_id": normalized_platform,
                    "source": source,
                },
            )
        except Exception:
            logger.exception("AI 酒馆写入自动绑定审计失败")
        await self.broker.publish(
            {
                "type": "settings",
                "action": "group_auto_allowed",
                "group_id": normalized_group,
            }
        )
        return True

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "group_id", "") or "")

    @staticmethod
    def _platform_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return origin.split(":", 1)[0] if ":" in origin else "qq"

    async def _write_security_audit(
        self,
        *,
        sender_id: str,
        action: str,
        group_id: str,
        platform_id: str,
        reason: str,
    ) -> None:
        try:
            await self.database.write_audit(
                "",
                sender_id,
                "security.command_denied",
                group_id,
                {
                    "action": action or "unknown",
                    "platform_id": platform_id,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception("AI 酒馆写入安全审计失败")

    @filter.command_group("酒馆", priority=200)
    def tavern():
        """AI 酒馆原生指令组。"""

    async def _handle_private_card_message(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        message: str,
    ) -> str | None:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        sender_id = str(event.get_sender_id() or "")
        draft = await self.database.card_draft_for_private(origin)
        if not command.matched and not draft:
            return None

        event.stop_event()
        try:
            if command.matched and command.action == "card":
                if not command.argument:
                    return (
                        "【私聊建卡】请发送 /酒馆 建卡 <群内验证码>。"
                    )
                bound = await self.database.bind_card_code(
                    command.argument,
                    sender_id,
                    origin,
                )
                return "【私聊身份绑定成功】\n" + format_card_prompt(bound)
            if command.matched and command.action == "card_preview":
                preview = await self.database.preview_card_draft(origin)
                return format_card_preview(preview)
            if command.matched and command.action == "card_stats_reset":
                reset = await self.database.reset_card_draft_stats(origin)
                return (
                    "【角色数值已重置】"
                    "文字角色资料均已保留。\n"
                    + format_card_prompt(reset)
                )
            if command.matched and command.action == "card_confirm":
                result = await self.database.confirm_card_draft(origin)
                status = (
                    "角色卡已自动通过，可以回群发送 /酒馆 准备。"
                    if result.get("auto_approved")
                    else "角色卡已提交审核，审核通过后再回群准备。"
                )
                return (
                    f"【角色卡已确认】{result.get('character_name')}\n"
                    f"{status}"
                )
            if command.matched and command.action == "card_cancel":
                await self.database.cancel_card_draft(origin)
                return "【建卡已取消】草稿已归档，本次占用席位已释放。"
            if command.matched and command.action == "card_fill":
                value = command.argument
            elif command.matched:
                return (
                    "【私聊建卡】可用：建卡、填写、预览、"
                    "重填数值、确认建卡、取消建卡。"
                )
            else:
                value = message
            result = await self.database.fill_card_draft(origin, value)
            return format_card_prompt(result)
        except (DatabaseNotFoundError, PermissionError, ValueError) as exc:
            return f"【私聊建卡】{exc}"
        except Exception:
            logger.exception("AI 酒馆私聊建卡失败")
            return "【私聊建卡】处理失败，草稿没有丢失，请稍后重试。"

    async def _run_native_command(
        self,
        event: AstrMessageEvent,
        action: str,
    ) -> str | None:
        """Dispatch a command already matched by AstrBot's native router."""

        event.stop_event()
        getter = getattr(event, "get_message_str", None)
        message = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        )
        parts = message.strip().split(maxsplit=2)
        command = ParsedCommand(
            matched=True,
            action=action,
            argument=parts[2].strip() if len(parts) > 2 else "",
            raw_action=parts[1].strip() if len(parts) > 1 else action,
        )
        config = self.runtime_config()
        group_id = self._group_id(event)
        sender_id = str(event.get_sender_id() or "")
        platform_id = self._platform_id(event)
        logger.info(
            "AI 酒馆原生命令：platform=%s group=%s sender=%s command=%s",
            platform_id,
            group_id,
            sender_id,
            action,
        )
        if not group_id:
            if action in PRIVATE_CARD_ACTIONS:
                return await self._handle_private_card_message(
                    event,
                    command,
                    message,
                )
            return "【酒馆】该指令仅支持群聊。"
        if action in PRIVATE_ONLY_CARD_ACTIONS:
            return "【酒馆】该建卡命令请在与 Bot 的私聊中使用。"
        try:
            return await self._handle_command(
                event=event,
                command=command,
                config=config,
                group_id=group_id,
                platform_id=platform_id,
                sender_id=sender_id,
            )
        except Exception:
            logger.exception("AI 酒馆原生管理命令发生未处理异常")
            can_respond = config.is_admin(sender_id) or (
                action == "status" and config.public_status
            )
            if can_respond:
                return "【酒馆】管理命令处理失败，请查看 AstrBot 日志。"
            return None

    @tavern.command("开启", alias={"启动"}, priority=200)
    async def tavern_start(self, event: AstrMessageEvent):
        """列出副本，或按命令后的副本标识开启。"""

        response = await self._run_native_command(event, "start")
        if response:
            yield event.plain_result(response)

    @tavern.command("开演", alias={"开始故事"}, priority=200)
    async def tavern_perform(self, event: AstrMessageEvent):
        """完成准备检查并正式开始故事。"""

        response = await self._run_native_command(event, "perform")
        if response:
            yield event.plain_result(response)

    @tavern.command("暂停", priority=200)
    async def tavern_pause(self, event: AstrMessageEvent):
        """暂停当前酒馆会话。"""

        response = await self._run_native_command(event, "pause")
        if response:
            yield event.plain_result(response)

    @tavern.command("继续", alias={"恢复"}, priority=200)
    async def tavern_resume(self, event: AstrMessageEvent):
        """继续当前酒馆会话。"""

        response = await self._run_native_command(event, "resume")
        if response:
            yield event.plain_result(response)

    @tavern.command("关闭", priority=200)
    async def tavern_close(self, event: AstrMessageEvent):
        """关闭当前酒馆会话。"""

        response = await self._run_native_command(event, "close")
        if response:
            yield event.plain_result(response)

    @tavern.command("完结", priority=200)
    async def tavern_finish(self, event: AstrMessageEvent):
        """二次确认后归档当前故事。"""

        response = await self._run_native_command(event, "finish")
        if response:
            yield event.plain_result(response)

    @tavern.command("强制终止", priority=200)
    async def tavern_abort(self, event: AstrMessageEvent):
        """二次确认后异常终止并永久归档当前故事。"""

        response = await self._run_native_command(event, "abort")
        if response:
            yield event.plain_result(response)

    @tavern.command("安全暂停", priority=200)
    async def tavern_safety_pause(self, event: AstrMessageEvent):
        """任一出场玩家都可立即冻结故事与全部计时。"""

        response = await self._run_native_command(event, "safety_pause")
        if response:
            yield event.plain_result(response)

    @tavern.command("维护", priority=200)
    async def tavern_maintenance(self, event: AstrMessageEvent):
        """将当前酒馆会话切换至维护状态。"""

        response = await self._run_native_command(event, "maintenance")
        if response:
            yield event.plain_result(response)

    @tavern.command("状态", priority=200)
    async def tavern_status(self, event: AstrMessageEvent):
        """查看当前酒馆会话状态。"""

        response = await self._run_native_command(event, "status")
        if response:
            yield event.plain_result(response)

    @tavern.command("存档", priority=200)
    async def tavern_save(self, event: AstrMessageEvent):
        """保存当前酒馆会话。"""

        response = await self._run_native_command(event, "save")
        if response:
            yield event.plain_result(response)

    @tavern.command("读档", priority=200)
    async def tavern_load(self, event: AstrMessageEvent):
        """读取当前酒馆存档。"""

        response = await self._run_native_command(event, "load")
        if response:
            yield event.plain_result(response)

    @tavern.command("回滚", priority=200)
    async def tavern_rollback(self, event: AstrMessageEvent):
        """回滚当前酒馆会话的上一回合。"""

        response = await self._run_native_command(event, "rollback")
        if response:
            yield event.plain_result(response)

    @tavern.command("世界列表", priority=200)
    async def tavern_worlds(self, event: AstrMessageEvent):
        """列出可用世界包。"""

        response = await self._run_native_command(event, "worlds")
        if response:
            yield event.plain_result(response)

    @tavern.command("副本列表", alias={"副本"}, priority=200)
    async def tavern_instances(self, event: AstrMessageEvent):
        """列出当前群可选择的剧情副本。"""

        response = await self._run_native_command(event, "instances")
        if response:
            yield event.plain_result(response)

    @tavern.command("加入", priority=200)
    async def tavern_join(self, event: AstrMessageEvent):
        """加入当前群的多人回合队列。"""

        response = await self._run_native_command(event, "join")
        if response:
            yield event.plain_result(response)

    @tavern.command("建卡", priority=200)
    async def tavern_card(self, event: AstrMessageEvent):
        """在群内查看建卡码，或在私聊中绑定建卡码。"""

        response = await self._run_native_command(event, "card")
        if response:
            yield event.plain_result(response)

    @tavern.command("填写", priority=200)
    async def tavern_card_fill(self, event: AstrMessageEvent):
        """在私聊中填写当前角色卡字段。"""

        response = await self._run_native_command(event, "card_fill")
        if response:
            yield event.plain_result(response)

    @tavern.command("预览", priority=200)
    async def tavern_card_preview(self, event: AstrMessageEvent):
        """在私聊中预览完整角色卡。"""

        response = await self._run_native_command(event, "card_preview")
        if response:
            yield event.plain_result(response)

    @tavern.command("重填数值", priority=200)
    async def tavern_card_stats_reset(self, event: AstrMessageEvent):
        """保留文字角色资料，仅重新分配角色数值。"""

        response = await self._run_native_command(
            event,
            "card_stats_reset",
        )
        if response:
            yield event.plain_result(response)

    @tavern.command("确认建卡", priority=200)
    async def tavern_card_confirm(self, event: AstrMessageEvent):
        """在私聊中提交角色卡审核。"""

        response = await self._run_native_command(event, "card_confirm")
        if response:
            yield event.plain_result(response)

    @tavern.command("取消建卡", priority=200)
    async def tavern_card_cancel(self, event: AstrMessageEvent):
        """在私聊中取消角色卡草稿并释放席位。"""

        response = await self._run_native_command(event, "card_cancel")
        if response:
            yield event.plain_result(response)

    @tavern.command("角色", priority=200)
    async def tavern_character(self, event: AstrMessageEvent):
        """查看自己的副本角色状态。"""

        response = await self._run_native_command(event, "character")
        if response:
            yield event.plain_result(response)

    @tavern.command("准备", priority=200)
    async def tavern_ready(self, event: AstrMessageEvent):
        """在准备大厅确认本次出场。"""

        response = await self._run_native_command(event, "ready")
        if response:
            yield event.plain_result(response)

    @tavern.command("阵容", priority=200)
    async def tavern_roster(self, event: AstrMessageEvent):
        """查看当前角色卡、准备与入场状态。"""

        response = await self._run_native_command(event, "roster")
        if response:
            yield event.plain_result(response)

    @tavern.command("审核", priority=200)
    async def tavern_review(self, event: AstrMessageEvent):
        """列出、查看并处理待审核角色卡。"""

        response = await self._run_native_command(event, "review")
        if response:
            yield event.plain_result(response)

    @tavern.command("选择", priority=200)
    async def tavern_choose(self, event: AstrMessageEvent):
        """选择当前回合的 A/B/C/D 行动。"""

        response = await self._run_native_command(event, "choose")
        if response:
            yield event.plain_result(response)

    @tavern.command("重整选项", priority=200)
    async def tavern_reroll(self, event: AstrMessageEvent):
        """免费重整本回合选项一次。"""

        response = await self._run_native_command(event, "reroll")
        if response:
            yield event.plain_result(response)

    @tavern.command("灵感", priority=200)
    async def tavern_inspiration(self, event: AstrMessageEvent):
        """查看灵感，或在选择检定选项时消耗一点取得优势。"""

        response = await self._run_native_command(event, "inspiration")
        if response:
            yield event.plain_result(response)

    @tavern.command("灵感重投", priority=200)
    async def tavern_inspiration_reroll(self, event: AstrMessageEvent):
        """选择检定选项并消耗一点灵感重投完整骰池。"""

        response = await self._run_native_command(
            event,
            "inspiration_reroll",
        )
        if response:
            yield event.plain_result(response)

    @tavern.command("投票", priority=200)
    async def tavern_vote(self, event: AstrMessageEvent):
        """参与当前集体决策。"""

        response = await self._run_native_command(event, "vote")
        if response:
            yield event.plain_result(response)

    @tavern.command("暂离", priority=200)
    async def tavern_away(self, event: AstrMessageEvent):
        """暂离回合队列但保留席位。"""

        response = await self._run_native_command(event, "away")
        if response:
            yield event.plain_result(response)

    @tavern.command("返回队列", priority=200)
    async def tavern_return_queue(self, event: AstrMessageEvent):
        """从下一轮队尾重新加入行动。"""

        response = await self._run_native_command(event, "return_queue")
        if response:
            yield event.plain_result(response)

    @tavern.command("申请返场", priority=200)
    async def tavern_return_request(self, event: AstrMessageEvent):
        """为已退场角色申请剧情返场。"""

        response = await self._run_native_command(event, "return_request")
        if response:
            yield event.plain_result(response)

    @tavern.command("退出", priority=200)
    async def tavern_leave(self, event: AstrMessageEvent):
        """退出当前群的多人回合队列。"""

        response = await self._run_native_command(event, "leave")
        if response:
            yield event.plain_result(response)

    @tavern.command("顺序", alias={"轮次"}, priority=200)
    async def tavern_order(self, event: AstrMessageEvent):
        """查看当前轮次与行动顺序。"""

        response = await self._run_native_command(event, "order")
        if response:
            yield event.plain_result(response)

    @tavern.command("跳过", priority=200)
    async def tavern_skip(self, event: AstrMessageEvent):
        """当前玩家主动跳过自己的行动。"""

        response = await self._run_native_command(event, "skip")
        if response:
            yield event.plain_result(response)

    @tavern.command("下一位", priority=200)
    async def tavern_next(self, event: AstrMessageEvent):
        """管理员强制跳过当前行动者。"""

        response = await self._run_native_command(event, "next")
        if response:
            yield event.plain_result(response)

    @tavern.command("帮助", priority=200)
    async def tavern_help(self, event: AstrMessageEvent):
        """显示酒馆指令帮助。"""

        response = await self._run_native_command(event, "help")
        if response:
            yield event.plain_result(response)

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        config = self.runtime_config()
        cleaned = await self.database.cleanup(config.audit_retention_days)
        if not self._timer_task or self._timer_task.done():
            self._timer_task = asyncio.create_task(
                self._timer_loop(),
                name="ai-tavern-timers",
            )
        logger.info(
            "AI 酒馆已加载：数据库=%s，清理审计=%s",
            self.database.path,
            cleaned.get("audit_logs", 0),
        )
        if self.database.migration_backup_path:
            logger.info(
                "AI 酒馆旧库迁移备份已保存：%s",
                self.database.migration_backup_path,
            )
        if self.database.legacy_retained_path:
            logger.info(
                "AI 酒馆旧 tavern.sqlite3 已转存：%s",
                self.database.legacy_retained_path,
            )

    async def _timer_loop(self) -> None:
        try:
            while True:
                notifications = await self.database.process_due_timers()
                for item in notifications:
                    await self._send_timer_notice(item)
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AI 酒馆计时器轮询异常，将在稍后重试")
            await asyncio.sleep(15)
            if not self._timer_task or self._timer_task.cancelled():
                return
            self._timer_task = asyncio.create_task(
                self._timer_loop(),
                name="ai-tavern-timers-retry",
            )

    async def _send_timer_notice(self, item: Mapping[str, Any]) -> None:
        try:
            session = await self.database.get_session(
                str(item.get("session_id") or "")
            )
            instance_config = await self.database.get_instance_config(
                session["id"]
            )
            if not instance_config["time_rules"].get(
                "announce_timeouts",
                True,
            ):
                return
            timer_type = str(item.get("timer_type") or "")
            kind = str(item.get("kind") or "")
            labels = {
                "turn": "行动回合",
                "vote": "集体投票",
                "card_code": "私聊建卡码",
                "card_completion": "角色卡创建",
                "ready": "准备确认",
                "preparation": "准备大厅",
                "standby": "候补保留",
                "all_idle": "全员无互动",
            }
            if kind == "idle_pause":
                text = "【酒馆已自动暂停】全员超过设定时间没有酒馆互动。"
                text += "全部计时已冻结；重新准备后由主持人发送 /酒馆 继续。"
            elif kind == "reminder":
                remaining = _format_remaining_time(
                    item.get("remaining_seconds")
                )
                prompts = {
                    "turn": "请及时完成本回合操作。",
                    "vote": "请尚未投票的玩家完成投票。",
                    "card_code": "请及时绑定私聊建卡码。",
                    "card_completion": "请及时完成角色卡创建。",
                    "ready": "请及时完成准备确认。",
                    "preparation": "请及时完成准备流程。",
                    "standby": "请在保留期内返回队列。",
                }
                text = (
                    f"【酒馆倒计时】"
                    f"{labels.get(timer_type, timer_type)}"
                    f"剩余 {remaining}。"
                    f"{prompts.get(timer_type, '请及时完成对应操作。')}"
                )
            else:
                text = (
                    f"【酒馆计时】"
                    f"{labels.get(timer_type, timer_type)}已经到期。"
                )
                if kind == "expired":
                    text += "系统已按当前副本的超时规则处理。"
            origin = str(session.get("unified_origin") or "")
            sender = getattr(self.context, "send_message", None)
            if not origin or not callable(sender):
                return
            from astrbot.api.event import MessageChain

            chain = MessageChain()
            mention_count = 0
            targets = item.get("targets")
            if isinstance(targets, Sequence):
                seen: set[str] = set()
                for target in targets:
                    if not isinstance(target, Mapping):
                        continue
                    user_id = str(target.get("user_id") or "")
                    if not user_id or user_id in seen:
                        continue
                    seen.add(user_id)
                    display_name = str(
                        target.get("display_name") or user_id
                    )
                    chain.at(display_name, user_id).message(" ")
                    mention_count += 1
            if kind == "reminder" and timer_type in {
                "vote",
                "preparation",
            } and mention_count == 0:
                return
            sent = await sender(origin, chain.message(text))
            if not sent:
                logger.warning(
                    "AI 酒馆计时通知未找到可用会话：session=%s",
                    session["id"],
                )
        except Exception:
            # QQ 官方平台不保证支持主动群发；状态处理仍已持久化。
            logger.warning(
                "AI 酒馆计时通知发送失败：session=%s",
                item.get("session_id"),
            )

    @filter.event_message_type(
        getattr(filter.EventMessageType, "PRIVATE_MESSAGE", "private"),
        priority=110,
    )
    async def on_private_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "message_str", "") or "").strip()
        command = parse_tavern_command(message)
        response = await self._handle_private_card_message(
            event,
            command,
            message,
        )
        if response:
            yield event.plain_result(response)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def on_group_message(self, event: AstrMessageEvent):
        message = str(event.message_str or "")
        command = parse_tavern_command(message)
        normalized_message = message.strip()
        if (
            not command.matched
            and bool(getattr(event, "is_at_or_wake_command", False))
            and (
                normalized_message == "酒馆"
                or (
                    normalized_message.startswith("酒馆")
                    and normalized_message[2:3].isspace()
                )
            )
        ):
            command = parse_tavern_command("/" + normalized_message)
        config = self.runtime_config()
        group_id = self._group_id(event)
        sender_id = str(event.get_sender_id() or "")
        platform_id = self._platform_id(event)
        content = (
            None
            if command.matched
            else parse_story_trigger(message, config.trigger_prefix)
        )
        if config.debug and (command.matched or content is not None):
            logger.debug(
                "AI 酒馆事件：platform=%s group=%s sender=%s command=%s",
                platform_id,
                group_id,
                sender_id,
                command.action if command.matched else "story",
            )

        if command.matched:
            event.stop_event()
            try:
                response = await self._handle_command(
                    event=event,
                    command=command,
                    config=config,
                    group_id=group_id,
                    platform_id=platform_id,
                    sender_id=sender_id,
                )
            except Exception:
                logger.exception("AI 酒馆管理命令发生未处理异常")
                can_respond = config.is_admin(sender_id) or (
                    command.action == "status" and config.public_status
                )
                response = (
                    "【酒馆】管理命令处理失败，请查看 AstrBot 日志。"
                    if can_respond
                    else None
                )
            if response:
                yield event.plain_result(response)
            return

        if content is None:
            return
        if not config.is_group_allowed(group_id):
            return
        session = await self.database.get_session_by_group(
            platform_id,
            group_id,
        )

        event.stop_event()
        if not session or session["state"] == SESSION_CLOSED:
            yield event.plain_result(
                "【酒馆】当前群尚未开馆，请由管理员发送 /酒馆 开启。"
            )
            return
        if session["state"] == SESSION_PREPARING:
            yield event.plain_result(
                "【故事尚未开演】当前处于角色准备阶段。"
                "请先完成角色卡并发送 /酒馆 准备，"
                "由主持人发送 /酒馆 开演 或 /酒馆 继续。"
            )
            return
        if session["state"] == SESSION_PAUSED:
            yield event.plain_result(
                "【酒馆】剧情已暂停，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_FINISHED:
            yield event.plain_result(
                "【酒馆】故事已经完结，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_MAINTENANCE:
            yield event.plain_result(
                "【酒馆】当前处于维护模式，本条内容未记录。"
            )
            return

        try:
            vote = await self.database.active_vote(session["id"])
            if vote:
                yield event.plain_result(
                    "【集体投票进行中】请使用 /酒馆 投票 A；"
                    "投票不会消耗个人行动机会。"
                )
                return
            choice_key, flavor_text = parse_choice_input(content)
            reply = await self.engine.process_choice(
                event=event,
                session_id=session["id"],
                sender_id=sender_id,
                sender_name=str(event.get_sender_name() or sender_id),
                choice_key=choice_key,
                flavor_text=flavor_text,
            )
            yield event.plain_result(reply.text)
        except TavernTurnOrderError as exc:
            yield event.plain_result(f"【回合秩序】{exc}")
        except TavernBusyError as exc:
            yield event.plain_result(f"【酒馆】{exc}")
        except TavernPlayerDisabledError:
            yield event.plain_result("【酒馆】你的玩家身份当前不可用。")
        except (TavernEngineError, ValueError) as exc:
            await self.database.write_audit(
                session["id"],
                sender_id,
                "turn.failed",
                "",
                {"error": str(exc)[:500]},
            )
            logger.warning("AI 酒馆本轮失败：%s", exc)
            yield event.plain_result(
                f"【酒馆】本轮裁定未完成，世界状态没有改变。\n{exc}"
            )
        except Exception as exc:
            await self.database.write_audit(
                session["id"],
                sender_id,
                "turn.failed",
                "",
                {"error_type": type(exc).__name__},
            )
            logger.exception("AI 酒馆处理群消息时发生异常")
            yield event.plain_result(
                "【酒馆】叙事引擎出现内部错误，世界状态没有改变。"
            )

    async def _handle_command(
        self,
        *,
        event: AstrMessageEvent,
        command: ParsedCommand,
        config: TavernConfig,
        group_id: str,
        platform_id: str,
        sender_id: str,
    ) -> str | None:
        is_admin = config.is_admin(sender_id)

        if not config.admin_ids:
            await self._write_security_audit(
                sender_id=sender_id,
                action=command.action,
                group_id=group_id,
                platform_id=platform_id,
                reason="admin_not_configured",
            )
            return (
                "【酒馆尚未初始化】请先在酒馆控制台填写至少一个"
                "真实管理员 ID；随后由该 ID 在目标群发送 /酒馆 开启，"
                "系统会自动识别并绑定平台实例 ID 与群 ID。"
            )

        session = await self.database.get_session_by_group(
            platform_id,
            group_id,
        )
        roles = (
            await self.database.permission_roles(session["id"], sender_id)
            if session
            else set()
        )
        is_host = is_admin or "host" in roles
        is_moderator = is_host or "moderator" in roles
        host_actions = {
            "start",
            "perform",
            "pause",
            "resume",
            "close",
            "finish",
            "abort",
            "save",
            "load",
            "rollback",
            "save_list",
            "review",
            "extend",
            "instances",
            "worlds",
        }
        moderator_actions = {
            "next",
            "move",
            "designate",
            "ban",
            "unban",
            "ban_list",
        }

        group_allowed = config.is_group_allowed(group_id)
        public_action = group_allowed and (
            command.action in PLAYER_ACTIONS
            or (
                command.action == "status"
                and config.public_status
            )
        )
        privileged_action = (
            command.action in host_actions and is_host
        ) or (
            command.action in moderator_actions and is_moderator
        )
        harmless_action = command.action in {"help", "unknown"}
        if not is_admin and not public_action and not privileged_action and not harmless_action:
            await self._write_security_audit(
                sender_id=sender_id,
                action=command.action,
                group_id=group_id,
                platform_id=platform_id,
                reason="sender_not_authorized",
            )
            if config.unauthorized_command_behavior == "deny":
                return "【酒馆】该命令只允许授权管理员使用。"
            return None

        auto_bound = False
        if not group_allowed:
            if is_admin and command.action == "start":
                try:
                    auto_bound = await self._allow_group(
                        group_id=group_id,
                        platform_id=platform_id,
                        actor_id=sender_id,
                        source="authorized_group_command",
                    )
                    group_allowed = True
                except ValueError as exc:
                    return f"【酒馆】无法识别当前群：{exc}"
                except Exception:
                    logger.exception("AI 酒馆自动绑定群失败")
                    return (
                        "【酒馆】自动绑定当前群失败，请查看 AstrBot 日志，"
                        "或在控制台手动填写平台实例 ID 与群 ID。"
                    )
            elif command.action not in {
                "help",
                "unknown",
                "worlds",
                "instances",
            }:
                return (
                    "【酒馆】本群尚未绑定。请由授权管理员发送 "
                    "/酒馆 开启，系统会自动识别平台实例 ID 与群 ID。"
                )

        if command.action in {"help", "unknown"}:
            help_text = HELP_TEXT.replace(
                "jg + 空格 + 行动内容",
                f"{config.trigger_prefix} + 空格 + 行动内容",
            )
            if command.action == "unknown":
                return (
                    f"【酒馆】未知命令：{command.raw_action}\n\n{help_text}"
                )
            return help_text

        try:
            if command.action == "worlds":
                worlds = await self.database.list_worlds()
                lines = [
                    (
                        f"· {item['name']}（{item['slug']}）"
                        f" · 推荐 {player_limits(item)['recommended_min']}"
                        f"—{player_limits(item)['recommended_max']} 人"
                        f" · 上限 {player_limits(item)['maximum']} 人"
                    )
                    for item in worlds
                ]
                return "【可用世界包】\n" + ("\n".join(lines) or "暂无")

            if command.action == "instances":
                page = parse_instance_list_page(
                    command.argument,
                    allow_bare_number=True,
                )
                if page is None:
                    return (
                        "【酒馆】格式：/酒馆 副本列表 [页码]\n"
                        "例如：/酒馆 副本列表 2"
                    )
                instances = await self.database.list_group_sessions(
                    platform_id,
                    group_id,
                )
                worlds = (
                    await self.database.list_worlds()
                    if not instances
                    else None
                )
                return format_instance_list(
                    instances,
                    worlds,
                    page=page,
                )

            if command.action == "start":
                list_page = parse_instance_list_page(command.argument)
                if not command.argument or list_page is not None:
                    instances = await self.database.list_group_sessions(
                        platform_id,
                        group_id,
                    )
                    worlds = (
                        await self.database.list_worlds()
                        if not instances
                        else None
                    )
                    prefix = (
                        "当前群已完成绑定，但尚未启动任何副本。"
                        f"\n平台实例 ID：{platform_id}"
                        f"\n群 ID：{group_id}\n"
                        if auto_bound
                        else ""
                    )
                    return prefix + format_instance_list(
                        instances,
                        worlds,
                        page=list_page or 1,
                    )

                selected = await self.database.get_session_by_group_ref(
                    platform_id,
                    group_id,
                    command.argument,
                )
                created = not selected or bool(
                    selected
                    and selected.get("state") == SESSION_FINISHED
                )
                if not selected:
                    session = await self.database.ensure_session(
                        platform_id,
                        group_id,
                        str(event.unified_msg_origin or ""),
                        command.argument,
                        sender_id,
                    )
                elif selected.get("state") == SESSION_FINISHED:
                    session = await self.database.ensure_session(
                        platform_id,
                        group_id,
                        str(event.unified_msg_origin or ""),
                        str(selected["world_id"]),
                        sender_id,
                        str(selected["instance_slug"]),
                        str(selected["instance_name"]),
                    )
                else:
                    session = selected
                if created:
                    created_world = await self.database.get_world(
                        session["world_id"]
                    )
                    world_rules = created_world.get("rules") or {}
                    world_time = (
                        world_rules.get("time_rules")
                        if isinstance(world_rules, Mapping)
                        else {}
                    )
                    merged_time_rules = normalize_time_rules(
                        {
                            **dict(config.time_rules),
                            **(
                                dict(world_time)
                                if isinstance(world_time, Mapping)
                                else {}
                            ),
                        }
                    )
                    await self.database.save_instance_time_rules(
                        session["id"],
                        merged_time_rules,
                        sender_id,
                    )
                session = await self.database.transition_session(
                    session["id"],
                    SESSION_PREPARING,
                    sender_id,
                )
                await self.database.grant_permission(
                    session["id"],
                    sender_id,
                    "host",
                    sender_id,
                )
                instance = await self.database.get_instance_config(
                    session["id"]
                )
                world = instance["world_snapshot"]
                limits = player_limits(world)
                roster = await self.database.list_roster(session["id"])
                summary = str(
                    session["world_state"].get("scene_summary")
                    or world.get("description")
                    or "尚无剧情回顾"
                )
                await self.broker.publish(
                    {
                        "type": "session",
                        "action": "prepare",
                        "session_id": session["id"],
                    }
                )
                return (
                    f"【酒馆已开启】{session['instance_name']}"
                    "\n当前阶段：准备中（故事尚未推进）"
                    f"\n副本标识：{session['instance_slug']}"
                    f"\n世界包：{session['world_name']}"
                    f"\n推荐人数：{limits['recommended_min']}"
                    f"—{limits['recommended_max']} 人"
                    f" · 最低 {limits['minimum_start']} 人"
                    f" · 强制上限 {limits['maximum']} 人"
                    f"\n平台实例 ID：{platform_id}"
                    f"\n群 ID：{group_id}"
                    + (
                        "\n已自动将当前群加入允许群列表。"
                        if auto_bound
                        else ""
                    )
                    + f"\n\n【故事回顾】{summary}"
                    + "\n\n"
                    + format_roster(roster)
                    + (
                        "\n\n玩家发送 /酒馆 加入，按提示私聊建卡，"
                        "完成后发送 /酒馆 准备。"
                        "\n主持人最后发送 /酒馆 开演；此时不会自动开演。"
                    )
                )

            if command.action == "status":
                if not session:
                    return "【酒馆状态】尚未为本群创建会话。"
                location = session["world_state"].get("location", "未记录")
                turn = await self.database.get_turn_status(session["id"])
                roster = await self.database.list_roster(session["id"])
                vote = await self.database.active_vote(session["id"])
                choice = await self.database.active_choice_set(session["id"])
                rules = await self.database.get_session_rule_state(
                    session["id"]
                )
                progress = rules.get("progress") or {}
                total_milestones = int(
                    progress.get("total_milestones") or 0
                )
                completed_milestones = int(
                    progress.get("completed_milestones") or 0
                )
                progress_text = (
                    f"{completed_milestones}/{total_milestones}"
                    f"（{round(completed_milestones * 100 / total_milestones)}%）"
                    if total_milestones > 0
                    else "未设置正式里程碑"
                )
                current = (
                    turn["current_name"]
                    or turn["current_user_id"]
                    or "等待玩家加入"
                )
                workflow = (
                    f"集体投票第 {vote['stage']} 轮"
                    if vote
                    else (
                        "等待 A/B/C/D 选择"
                        if choice
                        else "无活动流程"
                    )
                )
                return (
                    "【酒馆状态】\n"
                    f"状态：{session['state']}\n"
                    f"副本：{session['instance_name']}"
                    f"（{session['instance_slug']}）\n"
                    f"世界：{session['world_name']}（{session['world_slug']}）\n"
                    f"剧情回合：{session['turn_no']}\n"
                    f"多人轮次：第 {turn['round_no']} 轮\n"
                    f"当前行动者：{current}\n"
                    f"流程：{workflow}\n"
                    f"角色数：{len(roster)}\n"
                    f"章节：{progress.get('chapter') or '未记录'}\n"
                    f"当前目标："
                    f"{progress.get('current_objective') or '未记录'}\n"
                    f"里程碑：{progress_text}\n"
                    f"行动格式：{config.trigger_prefix} A\n"
                    f"地点：{location}"
                )

            if not session:
                return "【酒馆】本群尚未创建会话，请先使用 /酒馆 开启。"

            if command.action == "join":
                result = await self.database.reserve_participant(
                    session["id"],
                    sender_id,
                    str(event.get_sender_name() or sender_id),
                )
                if result.get("binding_code"):
                    return (
                        "【席位已预留】\n"
                        f"建卡码：{result['binding_code']}\n"
                        f"有效期至：{result.get('binding_expires_at')}\n\n"
                        "请私聊 Bot 发送：\n"
                        f"/酒馆 建卡 {result['binding_code']}\n\n"
                        "QQ 官方机器人无法保证首次主动私聊，"
                        "因此请由玩家主动打开私聊。"
                    )
                return (
                    "【你已加入当前副本】\n"
                    f"角色卡：{result.get('card_status')}"
                    f" · 状态：{result.get('participation_status')}\n"
                    "如已通过审核，请发送 /酒馆 准备。"
                )
            if command.action == "card":
                participant = next(
                    (
                        item
                        for item in await self.database.list_roster(
                            session["id"]
                        )
                        if item["group_user_id"] == sender_id
                    ),
                    None,
                )
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                code = str(participant.get("binding_code") or "")
                if code:
                    return (
                        f"【建卡码】{code}\n"
                        f"请私聊 Bot 发送：/酒馆 建卡 {code}"
                    )
                if participant["card_status"] == "approved":
                    return "【角色卡】已经审核通过，请发送 /酒馆 准备。"
                return (
                    "【角色卡】建卡流程已经绑定或等待审核；"
                    "请回到与 Bot 的私聊继续。"
                )
            if command.action == "character":
                participant = await self.database.get_participant(
                    session["id"],
                    user_id=sender_id,
                )
                return (
                    "【我的角色】\n"
                    f"角色：{participant.get('character_name') or '尚未命名'}\n"
                    f"代号：{participant.get('character_code') or '尚未设置'}\n"
                    f"角色卡：{participant.get('card_status')}\n"
                    f"准备：{'是' if participant.get('ready') else '否'}\n"
                    f"入场：{participant.get('participation_status')}"
                )
            if command.action == "ready":
                participant = await self.database.set_participant_ready(
                    session["id"],
                    sender_id,
                    True,
                )
                preflight = await self.database.opening_preflight(
                    session["id"]
                )
                waiting = len(preflight["blockers"])
                suffix = (
                    "\n【全员准备完成】主持人现在可以发送 /酒馆 开演"
                    if preflight["ok"]
                    else f"\n当前仍有 {waiting} 项准备阻塞。"
                )
                return (
                    f"【{participant.get('character_name') or participant.get('display_name')} 已准备】"
                    + suffix
                )
            if command.action == "roster":
                return format_roster(
                    await self.database.list_roster(session["id"])
                )
            if command.action == "review":
                roster = await self.database.list_roster(session["id"])
                pending = _pending_review_cards(roster)
                argument = str(command.argument or "").strip()
                list_page = parse_instance_list_page(argument)
                if not argument or list_page is not None:
                    return format_pending_reviews(
                        pending,
                        page=list_page or 1,
                    )

                parts = argument.split(maxsplit=2)
                if parts[0] in {"查看", "详情", "view"}:
                    if len(parts) < 2:
                        return (
                            "【酒馆】格式：/酒馆 审核 查看 "
                            "<序号或审核号>"
                        )
                    target = _resolve_pending_review(pending, parts[1])
                    instance = await self.database.get_instance_config(
                        session["id"]
                    )
                    return format_review_card(
                        target,
                        instance["character_card_template"],
                    )

                target = _resolve_pending_review(pending, parts[0])
                if len(parts) == 1 or parts[1] in {
                    "查看",
                    "详情",
                    "view",
                }:
                    instance = await self.database.get_instance_config(
                        session["id"]
                    )
                    return format_review_card(
                        target,
                        instance["character_card_template"],
                    )

                decisions = {
                    "通过": True,
                    "approve": True,
                    "驳回": False,
                    "拒绝": False,
                    "reject": False,
                }
                if parts[1] not in decisions:
                    return (
                        "【酒馆】格式：\n"
                        "/酒馆 审核\n"
                        "/酒馆 审核 <序号或审核号>\n"
                        "/酒馆 审核 <序号或审核号> "
                        "<通过|驳回> [备注]"
                    )
                approved = decisions[parts[1]]
                note = parts[2] if len(parts) > 2 else ""
                participant = await self.database.review_character_card(
                    session["id"],
                    str(target["id"]),
                    approved,
                    sender_id,
                    note,
                )
                remaining = len(pending) - 1
                return (
                    f"【角色卡审核】{participant.get('character_name')}"
                    f" · {'已通过' if approved else '已驳回'}"
                    + (f"\n备注：{note}" if note else "")
                    + f"\n剩余待审核：{max(0, remaining)} 人"
                    + (
                        "\n发送 /酒馆 审核，查看剩余名单。"
                        if remaining > 0
                        else ""
                    )
                )
            if command.action == "perform":
                result = await self.database.activate_story(
                    session["id"],
                    sender_id,
                    resume=False,
                )
                if not result["started"]:
                    return (
                        "【暂时无法开演】\n· "
                        + "\n· ".join(result["blockers"])
                    )
                current = result["current_participant"]
                return (
                    f"【故事正式开演】{session['instance_name']}\n"
                    f"出场角色："
                    + "、".join(
                        item.get("character_name") or item.get("display_name")
                        for item in result["participants"]
                    )
                    + f"\n当前行动者："
                    f"{current.get('character_name') or current.get('display_name')}"
                    + (
                        f"\n\n{result['opening']}"
                        if result.get("opening")
                        else ""
                    )
                    + "\n\n"
                    + format_choices(
                        current.get("character_name")
                        or current.get("display_name"),
                        result["choice_set"]["choices"],
                    )
                )
            if command.action == "choose":
                key, flavor = parse_choice_input(command.argument)
                reply = await self.engine.process_choice(
                    event=event,
                    session_id=session["id"],
                    sender_id=sender_id,
                    sender_name=str(event.get_sender_name() or sender_id),
                    choice_key=key,
                    flavor_text=flavor,
                )
                return reply.text
            if command.action in {"inspiration", "inspiration_reroll"}:
                if (
                    command.action == "inspiration"
                    and not command.argument
                ):
                    status = await self.database.inspiration_status(
                        session["id"],
                        sender_id,
                    )
                    return (
                        f"【灵感】{status['character_name']}："
                        f"{status['balance']}/{status['maximum']} 点\n"
                        "用法：/酒馆 灵感 A 优势，或 "
                        "/酒馆 灵感重投 A"
                    )
                argument = command.argument.strip()
                parts = argument.split(maxsplit=2)
                if not parts:
                    return "【酒馆】请提供检定选项，例如 /酒馆 灵感 A 优势"
                key = parts[0]
                mode = (
                    "reroll"
                    if command.action == "inspiration_reroll"
                    else "advantage"
                )
                flavor = ""
                if len(parts) >= 2:
                    mode_text = parts[1].lower()
                    if mode_text in {"重投", "reroll"}:
                        mode = "reroll"
                        flavor = parts[2] if len(parts) >= 3 else ""
                    elif mode_text in {"优势", "advantage"}:
                        mode = "advantage"
                        flavor = parts[2] if len(parts) >= 3 else ""
                    else:
                        flavor = " ".join(parts[1:])
                reply = await self.engine.process_choice(
                    event=event,
                    session_id=session["id"],
                    sender_id=sender_id,
                    sender_name=str(event.get_sender_name() or sender_id),
                    choice_key=key,
                    flavor_text=flavor,
                    inspiration_mode=mode,
                )
                return reply.text
            if command.action == "reroll":
                result = await self.engine.reroll_choices(
                    event=event,
                    session_id=session["id"],
                    sender_id=sender_id,
                )
                participant = result.get("participant") or (
                    await self.database.get_participant(
                        session["id"],
                        user_id=sender_id,
                    )
                )
                return format_choices(
                    participant.get("character_name")
                    or participant.get("display_name"),
                    result["choices"],
                    rerolls_left=max(0, 1 - int(result["reroll_count"])),
                )
            if command.action == "vote":
                key, _ = parse_choice_input(command.argument)
                result = await self.database.cast_vote(
                    session["id"],
                    sender_id,
                    key,
                )
                tally = result["tally"]
                counts = "、".join(
                    f"{name}:{count}"
                    for name, count in tally["counts"].items()
                )
                if result.get("runoff"):
                    return (
                        f"【投票已记录】{counts}\n"
                        "第一轮无人过半，已进入前两项决选。\n"
                        + format_vote(result["vote"])
                    )
                if result["resolved"]:
                    vote = result["vote"]
                    if vote["status"] == "passed":
                        return (
                            f"【表决通过】多数选择 {vote['winner_key']}。"
                            "\n当前玩家的个人行动机会没有被消耗。"
                        )
                    return (
                        "【表决未通过】未形成有效多数，队伍维持现状。"
                        "\n当前玩家重新获得一组个人选项。"
                    )
                return (
                    f"【投票已记录】{counts}\n"
                    f"已投 {tally['cast_count']}/{tally['eligible_count']}，"
                    "截止前可以改票。"
                )
            if command.action == "away":
                participant = await self.database.set_participant_away(
                    session["id"],
                    sender_id,
                )
                return (
                    f"【已暂离】{participant.get('character_name') or participant.get('display_name')}"
                    "\n席位仍为你保留；返回时发送 /酒馆 返回队列。"
                )
            if command.action == "return_queue":
                participant = await self.database.return_to_queue(
                    session["id"],
                    sender_id,
                )
                return (
                    f"【返回申请已确认】将在第 "
                    f"{participant.get('effective_round')} 轮队尾生效。"
                )
            if command.action == "return_request":
                result = await self.database.request_return(
                    session["id"],
                    sender_id,
                )
                vote = await self.database.active_vote(session["id"])
                return (
                    f"【返场申请】{result['character_name']}\n"
                    f"剧情条件：{result['objective']}\n\n"
                    + (format_vote(vote) if vote else "")
                )
            if command.action == "delegate":
                parts = command.argument.split(maxsplit=1)
                if not parts:
                    return (
                        "【酒馆】格式：/酒馆 授权代控 <真实用户ID> "
                        "[时长，例如 2小时]"
                    )
                duration = (
                    parse_duration(parts[1])
                    if len(parts) > 1
                    else None
                )
                grant = await self.database.grant_delegation(
                    session["id"],
                    sender_id,
                    parts[0],
                    sender_id,
                    duration_seconds=duration,
                )
                return (
                    f"【代控已授权】用户 {grant['delegate_user_id']}\n"
                    f"到期：{grant['expires_at'] or '不限时'}\n"
                    "角色本人再次行动、主动撤销、退场或封禁时立即失效。"
                )
            if command.action == "delegate_revoke":
                count = await self.database.revoke_delegation(
                    session["id"],
                    sender_id,
                    sender_id,
                )
                return f"【代控已撤销】共撤销 {count} 条有效授权。"
            if command.action == "leave":
                if command.argument:
                    if not is_moderator:
                        raise PermissionError("只有主持人或秩序管理员能让他人退场")
                    result = await self.database.retire_participant(
                        session["id"],
                        command.argument,
                        sender_id,
                        forced=True,
                        reason="moderator_exit",
                    )
                else:
                    result = await self.database.retire_self(
                        session["id"],
                        sender_id,
                    )
                return (
                    "【角色已正式退场】席位已经释放，角色历史仍被归档。\n"
                    + result["narrative"]
                )
            if command.action == "order":
                if session["state"] == SESSION_PREPARING:
                    return (
                        "【准备阶段阵容】尚未建立正式回合顺序。\n"
                        + format_roster(
                            await self.database.list_roster(session["id"])
                        )
                    )
                vote = await self.database.active_vote(session["id"])
                if vote:
                    return (
                        format_turn_status(
                            await self.database.get_turn_status(session["id"])
                        )
                        + "\n\n"
                        + format_vote(vote)
                    )
                choice = await self.database.active_choice_set(session["id"])
                text = format_turn_status(
                    await self.database.get_turn_status(session["id"])
                )
                if choice and choice.get("participant"):
                    text += "\n\n" + format_choices(
                        choice["participant"].get("character_name")
                        or choice["participant"].get("display_name"),
                        choice["choices"],
                        rerolls_left=max(
                            0,
                            1 - int(choice["reroll_count"]),
                        ),
                    )
                return text
            if command.action == "skip":
                controlled_user_id = ""
                if command.argument:
                    target = await self.database.get_participant(
                        session["id"],
                        participant_ref=command.argument,
                    )
                    controlled_user_id = target["group_user_id"]
                    if (
                        controlled_user_id != sender_id
                        and not is_moderator
                    ):
                        control = (
                            await self.database.authorize_participant_control(
                                session["id"],
                                target["id"],
                                sender_id,
                                "skip",
                            )
                        )
                        if not control["authorized"]:
                            raise PermissionError("没有跳过该角色的权限")
                turn = await self.engine.skip_player(
                    session_id=session["id"],
                    sender_id=sender_id,
                    force=bool(
                        controlled_user_id
                        and controlled_user_id != sender_id
                        and is_moderator
                    ),
                    controlled_user_id=controlled_user_id,
                )
                if turn.get("current_user_id"):
                    turn = await self.database.designate_turn(
                        session["id"],
                        turn["current_user_id"],
                        sender_id,
                    )
                return "【本次行动已跳过】\n" + format_turn_status(turn)
            if command.action == "next":
                turn = await self.engine.skip_player(
                    session_id=session["id"],
                    sender_id=sender_id,
                    force=True,
                )
                if turn.get("current_user_id"):
                    turn = await self.database.designate_turn(
                        session["id"],
                        turn["current_user_id"],
                        sender_id,
                    )
                return "【管理员已推进至下一位】\n" + format_turn_status(turn)
            if command.action == "move":
                target_text, separator, position_text = (
                    command.argument.rpartition(" ")
                )
                if not separator:
                    return "【酒馆】格式：/酒馆 移至 <角色名或代号> <顺序>"
                target = await self.database.get_participant(
                    session["id"],
                    participant_ref=target_text,
                )
                position = int(position_text)
                turn = await self.database.get_turn_status(session["id"])
                order = [
                    str(item["user_id"]) for item in turn["order"]
                ]
                user_id = target["group_user_id"]
                if user_id not in order:
                    raise ValueError("该角色当前不在回合队列")
                order.remove(user_id)
                order.insert(
                    max(0, min(len(order), position - 1)),
                    user_id,
                )
                turn = await self.database.set_turn_order(
                    session["id"],
                    order,
                    sender_id,
                )
                return "【未来行动顺序已调整】\n" + format_turn_status(turn)
            if command.action == "designate":
                target = await self.database.get_participant(
                    session["id"],
                    participant_ref=command.argument,
                )
                turn = await self.database.designate_turn(
                    session["id"],
                    target["group_user_id"],
                    sender_id,
                )
                return "【当前行动者已指定】\n" + format_turn_status(turn)
            if command.action == "ban":
                parts = command.argument.split()
                if not parts:
                    return (
                        "【酒馆】格式：/酒馆 封禁 <角色> "
                        "[时长] [原因]"
                    )
                ref = parts.pop(0)
                scope = "instance"
                scope_map = {
                    "副本": "instance",
                    "群": "group",
                    "全局": "global",
                }
                if parts and parts[0] in scope_map:
                    scope = scope_map[parts.pop(0)]
                    if scope == "global" and not is_admin:
                        raise PermissionError("全局封禁只允许插件管理员")
                duration = None
                if parts:
                    try:
                        duration = parse_duration(parts[0])
                        parts.pop(0)
                    except ValueError:
                        duration = None
                result = await self.database.create_ban(
                    session["id"],
                    ref,
                    sender_id,
                    scope=scope,
                    duration_seconds=duration,
                    reason=" ".join(parts),
                )
                return (
                    "【封禁已生效】已原子移出队列、撤销授权并释放席位。\n"
                    + result["narrative"]
                    + f"\n范围：{result['ban']['scope']}"
                    f" · 到期：{result['ban']['expires_at'] or '永久'}"
                )
            if command.action == "unban":
                if not command.argument:
                    return "【酒馆】格式：/酒馆 解封 <角色名或代号>"
                count = await self.database.revoke_ban(
                    session["id"],
                    command.argument,
                    sender_id,
                )
                return f"【解封完成】撤销 {count} 条有效封禁记录。"
            if command.action == "ban_list":
                bans = await self.database.list_bans(session["id"])
                if not bans:
                    return "【黑名单】当前没有有效封禁。"
                return "【黑名单】\n" + "\n".join(
                    (
                        f"· {item['user_id']} · {item['scope']}"
                        f" · {item['reason'] or '未注明'}"
                        f" · 至 {item['expires_at'] or '永久'}"
                    )
                    for item in bans
                )
            if command.action == "extend":
                target, separator, duration_text = (
                    command.argument.rpartition(" ")
                )
                if not separator:
                    return (
                        "【酒馆】格式：/酒馆 延时 "
                        "<角色名、代号或准备阶段> <30分钟>"
                    )
                timer = await self.database.extend_active_timer(
                    session["id"],
                    target,
                    parse_duration(duration_text),
                    sender_id,
                )
                return (
                    "【计时已延长】"
                    f"{timer.get('character_name') or timer['timer_type']}"
                    f" · 新截止：{timer.get('deadline_at') or '暂停中'}"
                )

            if command.action == "pause":
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
                session = await self.database.transition_session(
                    session["id"],
                    SESSION_PAUSED,
                    sender_id,
                )
                return (
                    "【酒馆已暂停】现场、未完成选项、投票和剩余时间"
                    "均已持久化；暂停期间默认不计时。"
                )
            if command.action == "safety_pause":
                if session["state"] == SESSION_PAUSED:
                    return "【安全暂停】故事与全部计时已经处于冻结状态。"
                if not is_host:
                    participant = await self.database.get_participant(
                        session["id"],
                        user_id=sender_id,
                    )
                    if participant.get("participation_status") != "active":
                        raise PermissionError("只有当前出场玩家可以发起安全暂停")
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
                await self.database.transition_session(
                    session["id"],
                    SESSION_PAUSED,
                    sender_id,
                )
                await self.database.write_audit(
                    session["id"],
                    sender_id,
                    "session.safety_pause",
                    session["id"],
                    {"reason_disclosed": False},
                )
                return (
                    "【安全暂停】故事、行动、投票与全部计时已立即冻结。"
                    "\n无需在群内说明原因；由主持人与参与者确认边界后再恢复。"
                )
            if command.action == "resume":
                if session["state"] in {
                    SESSION_PAUSED,
                    SESSION_MAINTENANCE,
                }:
                    session = await self.database.transition_session(
                        session["id"],
                        SESSION_PREPARING,
                        sender_id,
                    )
                    return (
                        "【恢复准备大厅】剧情尚未继续，计时仍暂停。\n"
                        f"上次位置：{session['world_state'].get('location', '未记录')}\n"
                        f"剧情回合：{session['turn_no']}\n\n"
                        + format_roster(
                            await self.database.list_roster(session["id"])
                        )
                        + "\n\n全员重新发送 /酒馆 准备；完成后主持人再次发送 /酒馆 继续。"
                    )
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有暂停后进入准备大厅的副本可以继续"
                    )
                result = await self.database.activate_story(
                    session["id"],
                    sender_id,
                    resume=True,
                )
                if not result["started"]:
                    return (
                        "【暂时无法继续】\n· "
                        + "\n· ".join(result["blockers"])
                    )
                await self.database.resume_session_timers(
                    session["id"],
                    sender_id,
                )
                current = result["current_participant"]
                workflow_text = (
                    format_vote(result["vote"])
                    if result.get("vote")
                    else format_choices(
                        current.get("character_name")
                        or current.get("display_name"),
                        result["choice_set"]["choices"],
                        rerolls_left=max(
                            0,
                            1
                            - int(
                                result["choice_set"].get(
                                    "reroll_count",
                                    0,
                                )
                            ),
                        ),
                    )
                )
                return (
                    f"【故事继续】{session['instance_name']}\n"
                    f"当前行动者："
                    f"{current.get('character_name') or current.get('display_name')}"
                    "\n\n"
                    + workflow_text
                )
            if command.action == "close":
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
                session = await self.database.transition_session(
                    session["id"],
                    SESSION_CLOSED,
                    sender_id,
                )
                return "【酒馆已关闭】关闭期间不处理消息、不调用模型。"
            if command.action == "finish":
                if command.argument not in {"确认", "CONFIRM", "confirm"}:
                    return (
                        "【确认完结】完结后只允许查看与导出。"
                        "请发送 /酒馆 完结 确认。"
                    )
                await self.database.finalize_session(
                    session["id"],
                    sender_id,
                    termination_type="completed",
                    reason="正常完结",
                )
                return (
                    "【故事已完结】已创建最终保护存档并永久归档。"
                    "\n角色、NPC、长期记忆、剧情账本、时间线和存档均只读保留；"
                    "如需续作，请从最终存档克隆新副本。"
                )
            if command.action == "abort":
                parts = command.argument.split(maxsplit=1)
                confirmed = bool(
                    parts
                    and parts[0] in {"确认", "CONFIRM", "confirm"}
                )
                reason = parts[1].strip() if len(parts) > 1 else ""
                if not confirmed or not reason:
                    return (
                        "【确认强制终止】此操作会创建最终保护存档并永久只读归档。"
                        "\n请发送：/酒馆 强制终止 确认 <原因>"
                    )
                await self.database.finalize_session(
                    session["id"],
                    sender_id,
                    termination_type="aborted",
                    reason=reason,
                )
                return (
                    "【故事已强制终止】已保存最终保护存档并永久归档。"
                    f"\n终止原因：{reason}"
                )
            if command.action == "maintenance":
                session = await self.database.transition_session(
                    session["id"],
                    SESSION_MAINTENANCE,
                    sender_id,
                )
                return "【维护模式】仅保留管理操作，剧情不会推进。"
            if command.action == "save_list":
                snapshots = await self.database.list_snapshots(session["id"])
                if not snapshots:
                    return "【存档列表】当前没有存档。"
                return "【存档列表】\n" + "\n".join(
                    (
                        f"· {item['name']} · 第 {item['turn_no']} 回合"
                        f" · {item['kind']} · {item['created_at']}"
                    )
                    for item in snapshots
                )
            if command.action == "recap":
                events = await self.database.recent_events(
                    session["id"],
                    12,
                )
                narrative = [
                    item
                    for item in events
                    if item["role"] in {"narrator", "system"}
                ][-6:]
                return (
                    f"【故事回顾】{session['instance_name']}\n"
                    f"地点：{session['world_state'].get('location', '未记录')}\n"
                    f"摘要：{session['world_state'].get('scene_summary', '暂无')}\n\n"
                    + (
                        "\n".join(f"· {item['content']}" for item in narrative)
                        if narrative
                        else "暂无正式剧情记录。"
                    )
                )
            if command.action == "save":
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError(
                        "只有正式运行中的故事可以创建新剧情存档"
                    )
                if not command.argument:
                    return "【酒馆】请提供存档名：/酒馆 存档 <名称>"
                snapshot = await self.database.create_snapshot(
                    session["id"],
                    command.argument,
                    sender_id,
                    replace=False,
                )
                return (
                    f"【存档完成】{snapshot['name']}"
                    f"\n记录于第 {snapshot['turn_no']} 回合。"
                )
            if command.action == "load":
                if not command.argument:
                    return "【酒馆】请提供存档名：/酒馆 读档 <名称>"
                restored = await self.database.restore_snapshot(
                    session["id"],
                    command.argument,
                    sender_id,
                )
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
                return (
                    f"【读档完成】已恢复至第 {restored['turn_no']} 回合。"
                    "\n会话已暂停；发送 /酒馆 继续 进入恢复准备大厅。"
                )
            if command.action == "rollback":
                restored = await self.database.restore_latest_auto(
                    session["id"],
                    sender_id,
                )
                await self.database.pause_session_timers(
                    session["id"],
                    sender_id,
                )
                return (
                    f"【回滚完成】已恢复至第 {restored['turn_no']} 回合。"
                    "\n会话已暂停；发送 /酒馆 继续 进入恢复准备大厅。"
                )
        except (
            DatabaseNotFoundError,
            InvalidTransitionError,
            PermissionError,
            ValueError,
        ) as exc:
            return f"【酒馆】{exc}"
        except Exception:
            logger.exception("AI 酒馆管理命令失败")
            return "【酒馆】管理操作失败，请查看 AstrBot 日志。"
        return HELP_TEXT

    async def terminate(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
            try:
                await self._timer_task
            except asyncio.CancelledError:
                pass
        self._timer_task = None
        await self.broker.close()
        logger.info("AI 酒馆已停止。")
