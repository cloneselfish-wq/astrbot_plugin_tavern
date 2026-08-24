"""D1-ARC-001 会话命令应用层（平台无关，不持有平台事件对象）。

本模块把 main.py 中「世界列表 / 副本列表 / 状态 / 开启 / 加入 /
准备 / 开演 / 退出 / 暂停 / 恢复 / 继续 / 关闭 / 完结 / 强制终止」会话
生命周期分支抽为纯应用服务：

- 输入：``RequestContext``（``tavern/runtime/request.py`` 权威定义）、
  ``ParsedCommand``（已解析命令）、``SessionGateway``（数据与配置依赖
  协议）；
- 输出：``CommandResult``（``tavern/commands/models.py`` 权威定义）：
  外显文本 + 投递意图（delivery）+ 是否已消费（handled）；
- 所有写操作只编排 gateway 协议方法，本模块不写 SQL、不 import
  任何宿主平台类、不持有平台事件对象；玩家无法通过命令文本宣布或伪造
  状态变化，一切权威状态变化由仓储层在事务内完成。

入口适配层（main.py 路由接线时）职责：

1. 从平台事件构建 ``RequestContext``（``tavern/entry/event_context.py``），
   注入 ``roles``（admin/host/moderator/player）与 ``session_id``；
2. 调用 :meth:`SessionCommandService.handle`；返回 ``None`` 表示本服务
   不处理该动作，``CommandResult(handled=True, text=None)`` 表示已消费
   但无需回复（未授权且配置为静默忽略）；
3. 回复 ``text`` 并分发 ``delivery`` 意图：
   - ``broker_event``：按 payload 发布副本事件（如 session prepare）；
   - ``sync_group_target``：保存当前群真实投递目标（join）；
   - ``revoke_private_target``：私聊来源丢失 / 退场时降级已验证目标。
4. gateway 中的适配方法（``send_card_code_private`` /
   ``validate_world_runtime`` / ``release_session_lock``）由宿主实现，
   其返回结果直接影响 join / perform / resume 的回复文案。

``SESSION_ACTIONS`` 是本服务负责的命令集合（唯一来源；路由接线时用本
常量替换 main.py 中的对应分发分支，避免双源）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol, runtime_checkable

from ..constants import (
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from ..database import DatabaseNotFoundError, InvalidTransitionError
from ..lifecycle import format_choices, normalize_time_rules, player_limits
from ..presentation import (
    format_instance_list,
    format_recovered_timer,
    format_roster,
    format_vote,
    parse_instance_list_page,
)
from ..runtime.request import (
    ROLE_ADMIN,
    ROLE_HOST,
    ROLE_MODERATOR,
    RequestContext,
)
from ..security import ParsedCommand

from .models import (
    INTENT_REVOKE_PRIVATE_TARGET,
    CommandResult,
    DeliveryIntent,
)

logger = logging.getLogger(__name__)


def format_gameplay_brief(world: Mapping[str, Any]) -> str:
    """Render the frozen public gameplay contract without inventing details."""

    brief = world.get("gameplay_brief")
    if not isinstance(brief, Mapping):
        return "【副本玩法】\n作者未提供玩法说明。开演前请向主持人确认规则与风险。"
    tone = str(brief.get("tone") or "").strip()
    core_loop = str(brief.get("core_loop") or "").strip()
    special_rules = [
        str(item or "").strip()
        for item in brief.get("special_rules", [])
        if str(item or "").strip()
    ] if isinstance(brief.get("special_rules"), list) else []
    recommended = str(brief.get("recommended_for") or "").strip()
    lines = ["【副本玩法】"]
    lines.extend(item for item in (tone, core_loop) if item)
    lines.extend(f"- {item}" for item in special_rules)
    if recommended:
        lines.append(f"适合：{recommended}")
    if len(lines) == 1:
        lines.append("作者未提供玩法说明。开演前请向主持人确认规则与风险。")
    return "\n".join(lines)


def format_preparation_steps() -> str:
    """Render a compact, high-contrast preparation flow for chat clients."""

    return (
        "【接下来怎么做】"
        "\n\n① 玩家加入"
        "\n/团 加入"
        "\n按私聊提示完成角色卡。"
        "\n\n② 玩家准备"
        "\n/团 准备"
        "\n\n③ 可选：添加 AI 队友（主持人）"
        "\n/团 AI队友 2 确认"
        "\n\n④ 开始故事（主持人）"
        "\n/团 开演"
        "\n系统不会自动开演。"
    )

# 会话命令集合（唯一来源）。「世界列表 / 副本列表」是会话的列表入口，
# 「加入」同时覆盖重加入（席位归档后再次占位）路径。
SESSION_ACTIONS = frozenset(
    {
        "worlds",
        "instances",
        "status",
        "start",
        "join",
        "ai_companions",
        "ready",
        "perform",
        "leave",
        "pause",
        "cancel_generation",
        "retry_turn",
        "recover",
        "resume",
        "close",
        "finish",
        "abort",
    }
)

# 投递意图种类（入口适配层按 kind 分派到对应平台操作）。
INTENT_BROKER_EVENT = "broker_event"
INTENT_SYNC_GROUP_TARGET = "sync_group_target"

# 需要主持人权限的动作。
_HOST_ACTIONS = frozenset(
    {
        "worlds",
        "instances",
        "start",
        "ai_companions",
        "perform",
        "pause",
        "retry_turn",
        "recover",
        "resume",
        "close",
        "finish",
        "abort",
    }
)

# 普通玩家在已授权群可直接执行的动作。
_PLAYER_ACTIONS = frozenset(
    {"join", "ready", "leave", "cancel_generation", "retry_turn"}
)

# 未授权群时仍放行查看的动作（与 main.py 行为一致）。
_UNAUTHORIZED_GROUP_EXEMPT = frozenset({"help", "unknown", "worlds", "instances"})

# 暂停态拦截的玩法动作（与 main.py 暂停守卫一致）。
_PAUSED_BLOCKED_ACTIONS = frozenset({"join", "ready", "leave", "perform"})

_SESSION_STATE_LABELS = {
    SESSION_CLOSED: "已关闭",
    SESSION_PREPARING: "准备中",
    SESSION_RUNNING: "运行中",
    SESSION_PAUSED: "已暂停",
    SESSION_FINISHED: "已完结",
    SESSION_MAINTENANCE: "维护中",
}

_OPERATION_LABELS = {
    "worlds": "查看世界列表",
    "instances": "查看副本列表",
    "status": "查看副本状态",
    "start": "开启副本",
    "join": "加入副本",
    "ai_companions": "配置 AI 队友",
    "ready": "准备",
    "perform": "开演",
    "leave": "退出副本",
    "pause": "暂停副本",
    "cancel_generation": "取消本轮生成",
    "retry_turn": "重试本轮",
    "recover": "进入恢复准备大厅",
    "resume": "继续剧情",
    "close": "关闭副本",
    "finish": "完结副本",
    "abort": "强制终止副本",
}

_DOMAIN_RECOVERY = {
    "worlds": "重新发送 /团 世界列表。",
    "instances": "重新发送 /团 副本列表。",
    "status": "重新发送 /团 状态。",
    "start": "重新发送 /团 开启 <副本标识>，或查看 /团 帮助。",
    "join": "确认本群已开团并处于准备阶段后，重新发送 /团 加入。",
    "ai_companions": "准备阶段发送 /团 AI队友 <数量> <确认|自动|暂停>。",
    "ready": "确认角色卡已通过审核后，重新发送 /团 准备。",
    "perform": "确认全员准备完成后，重新发送 /团 开演。",
    "leave": "重新发送 /团 退出；如提示无权限，请联系主持人处理。",
    "pause": "确认副本状态后重试；如需恢复请发送 /团 恢复。",
    "cancel_generation": "确认本轮仍在生成后发送 /团 取消。",
    "retry_turn": "确认表决结果或本轮操作仍可恢复后发送 /团 重试本轮。",
    "recover": "确认副本状态后重试；如仍无法恢复请联系主持人。",
    "resume": "确认已进入恢复准备大厅后，重新发送 /团 继续。",
    "close": "确认副本状态后重新发送 /团 关闭。",
    "finish": "确认无误后重新发送 /团 完结 确认。",
    "abort": "确认无误后重新发送 /团 强制终止 确认 <原因>。",
}

_ARCHIVED_TEXT = (
    "【开团】该副本已经完结并永久归档为只读，本轮命令未执行。\n"
    "下一步：查看 /团 回顾 或 /团 顺序 查看最终档案；"
    "新冒险由管理员重新开启。"
)

_PAUSED_TEXT = (
    "【开团】剧情已暂停，暂不可进行选项 / 投票 / 行动 / 建卡等玩法操作。\n"
    "请先由主持人发送 /团 恢复 进入恢复准备大厅，"
    "再发送 /团 继续 续演。"
)


@runtime_checkable
class SessionGateway(Protocol):
    """会话命令对适配层的数据与配置依赖协议（宿主装配时由
    ``TavernDatabase`` 与引擎/投递能力组合实现）。

    只包含本服务使用到的方法；签名与 ``tavern/repositories`` 现有公开
    方法一致，宿主接入时自然满足。
    """

    # ── 配置读取 ──────────────────────────────────────────────
    def is_group_allowed(self, group_id: str) -> bool: ...

    def public_status_enabled(self) -> bool: ...

    def unauthorized_behavior(self) -> str: ...

    def trigger_prefix(self) -> str: ...

    def time_rules(self) -> Mapping[str, Any]: ...

    # ── 安全审计 ──────────────────────────────────────────────
    async def write_security_audit(
        self,
        *,
        sender_id: str,
        action: str,
        group_id: str,
        platform_id: str,
        reason: str,
    ) -> None: ...

    # ── 会话 / 世界读取 ────────────────────────────────────────
    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> Mapping[str, Any] | None: ...

    async def list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[Mapping[str, Any]]: ...

    async def list_worlds(self) -> list[Mapping[str, Any]]: ...

    async def get_world(self, world_ref: str) -> Mapping[str, Any]: ...

    async def get_instance_config(self, session_id: str) -> Mapping[str, Any]: ...

    async def get_session_rule_state(
        self,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    async def get_control_state(self, session_id: str) -> Mapping[str, Any]: ...

    async def get_turn_status(self, session_id: str) -> Mapping[str, Any]: ...

    async def list_roster(self, session_id: str) -> list[Mapping[str, Any]]: ...

    async def list_ai_companions(self, session_id: str) -> Mapping[str, Any]: ...

    async def configure_ai_companions(
        self,
        *,
        session_id: str,
        count: int,
        mode: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    async def active_vote(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def active_choice_set(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[Mapping[str, Any]]: ...

    async def list_timers(self, session_id: str) -> list[Mapping[str, Any]]: ...

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
    ) -> Mapping[str, Any]: ...

    # ── 会话写操作（宿主接入 TavernDatabase；本服务只编排） ────
    async def ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str = "",
        instance_name: str = "",
    ) -> Mapping[str, Any]: ...

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def save_instance_time_rules(
        self,
        session_id: str,
        rules: Mapping[str, Any],
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> Mapping[str, Any]: ...

    async def set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool,
    ) -> Mapping[str, Any]: ...

    async def retire_self(
        self,
        session_id: str,
        user_id: str,
    ) -> Mapping[str, Any]: ...

    async def retire_participant(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        forced: bool,
        reason: str,
    ) -> Mapping[str, Any]: ...

    async def pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int: ...

    async def active_session_operation(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def latest_retryable_session_operation(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def cancel_and_wait_operation(
        self,
        session_id: str,
        actor_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Mapping[str, Any]: ...

    async def retry_session_operation(
        self,
        session_id: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int: ...

    async def activate_story(
        self,
        session_id: str,
        actor_id: str,
        *,
        resume: bool,
    ) -> Mapping[str, Any]: ...

    async def opening_decision(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def prepare_opening_decision(
        self,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    async def override_opening_decision(
        self,
        session_id: str,
        scene_ref: str,
        principal_ref: str,
        expected_revision: int,
    ) -> Mapping[str, Any]: ...

    async def finalize_session(
        self,
        session_id: str,
        actor_id: str,
        *,
        termination_type: str,
        reason: str,
    ) -> Mapping[str, Any]: ...

    async def opening_preflight(self, session_id: str) -> Mapping[str, Any]: ...

    async def allow_group(
        self,
        group_id: str,
        platform_id: str,
        actor_id: str,
    ) -> bool: ...

    # ── 引擎 / 投递适配（结果影响回复文案） ────────────────────
    async def send_card_code_private(
        self,
        session_id: str,
        participant: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def validate_world_runtime(self, world_snapshot: Mapping[str, Any]) -> None: ...

    async def release_session_lock(self, session_id: str) -> None: ...




class SessionLifecycleMixin:
    async def _select_instance(
        self,
        start_ref: str,
        group_sessions: list[Mapping[str, Any]],
        gateway: SessionGateway,
        ctx: RequestContext,
    ) -> tuple[Mapping[str, Any] | None, str]:
        """按序号 / 完整副本名 / 副本引用解析，退回世界序号 / 世界名。

        返回 ``(选中的副本, 解析后的世界引用)``；未选中副本时第二项为
        世界 ID（供 ``ensure_session`` 使用），否则为原引用。
        """

        selected = None
        if start_ref.isdigit():
            selection = int(start_ref)
            if 1 <= selection <= len(group_sessions):
                selected = group_sessions[selection - 1]
        if selected is None:
            name_matches = [
                item
                for item in group_sessions
                if str(item.get("instance_name") or "").strip()
                == start_ref
            ]
            if len(name_matches) == 1:
                selected = name_matches[0]
            elif len(name_matches) > 1:
                raise ValueError(
                    "当前群有多个同名副本，请使用副本列表中的序号。"
                )
        if selected is None:
            selected = await gateway.get_session_by_group_ref(
                ctx.platform,
                ctx.group_id,
                start_ref,
            )
        if selected is None:
            worlds = await gateway.list_worlds()
            if start_ref.isdigit():
                selection = int(start_ref)
                if 1 <= selection <= len(worlds):
                    start_ref = str(worlds[selection - 1]["id"])
            else:
                world_matches = [
                    world
                    for world in worlds
                    if str(world.get("name") or "").strip()
                    == start_ref
                ]
                if len(world_matches) == 1:
                    start_ref = str(world_matches[0]["id"])
                elif len(world_matches) > 1:
                    raise ValueError(
                        "存在多个同名世界，请使用世界列表中的序号。"
                    )
        return selected, start_ref
    async def _handle_ai_companions(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        session: Mapping[str, Any],
    ) -> CommandResult:
        session_id = str(session.get("id") or "")
        argument = str(command.argument or "").strip()
        current = await self.gateway.list_ai_companions(session_id)
        items = list(current.get("items") or [])
        if not argument:
            mode_label = {
                "confirm": "主持人确认",
                "automatic": "自动行动",
                "paused": "暂停",
            }.get(str(current.get("mode") or ""), "尚未配置")
            return CommandResult.reply(
                "【AI 队友】\n"
                f"当前数量：{len(items)} 名\n"
                f"行动模式：{mode_label}\n\n"
                "说明：AI 队友使用世界包提供的角色预设，不计入最低开团人数。\n"
                "下一步：准备阶段可发送\n"
                "/团 AI队友 2 确认\n"
                "也可把“确认”改为“自动”或“暂停”；数量 0 表示移除。"
            )
        if str(session.get("state") or "") != SESSION_PREPARING:
            raise InvalidTransitionError(
                "只能在准备阶段调整 AI 队友数量和行动模式"
            )
        parts = argument.split()
        try:
            count = int(parts[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError(
                "格式应为 /团 AI队友 <0—8> <确认|自动|暂停>"
            ) from exc
        if count < 0 or count > 8:
            raise ValueError("AI 队友数量必须是 0—8 的整数")
        mode_text = parts[1] if len(parts) > 1 else "确认"
        mode = {
            "确认": "confirm",
            "主持确认": "confirm",
            "confirm": "confirm",
            "自动": "automatic",
            "automatic": "automatic",
            "暂停": "paused",
            "paused": "paused",
        }.get(mode_text.lower())
        if not mode:
            raise ValueError("行动模式只支持确认、自动或暂停")
        revision = int(session.get("revision") or 0)
        result = await self.gateway.configure_ai_companions(
            session_id=session_id,
            count=count,
            mode=mode,
            expected_session_revision=revision,
            idempotency_key=(
                f"bot-ai:{session_id}:{revision}:{count}:{mode}"
            ),
        )
        configured = list(result.get("items") or [])
        mode_label = {
            "confirm": "主持人确认",
            "automatic": "自动行动",
            "paused": "暂停",
        }[mode]
        return CommandResult.reply(
            "【AI 队友配置完成】\n"
            f"结果：当前共 {len(configured)} 名 AI 队友，模式为“{mode_label}”。\n"
            "自动处理：队友角色资料、行动顺序与投票策略已按当前世界保存。\n"
            "下一步：玩家完成建卡与准备后，主持人发送\n"
            "/团 开演"
        )
    async def _handle_join(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
    ) -> CommandResult:
        sender_name = str(ctx.metadata.get("sender_name") or "").strip()
        if not sender_name:
            return CommandResult.reply(
                "【加入失败】\n"
                "失败操作：为你预留副本席位。\n"
                "原因：平台没有提供可公开显示的群昵称或显示名。\n"
                "自动处理：系统没有写入席位、角色资料或账号标识。\n"
                "下一步：请先设置群昵称或平台显示名，再发送\n"
                "/团 加入"
            )
        result = await self.gateway.reserve_participant(
            session["id"],
            ctx.user_id,
            sender_name,
        )
        deliveries = [
            DeliveryIntent(
                INTENT_SYNC_GROUP_TARGET,
                {"session_id": session["id"]},
            )
        ]
        if not str(result.get("private_origin") or "").strip():
            # 重加入（席位归档后再次占位）会清除私聊来源：
            # 旧已验证目标必须同步降级，避免后续误投。
            deliveries.append(
                DeliveryIntent(
                    INTENT_REVOKE_PRIVATE_TARGET,
                    {
                        "platform_id": ctx.platform,
                        "user_id": ctx.user_id,
                        "reason": "rejoin_unbound",
                    },
                )
            )
        if result.get("binding_code"):
            title = (
                "【建卡码已自动补发】"
                if result.get("binding_code_reissued")
                else "【席位已预留】"
            )
            outcome = await self.gateway.send_card_code_private(
                session["id"],
                result,
            )
            delivery_line = (
                "建卡入口已发送到你的私聊。"
                if outcome.get("ok")
                else "建卡入口已进入待投递队列，系统会在后台继续重试。"
            )
            return CommandResult.reply(
                f"{title}\n"
                f"{delivery_line}\n"
                "群聊不会显示建卡码。\n\n"
                "如果暂未收到：先主动打开与 Bot 的私聊并发送任意消息，"
                "再回群发送 /团 建卡 重试；"
                "仍失败时请主持人在 WebUI 的待投递面板重试。",
                delivery=tuple(deliveries),
            )
        return CommandResult.reply(
            "【你已加入当前副本】\n"
            f"角色卡：{result.get('card_status')}"
            f" · 状态：{result.get('participation_status')}\n"
            "如已通过审核，请发送 /团 准备。",
            delivery=tuple(deliveries),
        )
    async def _handle_ready(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
    ) -> CommandResult:
        participant = await self.gateway.set_participant_ready(
            session["id"],
            ctx.user_id,
            True,
        )
        preflight = await self.gateway.opening_preflight(
            session["id"]
        )
        waiting = int(
            preflight.get("blocker_count")
            or len(preflight.get("blockers") or [])
        )
        suffix = (
            (
                "\n【全员准备完成】主持人现在可以发送 /团 继续"
                if preflight.get("resume_mode")
                else "\n【全员准备完成】主持人现在可以发送 /团 开演"
            )
            if preflight["ok"]
            else f"\n当前仍有 {waiting} 项准备阻塞。"
        )
        return CommandResult.reply(
            f"【{participant.get('character_name') or participant.get('display_name')} 已准备】"
            + suffix
        )
    async def _handle_perform(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        session: Mapping[str, Any],
    ) -> CommandResult:
        instance_config = await self.gateway.get_instance_config(
            session["id"]
        )
        await self.gateway.validate_world_runtime(
            instance_config["world_snapshot"]
        )
        requested = str(command.argument or "").strip()
        opening: Mapping[str, Any] = {}
        if requested:
            opening = await self.gateway.prepare_opening_decision(session["id"])
            candidates = list(opening.get("candidates") or [])
            selected = None
            if requested.isdigit():
                position = int(requested)
                if 1 <= position <= len(candidates):
                    selected = candidates[position - 1]
            if selected is None:
                requested_key = requested.casefold()
                selected = next(
                    (
                        item
                        for item in candidates
                        if requested_key
                        in {
                            str(item.get("label") or "").strip().casefold(),
                            str(item.get("option_ref") or "").strip().casefold(),
                        }
                    ),
                    None,
                )
            if selected is None:
                options = "\n\n".join(
                    f"{index}. {item.get('label')}\n"
                    f"{item.get('reason') or '适合当前队伍。'}"
                    for index, item in enumerate(candidates, start=1)
                )
                return CommandResult.reply(
                    "【开局未修改】\n"
                    "原因：没有找到你指定的开局名称或序号。\n"
                    "自动处理：系统保留了当前推荐，没有开演。\n"
                    "可选开局：\n\n"
                    + (options or "当前世界没有可选开局。")
                    + "\n\n下一步命令：\n/团 开演 <序号或完整名称>"
                )
            opening = await self.gateway.override_opening_decision(
                session["id"],
                str(selected.get("scene_ref") or ""),
                ctx.user_id,
                int(opening.get("revision") or 0),
            )
        result = await self.gateway.activate_story(
            session["id"],
            ctx.user_id,
            resume=False,
        )
        if not result["started"]:
            return CommandResult.reply(
                "【暂时无法开演】\n· "
                + "\n· ".join(
                    result.get("blocker_messages")
                    or ["准备尚未完成"]
                )
            )
        opening = result.get("opening_decision") or opening
        current = result["current_participant"]
        selected_opening = opening.get("selected") or {}
        return CommandResult.reply(
            f"【故事正式开演】{session['instance_name']}\n"
            f"开局：《{selected_opening.get('label') or '故事序章'}》\n"
            f"选择理由：{selected_opening.get('reason') or '适合当前队伍。'}\n"
            "出场角色："
            + "、".join(
                item.get("character_name") or item.get("display_name")
                for item in result["participants"]
            )
            + f"\n当前行动者："
            f"{current.get('character_name') or current.get('display_name')}"
            + "\n\n"
            + format_gameplay_brief(instance_config["world_snapshot"])
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
                trigger_prefix=self.gateway.trigger_prefix(),
            )
        )
