"""D1-ARC-001 管理命令应用层（平台无关）。

把 main.py 中「封禁 / 解封 / 黑名单 / Token 用量 / 限额 / 维护模式」命令分支
抽为纯应用服务：输入统一为 :class:`RequestContext` 与 :class:`ParsedCommand`，
输出统一为 :class:`CommandResult`，不 import 任何 AstrBot 消息类，不直接写
Repository / SQL，权限判定 fail-closed（角色缺失即拒绝）。

权限边界（与 main.py 现行判定一致）：

- 封禁 / 解封 / 黑名单：主持人及以上；「全局」封禁仅管理员；
- Token 用量 / 限额 / 维护模式：主持人级别以上的副本主持人或管理员；
- 管理员始终放行（由入口层把管理员身份写入 ``RequestContext.roles``）。

所有预期失败都返回四要素文案：操作失败内容、失败原因、系统是否自动处理、
用户下一步命令；玩家可见文本不包含稳定 ID、内部字段或 JSON 表名。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..constants import SESSION_MAINTENANCE
from ..database import DatabaseNotFoundError, InvalidTransitionError
from ..lifecycle import parse_duration
from ..runtime.request import ROLE_ADMIN, ROLE_HOST, ROLE_MODERATOR
from .models import CommandResult, DeliveryIntent, ParsedCommand, RequestContext
from .world_commands import command_error_text

__all__ = [
    "AdminCommandError",
    "AdminCommandService",
    "AdminGateway",
    "MODERATOR_ACTIONS",
    "HOST_ACTIONS",
]


@dataclass(frozen=True, slots=True)
class AdminCommandError(ValueError):
    """管理命令应用层可预期错误。"""

    code: str
    message: str
    recovery: str = ""

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class SessionRequiredError(AdminCommandError):
    code: str = "admin.session_required"
    message: str = "当前群尚未建立副本。"
    recovery: str = "请先发送 /团 开启 <序号或完整世界名> 建立副本。"


@runtime_checkable
class AdminGateway(Protocol):
    """管理命令对数据层的依赖协议（由 TavernDatabase 实现）。"""

    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
    ) -> Mapping[str, Any]: ...

    async def create_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        scope: str = "instance",
        duration_seconds: int | None = None,
        reason: str = "",
    ) -> Mapping[str, Any]: ...

    async def revoke_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
    ) -> int: ...

    async def list_bans(
        self,
        session_id: str = "",
    ) -> list[Mapping[str, Any]]: ...

    async def token_usage_summary(
        self,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    async def set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...


MODERATOR_ACTIONS = frozenset({"ban", "unban", "ban_list"})
HOST_ACTIONS = frozenset({"usage", "quota", "maintenance"})

_SCOPE_LABELS = {
    "instance": "副本",
    "group": "群",
    "global": "全局",
}


def _format_window(seconds: Any) -> str:
    """把秒数格式化为玩家可读时长（不暴露底层字段）。"""

    try:
        total = max(1, int(seconds))
    except (TypeError, ValueError):
        return "未知时长"
    days, remainder = divmod(total, 86400)
    hours, minutes = divmod(remainder, 3600)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if parts or minutes:
        parts.append(f"{max(1, minutes)}分钟")
    return "".join(parts)


def _denied_reply(operation: str) -> CommandResult:
    return CommandResult.reply(
        command_error_text(
            title="权限不足",
            operation=operation,
            reason="当前账号没有执行该操作的权限。",
            auto_handled="系统未自动处理，未执行任何变更。",
            next_step="请联系主持人或插件管理员；主持人以上可执行本命令。",
        )
    )


class AdminCommandService:
    """管理命令编排服务：只调用注入的 ``AdminGateway``，返回纯 CommandResult。"""

    def __init__(self, gateway: AdminGateway) -> None:
        self._gateway = gateway

    def handles(self) -> frozenset[str]:
        return MODERATOR_ACTIONS | HOST_ACTIONS

    def _role_level(self, ctx: RequestContext) -> int:
        if ctx.has_role(ROLE_ADMIN):
            return 3
        if ctx.has_role(ROLE_HOST):
            return 2
        if ctx.has_role(ROLE_MODERATOR):
            return 1
        return 0

    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult:
        if command.action not in self.handles():
            return CommandResult.ignored()
        session = await self._gateway.get_session_by_group(
            ctx.platform,
            ctx.group_id,
        )
        if session is None:
            error = SessionRequiredError()
            return CommandResult.reply(
                command_error_text(
                    title="副本不存在",
                    operation=f"执行管理操作（{command.raw_action or command.action}）。",
                    reason=error.message,
                    auto_handled="系统未自动处理，未执行任何变更。",
                    next_step=error.recovery,
                )
            )
        level = self._role_level(ctx)
        required = (
            1 if command.action in MODERATOR_ACTIONS else 2
        )
        if level < required:
            return _denied_reply(f"执行管理操作（{command.raw_action or command.action}）")
        try:
            if command.action == "ban":
                return await self._ban(ctx, session, command.argument)
            if command.action == "unban":
                return await self._unban(ctx, session, command.argument)
            if command.action == "ban_list":
                return await self._ban_list(ctx, session)
            if command.action == "usage":
                return await self._usage(session)
            if command.action == "quota":
                return await self._quota(ctx, session, command.argument)
            if command.action == "maintenance":
                return await self._maintenance(ctx, session)
        except AdminCommandError as exc:
            return CommandResult.reply(
                command_error_text(
                    title="操作失败",
                    operation=f"执行管理操作（{command.raw_action or command.action}）。",
                    reason=exc.message,
                    auto_handled="系统未自动处理，未执行任何变更。",
                    next_step=exc.recovery,
                )
            )
        except DatabaseNotFoundError as exc:
            return CommandResult.reply(
                command_error_text(
                    title="操作失败",
                    operation=f"执行管理操作（{command.raw_action or command.action}）。",
                    reason=str(exc),
                    auto_handled="系统未自动处理，未执行任何变更。",
                    next_step="请核对角色名或副本状态后重新输入；可发送 /团 阵容 查看当前成员。",
                )
            )
        except InvalidTransitionError as exc:
            return CommandResult.reply(
                command_error_text(
                    title="操作失败",
                    operation=f"执行管理操作（{command.raw_action or command.action}）。",
                    reason=str(exc),
                    auto_handled="系统未自动处理，未执行任何变更。",
                    next_step="该副本处于只读或状态不允许变更；如需继续请先由主持人调整副本状态。",
                )
            )
        return CommandResult.ignored()

    async def _ban(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        argument: str,
    ) -> CommandResult:
        parts = str(argument or "").strip().split()
        if not parts:
            return CommandResult.reply(
                command_error_text(
                    title="封禁",
                    operation="封禁角色。",
                    reason="缺少角色名或代号。",
                    auto_handled="系统未自动处理。",
                    next_step="格式：/团 封禁 <角色> [副本|群|全局] [时长] [原因]，例如 /团 封禁 卡密 群 24小时 刷屏。",
                )
            )
        ref = parts.pop(0)
        scope = "instance"
        scope_map = {"副本": "instance", "群": "group", "全局": "global"}
        if parts and parts[0] in scope_map:
            scope = scope_map[parts.pop(0)]
            if scope == "global" and not ctx.has_role(ROLE_ADMIN):
                return CommandResult.reply(
                    command_error_text(
                        title="权限不足",
                        operation="执行全局封禁。",
                        reason="全局封禁只允许插件管理员。",
                        auto_handled="系统未自动处理，未执行任何变更。",
                        next_step="请改用副本或群范围封禁，或由管理员执行全局封禁。",
                    )
                )
        duration: int | None = None
        if parts:
            try:
                duration = parse_duration(parts[0])
                parts.pop(0)
            except ValueError:
                duration = None
        result = await self._gateway.create_ban(
            str(session["id"]),
            ref,
            ctx.user_id,
            scope=scope,
            duration_seconds=duration,
            reason=" ".join(parts),
        )
        ban = result.get("ban") or {}
        narrative = str(result.get("narrative") or "")
        scope_label = _SCOPE_LABELS.get(scope, scope)
        expires = str(ban.get("expires_at") or "").strip() or "永久"
        return CommandResult.reply(
            "【封禁已生效】已原子移出队列、撤销授权并释放席位。\n"
            + narrative
            + f"\n范围：{scope_label} · 到期：{expires}"
        )

    async def _unban(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        argument: str,
    ) -> CommandResult:
        if not str(argument or "").strip():
            return CommandResult.reply(
                command_error_text(
                    title="解封",
                    operation="撤销封禁。",
                    reason="缺少角色名或代号。",
                    auto_handled="系统未自动处理。",
                    next_step="格式：/团 解封 <角色名或代号>。",
                )
            )
        count = await self._gateway.revoke_ban(
            str(session["id"]),
            str(argument).strip(),
            ctx.user_id,
        )
        if count:
            return CommandResult.reply(f"【解封完成】撤销 {count} 条有效封禁记录。")
        return CommandResult.reply(
            "【解封】该角色当前没有有效封禁。\n"
            "系统未自动处理任何记录。\n"
            "下一步：发送 /团 黑名单 查看当前有效名单。"
        )

    async def _ban_list(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
    ) -> CommandResult:
        bans = await self._gateway.list_bans(str(session["id"]))
        if not bans:
            return CommandResult.reply("【黑名单】当前没有有效封禁。")
        lines = ["【黑名单】"]
        for item in bans:
            name = await self._participant_label(
                str(session["id"]),
                str(item.get("user_id") or ""),
            )
            scope = str(item.get("scope") or "")
            scope_label = _SCOPE_LABELS.get(scope, "未知范围")
            reason = str(item.get("reason") or "").strip() or "未注明"
            expires = str(item.get("expires_at") or "").strip() or "永久"
            lines.append(
                f"· 「{name}」 · {scope_label} · {reason} · 至 {expires}"
            )
        lines.append("")
        lines.append("解封：/团 解封 <角色名或代号>")
        return CommandResult.reply("\n".join(lines))

    async def _participant_label(self, session_id: str, user_id: str) -> str:
        if not user_id:
            return "已离场玩家"
        try:
            participant = await self._gateway.get_participant(
                session_id,
                user_id=user_id,
            )
        except Exception:
            return "已离场玩家"
        name = str(
            participant.get("character_name")
            or participant.get("display_name")
            or ""
        ).strip()
        return name or "角色资料缺失"

    async def _usage(self, session: Mapping[str, Any]) -> CommandResult:
        usage = await self._gateway.token_usage_summary(str(session["id"]))
        session_usage = usage.get("session") or {}
        group_usage = usage.get("group") or {}
        lines = [
            "【Token 用量】",
            (
                "当前副本："
                f"1小时 {session_usage.get('hour', 0)} · "
                f"24小时 {session_usage.get('day', 0)} · "
                f"累计 {session_usage.get('all', 0)}"
            ),
            (
                "当前群："
                f"1小时 {group_usage.get('hour', 0)} · "
                f"24小时 {group_usage.get('day', 0)} · "
                f"累计 {group_usage.get('all', 0)}"
            ),
        ]
        quotas = usage.get("quotas") or []
        if quotas:
            lines.append("滚动限额：")
            for item in quotas:
                scope_label = (
                    "群" if item.get("scope_type") == "group" else "副本"
                )
                disabled = "" if item.get("enabled") else "（已关闭）"
                lines.append(
                    f"· {scope_label}：{item.get('used', 0)}/"
                    f"{item.get('token_limit', 0)}，"
                    f"剩余 {item.get('remaining', 0)}，"
                    f"窗口 {_format_window(item.get('window_seconds'))}"
                    + disabled
                )
        else:
            lines.append("滚动限额：未设置")
        return CommandResult.reply("\n".join(lines))

    async def _quota(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
        argument: str,
    ) -> CommandResult:
        parts = str(argument or "").strip().split()
        if not parts or parts[0] not in {"群", "副本"}:
            return CommandResult.reply(
                command_error_text(
                    title="Token 限额",
                    operation="设置滚动 Token 限额。",
                    reason="缺少范围或格式不正确。",
                    auto_handled="系统未自动处理。",
                    next_step=(
                        "格式：/团 限额 群 24小时 500000；"
                        "/团 限额 副本 1小时 100000；"
                        "/团 限额 群 关"
                    ),
                )
            )
        scope_type = "group" if parts[0] == "群" else "session"
        if len(parts) == 2 and parts[1] in {"关", "关闭"}:
            current_usage = await self._gateway.token_usage_summary(
                str(session["id"])
            )
            current = next(
                (
                    item for item in (current_usage.get("quotas") or [])
                    if item.get("scope_type") == scope_type
                ),
                None,
            )
            if not current:
                return CommandResult.reply(
                    "【Token 限额】该范围尚未设置限额。\n"
                    "系统未自动处理。\n"
                    "下一步：发送 /团 限额 群 24小时 500000 进行设置。"
                )
            await self._gateway.set_token_quota(
                str(session["id"]),
                scope_type,
                window_seconds=int(current["window_seconds"]),
                token_limit=int(current["token_limit"]),
                enabled=False,
                actor_id=ctx.user_id,
            )
            return CommandResult.reply(
                f"【Token 限额已关闭】{parts[0]}范围不再拦截请求。"
            )
        if len(parts) != 3:
            return CommandResult.reply(
                command_error_text(
                    title="Token 限额",
                    operation="设置滚动 Token 限额。",
                    reason="需要同时提供时间窗口和 Token 上限。",
                    auto_handled="系统未自动处理。",
                    next_step="格式：/团 限额 副本 1小时 100000。",
                )
            )
        try:
            window_seconds = parse_duration(parts[1])
            token_limit = int(parts[2])
        except (TypeError, ValueError):
            return CommandResult.reply(
                command_error_text(
                    title="Token 限额",
                    operation="设置滚动 Token 限额。",
                    reason="时间窗口或 Token 上限格式无效。",
                    auto_handled="系统未自动处理。",
                    next_step="请使用有效时长（如 24小时、1小时）和正整数上限，例如 /团 限额 副本 1小时 100000。",
                )
            )
        result = await self._gateway.set_token_quota(
            str(session["id"]),
            scope_type,
            window_seconds=window_seconds,
            token_limit=token_limit,
            enabled=True,
            actor_id=ctx.user_id,
        )
        item = next(
            (
                entry for entry in (result.get("quotas") or [])
                if entry.get("scope_type") == scope_type
            ),
            None,
        )
        if item is None:
            return CommandResult.reply(
                command_error_text(
                    title="Token 限额",
                    operation="读取限额设置结果。",
                    reason="限额已保存，但未能读取到生效记录。",
                    auto_handled="系统已保存本次设置。",
                    next_step="发送 /团 用量 查看当前限额与用量。",
                )
            )
        return CommandResult.reply(
            f"【Token 限额已设置】{parts[0]}\n"
            f"窗口：{_format_window(item.get('window_seconds'))}\n"
            f"上限：{item.get('token_limit')} Token\n"
            f"当前已用：{item.get('used', 0)} · 剩余：{item.get('remaining', 0)}"
        )

    async def _maintenance(
        self,
        ctx: RequestContext,
        session: Mapping[str, Any],
    ) -> CommandResult:
        await self._gateway.transition_session(
            str(session["id"]),
            SESSION_MAINTENANCE,
            ctx.user_id,
        )
        return CommandResult.reply(
            "【维护模式】仅保留管理操作，剧情不会推进。\n"
            "下一步：维护结束后由主持人发送 /团 恢复 退出维护模式。"
        )
