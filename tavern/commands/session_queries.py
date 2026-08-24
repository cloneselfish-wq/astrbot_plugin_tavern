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




class SessionQueriesMixin:
    def __init__(self, gateway: SessionGateway) -> None:
        self.gateway = gateway
    def handles(self, action: str) -> bool:
        """该命令动作是否属于会话命令（供路由分发使用）。"""

        return action in SESSION_ACTIONS
    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult | None:
        if not self.handles(command.action):
            return None
        is_host = ctx.has_role(ROLE_ADMIN) or ctx.has_role(ROLE_HOST)
        is_moderator = is_host or ctx.has_role(ROLE_MODERATOR)

        group_allowed = self.gateway.is_group_allowed(ctx.group_id)
        public_action = group_allowed and (
            command.action in _PLAYER_ACTIONS
            or (command.action == "status" and self.gateway.public_status_enabled())
        )
        privileged_action = is_host and command.action in _HOST_ACTIONS
        if (
            not ctx.has_role(ROLE_ADMIN)
            and not public_action
            and not privileged_action
        ):
            await self.gateway.write_security_audit(
                sender_id=ctx.user_id,
                action=command.action,
                group_id=ctx.group_id,
                platform_id=ctx.platform,
                reason="sender_not_authorized",
            )
            if self.gateway.unauthorized_behavior() == "deny":
                return CommandResult.reply(
                    "【开团】该命令只允许授权管理员使用。"
                )
            return CommandResult(handled=True, text=None)

        auto_bound = False
        if not group_allowed:
            if is_host and command.action == "start":
                try:
                    auto_bound = await self.gateway.allow_group(
                        ctx.group_id,
                        ctx.platform,
                        ctx.user_id,
                    )
                    group_allowed = True
                except ValueError as exc:
                    return CommandResult.reply(f"【开团】无法识别当前群：{exc}")
                except Exception:
                    logger.exception("321开团自动绑定群失败")
                    return CommandResult.reply(
                        "【开团】当前群授权失败，系统没有创建副本。\n"
                        "原因：无法保存当前群的授权信息。\n"
                        "下一步：请在控制台检查群授权配置后重试 /团 开启。"
                    )
            elif command.action not in _UNAUTHORIZED_GROUP_EXEMPT:
                return CommandResult.reply(
                    "【开团】本群尚未授权。请由管理员发送 /团 开启，"
                    "系统会自动完成授权并显示可用世界。"
                )

        try:
            return await self._dispatch(
                ctx,
                command,
                is_host=is_host,
                is_moderator=is_moderator,
                auto_bound=auto_bound,
            )
        except (
            DatabaseNotFoundError,
            InvalidTransitionError,
            PermissionError,
            ValueError,
        ) as exc:
            return CommandResult.reply(self._domain_failure_text(command.action, exc))
    async def _dispatch(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        *,
        is_host: bool,
        is_moderator: bool,
        auto_bound: bool,
    ) -> CommandResult:
        if command.action == "worlds":
            return await self._handle_worlds(ctx)
        if command.action == "instances":
            return await self._handle_instances(ctx, command)

        session = await self.gateway.get_session_by_group(
            ctx.platform,
            ctx.group_id,
        )
        if command.action == "start":
            return await self._handle_start(
                ctx,
                command,
                session,
                auto_bound=auto_bound,
            )
        if command.action == "status":
            return await self._handle_status(ctx, session)
        if session is None:
            return CommandResult.reply(
                "【开团】本群尚未创建会话，请先使用 /团 开启。"
            )
        if str(session.get("state") or "") == SESSION_FINISHED:
            return CommandResult.reply(_ARCHIVED_TEXT)
        if (
            str(session.get("state") or "") == SESSION_PAUSED
            and command.action in _PAUSED_BLOCKED_ACTIONS
        ):
            return CommandResult.reply(_PAUSED_TEXT)

        if command.action == "join":
            return await self._handle_join(ctx, session)
        if command.action == "ai_companions":
            return await self._handle_ai_companions(ctx, command, session)
        if command.action == "ready":
            return await self._handle_ready(ctx, session)
        if command.action == "perform":
            return await self._handle_perform(ctx, command, session)
        if command.action == "leave":
            return await self._handle_leave(ctx, command, session, is_moderator=is_moderator)
        if command.action == "pause":
            return await self._handle_pause(ctx, session)
        if command.action == "cancel_generation":
            return await self._handle_cancel_generation(
                ctx,
                session,
                is_host=is_host,
            )
        if command.action == "retry_turn":
            return await self._handle_retry_turn(
                ctx,
                session,
                is_host=is_host,
            )
        if command.action == "recover":
            return await self._handle_recover(ctx, session)
        if command.action == "resume":
            return await self._handle_resume(ctx, session)
        if command.action == "close":
            return await self._handle_close(ctx, session)
        if command.action == "finish":
            return await self._handle_finish(ctx, command, session)
        if command.action == "abort":
            return await self._handle_abort(ctx, command, session)
        return None
    async def _handle_worlds(self, ctx: RequestContext) -> CommandResult:
        worlds = await self.gateway.list_worlds()
        lines = [
            (
                f"{index}. 《{item['name']}》"
                f" · 推荐 {player_limits(item)['recommended_min']}"
                f"—{player_limits(item)['recommended_max']} 人"
                f" · 上限 {player_limits(item)['maximum']} 人"
            )
            for index, item in enumerate(worlds, start=1)
        ]
        return CommandResult.reply(
            "【可用世界包】\n"
            + ("\n".join(lines) or "暂无可用世界")
            + (
                "\n\n建立副本：/团 开启 <序号或完整世界名>"
                if lines
                else ""
            )
        )
    async def _handle_instances(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult:
        page = parse_instance_list_page(
            command.argument,
            allow_bare_number=True,
        )
        if page is None:
            return CommandResult.reply(
                "【开团】格式：/团 副本列表 [页码]\n"
                "例如：/团 副本列表 2"
            )
        instances = await self.gateway.list_group_sessions(
            ctx.platform,
            ctx.group_id,
        )
        worlds = (
            await self.gateway.list_worlds()
            if not instances
            else None
        )
        return CommandResult.reply(
            format_instance_list(
                instances,
                worlds,
                page=page,
            )
        )
    async def _handle_status(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any] | None,
    ) -> CommandResult:
        if session is None:
            return CommandResult.reply("【开团状态】尚未为本群创建会话。")
        session_id = str(session["id"])
        location = session["world_state"].get("location", "未记录")
        turn = await self.gateway.get_turn_status(session_id)
        roster = await self.gateway.list_roster(session_id)
        vote = await self.gateway.active_vote(session_id)
        choice = await self.gateway.active_choice_set(session_id)
        rules = await self.gateway.get_session_rule_state(session_id)
        progress = rules.get("progress") or {}
        total_milestones = int(progress.get("total_milestones") or 0)
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
            or (
                "已指定，等待角色资料"
                if turn.get("current_user_id")
                else "等待玩家加入"
            )
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
        control = await self.gateway.get_control_state(session_id)
        control_text = (
            "真人主持 · "
            f"{'已指定' if control.get('active_dm_user_id') else '未指定'}"
            f" · 第 {control.get('beat_no', 0)} 段"
            if control.get("mode") == "dm"
            else "AI 自动"
        )
        return CommandResult.reply(
            "【开团状态】\n"
            f"状态：{_SESSION_STATE_LABELS.get(session['state'], '状态异常')}\n"
            f"副本：《{session['instance_name']}》\n"
            f"世界：{session['world_name']}\n"
            f"剧情回合：{session['turn_no']}\n"
            f"多人轮次：第 {turn['round_no']} 轮\n"
            f"当前行动者：{current}\n"
            f"流程：{workflow}\n"
            f"控制模式：{control_text}\n"
            f"角色数：{len(roster)}\n"
            f"章节：{progress.get('chapter') or '未记录'}\n"
            f"当前目标：{progress.get('current_objective') or '未记录'}\n"
            f"里程碑：{progress_text}\n"
            f"行动格式：{self.gateway.trigger_prefix()} A\n"
            f"地点：{location}"
        )
    async def _handle_start(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        session: Mapping[str, Any] | None,
        *,
        auto_bound: bool,
    ) -> CommandResult:
        gateway = self.gateway
        list_page = parse_instance_list_page(command.argument)
        if not command.argument or list_page is not None:
            instances = await gateway.list_group_sessions(
                ctx.platform,
                ctx.group_id,
            )
            worlds = (
                await gateway.list_worlds()
                if not instances
                else None
            )
            prefix = (
                "当前群已完成授权，但尚未启动任何副本。\n"
                if auto_bound
                else ""
            )
            return CommandResult.reply(
                prefix
                + format_instance_list(
                    instances,
                    worlds,
                    page=list_page or 1,
                )
            )

        start_ref = str(command.argument or "").strip()
        group_sessions = await gateway.list_group_sessions(
            ctx.platform,
            ctx.group_id,
        )
        selected, start_ref = await self._select_instance(
            start_ref,
            group_sessions,
            gateway,
            ctx,
        )
        created = not selected or bool(
            selected
            and selected.get("state") == SESSION_FINISHED
        )
        if not selected:
            session = await gateway.ensure_session(
                ctx.platform,
                ctx.group_id,
                ctx.origin,
                start_ref,
                ctx.user_id,
            )
        elif selected.get("state") == SESSION_FINISHED:
            session = await gateway.ensure_session(
                ctx.platform,
                ctx.group_id,
                ctx.origin,
                str(selected["world_id"]),
                ctx.user_id,
                str(selected["instance_slug"]),
                str(selected["instance_name"]),
            )
        else:
            session = selected

        if created:
            created_world = await gateway.get_world(
                str(session["world_id"])
            )
            world_rules = created_world.get("rules") or {}
            world_time = (
                world_rules.get("time_rules")
                if isinstance(world_rules, Mapping)
                else {}
            )
            merged_time_rules = normalize_time_rules(
                {
                    **dict(gateway.time_rules()),
                    **(
                        dict(world_time)
                        if isinstance(world_time, Mapping)
                        else {}
                    ),
                }
            )
            await gateway.save_instance_time_rules(
                session["id"],
                merged_time_rules,
                ctx.user_id,
            )
        elif int(session.get("turn_no") or 0) > 0:
            await gateway.pause_session_timers(
                session["id"],
                ctx.user_id,
            )

        session = await gateway.transition_session(
            session["id"],
            SESSION_PREPARING,
            ctx.user_id,
        )
        await gateway.grant_permission(
            session["id"],
            ctx.user_id,
            "host",
            ctx.user_id,
        )
        instance = await gateway.get_instance_config(session["id"])
        world = instance["world_snapshot"]
        limits = player_limits(world)
        roster = await gateway.list_roster(session["id"])
        summary = str(
            session["world_state"].get("scene_summary")
            or world.get("description")
            or "尚无剧情回顾"
        )
        deliveries = (
            DeliveryIntent(
                INTENT_BROKER_EVENT,
                {
                    "type": "session",
                    "action": "prepare",
                    "hook": "session_created",
                    "session_id": session["id"],
                },
            ),
        )
        return CommandResult.reply(
            f"【开团已开启】《{session['instance_name']}》"
            "\n当前阶段：准备中（故事尚未推进）"
            f"\n世界：{session['world_name']}"
            f"\n推荐人数：{limits['recommended_min']}"
            f"—{limits['recommended_max']} 人"
            f" · 最低 {limits['minimum_start']} 人"
            f" · 强制上限 {limits['maximum']} 人"
            + (
                "\n已自动将当前群加入允许群列表。"
                if auto_bound
                else ""
            )
            + "\n\n"
            + format_gameplay_brief(world)
            + f"\n\n【故事回顾】{summary}"
            + "\n\n"
            + format_roster(roster)
            + (
                (
                    "\n\n这是已有剧情进度的副本，暂停时的对话、"
                    "行动者、投票与选项均已保留。"
                    "\n全员确认准备后，主持人发送 /团 继续；"
                    "不要使用 /团 开演。"
                )
                if int(session.get("turn_no") or 0) > 0
                else "\n\n" + format_preparation_steps()
            ),
            delivery=deliveries,
        )
