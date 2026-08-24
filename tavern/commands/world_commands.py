"""D1-ARC-001 世界命令应用层（平台无关）。

把 main.py 中「世界列表」命令分支与「内置世界安装 / 状态」能力抽为纯应用
服务：输入统一为 :class:`RequestContext` 与 :class:`ParsedCommand`，输出
统一为 :class:`CommandResult`（外显文本 + 投递意图），不 import 任何
AstrBot 消息类，不直接写 Repository / SQL。

职责边界：

- ``list_worlds``：玩家可见世界列表；世界技术字段（内容版本等）仅管理员可见；
- ``builtin_world_status`` / ``install_builtin_world``：管理员内置世界能力，
  实际安装动作由宿主注入的 ``WorldGateway`` 完成，本服务只做参数解析、
  权限判定、状态归并与玩家可见文案；
- 空世界 / 构建错误 / 维护降级均返回四要素错误文案：
  操作失败内容、失败原因、系统是否自动处理、用户下一步命令。

当前接线状态：main.py 的 `worlds` 分支已调用本服务，并由入口层提供
`WorldGateway` 实现。`builtin_world_status` / `install_builtin_world`
仍仅供 WebUI 与宿主恢复流程使用，BOT 解析表暂未提供对应动作。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..builtin_worlds import builtin_world_spec_by_key, builtin_world_specs
from ..lifecycle import player_limits
from ..runtime.request import ROLE_ADMIN
from .models import CommandResult, DeliveryIntent, ParsedCommand, RequestContext

__all__ = [
    "PermissionDeniedError",
    "WorldCommandError",
    "WorldCommandService",
    "WorldGateway",
    "command_error_text",
]


@dataclass(frozen=True, slots=True)
class WorldCommandError(ValueError):
    """世界命令应用层可预期错误。

    ``code`` 供入口层记录，``message`` 是玩家可见原因，``recovery`` 是
    玩家下一步可执行的命令说明。
    """

    code: str
    message: str
    recovery: str = ""

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class PermissionDeniedError(WorldCommandError):
    code: str = "world.permission_denied"
    message: str = "该操作仅允许插件管理员使用。"
    recovery: str = "请由管理员执行该操作；普通玩家可使用 /团 世界列表 查看可用世界。"


def command_error_text(
    *,
    title: str,
    operation: str,
    reason: str,
    auto_handled: str,
    next_step: str,
) -> str:
    """四要素错误文案：什么操作失败、原因、系统是否自动处理、下一步。"""

    return (
        f"【{title}】\n"
        f"操作：{operation}\n"
        f"原因：{reason}\n"
        f"处理：{auto_handled}\n"
        f"下一步：{next_step}"
    )


def _permission_reply(title: str, operation: str) -> CommandResult:
    return CommandResult.reply(
        command_error_text(
            title=title,
            operation=operation,
            reason="当前账号没有执行该操作的权限。",
            auto_handled="系统未自动处理，未执行任何变更。",
            next_step="请联系主持人或插件管理员；也可发送 /团 帮助 查看可执行命令。",
        )
    )


def _builtin_spec_or_none(key: str) -> Mapping[str, Any] | None:
    try:
        return builtin_world_spec_by_key(key)
    except ValueError:
        return None


def resolve_builtin_key(value: str) -> str:
    """把玩家输入解析为内置世界 key（不向玩家暴露内部 key）。"""

    text = str(value or "").strip()
    if not text:
        raise WorldCommandError(
            code="world.builtin_key_required",
            message="缺少要安装的内置世界名称。",
            recovery="请发送：/团 世界安装 <世界名>。",
        )
    try:
        return builtin_world_spec_by_key(text).key
    except ValueError:
        pass
    lowered = text.casefold()
    for spec in builtin_world_specs():
        if lowered in {spec.key.casefold(), str(spec.display_name or "").casefold()}:
            return spec.key
    available = "、".join(str(spec.display_name) for spec in builtin_world_specs())
    raise WorldCommandError(
        code="world.builtin_unknown",
        message=f"未找到内置世界：{text}。",
        recovery=f"当前可选：{available or '无'}；请使用完整名称重新输入。",
    )


@runtime_checkable
class WorldGateway(Protocol):
    """世界命令对数据层 / 宿主的依赖协议（由 TavernDatabase / main.py 实现）。"""

    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[Mapping[str, Any]]: ...

    async def builtin_world_status(self) -> list[Mapping[str, Any]]: ...

    async def install_builtin_world(self, key: str) -> Mapping[str, Any]: ...


_STATE_LABELS = {
    "ready": "已就绪",
    "degraded": "已降级",
    "blocked": "不可用",
    "installing": "安装中",
    "pending": "等待安装",
}


class WorldCommandService:
    """世界命令编排服务：只调用注入的 ``WorldGateway``，返回纯 CommandResult。"""

    def __init__(self, gateway: WorldGateway) -> None:
        self._gateway = gateway

    def handles(self) -> frozenset[str]:
        return frozenset({"worlds"})

    async def handle(
        self,
        ctx: RequestContext,
        command: ParsedCommand,
    ) -> CommandResult:
        if command.action == "worlds":
            return await self.list_worlds(ctx)
        return CommandResult.ignored()

    async def list_worlds(self, ctx: RequestContext) -> CommandResult:
        """玩家可见世界列表；内容版本等世界技术字段仅管理员可见。"""

        worlds = await self._gateway.list_worlds()
        if not worlds:
            return CommandResult.reply(
                command_error_text(
                    title="可用世界包",
                    operation="读取可用世界列表。",
                    reason="当前没有已安装且未归档的世界包。",
                    auto_handled="系统未自动处理。",
                    next_step="请联系管理员安装世界包后重试；安装完成后发送 /团 世界列表 查看。",
                )
            )
        is_admin = ctx.has_role(ROLE_ADMIN)
        lines: list[str] = []
        for index, item in enumerate(worlds, start=1):
            name = str(item.get("name") or "").strip()
            if not name:
                name = "名称缺失"
            limits = player_limits(item)
            line = (
                f"{index}. 《{name}》"
                f" · 推荐 {limits['recommended_min']}—{limits['recommended_max']} 人"
                f" · 上限 {limits['maximum']} 人"
            )
            if is_admin:
                version = str(item.get("content_version") or "").strip()
                if version:
                    line += f" · 内容版本 {version}"
                if not str(item.get("name") or "").strip():
                    line += "（世界数据异常，请检查世界包）"
            lines.append(line)
        return CommandResult.reply(
            "【可用世界包】\n"
            + "\n".join(lines)
            + "\n\n建立副本：/团 开启 <序号或完整世界名>"
        )

    async def builtin_world_status(self, ctx: RequestContext) -> CommandResult:
        """内置世界安装状态（仅管理员；技术字段不进入普通玩家文案）。"""

        if not ctx.has_role(ROLE_ADMIN):
            return _permission_reply("权限不足", "查看内置世界安装状态")
        rows = await self._gateway.builtin_world_status()
        if not rows:
            return CommandResult.reply(
                command_error_text(
                    title="内置世界状态",
                    operation="读取内置世界目录。",
                    reason="内置世界目录为空或尚未初始化。",
                    auto_handled="系统未自动处理。",
                    next_step="请检查插件安装文件后重启插件，再重试本命令。",
                )
            )
        lines = ["【内置世界状态】"]
        for row in rows:
            key = str(row.get("key") or "").strip()
            spec = _builtin_spec_or_none(key)
            name = (
                str(row.get("name") or "").strip()
                or (str(getattr(spec, "display_name", "")) if spec else "")
                or key
                or "名称缺失"
            )
            state = str(row.get("state") or "").strip()
            label = _STATE_LABELS.get(state, "未知状态")
            line = f"· 《{name}》 · {label}"
            version = str(row.get("installed_content_version") or "").strip()
            if version:
                line += f" · 内容版本 {version}"
            lines.append(line)
            message = str(row.get("message") or "").strip()
            if message:
                lines.append(f"  {message}")
            last_error = str(row.get("last_error") or "").strip()
            if last_error:
                lines.append(f"  技术原因：{last_error}")
        lines.append("")
        lines.append("安装：/团 世界安装 <世界名>（管理员）")
        return CommandResult.reply("\n".join(lines))

    async def install_builtin_world(
        self,
        ctx: RequestContext,
        key: str,
    ) -> CommandResult:
        """安装 / 重试内置世界（仅管理员；幂等，可安全重复）。"""

        if not ctx.has_role(ROLE_ADMIN):
            return _permission_reply("权限不足", "安装内置世界")
        try:
            normalized = resolve_builtin_key(key)
        except WorldCommandError as exc:
            return CommandResult.reply(
                command_error_text(
                    title="世界安装",
                    operation="解析内置世界名称。",
                    reason=exc.message,
                    auto_handled="系统未自动处理，未开始安装。",
                    next_step=exc.recovery,
                )
            )
        spec = _builtin_spec_or_none(normalized)
        name = str(getattr(spec, "display_name", "")) if spec else normalized
        status = await self._gateway.install_builtin_world(normalized)
        state = str(status.get("state") or "").strip()
        if state == "ready":
            version = str(status.get("installed_content_version") or "").strip()
            version_text = f" · 内容版本 {version}" if version else ""
            return CommandResult.reply(
                f"【世界安装完成】《{name}》已就绪{version_text}。\n"
                "现在可发送 /团 世界列表 查看，或 /团 开启 <序号或完整世界名> 建立副本。"
            )
        if state == "installing":
            return CommandResult.reply(
                f"【世界安装中】《{name}》正在验证并安装。\n"
                "系统已记录安装任务，无需重复发送。\n"
                "下一步：稍后发送 /团 内置世界状态 查看结果。"
            )
        if state == "degraded":
            version = str(status.get("installed_content_version") or "").strip()
            return CommandResult.reply(
                command_error_text(
                    title="世界安装",
                    operation=f"安装《{name}》新版本。",
                    reason="新版本文件缺失或安装失败。",
                    auto_handled=(
                        "系统已自动继续使用上次成功版本"
                        + (f"（内容版本 {version}）" if version else "")
                        + "，当前世界仍可用。"
                    ),
                    next_step="可稍后重试安装；也可发送 /团 内置世界状态 查看详情。",
                )
            )
        message = str(status.get("message") or "").strip()
        return CommandResult.reply(
            command_error_text(
                title="世界安装",
                operation=f"安装《{name}》。",
                reason=message or "内置世界文件缺失或安装失败，当前不可用。",
                auto_handled="系统未自动处理，没有写入任何世界数据。",
                next_step=(
                    "请检查插件安装文件完整后重试本命令；"
                    "仍失败请联系维护人员查看技术原因。"
                ),
            )
        )
