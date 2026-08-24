"""Single application-command registry and dispatcher."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .contracts import CommandError, CommandResult
from .request import RequestContext


CommandHandler = Callable[
    [RequestContext, Any],
    CommandResult | Awaitable[CommandResult],
]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    action: str
    mode: str
    service: str
    capability: str = ""
    session_required: bool = False
    expected_revision: bool = False
    idempotency_required: bool = False
    readonly_states: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        action = str(self.action or "").strip()
        mode = str(self.mode or "").strip()
        service = str(self.service or "").strip()
        if not action:
            raise ValueError("Router action 不能为空")
        if mode not in {"query", "deterministic_write", "ai_write"}:
            raise ValueError(f"Router action {action} 的 mode 无效：{mode}")
        if not service:
            raise ValueError(f"Router action {action} 缺少 service")
        if mode != "query" and not self.idempotency_required:
            raise ValueError(f"写命令 {action} 必须要求幂等键")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "service", service)
        object.__setattr__(
            self,
            "readonly_states",
            frozenset(str(item) for item in self.readonly_states),
        )


class ApplicationRouter:
    """Routes every registered BOT/Web action to one application handler."""

    def __init__(self) -> None:
        self._specs: dict[str, CommandSpec] = {}
        self._handlers: dict[str, CommandHandler] = {}

    def register(
        self,
        spec: CommandSpec,
        handler: CommandHandler,
    ) -> None:
        if spec.action in self._specs:
            raise ValueError(f"Router action 重复注册：{spec.action}")
        if not callable(handler):
            raise TypeError(f"Router service 不可调用：{spec.service}")
        self._specs[spec.action] = spec
        self._handlers[spec.action] = handler

    def spec(self, action: str) -> CommandSpec | None:
        return self._specs.get(str(action or "").strip())

    def registry(self) -> tuple[CommandSpec, ...]:
        return tuple(self._specs[key] for key in sorted(self._specs))

    def registry_view(self) -> list[dict[str, Any]]:
        return [
            {
                "action": spec.action,
                "mode": spec.mode,
                "service": spec.service,
                "capability": spec.capability,
                "session_required": spec.session_required,
                "expected_revision": spec.expected_revision,
                "idempotency_required": spec.idempotency_required,
                "readonly_states": sorted(spec.readonly_states),
            }
            for spec in self.registry()
        ]

    async def dispatch(
        self,
        ctx: RequestContext,
        command: Any,
    ) -> CommandResult:
        action = str(getattr(command, "action", "") or "").strip()
        spec = self._specs.get(action)
        if spec is None:
            return CommandResult.failed(
                CommandError(
                    code="command.unknown",
                    operation="识别命令",
                    reason="没有找到可执行的命令。",
                    automatic_action="系统未修改任何数据。",
                    next_command="/团 帮助",
                    correlation_id=ctx.correlation_id,
                )
            )
        error = self._validate_request(spec, ctx)
        if error is not None:
            return CommandResult.failed(error)
        result = self._handlers[action](ctx, command)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, CommandResult):
            raise TypeError(
                f"Router service {spec.service} 必须返回 CommandResult"
            )
        if not result.correlation_id:
            result.correlation_id = ctx.correlation_id
        return result

    @staticmethod
    def _validate_request(
        spec: CommandSpec,
        ctx: RequestContext,
    ) -> CommandError | None:
        if spec.capability and spec.capability not in ctx.capabilities:
            return CommandError(
                code="command.permission_denied",
                operation="执行命令",
                reason="当前账号没有执行此操作的权限。",
                automatic_action="系统未修改任何数据。",
                next_command="请联系主持人或管理员。",
                correlation_id=ctx.correlation_id,
                status_code=403,
            )
        if spec.session_required and not ctx.session_id:
            return CommandError(
                code="command.session_required",
                operation="执行副本操作",
                reason="当前请求没有关联到可用副本。",
                automatic_action="系统未修改任何数据。",
                next_command="/团 开启",
                correlation_id=ctx.correlation_id,
            )
        if spec.idempotency_required and not ctx.idempotency_key:
            return CommandError(
                code="command.idempotency_required",
                operation="提交操作",
                reason="请求缺少防重复凭据。",
                automatic_action="系统未修改任何数据。",
                next_command="请从当前页面或原消息重新提交。",
                correlation_id=ctx.correlation_id,
            )
        if spec.expected_revision and ctx.expected_revision is None:
            return CommandError(
                code="command.revision_required",
                operation="提交操作",
                reason="当前状态版本已无法确认。",
                automatic_action="系统未修改任何数据，并要求刷新状态。",
                next_command="请刷新后重试。",
                correlation_id=ctx.correlation_id,
                status_code=409,
            )
        return None


def build_router(
    registrations: Mapping[CommandSpec, CommandHandler],
) -> ApplicationRouter:
    router = ApplicationRouter()
    for spec, handler in registrations.items():
        router.register(spec, handler)
    return router


__all__ = [
    "ApplicationRouter",
    "CommandHandler",
    "CommandSpec",
    "build_router",
]
