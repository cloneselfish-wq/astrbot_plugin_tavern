"""D1-ARC-001 投票命令应用层（不持有 AstrBot 事件对象）。

职责边界：
- 输入：RequestContext / ParsedCommand / VoteGateway（只读查询协议）；
- 校验：副本状态、选项合法性、重复投票（相同选项不再重复落票）；
- 输出：VoteCastRequest（结构化落票请求）+ 纯渲染函数，把 cast 结果
  投影为玩家可见文本；表决通过时输出 process_vote_resolution 的
  结构化 engine 请求、broker 事件与失败兜底请求。
- 玩家输入只能产生 A/B/C/D 选项键；结果文本一律来自系统 tally / vote
  状态，玩家无法通过命令文本宣布结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .turn_commands import (
    CommandError,
    CommandResult,
    EngineRequest,
    FallbackRequest,
    RequestContext,
    VoteCastRequest,
    session_guard,
)
from ..lifecycle import parse_choice_input
from ..messaging import render_message_type
from ..messaging.player import PlayerMessage, render_player_message
from .presentation import format_vote
from ..security import ParsedCommand


class VoteGateway(Protocol):
    """投票命令只读查询协议（由适配层接入 TavernDatabase）。"""

    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def active_vote(self, session_id: str) -> Mapping[str, Any] | None: ...


class VoteCommandHandler:
    """投票命令应用服务：只编排，不持有平台对象、不直接写库。"""

    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
        gateway: VoteGateway,
    ) -> CommandResult | None:
        if command.action != "vote":
            return None
        session = await gateway.get_session_by_group(
            ctx.platform_id,
            ctx.group_id,
        )
        guard = session_guard(session)
        if guard:
            return CommandResult(error=guard)
        assert session is not None
        session_id = str(session["id"])
        try:
            key, _flavor = parse_choice_input(command.argument)
        except ValueError as exc:
            return CommandResult(
                error=CommandError(
                    "vote.option.invalid",
                    operation="提交投票",
                    reason=str(exc),
                    automatic_action="系统没有记录选票，也没有改变当前票数。",
                    next_command="/团 投票 A",
                )
            )
        vote = await gateway.active_vote(session_id)
        if not vote:
            return CommandResult(
                error=CommandError(
                    "vote.none",
                    operation="提交投票",
                    reason="当前没有进行中的全队表决。",
                    automatic_action="系统没有记录选票。",
                    next_command="/团 当前",
                )
            )
        valid_keys = {
            str(item.get("key")) for item in vote.get("options", [])
        }
        if key not in valid_keys:
            return CommandResult(
                error=CommandError(
                    "vote.option.invalid",
                    operation="提交投票",
                    reason="可用选项是：" + "、".join(sorted(valid_keys)) + "。",
                    automatic_action="系统没有记录无效选项。",
                    next_command="/团 投票 <选项字母>",
                )
            )
        # 重复投票：已投相同选项不再重复落票；改票（不同选项）仍允许。
        for ballot in vote.get("ballots", []):
            if str(ballot.get("user_id") or "") != ctx.user_id:
                continue
            if str(ballot.get("option_key") or "").upper() == key:
                return CommandResult(
                    player_message=PlayerMessage.dynamic(
                        title="投票已经记录",
                        summary=f"你已经选择了 {key}，系统没有重复计票。",
                        sections=("截止前仍可提交其他选项修改这一票。",),
                        actions=("/团 当前",),
                    )
                )
            break
        return CommandResult(
            send_strategy="none",
            vote_casts=[
                VoteCastRequest(
                    session_id=session_id,
                    user_id=ctx.user_id,
                    option_key=key,
                )
            ],
        )


@dataclass(frozen=True, slots=True)
class VoteOutcomeRender:
    """cast 结果的纯渲染：外显文本或后续结构化请求。"""

    text: str | None = None
    engine_request: EngineRequest | None = None
    broker_event: dict[str, Any] | None = None
    fallback: FallbackRequest | None = None
    failure_text: str | None = None
    passed_headline: str | None = None


def render_vote_outcome(outcome: Mapping[str, Any]) -> VoteOutcomeRender:
    """把 cast_vote 返回结果投影为玩家可见文本与后续动作。"""
    vote = outcome.get("vote") or {}
    tally = outcome.get("tally") or {}
    counts = "、".join(
        f"{name}:{count}" for name, count in tally.get("counts", {}).items()
    )
    if outcome.get("runoff"):
        return VoteOutcomeRender(
            text=render_player_message(
                PlayerMessage.dynamic(
                    title="投票进入决选",
                    summary=f"当前票数：{counts or '暂时没有有效票数'}。",
                    sections=(
                        "第一轮没有选项取得多数，系统已保留前两项并开启决选。",
                        format_vote(vote),
                    ),
                    actions=("/团 投票 <选项字母>",),
                )
            )
        )
    if outcome.get("resolved"):
        if str(vote.get("decision_status") or "") == "decided":
            winner = str(vote.get("winner_key") or "")
            session_id = str(vote.get("session_id") or "")
            return VoteOutcomeRender(
                engine_request=EngineRequest(
                    method="process_vote_resolution",
                    params={
                        "session_id": session_id,
                        "vote": vote,
                    },
                    render="vote_resolution",
                ),
                broker_event={
                    "type": "vote",
                    "action": "resolved",
                    "session_id": session_id,
                    "status": "decided",
                    "winner_key": winner,
                },
                failure_text=(
                    "【表决结果尚未写入故事】\n\n"
                    f"失败操作：落实胜出选项 {winner}。\n\n"
                    "原因：{error}\n\n"
                    "自动处理：系统已保留表决决定与检定结果，世界状态尚未改变。\n\n"
                    "下一步\n\n"
                    "/团 重试本轮"
                ),
                passed_headline=(
                    f"【表决通过】\n\n全队选择了 {winner}。\n\n{{body}}"
                ),
            )
        return VoteOutcomeRender(
            text=render_player_message(
                PlayerMessage.dynamic(
                    title="表决未通过",
                    summary="本次表决没有形成有效多数，队伍维持现状。",
                    sections=("系统已为当前行动者重新准备个人行动选项。",),
                    actions=("/团 当前",),
                )
            )
        )
    return VoteOutcomeRender(
        text=render_player_message(
            PlayerMessage.dynamic(
                title="投票已记录",
                summary=f"当前票数：{counts or '暂时没有有效票数'}。",
                sections=(
                    f"已有 {tally.get('cast_count', 0)}/"
                    f"{tally.get('eligible_count', 0)} 名玩家投票；"
                    "截止前可以改票。",
                ),
                actions=("/团 当前",),
            )
        )
    )


def render_vote_resolution_success(
    reply: Mapping[str, Any],
    headline: str,
) -> str:
    """把 process_vote_resolution 的 EngineReply 投影为通过文案。"""
    parts = [
        part
        for part in (
            str(reply.get("story_text") or "").strip(),
            str(reply.get("turn_text") or "").strip(),
        )
        if part
    ]
    body = "\n\n".join(parts) if parts else str(reply.get("text") or "")
    return headline.format(body=body)


__all__ = [
    "VoteCommandHandler",
    "VoteGateway",
    "VoteOutcomeRender",
    "render_vote_outcome",
    "render_vote_resolution_success",
]
