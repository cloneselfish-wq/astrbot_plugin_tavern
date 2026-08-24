"""D1 技能成长命令应用层（平台无关）。

玩家通过 ``/团 成长`` 查看当前技能卡，通过
``/团 成长 确认 [序号]`` 确认已经满足条件的升级。玩家始终使用当前
列表序号，不输入 ``track_id``、能力引用或其它稳定内部标识。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ..runtime.request import RequestContext
from ..security import ParsedCommand

from .models import CommandResult

__all__ = ["GrowthCommandService", "GrowthGateway"]


@runtime_checkable
class GrowthGateway(Protocol):
    async def growth_context_for_private(
        self,
        private_origin: str,
    ) -> Mapping[str, Any] | None: ...

    async def list_growth_profiles(
        self,
        session_id: str,
        participant_id: str = "",
        *,
        viewer_role: str = "player",
        include_technical_refs: bool = False,
    ) -> Mapping[str, Any]: ...

    async def confirm_growth(
        self,
        session_id: str,
        participant_id: str,
        track_ref: str,
        *,
        actor: str = "",
        private_origin: str = "",
        operation_id: str = "",
        authority_confirm: bool = False,
    ) -> Mapping[str, Any]: ...


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _texts(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _profile_title(profile: Mapping[str, Any]) -> str:
    name = str(profile.get("capability_name") or "能力资料缺失").strip()
    level = str(profile.get("level_label") or "").strip()
    position = str(profile.get("position_label") or "").strip()
    suffix = " · ".join(item for item in (level, position) if item)
    return f"〈{name}〉" + (f" · {suffix}" if suffix else "")


def _format_profile(index: int, profile: Mapping[str, Any]) -> str:
    lines = [f"{index}. {_profile_title(profile)}"]
    source = _mapping(profile.get("source"))
    profession = str(source.get("profession") or "").strip()
    specialization = str(source.get("specialization") or "").strip()
    if profession or specialization:
        lines.append(
            "来源："
            + " · ".join(item for item in (profession, specialization) if item)
        )
    summary = str(profile.get("summary") or "").strip()
    if summary:
        lines.extend(["", summary])
    effects = _texts(profile.get("effects"))
    if effects:
        lines.extend(["", "当前效果", *[f"· {item}" for item in effects]])
    costs = _texts(profile.get("costs"))
    limitations = _texts(profile.get("limitations"))
    if costs or limitations:
        lines.extend(
            [
                "",
                "代价与限制",
                *[f"· {item}" for item in costs],
                *[f"· {item}" for item in limitations],
            ]
        )
    evidence = _items(profile.get("evidence"))
    milestones = _items(profile.get("milestones"))
    lines.extend(
        [
            "",
            f"成长记录：证据 {len(evidence)} 项 · 里程碑 {len(milestones)} 项",
        ]
    )
    pending = _mapping(profile.get("pending"))
    if pending:
        target = str(pending.get("target_name") or "下一等级").strip()
        lines.extend(["", f"可升级为：〈{target}〉"])
        added = _texts(pending.get("added_effects"))
        retained = _texts(pending.get("retained_limitations"))
        new_costs = _texts(pending.get("new_costs"))
        if added:
            lines.extend(["新增效果", *[f"· {item}" for item in added]])
        if retained or new_costs:
            lines.extend(
                [
                    "新代价与保留限制",
                    *[f"· {item}" for item in new_costs],
                    *[f"· {item}" for item in retained],
                ]
            )
        lines.append(f"确认：/团 成长 确认 {index}")
    elif profile.get("maximum_reached"):
        lines.extend(["", "已达到本轨迹最高等级。"])
    else:
        unmet = _texts(profile.get("unmet_conditions"))
        if unmet:
            lines.extend(
                ["", "下一等级尚未满足", *[f"· {item}" for item in unmet]]
            )
    history = _items(profile.get("history"))
    if history:
        names = [
            str(item.get("from_name") or "").strip()
            for item in history
            if str(item.get("from_name") or "").strip()
        ]
        if names:
            lines.extend(["", "历史名称：" + " → ".join(f"〈{n}〉" for n in names)])
    return "\n".join(lines)


class GrowthCommandService:
    def __init__(self, gateway: GrowthGateway) -> None:
        self.gateway = gateway

    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult:
        if command.action != "growth":
            return CommandResult.ignored()
        if not ctx.private:
            return CommandResult.reply(
                "【技能成长】成长记录包含角色个人进度，请在与 Bot 的私聊中发送：\n"
                "/团 成长"
            )
        context = await self.gateway.growth_context_for_private(ctx.origin)
        if context is None:
            return CommandResult.reply(
                "【技能成长读取失败】\n"
                "操作：读取你的技能成长记录。\n"
                "原因：当前私聊尚未绑定到已审核且在场的角色。\n"
                "自动处理：系统没有修改任何技能或成长记录。\n"
                "下一步：先在目标群发送 /团 加入，并完成私聊建卡绑定。"
            )
        profiles = _mapping(context.get("profiles"))
        tracks = _items(profiles.get("tracks"))
        if not tracks:
            return CommandResult.reply(
                "【技能成长】当前角色没有可展示的成长轨迹。\n"
                "本世界可能未启用成长，或角色尚未获得对应标志能力。"
            )
        argument = str(command.argument or "").strip()
        if not argument or argument in {"查看", "状态", "预览"}:
            return CommandResult.reply(
                f"【技能成长】「{context.get('character_name') or '角色'}」\n\n"
                + "\n\n".join(
                    _format_profile(index, profile)
                    for index, profile in enumerate(tracks, start=1)
                )
            )
        parts = argument.split()
        if not parts or parts[0] not in {"确认", "confirm", "CONFIRM"}:
            return CommandResult.reply(
                "【技能成长】无法识别本次操作。\n"
                "查看：/团 成长\n"
                "确认：/团 成长 确认 <当前页序号>"
            )
        pending_indexes = [
            index
            for index, profile in enumerate(tracks, start=1)
            if isinstance(profile.get("pending"), Mapping)
        ]
        if len(parts) >= 2:
            if not parts[1].isdigit():
                return CommandResult.reply(
                    "【技能成长确认失败】\n"
                    "原因：序号格式不正确。\n"
                    "自动处理：系统没有升级任何技能。\n"
                    "下一步：发送 /团 成长 查看当前页有效序号。"
                )
            ordinal = int(parts[1])
        elif len(pending_indexes) == 1:
            ordinal = pending_indexes[0]
        else:
            return CommandResult.reply(
                "【技能成长确认失败】\n"
                "原因：当前有多个可处理技能，无法判断目标。\n"
                "自动处理：系统没有升级任何技能。\n"
                "下一步：发送 /团 成长，并使用 "
                "/团 成长 确认 <序号>。"
            )
        if ordinal < 1 or ordinal > len(tracks):
            return CommandResult.reply(
                "【技能成长确认失败】\n"
                "原因：该序号不在当前技能列表中。\n"
                "自动处理：系统没有升级任何技能。\n"
                "下一步：发送 /团 成长 查看当前页有效序号。"
            )
        selected = tracks[ordinal - 1]
        if not isinstance(selected.get("pending"), Mapping):
            return CommandResult.reply(
                "【技能成长确认失败】\n"
                f"原因：第 {ordinal} 项当前没有可确认的升级。\n"
                "自动处理：系统没有升级任何技能。\n"
                "下一步：查看该技能的未满足条件，继续推进剧情并积累成长证据。"
            )
        technical_profiles = await self.gateway.list_growth_profiles(
            str(context.get("session_id") or ""),
            participant_id=str(context.get("participant_id") or ""),
            viewer_role="player",
            include_technical_refs=True,
        )
        technical_tracks = _items(
            _mapping(technical_profiles).get("tracks")
        )
        if ordinal > len(technical_tracks):
            return CommandResult.reply(
                "【技能成长确认失败】\n"
                "原因：技能列表已变化，当前序号已失效。\n"
                "自动处理：系统没有升级任何技能。\n"
                "下一步：重新发送 /团 成长 后再确认。"
            )
        technical = _mapping(technical_tracks[ordinal - 1].get("technical"))
        track_ref = str(technical.get("track_ref") or "").strip()
        if not track_ref:
            return CommandResult.reply(
                "【技能成长确认失败】\n"
                "原因：系统无法解析该技能的权威成长轨迹。\n"
                "自动处理：系统没有升级任何技能。\n"
                "下一步：请联系主持人检查世界包成长轨迹。"
            )
        result = await self.gateway.confirm_growth(
            str(context.get("session_id") or ""),
            str(context.get("participant_id") or ""),
            track_ref,
            actor=ctx.user_id,
            private_origin=ctx.origin,
            authority_confirm=ctx.has_role("admin"),
        )
        view = _mapping(result.get("view"))
        message = str(result.get("message") or "技能成长已确认。").strip()
        return CommandResult.reply(
            "【技能成长已确认】\n"
            + message
            + ("\n\n" + _format_profile(ordinal, view) if view else "")
        )
