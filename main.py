from __future__ import annotations

import asyncio
import html
import re
import time
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
from .tavern.bootstrap import build_runtime
from .tavern.help_topics import contextual_help
from .tavern.recaps import build_recap
from .tavern.operations import transport_event_id
from .tavern.lifecycle import (
    attribute_maps,
    card_stat_allocation,
    find_profession_preset,
    format_choices,
    normalize_time_rules,
    parse_choice_input,
    parse_duration,
    player_limits,
    resolve_profession_stats,
    uses_profession_preset_stats,
)
from .tavern.security import (
    ParsedCommand,
    parse_story_trigger,
    parse_tavern_command,
    validate_platform_id,
)


INSTANCE_LIST_PAGE_SIZE = 5
INSTANCE_INTRO_MAX_CHARS = 220
REVIEW_LIST_PAGE_SIZE = 5
# 计时轮询与通知频控
TIMER_POLL_INTERVAL_SECONDS = 15
# 同一个计时器在该窗口内只允许推送一次，防止重复行造成刷屏。
TIMER_NOTICE_DEDUP_SECONDS = 25
# 相邻两条主动通知之间的最小间隔，规避 QQ 官方主动消息频控。
TIMER_NOTICE_MIN_GAP_SECONDS = 2.0
PRIVATE_CARD_ACTIONS = frozenset(
    {
        "card",
        "card_fill",
        "card_previous",
        "card_modify",
        "card_current",
        "card_stats_reset",
        "card_timer_notice",
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
【AI 酒馆 v0.11.2｜多人叙事、真人 DM 与世界协议 v5】
主持：/酒馆 开启 <副本> → /酒馆 开演
恢复：/酒馆 暂停 → /酒馆 恢复 → 全员准备 → /酒馆 继续
玩家：/酒馆 加入｜角色｜准备｜阵容｜暂离｜返回队列｜退出
建卡：私聊 /酒馆 建卡 <验证码>｜当前步骤｜上一步｜修改 <字段>｜重填数值
回合：jg A｜/酒馆 选择 A｜/酒馆 重整选项
裁定：/酒馆 灵感｜/酒馆 灵感 A 优势｜/酒馆 灵感重投 A
集体：/酒馆 投票 A（不消耗个人行动）
记录：/酒馆 回顾｜存档列表｜存档 <名称>｜删档 <名称>｜读档｜回滚
管理：审核｜强制全员准备｜强制下一位｜倒计时｜用量｜限额｜移至｜指定
主持：/酒馆 主持 开启｜指引｜推进｜直述｜交棒｜自动｜状态｜接管
帮助：/酒馆 帮助 建卡｜回合｜投票｜回顾｜管理
安全：任一出场玩家可发送 /酒馆 安全暂停
结束：/酒馆 关闭｜/酒馆 完结 确认｜/酒馆 强制终止 确认 <原因>

普通群聊完全旁路；剧情只接受 A/B/C/D，不接受自由改写世界。"""


from .tavern.presentation import (
    parse_instance_list_page,
    _compact_instance_intro,
    format_turn_status,
    format_instance_list,
    _instance_list_footer,
    format_roster,
    format_vote,
    format_recovered_timer,
    world_preset_brief,
    _profession_preset_line,
    _format_profession_step_prompt,
    format_card_prompt,
    format_card_preview,
    _review_reference,
    _pending_review_cards,
    _resolve_pending_review,
    format_pending_reviews,
    format_review_card,
    _format_remaining_time,
    _story_reply_parts,
)


class TavernPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.plugin_config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._config_lock = asyncio.Lock()
        runtime = build_runtime(
            context=context,
            plugin_config=self.plugin_config,
            data_dir=self.data_dir,
            config_provider=self.runtime_config,
            logger=logger,
            allow_group=self._allow_group,
            config_lock=self._config_lock,
        )
        self.database = runtime.database
        self.broker = runtime.broker
        self.engine = runtime.engine
        self.web_console = runtime.web_console
        self.hooks = runtime.hooks
        self.extensions = runtime.extensions
        self.public_api = runtime.public_api
        self._timer_task: asyncio.Task[None] | None = None
        # 计时通知的去重与频控状态：
        # QQ 官方接口对主动消息有频控（40034100），
        # 一旦同一轮吐出多条提醒就会被整段拒绝并反复刷屏重试。
        self._timer_notice_last_sent: dict[str, float] = {}
        self._timer_notice_last_at: float = 0.0
        # 富卡片运行时状态：
        # 1) _rich_titles：按会话缓存「当前副本 · 世界」名，供卡片标题自动取用；
        # 2) _rich_fail_counts / _rich_broken：探测某档卡片是否真的可用。
        #    QQ 侧「无模板权限（304004）」不会抛异常，适配器只返回空
        #    message_id，若不检测就会静默发出空消息（表现为“指令没反应”）。
        self._rich_titles: dict[str, str] = {}
        self._rich_fail_counts: dict[str, int] = {}
        self._rich_broken: set[str] = set()

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

    async def _send_text(self, origin: str, text: str) -> bool:
        """Send one message without yielding from the active event handler."""

        target = str(origin or "").strip()
        content = str(text or "").strip()
        sender = getattr(self.context, "send_message", None)
        if not target or not content or not callable(sender):
            return False
        try:
            from astrbot.api.event import MessageChain

            sent = await sender(target, MessageChain().message(content))
            return sent is not False
        except Exception:
            logger.exception("AI 酒馆主动消息发送失败：origin=%s", target)
            return False

    async def _send_event_text(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> bool:
        text = str(text or "").strip()
        if not text:
            return False
        if self._rich_enabled() and await self._rich_try_send(event, text, None):
            return True
        return await self._send_text(self._rich_origin(event), text)

    async def _send_event_parts(
        self,
        event: AstrMessageEvent,
        parts: Sequence[str],
    ) -> list[str]:
        """Send ordered parts and return only parts that could not be sent."""

        unsent: list[str] = []
        for part in parts:
            text = str(part or "").strip()
            if text and not await self._send_event_text(event, text):
                unsent.append(text)
        return unsent

    # ── 富卡片（第二档真·结构化卡片）发送层 ──────────────────────────
    # 仅当「配置开启」且「当前事件对象支持 reply_markdown_aj / reply_ark」
    # （由 astrbot_plugin_qq_restapi 等 REST API 适配器提供）时才发送
    # 结构化卡片；否则自动退回第一档 emoji 纯文本，绝不向调用方抛出异常。
    def _rich_enabled(self, config: Any = None) -> bool:
        cfg = config or self.runtime_config()
        return bool(getattr(cfg, "rich_cards_enabled", True))

    def _rich_mode(self, config: Any = None) -> str:
        cfg = config or self.runtime_config()
        return str(getattr(cfg, "rich_card_mode", "markdown")).strip().lower()

    @staticmethod
    def _rich_origin(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _remember_rich_title(self, event: Any, session: Any) -> None:
        """缓存当前会话的副本 / 世界名，供富卡片标题自动取用。"""

        origin = self._rich_origin(event)
        if not origin or not session:
            return
        try:
            instance = str(session["instance_name"] or "").strip()
            world = str(session["world_name"] or "").strip()
        except Exception:
            return
        title = instance or world
        if not title:
            return
        if instance and world and world != instance:
            title = f"{instance} · {world}"
        self._rich_titles[origin] = f"🍺 {title}"[:60]

    def _rich_title(self, config: Any = None, event: Any = None) -> str:
        """卡片标题：配置显式填写优先，否则自动取当前正在游玩的世界。"""

        cfg = config or self.runtime_config()
        configured = str(getattr(cfg, "rich_card_title", "") or "").strip()
        if configured:
            return configured
        if event is not None:
            auto = self._rich_titles.get(self._rich_origin(event))
            if auto:
                return auto
        return "🍺 AI 酒馆"

    def _rich_note_failure(self, kind: str, reason: Any) -> None:
        """连续失败 2 次即熔断该档卡片，避免持续空发与日志刷屏。"""

        count = self._rich_fail_counts.get(kind, 0) + 1
        self._rich_fail_counts[kind] = count
        logger.warning(
            "AI 酒馆 %s 卡片发送失败（第 %d 次），尝试降级：%s",
            kind,
            count,
            reason,
        )
        if count >= 2 and kind not in self._rich_broken:
            self._rich_broken.add(kind)
            logger.warning(
                "AI 酒馆已停用 %s 卡片并回退纯文本。"
                "如需启用：markdown 需在 qq_restapi 适配器填写 "
                "markdown_aj_template_id / markdown_aj_keys；"
                "ark 需要 QQ 侧开通对应模板权限（被动消息 Ark 需申请）。",
                kind,
            )

    def _rich_note_success(self, kind: str) -> None:
        if self._rich_fail_counts.get(kind):
            self._rich_fail_counts[kind] = 0

    def _rich_usable(self, kind: str, mode: str) -> bool:
        return mode in (kind, "auto") and kind not in self._rich_broken

    async def _rich_try_send(self, event: Any, text: str, config: Any) -> bool:
        """按 markdown → ark 顺序尝试发卡片；成功返回 True。"""

        mode = self._rich_mode(config)
        if self._rich_usable("markdown", mode):
            fn = getattr(event, "reply_markdown_aj", None)
            if callable(fn):
                try:
                    # 适配器返回 message_id；为空说明平台侧拒绝了这条消息。
                    if await fn(text):
                        self._rich_note_success("markdown")
                        return True
                    self._rich_note_failure(
                        "markdown", "适配器未返回消息 ID（平台可能已拒绝）"
                    )
                except Exception as exc:
                    self._rich_note_failure("markdown", exc)
        if self._rich_usable("ark", mode):
            fn = getattr(event, "reply_ark", None)
            if callable(fn):
                try:
                    if await fn(
                        template_id=24,
                        kv_data=self._rich_ark_kv(
                            text, self._rich_title(config, event)
                        ),
                    ):
                        self._rich_note_success("ark")
                        return True
                    self._rich_note_failure(
                        "ark", "适配器未返回消息 ID（平台可能已拒绝）"
                    )
                except Exception as exc:
                    self._rich_note_failure("ark", exc)
        return False

    @staticmethod
    def _rich_ark_kv(text: str, title: str) -> list:
        # 模板 24（文本 + 缩略图）通用 kv；文本压成单行并截断，避免单字段超长。
        desc = str(text or "").strip().replace("\n", " ")[:300]
        return [
            {"key": "#TITLE#", "value": title},
            {"key": "#PROMPT#", "value": title},
            {"key": "#DESC#", "value": desc},
        ]

    async def _rich_result(self, event: Any, text: Any, config: Any = None):
        """返回可 yield 的消息对象（富卡片或 plain_result）；空文本返回 None。"""

        text = str(text or "").strip()
        if not text:
            return None
        if self._rich_enabled(config) and await self._rich_try_send(
            event, text, config
        ):
            # 已由适配器直接发出，返回 None 交由管线跳过（安全）。
            return None
        return event.plain_result(text)

    async def _notify_group_card_created(
        self,
        result: Mapping[str, Any],
    ) -> None:
        """Best-effort group notification after a private card is committed."""

        try:
            session_id = str(result.get("session_id") or "")
            if not session_id:
                return
            session = await self.database.get_session(session_id)
            name = str(result.get("character_name") or "未命名角色")
            review_text = (
                "已自动通过审核。"
                if result.get("auto_approved")
                else "等待审核。"
            )
            sent = await self._send_text(
                str(session.get("unified_origin") or ""),
                f"【酒馆】角色卡{name}已建立，{review_text}",
            )
            if not sent:
                logger.warning(
                    "AI 酒馆角色卡建成通知未找到可用群会话：session=%s",
                    session_id,
                )
        except Exception:
            # The card transaction has already committed. A delivery problem
            # must never make the private confirmation claim that creation
            # failed or encourage the player to submit a duplicate card.
            logger.exception("AI 酒馆角色卡建成通知发送失败")

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
                if bound.get("binding_code_reissued"):
                    return (
                        "【建卡码已过期，系统已自动补发】\n"
                        f"新建卡码：{bound['binding_code']}\n"
                        f"有效期至：{bound.get('binding_expires_at')}\n\n"
                        "请重新发送：\n"
                        f"/酒馆 建卡 {bound['binding_code']}"
                    )
                return "【私聊身份绑定成功】\n" + format_card_prompt(bound)
            if command.matched and command.action == "card_preview":
                preview = await self.database.preview_card_draft(origin)
                return format_card_preview(preview)
            if command.matched and command.action == "card_current":
                current = await self.database.card_draft_for_private(origin)
                if not current:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡"
                    )
                return format_card_prompt(current)
            if command.matched and command.action == "card_previous":
                previous = await self.database.previous_card_step(origin)
                return "【已返回上一步】\n" + format_card_prompt(previous)
            if command.matched and command.action == "card_modify":
                if not command.argument:
                    raise ValueError(
                        "请发送 /酒馆 修改 <字段名称或稳定key>"
                    )
                modified = await self.database.modify_card_field(
                    origin, command.argument
                )
                return "【已进入字段修改】\n" + format_card_prompt(modified)
            if command.matched and command.action == "card_stats_reset":
                reset = await self.database.reset_card_draft_stats(origin)
                if reset.get("profession_reset"):
                    return (
                        "【主副属性已重置】\n"
                        "职业与职业固定基础属性均已保留；"
                        "其他角色资料没有改变。\n"
                        "请重新选择主属性和副属性。\n"
                        + format_card_prompt(reset)
                    )
                return (
                    "【角色数值已保留】已保留你已分配的数值，"
                    "可重新调整未使用的剩余点数。\n"
                    + format_card_prompt(reset)
                )
            if command.matched and command.action == "card_timer_notice":
                setting = str(command.argument or "").strip().casefold()
                if setting in {"开", "开启", "on", "true", "1"}:
                    enabled: bool | None = True
                elif setting in {"关", "关闭", "off", "false", "0"}:
                    enabled = False
                elif setting:
                    raise ValueError(
                        "请使用 /酒馆 建卡提醒 开 或 /酒馆 建卡提醒 关"
                    )
                else:
                    enabled = None
                result = (
                    await self.database.set_card_completion_reminder(
                        origin,
                        enabled,
                    )
                )
                state = "已开启" if result["enabled"] else "已关闭"
                remaining = (
                    _format_remaining_time(result.get("remaining_seconds"))
                    if result.get("has_deadline")
                    else "不限时"
                )
                suffix = (
                    "之后每 2 分钟私聊提示一次剩余时间。"
                    if result["enabled"]
                    else "建卡计时仍会继续，到期结果仍会私聊通知。"
                )
                return (
                    f"【建卡倒计时提示{state}】"
                    f"当前剩余 {remaining}。{suffix}"
                )
            if command.matched and command.action == "card_confirm":
                result = await self.database.confirm_card_draft(origin)
                await self._notify_group_card_created(result)
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
                    "上一步、修改、当前步骤、重填数值、建卡提醒、"
                    "确认建卡、取消建卡。"
                )
            else:
                value = message
            result = await self.database.fill_card_draft(
                origin,
                value,
                source_event_id=transport_event_id(event),
            )
            if result.get("duplicate"):
                return "【私聊建卡】这条消息已经处理过，当前步骤未重复推进。\n" + format_card_prompt(result)
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
            yield await self._rich_result(event, response)

    @tavern.command("开演", alias={"开始故事"}, priority=200)
    async def tavern_perform(self, event: AstrMessageEvent):
        """完成准备检查并正式开始故事。"""

        response = await self._run_native_command(event, "perform")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("暂停", priority=200)
    async def tavern_pause(self, event: AstrMessageEvent):
        """暂停当前酒馆会话。"""

        response = await self._run_native_command(event, "pause")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("恢复", priority=200)
    async def tavern_recover(self, event: AstrMessageEvent):
        """进入恢复准备大厅，但不恢复剧情或计时。"""

        response = await self._run_native_command(event, "recover")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("继续", priority=200)
    async def tavern_resume(self, event: AstrMessageEvent):
        """在恢复准备完成后正式续演。"""

        response = await self._run_native_command(event, "resume")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("关闭", priority=200)
    async def tavern_close(self, event: AstrMessageEvent):
        """关闭当前酒馆会话。"""

        response = await self._run_native_command(event, "close")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("完结", priority=200)
    async def tavern_finish(self, event: AstrMessageEvent):
        """二次确认后归档当前故事。"""

        response = await self._run_native_command(event, "finish")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("强制终止", priority=200)
    async def tavern_abort(self, event: AstrMessageEvent):
        """二次确认后异常终止并永久归档当前故事。"""

        response = await self._run_native_command(event, "abort")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("安全暂停", priority=200)
    async def tavern_safety_pause(self, event: AstrMessageEvent):
        """任一出场玩家都可立即冻结故事与全部计时。"""

        response = await self._run_native_command(event, "safety_pause")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("维护", priority=200)
    async def tavern_maintenance(self, event: AstrMessageEvent):
        """将当前酒馆会话切换至维护状态。"""

        response = await self._run_native_command(event, "maintenance")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("状态", priority=200)
    async def tavern_status(self, event: AstrMessageEvent):
        """查看当前酒馆会话状态。"""

        response = await self._run_native_command(event, "status")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("主持", priority=200)
    async def tavern_dm(self, event: AstrMessageEvent):
        """切换真人 DM、推进叙事、交棒或恢复自动模式。"""

        response = await self._run_native_command(event, "dm")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("存档", priority=200)
    async def tavern_save(self, event: AstrMessageEvent):
        """保存当前酒馆会话。"""

        response = await self._run_native_command(event, "save")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("删档", priority=200)
    async def tavern_delete_save(self, event: AstrMessageEvent):
        """删除普通手动存档。"""

        response = await self._run_native_command(event, "delete_save")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("读档", priority=200)
    async def tavern_load(self, event: AstrMessageEvent):
        """读取当前酒馆存档。"""

        response = await self._run_native_command(event, "load")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("回滚", priority=200)
    async def tavern_rollback(self, event: AstrMessageEvent):
        """回滚当前酒馆会话的上一回合。"""

        response = await self._run_native_command(event, "rollback")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("世界列表", priority=200)
    async def tavern_worlds(self, event: AstrMessageEvent):
        """列出可用世界包。"""

        response = await self._run_native_command(event, "worlds")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("副本列表", alias={"副本"}, priority=200)
    async def tavern_instances(self, event: AstrMessageEvent):
        """列出当前群可选择的剧情副本。"""

        response = await self._run_native_command(event, "instances")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("加入", priority=200)
    async def tavern_join(self, event: AstrMessageEvent):
        """加入当前群的多人回合队列。"""

        response = await self._run_native_command(event, "join")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("建卡", priority=200)
    async def tavern_card(self, event: AstrMessageEvent):
        """在群内查看建卡码，或在私聊中绑定建卡码。"""

        response = await self._run_native_command(event, "card")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("填写", priority=200)
    async def tavern_card_fill(self, event: AstrMessageEvent):
        """在私聊中填写当前角色卡字段。"""

        response = await self._run_native_command(event, "card_fill")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("上一步", priority=200)
    async def tavern_card_previous(self, event: AstrMessageEvent):
        """返回并重新填写上一个可见建卡字段。"""

        response = await self._run_native_command(event, "card_previous")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("修改", priority=200)
    async def tavern_card_modify(self, event: AstrMessageEvent):
        """按字段名称或稳定 key 修改已有角色卡字段。"""

        response = await self._run_native_command(event, "card_modify")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("当前步骤", priority=200)
    async def tavern_card_current(self, event: AstrMessageEvent):
        """重新显示当前建卡步骤与对应预设。"""

        response = await self._run_native_command(event, "card_current")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("预览", priority=200)
    async def tavern_card_preview(self, event: AstrMessageEvent):
        """在私聊中预览完整角色卡。"""

        response = await self._run_native_command(event, "card_preview")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("重填数值", priority=200)
    async def tavern_card_stats_reset(self, event: AstrMessageEvent):
        """保留文字角色资料，仅重新分配角色数值。"""

        response = await self._run_native_command(
            event,
            "card_stats_reset",
        )
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("建卡提醒", priority=200)
    async def tavern_card_timer_notice(self, event: AstrMessageEvent):
        """在私聊中查询、开启或关闭角色卡倒计时提示。"""

        response = await self._run_native_command(
            event,
            "card_timer_notice",
        )
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("确认建卡", priority=200)
    async def tavern_card_confirm(self, event: AstrMessageEvent):
        """在私聊中提交角色卡审核。"""

        response = await self._run_native_command(event, "card_confirm")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("取消建卡", priority=200)
    async def tavern_card_cancel(self, event: AstrMessageEvent):
        """在私聊中取消角色卡草稿并释放席位。"""

        response = await self._run_native_command(event, "card_cancel")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("角色", priority=200)
    async def tavern_character(self, event: AstrMessageEvent):
        """查看自己的副本角色状态。"""

        response = await self._run_native_command(event, "character")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("准备", priority=200)
    async def tavern_ready(self, event: AstrMessageEvent):
        """在准备大厅确认本次出场。"""

        response = await self._run_native_command(event, "ready")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("强制全员准备", priority=200)
    async def tavern_force_ready(self, event: AstrMessageEvent):
        """由主持人将全部合格出场角色设为已准备。"""

        response = await self._run_native_command(event, "force_ready")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("阵容", priority=200)
    async def tavern_roster(self, event: AstrMessageEvent):
        """查看当前角色卡、准备与入场状态。"""

        response = await self._run_native_command(event, "roster")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("审核", priority=200)
    async def tavern_review(self, event: AstrMessageEvent):
        """列出、查看并处理待审核角色卡。"""

        response = await self._run_native_command(event, "review")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("选择", priority=200)
    async def tavern_choose(self, event: AstrMessageEvent):
        """选择当前回合的 A/B/C/D 行动。"""

        response = await self._run_native_command(event, "choose")
        if response:
            unsent = await self._send_event_parts(
                event,
                _story_reply_parts(response),
            )
            if unsent:
                yield event.plain_result("\n\n".join(unsent))

    @tavern.command("重整选项", priority=200)
    async def tavern_reroll(self, event: AstrMessageEvent):
        """免费重整本回合选项一次。"""

        await self._send_event_text(
            event,
            "🎲 【酒馆】已收到重整请求，正在重新生成本回合选项……",
        )
        response = await self._run_native_command(event, "reroll")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("灵感", priority=200)
    async def tavern_inspiration(self, event: AstrMessageEvent):
        """查看灵感，或在选择检定选项时消耗一点取得优势。"""

        getter = getattr(event, "get_message_str", None)
        raw_message = str(
            getter() if callable(getter) else getattr(event, "message_str", "")
        )
        choosing = len(raw_message.strip().split()) >= 3
        response = await self._run_native_command(event, "inspiration")
        if response:
            if choosing:
                unsent = await self._send_event_parts(
                    event,
                    _story_reply_parts(response),
                )
                if unsent:
                    yield event.plain_result("\n\n".join(unsent))
            else:
                yield await self._rich_result(event, response)

    @tavern.command("灵感重投", priority=200)
    async def tavern_inspiration_reroll(self, event: AstrMessageEvent):
        """选择检定选项并消耗一点灵感重投完整骰池。"""

        response = await self._run_native_command(
            event,
            "inspiration_reroll",
        )
        if response:
            unsent = await self._send_event_parts(
                event,
                _story_reply_parts(response),
            )
            if unsent:
                yield event.plain_result("\n\n".join(unsent))

    @tavern.command("投票", priority=200)
    async def tavern_vote(self, event: AstrMessageEvent):
        """参与当前集体决策。"""

        response = await self._run_native_command(event, "vote")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("倒计时", priority=200)
    async def tavern_countdown(self, event: AstrMessageEvent):
        """查询、总开关或逐类开关副本倒计时。"""

        response = await self._run_native_command(event, "countdown")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("用量", alias={"Token用量"}, priority=200)
    async def tavern_usage(self, event: AstrMessageEvent):
        """查看当前群与副本的模型 Token 用量。"""

        response = await self._run_native_command(event, "usage")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("限额", alias={"Token限额"}, priority=200)
    async def tavern_quota(self, event: AstrMessageEvent):
        """设置当前群或副本的滚动 Token 限额。"""

        response = await self._run_native_command(event, "quota")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("删除副本", priority=200)
    async def tavern_delete_session(self, event: AstrMessageEvent):
        """删除已关闭或已归档副本并移入回收目录。"""

        response = await self._run_native_command(event, "delete_session")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("暂离", priority=200)
    async def tavern_away(self, event: AstrMessageEvent):
        """暂离回合队列但保留席位。"""

        response = await self._run_native_command(event, "away")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("返回队列", priority=200)
    async def tavern_return_queue(self, event: AstrMessageEvent):
        """从下一轮队尾重新加入行动。"""

        response = await self._run_native_command(event, "return_queue")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("申请返场", priority=200)
    async def tavern_return_request(self, event: AstrMessageEvent):
        """为已退场角色申请剧情返场。"""

        response = await self._run_native_command(event, "return_request")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("退出", priority=200)
    async def tavern_leave(self, event: AstrMessageEvent):
        """退出当前群的多人回合队列。"""

        response = await self._run_native_command(event, "leave")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("顺序", alias={"轮次"}, priority=200)
    async def tavern_order(self, event: AstrMessageEvent):
        """查看当前轮次与行动顺序。"""

        response = await self._run_native_command(event, "order")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("跳过", priority=200)
    async def tavern_skip(self, event: AstrMessageEvent):
        """当前玩家主动跳过自己的行动。"""

        response = await self._run_native_command(event, "skip")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("强制下一位", priority=200)
    async def tavern_next(self, event: AstrMessageEvent):
        """管理员强制跳过当前行动者。"""

        response = await self._run_native_command(event, "next")
        if response:
            yield await self._rich_result(event, response)

    @tavern.command("帮助", priority=200)
    async def tavern_help(self, event: AstrMessageEvent):
        """显示酒馆指令帮助。"""

        response = await self._run_native_command(event, "help")
        if response:
            yield await self._rich_result(event, response)

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
    async def _timer_loop(self) -> None:
        # 自愈循环：异常时原地重试，不再另起一个 task。
        # 异常分支不得派生新循环，避免叠加并行轮询和重复提醒。
        while True:
            try:
                notifications = await self.database.process_due_timers()
                for item in notifications:
                    await self._send_timer_notice(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AI 酒馆计时器轮询异常，将在稍后重试")
            await asyncio.sleep(TIMER_POLL_INTERVAL_SECONDS)

    def _timer_notice_should_skip(self, item: Mapping[str, Any]) -> bool:
        """同一计时器短时间内重复触发时丢弃，防止刷屏。"""

        key = "|".join(
            (
                str(item.get("session_id") or ""),
                str(item.get("timer_type") or ""),
                str(item.get("participant_id") or ""),
                str(item.get("kind") or ""),
            )
        )
        now = time.monotonic()
        last = self._timer_notice_last_sent.get(key)
        if last is not None and now - last < TIMER_NOTICE_DEDUP_SECONDS:
            return True
        self._timer_notice_last_sent[key] = now
        if len(self._timer_notice_last_sent) > 512:
            cutoff = now - TIMER_NOTICE_DEDUP_SECONDS * 4
            self._timer_notice_last_sent = {
                cached_key: seen
                for cached_key, seen in self._timer_notice_last_sent.items()
                if seen >= cutoff
            }
        return False

    async def _send_timer_notice(self, item: Mapping[str, Any]) -> None:
        if self._timer_notice_should_skip(item):
            return
        try:
            session = await self.database.get_session(
                str(item.get("session_id") or "")
            )
            instance_config = await self.database.get_instance_config(
                session["id"]
            )
            timer_type = str(item.get("timer_type") or "")
            if timer_type != "card_completion" and not (
                instance_config["time_rules"].get(
                    "announce_timeouts",
                    True,
                )
            ):
                return
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
                text += (
                    "全部计时已冻结；先由主持人发送 /酒馆 恢复，"
                    "全员重新准备后再发送 /酒馆 继续。"
                )
            elif kind == "reminder":
                remaining = _format_remaining_time(
                    item.get("remaining_seconds")
                )
                prompts = {
                    "turn": "请及时完成本回合操作。",
                    "vote": "请尚未投票的玩家完成投票。",
                    "card_code": "请及时绑定私聊建卡码。",
                    "card_completion": (
                        "请及时完成角色卡创建；"
                        "关闭提示可发送 /酒馆 建卡提醒 关。"
                    ),
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

            targets = item.get("targets")
            target_items: list[Mapping[str, Any]] = []
            if isinstance(targets, Sequence) and not isinstance(
                targets,
                (str, bytes),
            ):
                seen: set[str] = set()
                for target in targets:
                    if not isinstance(target, Mapping):
                        continue
                    user_id = str(target.get("user_id") or "")
                    if not user_id or user_id in seen:
                        continue
                    seen.add(user_id)
                    target_items.append(target)

            private_delivery = timer_type == "card_completion"
            if private_delivery:
                private_origin = next(
                    (
                        str(target.get("private_origin") or "")
                        for target in target_items
                        if target.get("private_origin")
                    ),
                    "",
                )
                # QQ 官方机器人只能在玩家先建立私聊会话后主动私发。
                # 未绑定建卡码时不回退到群聊，避免泄漏建卡倒计时。
                if not private_origin:
                    return
                origin = private_origin

            chain = MessageChain()
            mention_count = 0
            if not private_delivery:
                platform_name = ""
                get_platform_inst = getattr(
                    self.context,
                    "get_platform_inst",
                    None,
                )
                if callable(get_platform_inst):
                    platform = get_platform_inst(
                        str(session.get("platform_id") or "")
                    )
                    if platform is not None:
                        meta = getattr(platform, "meta", None)
                        metadata = meta() if callable(meta) else None
                        platform_name = str(
                            getattr(metadata, "name", "") or ""
                        )
                if not platform_name:
                    platform_name = str(
                        session.get("platform_id") or ""
                    )
                qq_official = platform_name in {
                    "qq_official",
                    "qq_official_webhook",
                }
                for target in target_items:
                    user_id = str(target.get("user_id") or "")
                    display_name = str(
                        target.get("display_name") or user_id
                    )
                    if qq_official:
                        # AstrBot 4.26 的 QQ 官方适配器会忽略 At 组件；
                        # 使用 QQBot 官方文本交互协议才能形成真实艾特。
                        escaped_id = html.escape(user_id, quote=True)
                        chain.message(
                            f'<qqbot-at-user id="{escaped_id}" /> '
                        )
                    else:
                        chain.at(display_name, user_id).message(" ")
                    mention_count += 1
            if kind == "reminder" and timer_type in {
                "vote",
                "preparation",
            } and mention_count == 0:
                return
            # 主动消息之间保持最小间隔，避免一次吐出多条时触发
            # QQ 官方频控（40034100），进而被整批拒绝。
            gap = time.monotonic() - self._timer_notice_last_at
            if gap < TIMER_NOTICE_MIN_GAP_SECONDS:
                await asyncio.sleep(TIMER_NOTICE_MIN_GAP_SECONDS - gap)
            self._timer_notice_last_at = time.monotonic()
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

    @staticmethod
    def _looks_like_bare_tavern(text: str) -> bool:
        """Whether ``text`` is a tavern command missing its ``/`` prefix."""

        return text == "酒馆" or (
            text.startswith("酒馆") and text[2:3].isspace()
        )

    def _parse_command_relaxed(
        self,
        event: AstrMessageEvent,
        message: str,
    ) -> ParsedCommand:
        """Parse a tavern command, tolerating a missing ``/`` prefix.

        某些接入方式（QQ 官方平台 + qq_restapi 全量群消息）会吞掉开头的
        ``/``，并且把「视为被 @」的唤醒标记推迟到 handler 阶段才补写。
        而 AstrBot 的 ``CommandFilter`` 在更早的唤醒检查阶段就会因
        ``is_at_or_wake_command`` 为假而把全部原生指令过滤掉，导致指令
        静默失效。因此这里不再依赖唤醒标记，直接按裸指令兜底解析。
        """

        command = parse_tavern_command(message)
        if command.matched:
            return command
        text = str(message or "").strip()
        if not self._looks_like_bare_tavern(text):
            return command
        relaxed = parse_tavern_command("/" + text)
        if not relaxed.matched:
            return command
        if relaxed.action not in ("unknown", "help"):
            return relaxed
        # 裸「酒馆」与无法识别的动作只在确实被唤醒时回应，
        # 避免把普通群聊里的日常用词误当成指令。
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return relaxed
        return command

    @filter.event_message_type(
        getattr(filter.EventMessageType, "PRIVATE_MESSAGE", "private"),
        priority=110,
    )
    async def on_private_message(self, event: AstrMessageEvent):
        message = str(getattr(event, "message_str", "") or "").strip()
        command = self._parse_command_relaxed(event, message)
        response = await self._handle_private_card_message(
            event,
            command,
            message,
        )
        if response:
            yield await self._rich_result(event, response)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def on_group_message(self, event: AstrMessageEvent):
        message = str(event.message_str or "")
        command = self._parse_command_relaxed(event, message)
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
                yield await self._rich_result(event, response)
            return

        if content is None:
            return
        if not config.is_group_allowed(group_id):
            return
        session = await self.database.get_session_by_group(
            platform_id,
            group_id,
        )
        self._remember_rich_title(event, session)

        event.stop_event()
        if not session or session["state"] == SESSION_CLOSED:
            yield await self._rich_result(
                event, "【酒馆】当前群尚未开馆，请由管理员发送 /酒馆 开启。"
            )
            return
        if session["state"] == SESSION_PREPARING:
            next_command = (
                "/酒馆 继续"
                if int(session.get("turn_no") or 0) > 0
                else "/酒馆 开演"
            )
            yield await self._rich_result(
                event,
                "【故事尚未开演】当前处于角色准备阶段。"
                "请先完成角色卡并发送 /酒馆 准备，"
                f"由主持人发送 {next_command}。",
            )
            return
        if session["state"] == SESSION_PAUSED:
            yield await self._rich_result(
                event, "【酒馆】剧情已暂停，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_FINISHED:
            yield await self._rich_result(
                event, "【酒馆】故事已经完结，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_MAINTENANCE:
            yield await self._rich_result(
                event, "【酒馆】当前处于维护模式，本条内容未记录。"
            )
            return

        try:
            control = await self.database.get_control_state(session["id"])
            if control.get("mode") == "dm" and control.get("phase") != "player_handoff":
                if sender_id != str(control.get("active_dm_user_id") or ""):
                    yield await self._rich_result(
                        event,
                        "【主持人模式】当前剧情由活动 DM 接管；普通玩家输入暂不记录。",
                    )
                    return
                result = await self.engine.process_dm_beat(
                    event=event,
                    session_id=session["id"],
                    dm_user_id=sender_id,
                    instruction=content,
                    progress=lambda text: self._send_event_text(event, text),
                )
                yield await self._rich_result(
                    event,
                    f"【主持推进 · 第 {result['beat_no']} 段】\n{result['narrative']}",
                )
                return
            vote = await self.database.active_vote(session["id"])
            if vote:
                yield await self._rich_result(
                    event,
                    "【集体投票进行中】请使用 /酒馆 投票 A；"
                    "投票不会消耗个人行动机会。",
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
                progress=lambda text: self._send_event_text(event, text),
            )
            if control.get("mode") == "dm" and control.get("phase") == "player_handoff":
                await self.database.finish_dm_handoff(session["id"])
                reply.turn_text = "【本次交棒行动已完成】已回到等待 DM 主持状态。"
            reply_parts: list[str] = []
            if reply.story_text:
                reply_parts.append(reply.story_text)
            if reply.turn_text:
                reply_parts.append(reply.turn_text)
            if not reply.story_text and not reply.turn_text:
                reply_parts.append(reply.text)
            unsent = await self._send_event_parts(event, reply_parts)
            if unsent:
                yield event.plain_result("\n\n".join(unsent))
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
        self._remember_rich_title(event, session)
        roles = (
            await self.database.permission_roles(session["id"], sender_id)
            if session
            else set()
        )
        is_host = is_admin or "host" in roles
        is_moderator = is_host or "moderator" in roles
        control_state = (
            await self.database.get_control_state(session["id"])
            if session
            else {"mode": "auto", "active_dm_user_id": ""}
        )
        is_active_dm = bool(
            control_state.get("mode") == "dm"
            and str(control_state.get("active_dm_user_id") or "") == sender_id
        )
        host_actions = {
            "start",
            "perform",
            "pause",
            "recover",
            "resume",
            "close",
            "finish",
            "abort",
            "save",
            "delete_save",
            "load",
            "rollback",
            "save_list",
            "review",
            "force_ready",
            "extend",
            "countdown",
            "usage",
            "quota",
            "delete_session",
            "instances",
            "worlds",
            "dm",
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
            command.action in host_actions and (is_host or (command.action == "dm" and is_active_dm))
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
            turn = (
                await self.database.get_turn_status(session["id"])
                if session
                else {}
            )
            contextual = contextual_help(
                command.argument,
                session=session or {},
                turn=turn,
                user_id=sender_id,
                is_admin=is_admin,
            )
            return contextual if command.argument else contextual + "\n\n" + help_text

        # ── 暂停态守卫 ─────────────────────────────────────────────
        # 会话处于「已暂停」时，任何会改变剧情 / 选项 / 投票 / 队列 /
        # 角色卡的玩法指令都必须拦截，避免“已暂停却能重整选项、推进游戏”。
        # 仅放行主持人 / 管理员的会话管理类指令（恢复、续演、关闭、存档、
        # 状态查询等），这些指令在上方权限层已做 host 校验。
        if session and session["state"] == SESSION_PAUSED:
            paused_blocked_actions = {
                # 选项 / 行动 / 投票（核心玩法循环）
                "choose", "reroll", "inspiration", "inspiration_reroll",
                "vote",
                # 队列 / 回合控制
                "join", "ready", "away", "return_queue", "return_request",
                "delegate", "delegate_revoke", "leave", "order", "skip",
                "next", "move", "designate", "perform", "force_ready",
                # 角色卡建立
                "card", "card_fill", "card_preview", "card_stats_reset",
                "card_timer_notice", "card_confirm", "card_cancel",
                "dm",
            }
            if command.action in paused_blocked_actions:
                return (
                    "【酒馆】剧情已暂停，暂不可进行选项 / 投票 / 行动 / "
                    "建卡等玩法操作。\n请先由主持人发送 /酒馆 恢复 "
                    "进入恢复准备大厅，再发送 /酒馆 继续 续演。"
                )

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
                elif int(session.get("turn_no") or 0) > 0:
                    await self.database.pause_session_timers(
                        session["id"],
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
                        "hook": "session_created",
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
                        (
                            "\n\n这是已有剧情进度的副本，暂停时的对话、"
                            "行动者、投票与选项均已保留。"
                            "\n全员确认准备后，主持人发送 /酒馆 继续；"
                            "不要使用 /酒馆 开演。"
                        )
                        if int(session.get("turn_no") or 0) > 0
                        else (
                            "\n\n玩家发送 /酒馆 加入，按提示私聊建卡，"
                            "完成后发送 /酒馆 准备。"
                            "\n主持人最后发送 /酒馆 开演；此时不会自动开演。"
                        )
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
                control = await self.database.get_control_state(session["id"])
                control_text = (
                    f"DM 主持 · {control.get('active_dm_user_id') or '未指定'}"
                    f" · 第 {control.get('beat_no', 0)} 段"
                    if control.get("mode") == "dm"
                    else "AI 自动"
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
                    f"控制模式：{control_text}\n"
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

            if command.action == "dm":
                raw = str(command.argument or "").strip()
                sub, _, value = raw.partition(" ")
                sub = sub.strip()
                value = value.strip()
                if sub in {"", "状态"}:
                    state = await self.database.get_control_state(session["id"])
                    return (
                        "【主持模式状态】\n"
                        f"模式：{'DM 主持' if state['mode'] == 'dm' else 'AI 自动'}\n"
                        f"活动 DM：{state.get('active_dm_user_id') or '无'}\n"
                        f"阶段：{state.get('phase') or 'auto'}\n"
                        f"连续推进：{state.get('beat_no', 0)} 段\n"
                        f"一次性指引：{'已保存' if state.get('directive') else '无'}\n"
                        f"当前交棒目标：{state.get('current_actor_ref') or '无'}"
                    )
                if sub == "开启":
                    dm_id = value or sender_id
                    if dm_id != sender_id and not is_admin:
                        raise PermissionError("只有插件管理员可以指定其他活动 DM")
                    state = await self.database.enable_dm_mode(
                        session["id"], dm_id, sender_id
                    )
                    await self.broker.publish({
                        "type": "dm_control", "hook": "dm_mode_enabled",
                        "session_id": session["id"], "actor": dm_id,
                    })
                    return (
                        "【已进入主持人模式】\n"
                        f"当前活动 DM：{state['active_dm_user_id']}\n"
                        "旧选项已失效，旧行动计时器已停止；玩家顺序保留。"
                    )
                if sub == "接管":
                    if not is_admin:
                        raise PermissionError("只有插件管理员可以强制接管 DM")
                    state = await self.database.enable_dm_mode(
                        session["id"], sender_id, sender_id
                    )
                    return f"【主持权已接管】当前活动 DM：{state['active_dm_user_id']}"
                state = await self.database.get_control_state(session["id"])
                if state.get("mode") != "dm":
                    raise ValueError("当前未开启主持模式，请先发送 /酒馆 主持 开启")
                if sender_id != str(state.get("active_dm_user_id") or "") and not is_admin:
                    raise PermissionError("只有当前活动 DM 可以执行此操作")
                if sub == "指引":
                    state = await self.database.set_dm_directive(
                        session["id"], value, sender_id
                    )
                    return "【一次性主持指引已保存】将在下一次 AI 推进成功后自动清除。"
                if sub == "推进":
                    result = await self.engine.process_dm_beat(
                        event=event,
                        session_id=session["id"],
                        dm_user_id=sender_id,
                        instruction=value,
                        progress=lambda text: self._send_event_text(event, text),
                    )
                    return f"【主持推进 · 第 {result['beat_no']} 段】\n{result['narrative']}"
                if sub == "直述":
                    result = await self.database.commit_dm_beat(
                        session_id=session["id"],
                        expected_revision=int(session["revision"]),
                        dm_user_id=sender_id,
                        instruction=value,
                        narrative=value,
                        world_state=session["world_state"],
                        direct=True,
                    )
                    return f"【主持直述 · 第 {result['beat_no']} 段】\n{result['narrative']}"
                if sub == "交棒":
                    if not value:
                        raise ValueError("格式：/酒馆 主持 交棒 <角色名或 NPC:名称>")
                    if value.upper().startswith("NPC:") or value.startswith("NPC："):
                        npc_ref = value[4:].strip()
                        npcs = await self.database.list_session_characters(
                            session["id"], include_archived=False
                        )
                        npc = next((item for item in npcs if npc_ref in {
                            str(item.get("id") or ""), str(item.get("name") or ""),
                            *[str(alias) for alias in item.get("aliases", [])]
                        }), None)
                        if not npc:
                            raise ValueError("没有找到该 NPC")
                        await self.database.set_dm_handoff(
                            session["id"], "npc", str(npc["id"]), sender_id
                        )
                        result = await self.engine.process_dm_beat(
                            event=event, session_id=session["id"],
                            dm_user_id=sender_id,
                            instruction=f"让 NPC“{npc['name']}”依据其知识边界与当前状态行动一段；不替玩家行动。",
                            progress=lambda text: self._send_event_text(event, text),
                        )
                        return f"【NPC 演出 · {npc['name']}】\n{result['narrative']}\n\n已回到等待 DM 状态。"
                    target = await self.database.get_participant(
                        session["id"], participant_ref=value
                    )
                    await self.database.set_dm_handoff(
                        session["id"], "player", str(target["id"]), sender_id
                    )
                    turn = await self.database.designate_turn(
                        session["id"], target["group_user_id"], sender_id
                    )
                    return "【已交棒，主持模式保持开启】\n" + format_turn_status(turn)
                if sub == "自动":
                    if not value:
                        raise ValueError("格式：/酒馆 主持 自动 <玩家角色>")
                    target = await self.database.get_participant(
                        session["id"], participant_ref=value
                    )
                    turn = await self.database.designate_turn(
                        session["id"], target["group_user_id"], sender_id
                    )
                    await self.database.disable_dm_mode(session["id"], sender_id)
                    await self.broker.publish({
                        "type": "dm_control", "hook": "dm_mode_disabled",
                        "session_id": session["id"], "actor": sender_id,
                    })
                    return "【已恢复 AI 自动模式】\n" + format_turn_status(turn)
                raise ValueError(
                    "主持命令：开启、状态、指引、推进、直述、交棒、自动、接管"
                )

            if command.action == "join":
                result = await self.database.reserve_participant(
                    session["id"],
                    sender_id,
                    str(event.get_sender_name() or sender_id),
                )
                if result.get("binding_code"):
                    title = (
                        "【建卡码已自动补发】"
                        if result.get("binding_code_reissued")
                        else "【席位已预留】"
                    )
                    return (
                        f"{title}\n"
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
                    (
                        "\n【全员准备完成】主持人现在可以发送 /酒馆 继续"
                        if preflight.get("resume_mode")
                        else "\n【全员准备完成】主持人现在可以发送 /酒馆 开演"
                    )
                    if preflight["ok"]
                    else f"\n当前仍有 {waiting} 项准备阻塞。"
                )
                return (
                    f"【{participant.get('character_name') or participant.get('display_name')} 已准备】"
                    + suffix
                )
            if command.action == "force_ready":
                if command.argument not in {"确认", "confirm", "CONFIRM"}:
                    return (
                        "【确认强制全员准备】只会处理角色卡已审核通过、"
                        "且当前出场的玩家；不会绕过建卡或审核。"
                        "\n请发送：/酒馆 强制全员准备 确认"
                    )
                result = await self.database.force_all_ready(
                    session["id"],
                    sender_id,
                )
                lines = [
                    "【强制准备完成】",
                    f"已准备：{result['ready_count']} 人",
                ]
                if result["skipped"]:
                    lines.append(
                        "未处理："
                        + "；".join(
                            (
                                f"{item['name']}（{item['card_status']}/"
                                f"{item['participation_status']}）"
                            )
                            for item in result["skipped"]
                        )
                    )
                preflight = await self.database.opening_preflight(
                    session["id"]
                )
                lines.append(
                    "主持人现在可以发送 /酒馆 继续。"
                    if int(session.get("turn_no") or 0) > 0
                    else "主持人现在可以发送 /酒馆 开演。"
                )
                if not preflight["ok"]:
                    lines.append(
                        "仍有阻塞：" + "；".join(preflight["blockers"])
                    )
                return "\n".join(lines)
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
                instance_config = await self.database.get_instance_config(
                    session["id"]
                )
                self.engine.validate_world_runtime(
                    instance_config["world_snapshot"]
                )
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
                    progress=lambda text: self._send_event_text(event, text),
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
                    progress=lambda text: self._send_event_text(event, text),
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
                        # 0.11.2：表决通过后立即推进剧情并生成新选项。
                        # 旧实现只发送确认文本，从不触发后续叙事（WebUI
                        # 因只读展示数据库状态而“看似正常”）。
                        await self.broker.publish(
                            {
                                "type": "vote",
                                "action": "resolved",
                                "session_id": session["id"],
                                "status": "passed",
                                "winner_key": vote["winner_key"],
                            }
                        )
                        try:
                            reply = (
                                await self.engine.process_vote_resolution(
                                    event=event,
                                    session_id=session["id"],
                                    vote=vote,
                                )
                            )
                        except (TavernEngineError, ValueError) as exc:
                            await self.database.write_audit(
                                session["id"],
                                sender_id,
                                "vote.resolution_failed",
                                "",
                                {"error": str(exc)[:500]},
                            )
                            logger.warning(
                                "AI 酒馆表决推进失败：%s", exc
                            )
                            return (
                                f"【表决通过】多数选择 {vote['winner_key']}。"
                                "\n故事推进暂未完成："
                                f"{exc}\n可发送 /酒馆 状态 查看当前局面。"
                            )
                        parts = [
                            part
                            for part in (reply.story_text, reply.turn_text)
                            if part
                        ]
                        body = "\n\n".join(parts) if parts else reply.text
                        return (
                            f"【表决通过】多数选择 {vote['winner_key']}。\n"
                            f"{body}"
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

            if command.action == "countdown":
                labels = {
                    "card_code": "建卡码",
                    "card_completion": "角色卡完成",
                    "preparation": "准备大厅",
                    "ready": "准备确认",
                    "turn": "行动回合",
                    "vote": "集体投票",
                    "standby": "候补等待",
                }
                aliases = {
                    "总": "all",
                    "全部": "all",
                    "全局": "all",
                    "建卡码": "card_code",
                    "建卡": "card_completion",
                    "角色卡": "card_completion",
                    "准备阶段": "preparation",
                    "准备大厅": "preparation",
                    "准备": "ready",
                    "回合": "turn",
                    "行动": "turn",
                    "投票": "vote",
                    "候补": "standby",
                }
                argument = str(command.argument or "").strip()
                compact = argument.replace(" ", "")
                if not argument or argument in {"状态", "status"}:
                    policy = await self.database.get_timer_policy(
                        session["id"]
                    )
                else:
                    setting: bool | None = None
                    target_text = ""
                    for suffix, value in (
                        ("开启", True),
                        ("打开", True),
                        ("开", True),
                        ("关闭", False),
                        ("关", False),
                    ):
                        if compact.endswith(suffix):
                            target_text = compact[: -len(suffix)]
                            setting = value
                            break
                    if setting is None or target_text not in aliases:
                        return (
                            "【倒计时】格式：\n"
                            "/酒馆 倒计时 状态\n"
                            "/酒馆 倒计时 总关\n"
                            "/酒馆 倒计时 回合 关\n"
                            "/酒馆 倒计时 投票 开"
                        )
                    policy = await self.database.set_timer_policy(
                        session["id"],
                        aliases[target_text],
                        setting,
                        sender_id,
                    )
                lines = [
                    "【倒计时开关】",
                    "总开关："
                    + (
                        "开启"
                        if policy["global_enabled"]
                        else "关闭（全部冻结）"
                    ),
                ]
                for key, label in labels.items():
                    switch = policy["switches"][key]
                    effective = policy["effective"][key]
                    lines.append(
                        f"· {label}："
                        + (
                            "开启"
                            if effective
                            else (
                                "分类关闭"
                                if not switch
                                else "随总开关冻结"
                            )
                        )
                    )
                lines.append("关闭后保留真实剩余时间，不执行超时处罚。")
                return "\n".join(lines)

            if command.action == "usage":
                usage = await self.database.token_usage_summary(
                    session["id"]
                )
                lines = [
                    "【Token 用量】",
                    (
                        "当前副本："
                        f"1小时 {usage['session']['hour']} · "
                        f"24小时 {usage['session']['day']} · "
                        f"累计 {usage['session']['all']}"
                    ),
                    (
                        "当前群："
                        f"1小时 {usage['group']['hour']} · "
                        f"24小时 {usage['group']['day']} · "
                        f"累计 {usage['group']['all']}"
                    ),
                ]
                if usage["quotas"]:
                    lines.append("滚动限额：")
                    for item in usage["quotas"]:
                        scope = (
                            "群"
                            if item["scope_type"] == "group"
                            else "副本"
                        )
                        lines.append(
                            f"· {scope}：{item['used']}/"
                            f"{item['token_limit']}，"
                            f"剩余 {item['remaining']}，"
                            f"窗口 {_format_remaining_time(item['window_seconds'])}"
                            + ("" if item["enabled"] else "（已关闭）")
                        )
                else:
                    lines.append("滚动限额：未设置")
                return "\n".join(lines)

            if command.action == "quota":
                parts = str(command.argument or "").strip().split()
                if not parts or parts[0] not in {"群", "副本"}:
                    return (
                        "【Token 限额】格式：\n"
                        "/酒馆 限额 群 24小时 500000\n"
                        "/酒馆 限额 副本 1小时 100000\n"
                        "/酒馆 限额 群 关"
                    )
                scope_type = "group" if parts[0] == "群" else "session"
                if len(parts) == 2 and parts[1] in {"关", "关闭"}:
                    current_usage = await self.database.token_usage_summary(
                        session["id"]
                    )
                    current = next(
                        (
                            item for item in current_usage["quotas"]
                            if item["scope_type"] == scope_type
                        ),
                        None,
                    )
                    if not current:
                        return "【Token 限额】该范围尚未设置限额。"
                    await self.database.set_token_quota(
                        session["id"],
                        scope_type,
                        window_seconds=current["window_seconds"],
                        token_limit=current["token_limit"],
                        enabled=False,
                        actor_id=sender_id,
                    )
                    return f"【Token 限额已关闭】{parts[0]}范围不再拦截请求。"
                if len(parts) != 3:
                    return (
                        "【Token 限额】请提供时间窗口和 Token 上限，"
                        "例如：/酒馆 限额 副本 1小时 100000"
                    )
                window_seconds = parse_duration(parts[1])
                token_limit = int(parts[2])
                result = await self.database.set_token_quota(
                    session["id"],
                    scope_type,
                    window_seconds=window_seconds,
                    token_limit=token_limit,
                    enabled=True,
                    actor_id=sender_id,
                )
                item = next(
                    entry for entry in result["quotas"]
                    if entry["scope_type"] == scope_type
                )
                return (
                    f"【Token 限额已设置】{parts[0]}\n"
                    f"窗口：{_format_remaining_time(item['window_seconds'])}\n"
                    f"上限：{item['token_limit']} Token\n"
                    f"当前已用：{item['used']} · 剩余：{item['remaining']}"
                )

            if command.action == "delete_session":
                argument = str(command.argument or "").strip()
                prefix = "确认 "
                if not argument.startswith(prefix):
                    return (
                        "【确认删除整个副本】只能删除已关闭或已归档副本；"
                        "角色、剧情、Token 流水、存档和独立数据库会一并移入"
                        "回收目录。\n请发送：/酒馆 删除副本 确认 "
                        f"{session['instance_name']}"
                    )
                confirm_name = argument[len(prefix) :].strip()
                result = await self.database.delete_session(
                    session["id"],
                    sender_id,
                    confirm_name,
                )
                await self.engine.release_session_lock(session["id"])
                suffix = (
                    "\n副本文件已移入回收目录，可由服务器管理员恢复。"
                    if result.get("trash_path")
                    else "\n目录中没有残留的副本文件。"
                )
                if result.get("trash_error"):
                    suffix = (
                        "\n数据库记录已删除，但文件移入回收目录失败："
                        + result["trash_error"]
                    )
                return (
                    f"【副本已删除】{result['instance_name']}" + suffix
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
                    "\n恢复时请先发送 /酒馆 恢复。"
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
                    "\n无需在群内说明原因；由主持人与参与者确认边界后，"
                    "由主持人发送 /酒馆 恢复。"
                )
            if command.action == "recover":
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
                        + "\n\n全员重新发送 /酒馆 准备；"
                        "完成后主持人发送 /酒馆 继续。"
                    )
                if session["state"] == SESSION_PREPARING:
                    if int(session.get("turn_no") or 0) > 0:
                        return (
                            "【酒馆】已经位于恢复准备大厅。"
                            "请等待全员发送 /酒馆 准备；"
                            "全部完成后由主持人发送 /酒馆 继续。"
                        )
                    return (
                        "【酒馆】当前是新故事准备大厅，"
                        "准备完成后请使用 /酒馆 开演。"
                    )
                if session["state"] == SESSION_RUNNING:
                    return (
                        "【酒馆】故事当前正在运行，无需恢复。"
                        "如需停团，请先发送 /酒馆 暂停。"
                    )
                if session["state"] == SESSION_CLOSED:
                    return (
                        "【酒馆】副本当前已关闭；"
                        "请使用 /酒馆 开启 <副本标识> 重新进入。"
                    )
                raise InvalidTransitionError("当前副本状态不能进入恢复准备大厅")
            if command.action == "resume":
                if session["state"] in {
                    SESSION_PAUSED,
                    SESSION_MAINTENANCE,
                }:
                    return (
                        "【酒馆】副本仍处于暂停状态。"
                        "请先发送 /酒馆 恢复 进入恢复准备大厅；"
                        "本次没有切换状态，也没有恢复任何计时。"
                    )
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有恢复准备大厅中的副本可以继续"
                    )
                if int(session.get("turn_no") or 0) <= 0:
                    return (
                        "【酒馆】该副本尚未产生剧情，"
                        "新故事请使用 /酒馆 开演。"
                    )
                instance_config = await self.database.get_instance_config(
                    session["id"]
                )
                self.engine.validate_world_runtime(
                    instance_config["world_snapshot"]
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
                session = result["session"]
                current = result["current_participant"]
                active_vote = await self.database.active_vote(session["id"])
                recent_events = await self.database.recent_events(
                    session["id"],
                    80,
                )
                last_story = next(
                    (
                        str(item.get("content") or "")
                        for item in reversed(recent_events)
                        if item.get("role") == "narrator"
                    ),
                    str(
                        session["world_state"].get("scene_summary")
                        or "暂无剧情正文"
                    ),
                )
                workflow_text = (
                    format_vote(active_vote)
                    if active_vote
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
                timer_text = format_recovered_timer(
                    await self.database.list_timers(session["id"]),
                    vote_active=bool(active_vote),
                )
                return (
                    f"📜 【故事继续】{session['instance_name']}\n"
                    f"🎭 当前行动者："
                    f"{current.get('character_name') or current.get('display_name')}"
                    f"\n\n📖 【恢复时的最后剧情】\n{last_story}"
                    "\n\n"
                    + workflow_text
                    + "\n\n"
                    + timer_text
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
                await self.engine.release_session_lock(session["id"])
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
                await self.engine.release_session_lock(session["id"])
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
                await self.engine.release_session_lock(session["id"])
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
                return await build_recap(
                    self.database,
                    session,
                    sender_id,
                    command.argument,
                )
            if command.action == "save":
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError(
                        "只有正式运行中的故事可以创建新剧情存档"
                    )
                if not command.argument:
                    return "【酒馆】请提供存档名：/酒馆 存档 <名称>"
                name = str(command.argument).strip()
                replace = False
                if name.endswith(" 覆盖确认"):
                    name = name[: -len(" 覆盖确认")].strip()
                    replace = True
                existing = next(
                    (
                        item
                        for item in await self.database.list_snapshots(
                            session["id"]
                        )
                        if item["name"] == name
                    ),
                    None,
                )
                if existing and not replace:
                    return (
                        "【发现同名存档】\n"
                        f"存档：{existing['name']}\n"
                        f"位置：第 {existing['turn_no']} 回合\n"
                        f"创建时间：{existing['created_at']}\n\n"
                        f"确认覆盖请发送：/酒馆 存档 {name} 覆盖确认"
                    )
                snapshot = await self.database.create_snapshot(
                    session["id"],
                    name,
                    sender_id,
                    replace=replace,
                )
                return (
                    (
                        f"【覆盖成功】{snapshot['name']}"
                        if replace
                        else f"【存档完成】{snapshot['name']}"
                    )
                    + f"\n记录于第 {snapshot['turn_no']} 回合。"
                )
            if command.action == "delete_save":
                if not command.argument:
                    return "【酒馆】格式：/酒馆 删档 <存档名>"
                snapshots = await self.database.list_snapshots(
                    session["id"]
                )
                snapshot = next(
                    (
                        item for item in snapshots
                        if item["id"] == command.argument
                        or item["name"] == command.argument
                    ),
                    None,
                )
                if not snapshot:
                    raise DatabaseNotFoundError("存档不存在")
                await self.database.delete_snapshot(
                    snapshot["id"],
                    sender_id,
                )
                return f"【删档完成】已删除存档「{snapshot['name']}」。"
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
                    "\n会话已暂停；发送 /酒馆 恢复 进入恢复准备大厅。"
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
                    "\n会话已暂停；发送 /酒馆 恢复 进入恢复准备大厅。"
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
