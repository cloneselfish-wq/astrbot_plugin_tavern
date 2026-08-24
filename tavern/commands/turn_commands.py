"""D1-ARC-001 回合/行动命令应用层（不持有 AstrBot 事件对象）。

本模块把 main.py 中「选择 / 灵感 / 灵感重投 / 重整选项 / 全队 / 技能 /
顺序 / 跳过 / 强制下一位」命令分支抽为纯应用服务：

- 输入：RequestContext（平台无关上下文）、ParsedCommand（已解析命令）、
  TurnGateway（只读查询协议）；
- 输出：CommandResult（外显文本，或结构化 engine 调用请求）；
- 不 import 任何 AstrBot 消息类；不直接写 Repository；
  一切状态写入（engine 调用、投票落票、兜底恢复）都以结构化请求交给
  适配层执行，玩家无法通过命令文本宣布或伪造结果。

本文件同时承载命令应用层共享契约（RequestContext / CommandResult /
EngineRequest / VoteCastRequest / FallbackRequest / CommandError /
session_guard），供 tavern/commands/vote_commands.py 复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..constants import (
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from ..copy.entities import decorate_entity
from ..database import DatabaseNotFoundError
from ..lifecycle import format_choices, parse_choice_input
from ..runtime.contracts import CommandError, CommandResult
from ..runtime.request import RequestContext
from .presentation import (
    format_roster,
    format_turn_status,
    format_vote,
)
from ..security import ParsedCommand


@dataclass(frozen=True, slots=True)
class EngineRequest:
    """结构化 engine 调用请求；适配层补充平台事件后执行。"""

    method: str
    params: dict[str, Any]
    render: str = "engine_reply"
    render_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VoteCastRequest:
    """结构化投票请求；适配层交给投票仓储执行。"""

    session_id: str
    user_id: str
    option_key: str


@dataclass(frozen=True, slots=True)
class FallbackRequest:
    """engine 推进失败后的结构化兜底恢复请求。"""

    kind: str
    params: dict[str, Any]
    audit_action: str = ""


class TurnGateway(Protocol):
    """回合命令只读查询协议（由适配层接入 TavernDatabase）。"""

    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_turn_status(self, session_id: str) -> Mapping[str, Any]: ...

    async def active_choice_set(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def active_vote(self, session_id: str) -> Mapping[str, Any] | None: ...

    async def inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> Mapping[str, Any]: ...

    async def list_roster(self, session_id: str) -> list[Mapping[str, Any]]: ...

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
    ) -> Mapping[str, Any]: ...

    async def authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> Mapping[str, Any]: ...


def session_guard(
    session: Mapping[str, Any] | None,
    *,
    allow_preparing: bool = False,
    allow_archived_read: bool = False,
) -> CommandError | None:
    """副本状态守卫：未开馆 / 归档只读 / 维护 / 暂停 / 未开演。"""
    if session is None or str(session.get("state") or "") == SESSION_CLOSED:
        return CommandError(
            "session.missing",
            operation="执行跑团操作",
            reason="当前群还没有可用副本。",
            automatic_action="系统没有创建副本，也没有写入本次操作。",
            next_command="/团 开启",
        )
    state = str(session.get("state") or "")
    if state == SESSION_FINISHED:
        if allow_archived_read:
            return None
        return CommandError(
            "session.archived",
            operation="修改已归档副本",
            reason="这个故事已经结束并进入只读归档。",
            automatic_action="系统没有改动最终档案，也没有重新开启旧回合。",
            next_command="/团 回顾",
        )
    if state == SESSION_MAINTENANCE:
        return CommandError(
            "session.maintenance",
            operation="执行跑团操作",
            reason="副本正在维护，暂时不能写入新的玩法结果。",
            automatic_action="本次输入没有写入，已有进度仍然保留。",
            next_command="/团 当前",
            retryable=True,
        )
    if state == SESSION_PAUSED:
        return CommandError(
            "session.paused",
            operation="在暂停期间推进故事",
            reason="副本仍处于暂停状态，选项、投票和行动都被冻结。",
            automatic_action="系统保留了角色卡、当前行动权、投票和计时状态。",
            next_command="/团 恢复",
        )
    if state == SESSION_PREPARING and not allow_preparing:
        return CommandError(
            "session.preparing",
            operation="在准备阶段提交回合操作",
            reason="故事尚未开演，当前只接受建卡和准备确认。",
            automatic_action="本次回合输入没有写入；已完成的角色资料仍然保留。",
            next_command="/团 准备",
        )
    if state != SESSION_RUNNING and not (
        allow_preparing and state == SESSION_PREPARING
    ):
        return CommandError(
            "session.not_running",
            operation="推进当前副本",
            reason="副本当前不在可推进状态。",
            automatic_action="系统没有写入本次操作。",
            next_command="/团 当前",
        )
    return None


def parse_option_argument(argument: str) -> tuple[str, str]:
    """解析 A/B/C/D 选项；非法输入抛 ValueError（沿用领域文案）。"""
    return parse_choice_input(argument)


@dataclass(frozen=True, slots=True)
class SkillTarget:
    skill_name: str
    target_ref: str
    action_note: str


def parse_skill_argument(argument: str) -> SkillTarget | None:
    """解析「技能 <名称> [对 <目标>|给 <目标>|...] [动作说明]」。"""
    text = str(argument or "").strip()
    if not text:
        return None
    parts = text.split(maxsplit=1)
    skill_name = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    target_ref = ""
    action_note = rest
    for prefix in ("对 ", "给 ", "医治 ", "治疗 "):
        if rest.startswith(prefix):
            target_ref = rest[len(prefix):].strip()
            action_note = ""
            break
    return SkillTarget(
        skill_name=skill_name,
        target_ref=target_ref,
        action_note=action_note,
    )


def team_index_from_argument(argument: str) -> int:
    """「全队 2」/「全队」→ 全队行动候选项下标（0 基）。"""
    text = str(argument or "").strip()
    if text.isdigit():
        value = int(text)
        return max(0, value - 1) if value >= 1 else 0
    return 0


def story_parts(reply: Mapping[str, Any]) -> list[str]:
    """把 EngineReply 投影为分段外显文本（故事段 / 回合段）。"""
    parts = [
        str(reply.get("story_text") or "").strip(),
        str(reply.get("turn_text") or "").strip(),
    ]
    parts = [part for part in parts if part]
    if not parts:
        text = str(reply.get("text") or "").strip()
        if text:
            parts = [text]
    return parts


async def _choice_guard(
    ctx: RequestContext,
    session_id: str,
    gateway: TurnGateway,
    *,
    permission: str,
    no_options_message: str,
    order_code: str,
    order_message: str,
) -> tuple[CommandError | None, Mapping[str, Any] | None]:
    """选项阶段守卫：无选项 / 无行动角色 / 非当前行动者（含代控校验）。"""
    choice_set = await gateway.active_choice_set(session_id)
    if not choice_set:
        vote = await gateway.active_vote(session_id)
        if vote:
            return (
                CommandError(
                    "vote.in_progress",
                    operation="提交个人行动",
                    reason="当前正在进行全队表决，个人行动暂时锁定。",
                    automatic_action="本次输入没有写入，也没有改变任何票数。",
                    next_command="/团 投票 A",
                ),
                None,
            )
        return (
            CommandError(
                "turn.no_options",
                operation="选择本轮行动",
                reason=no_options_message,
                automatic_action="系统没有提交行动，也没有消耗资源。",
                next_command="/团 当前",
            ),
            None,
        )
    participant = choice_set.get("participant")
    if not participant:
        return (
            CommandError(
                "turn.no_options",
                operation="选择本轮行动",
                reason=no_options_message,
                automatic_action="系统没有提交行动，也没有消耗资源。",
                next_command="/团 当前",
            ),
            None,
        )
    control = await gateway.authorize_participant_control(
        session_id,
        participant["id"],
        ctx.user_id,
        permission,
    )
    if not control.get("authorized"):
        owner_name = str(
            participant.get("character_name")
            or participant.get("display_name")
            or ""
        ).strip()
        if "{owner}" in order_message and not owner_name:
            return (
                CommandError(
                    f"{order_code}.actor_name_missing",
                    operation="提交本轮行动",
                    reason=(
                        "当前行动者缺少可公开显示的角色名称，"
                        "系统无法安全确认行动归属。"
                    ),
                    automatic_action=(
                        "本次输入没有写入，也没有显示内部账号标识。"
                    ),
                    next_command="/团 当前",
                ),
                None,
            )
        owner = decorate_entity("character", owner_name)
        return (
            CommandError(
                order_code,
                operation="提交本轮行动",
                reason=order_message.format(owner=owner)
                .replace("【回合秩序】", "")
                .replace("【开团】", "")
                .strip(),
                automatic_action="本次输入没有写入，也没有改变行动顺序。",
                next_command="/团 当前",
            ),
            None,
        )
    return None, choice_set


class TurnCommandHandler:
    """回合/行动命令应用服务：只编排，不持有平台对象、不直接写库。"""

    ACTIONS = frozenset(
        {
            "choose",
            "inspiration",
            "inspiration_reroll",
            "reroll",
            "team",
            "skill",
            "order",
            "skip",
            "next",
        }
    )

    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        gateway: TurnGateway,
    ) -> CommandResult | None:
        if command.action not in self.ACTIONS:
            return None
        requires_public_name = command.action in {
            "choose",
            "inspiration_reroll",
            "team",
            "skill",
        } or (command.action == "inspiration" and bool(command.argument))
        if requires_public_name and not str(ctx.user_name or "").strip():
            return CommandResult(
                error=CommandError(
                    "turn.actor_name_missing",
                    operation="提交角色行动",
                    reason="平台没有提供可公开显示的玩家名称。",
                    automatic_action=(
                        "系统未记录本次行动，也没有用账号标识代替名称。"
                    ),
                    next_command="/团 当前",
                )
            )
        session = await gateway.get_session_by_group(
            ctx.platform_id,
            ctx.group_id,
        )
        if command.action == "order":
            guard = session_guard(
                session,
                allow_preparing=True,
                allow_archived_read=True,
            )
        else:
            guard = session_guard(session)
        if guard:
            return CommandResult(error=guard)
        assert session is not None
        if command.action == "order":
            return await self._handle_order(ctx, session, gateway)
        if command.action == "choose":
            return await self._handle_choose(ctx, session, gateway, command.argument)
        if command.action in {"inspiration", "inspiration_reroll"}:
            return await self._handle_inspiration(ctx, session, gateway, command)
        if command.action == "reroll":
            return await self._handle_reroll(ctx, session, gateway)
        if command.action == "team":
            return await self._handle_team(ctx, session, gateway, command.argument)
        if command.action == "skill":
            return await self._handle_skill(ctx, session, gateway, command.argument)
        if command.action == "skip":
            return await self._handle_skip(ctx, session, gateway, command.argument)
        if command.action == "next":
            return await self._handle_next(ctx, session, gateway)
        return None

    async def _handle_choose(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
        argument: str,
    ) -> CommandResult:
        try:
            key, flavor = parse_option_argument(argument)
        except ValueError as exc:
            return CommandResult(
                error=CommandError(
                    "turn.option.invalid",
                    f"【开团】{exc}",
                    "下一步：发送 /团 选择 A（或 B/C/D）。",
                )
            )
        error, _ = await _choice_guard(
            ctx,
            session["id"],
            gateway,
            permission="choose",
            no_options_message="当前没有可选择的行动选项",
            order_code="turn.order",
            order_message="【回合秩序】当前选项属于 {owner}，本条内容未记录。",
        )
        if error:
            return CommandResult(error=error)
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="process_choice",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                        "sender_name": ctx.user_name,
                        "choice_key": key,
                        "flavor_text": flavor,
                        "inspiration_mode": "",
                    },
                    render="engine_reply",
                )
            ],
        )

    async def _handle_inspiration(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
        command: ParsedCommand,
    ) -> CommandResult:
        session_id = session["id"]
        if command.action == "inspiration" and not command.argument:
            try:
                status = await gateway.inspiration_status(
                    session_id,
                    ctx.user_id,
                )
            except (DatabaseNotFoundError, KeyError):
                return CommandResult(
                    error=CommandError(
                        "turn.no_character",
                        "【开团】当前玩家没有副本角色。",
                        "下一步：先加入副本并完成角色卡。",
                    )
                )
            return CommandResult(
                text=(
                    f"【灵感】{status['character_name']}："
                    f"{status['balance']}/{status['maximum']} 点\n"
                    "用法：/团 灵感 A 优势，或 /团 灵感重投 A"
                )
            )
        parts = command.argument.strip().split(maxsplit=2)
        if not parts:
            return CommandResult(
                error=CommandError(
                    "turn.option.missing",
                    "【开团】请提供检定选项，例如 /团 灵感 A 优势。",
                )
            )
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
        normalized = key.upper()
        if normalized not in {"A", "B", "C", "D"}:
            return CommandResult(
                error=CommandError(
                    "turn.option.invalid",
                    "【开团】请选择 A、B、C 或 D，可在字母后补充简短演绎。",
                    "下一步：发送 /团 灵感 A 优势，或 /团 灵感重投 A。",
                )
            )
        error, _ = await _choice_guard(
            ctx,
            session_id,
            gateway,
            permission="choose",
            no_options_message="当前没有可选择的行动选项",
            order_code="turn.order",
            order_message="【回合秩序】当前选项属于 {owner}，本条内容未记录。",
        )
        if error:
            return CommandResult(error=error)
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="process_choice",
                    params={
                        "session_id": session_id,
                        "sender_id": ctx.user_id,
                        "sender_name": ctx.user_name,
                        "choice_key": normalized,
                        "flavor_text": flavor,
                        "inspiration_mode": mode,
                    },
                    render="engine_reply",
                )
            ],
        )

    async def _handle_reroll(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
    ) -> CommandResult:
        error, choice_set = await _choice_guard(
            ctx,
            session["id"],
            gateway,
            permission="reroll",
            no_options_message="当前没有可重整的个人行动选项",
            order_code="turn.reroll.denied",
            order_message="【开团】只能重整自己当前回合的选项。",
        )
        if error:
            return CommandResult(error=error)
        if choice_set and int(choice_set.get("reroll_count") or 0) >= 1:
            return CommandResult(
                error=CommandError(
                    "turn.reroll.limit",
                    "【开团】本回合的免费重整次数已经用完。",
                    "下一步：从当前 A/B/C/D 选项中选择继续。",
                )
            )
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="reroll_choices",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                    },
                    render="choice_set",
                    render_kwargs={
                        "headline": "🎲 【开团】已收到重整请求，正在重新生成本回合选项……",
                        "trigger_prefix": ctx.trigger_prefix,
                    },
                )
            ],
        )

    async def _handle_team(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
        argument: str,
    ) -> CommandResult:
        error, choice_set = await _choice_guard(
            ctx,
            session["id"],
            gateway,
            permission="choose",
            no_options_message="当前没有可选择的行动选项",
            order_code="turn.order",
            order_message="【回合秩序】当前行动属于 {owner}，本条内容未记录。",
        )
        if error:
            return CommandResult(error=error)
        team_choices = [
            item
            for item in (choice_set or {}).get("choices", [])
            if bool(item.get("collective"))
        ]
        if not team_choices:
            return CommandResult(
                error=CommandError(
                    "turn.team.none",
                    "【开团】当前没有全队行动候选项。",
                    "下一步：发送 /团 选择 A（或 B/C/D）继续个人行动。",
                )
            )
        index = team_index_from_argument(argument)
        if index < 0 or index >= len(team_choices):
            return CommandResult(
                error=CommandError(
                    "turn.team.invalid",
                    f"【开团】全队行动编号无效，当前有 {len(team_choices)} 项。",
                    "下一步：发送 /团 全队 <编号>（编号从 1 开始）。",
                )
            )
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="process_team_proposal",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                        "sender_name": ctx.user_name,
                        "index": index,
                    },
                    render="engine_reply",
                )
            ],
        )

    async def _handle_skill(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
        argument: str,
    ) -> CommandResult:
        parsed = parse_skill_argument(argument)
        if parsed is None:
            return CommandResult(
                error=CommandError(
                    "turn.skill.usage",
                    f"⚡ 用法：{ctx.trigger_prefix} 技能 <名称>"
                    f"（例如 {ctx.trigger_prefix} 技能 急救包扎 对 卡密，"
                    f"或 {ctx.trigger_prefix} 技能 短剑连击 攻击变异体）",
                )
            )
        turn = await gateway.get_turn_status(session["id"])
        if turn.get("current_user_id") and str(
            turn["current_user_id"]
        ) != ctx.user_id:
            current_name = str(turn.get("current_name") or "").strip()
            if not current_name:
                return CommandResult(
                    error=CommandError(
                        "turn.order.actor_name_missing",
                        "当前行动者缺少可公开显示的角色名称，"
                        "系统无法安全确认轮次。",
                        "/团 顺序",
                        operation="使用角色技能",
                        automatic_action="系统未记录本次技能，也未显示内部账号标识。",
                    )
                )
            current = decorate_entity("character", current_name)
            return CommandResult(
                error=CommandError(
                    "turn.order",
                    f"【回合秩序】当前轮到 {current}，本条操作未记录。",
                    "下一步：等待你的行动轮次后再使用技能。",
                )
            )
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="use_skill",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                        "sender_name": ctx.user_name,
                        "skill_name": parsed.skill_name,
                        "target_ref": parsed.target_ref,
                        "action_note": parsed.action_note,
                    },
                    render="engine_reply",
                )
            ],
        )

    async def _handle_order(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
    ) -> CommandResult:
        session_id = session["id"]
        if session["state"] == SESSION_PREPARING:
            roster = await gateway.list_roster(session_id)
            return CommandResult(
                text=(
                    "【准备阶段阵容】尚未建立正式回合顺序。\n"
                    + format_roster(roster)
                )
            )
        turn = await gateway.get_turn_status(session_id)
        vote = await gateway.active_vote(session_id)
        if vote:
            return CommandResult(
                text=format_turn_status(turn) + "\n\n" + format_vote(vote)
            )
        text = format_turn_status(turn)
        choice = await gateway.active_choice_set(session_id)
        if choice and choice.get("participant"):
            text += "\n\n" + format_choices(
                choice["participant"].get("character_name")
                or choice["participant"].get("display_name"),
                choice["choices"],
                rerolls_left=max(
                    0,
                    1 - int(choice.get("reroll_count") or 0),
                ),
                trigger_prefix=ctx.trigger_prefix,
            )
        return CommandResult(text=text)

    async def _handle_skip(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
        argument: str,
    ) -> CommandResult:
        controlled_user_id = ""
        if argument:
            try:
                target = await gateway.get_participant(
                    session["id"],
                    participant_ref=argument,
                )
            except (DatabaseNotFoundError, KeyError):
                return CommandResult(
                    error=CommandError(
                        "turn.participant.missing",
                        f"【开团】未找到角色「{argument}」。",
                        "下一步：发送 /团 阵容 查看当前角色名单。",
                    )
                )
            controlled_user_id = str(target.get("group_user_id") or "")
            if (
                controlled_user_id
                and controlled_user_id != ctx.user_id
                and not ctx.is_moderator
            ):
                control = await gateway.authorize_participant_control(
                    session["id"],
                    target["id"],
                    ctx.user_id,
                    "skip",
                )
                if not control.get("authorized"):
                    return CommandResult(
                        error=CommandError(
                            "turn.skip.denied",
                            "【开团】没有跳过该角色的权限。",
                            "下一步：只能跳过自己当前回合，"
                            "或由主持人发送 /团 强制下一位。",
                        )
                    )
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="skip_player",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                        "force": bool(
                            controlled_user_id
                            and controlled_user_id != ctx.user_id
                            and ctx.is_moderator
                        ),
                        "controlled_user_id": controlled_user_id,
                    },
                    render="skip_result",
                    render_kwargs={"headline": "【本次行动已跳过】"},
                )
            ],
        )

    async def _handle_next(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        gateway: TurnGateway,
    ) -> CommandResult:
        if not ctx.is_moderator:
            return CommandResult(
                error=CommandError(
                    "turn.next.denied",
                    "【开团】该命令只允许授权管理员使用。",
                    "下一步：请由主持人执行 /团 强制下一位。",
                )
            )
        return CommandResult(
            send_strategy="none",
            engine_requests=[
                EngineRequest(
                    method="skip_player",
                    params={
                        "session_id": session["id"],
                        "sender_id": ctx.user_id,
                        "force": True,
                        "controlled_user_id": "",
                    },
                    render="skip_result",
                    render_kwargs={"headline": "【管理员已推进至下一位】"},
                )
            ],
        )


__all__ = [
    "CommandError",
    "CommandResult",
    "EngineRequest",
    "FallbackRequest",
    "RequestContext",
    "TurnCommandHandler",
    "TurnGateway",
    "VoteCastRequest",
    "parse_option_argument",
    "parse_skill_argument",
    "session_guard",
    "story_parts",
    "team_index_from_argument",
]
