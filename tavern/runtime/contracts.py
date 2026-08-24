"""Platform-neutral command, error and delivery contracts.

This module is the single authority for result-side application contracts.
The dataclasses normalize both concise application fields and structured
delivery fields into one runtime model. Services do not create parallel
result or transport contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Mapping


def _readonly_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


_LEGACY_ERROR_TITLE = re.compile(r"^\s*(?:[^\w\s【]{1,3}\s*)?【[^】]+】\s*")
_NEXT_COMMAND = re.compile(r"(/团(?:\s+[^，。；\n]+)?)")
_OPERATION_LABELS = {
    "card": "处理角色卡",
    "delivery": "发送消息",
    "dm": "处理主持操作",
    "growth": "处理角色成长",
    "session": "处理副本操作",
    "turn": "提交本轮行动",
    "vote": "提交投票",
    "world": "处理世界操作",
}


def _operation_from_code(code: str) -> str:
    prefix = str(code or "").split(".", 1)[0]
    return _OPERATION_LABELS.get(prefix, "执行命令")


@dataclass(frozen=True, slots=True)
class CommandError:
    """One safe error envelope shared by BOT, Web and application services.

    ``message``/``reason`` and ``recovery``/``next_command`` are normalized
    pairs consumed by BOT and Web renderers from the same envelope.
    """

    code: str
    message: str = ""
    recovery: str = ""
    correlation_id: str = ""
    operation: str = ""
    reason: str = ""
    automatic_action: str = ""
    next_command: str = ""
    retryable: bool = False
    status_code: int = 400
    audience: str = "player"
    technical: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        code = str(self.code or "command.failed")
        reason = str(self.reason or self.message or "操作未完成。").strip()
        reason = _LEGACY_ERROR_TITLE.sub("", reason).strip()
        recovery = str(self.next_command or self.recovery or "").strip()
        command_match = _NEXT_COMMAND.search(recovery)
        recovery = command_match.group(1).strip() if command_match else recovery
        operation = str(self.operation or _operation_from_code(code)).strip()
        automatic = str(
            self.automatic_action
            or "系统未修改任何数据。"
        ).strip()
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "message", reason)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "recovery", recovery)
        object.__setattr__(self, "next_command", recovery)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "automatic_action", automatic)
        if self.technical is not None:
            object.__setattr__(
                self,
                "technical",
                _readonly_mapping(self.technical),
            )

    def public_dict(self, *, include_correlation: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "operation": self.operation,
            "reason": self.reason,
            "automatic_action": self.automatic_action,
            "next_command": self.next_command,
            "retryable": bool(self.retryable),
        }
        if include_correlation and self.correlation_id:
            payload["correlation_id"] = self.correlation_id
        return payload


@dataclass(frozen=True, slots=True)
class DeliveryIntent:
    """A host-independent delivery declaration.

    ``payload/text/record`` serve card, session and DM services, while
    ``target/projection/dedupe_key`` serve durable workers. No AstrBot event or
    message object may be stored here.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    audience: str = ""
    target: Any = None
    projection: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""
    required: bool = False
    sensitive: bool = False
    text: str = ""
    record: Mapping[str, Any] = field(default_factory=dict)
    delivery_id: str = ""
    status: str = ""
    queued: bool = False
    recipient_label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", str(self.kind or "").strip())
        object.__setattr__(self, "payload", _readonly_mapping(self.payload))
        object.__setattr__(
            self,
            "projection",
            _readonly_mapping(self.projection or self.payload),
        )
        object.__setattr__(self, "record", _readonly_mapping(self.record))
        if not self.dedupe_key:
            candidate = str(
                self.record.get("dedupe_key")
                or self.payload.get("dedupe_key")
                or self.delivery_id
                or ""
            )
            object.__setattr__(self, "dedupe_key", candidate)


@dataclass(slots=True)
class CommandResult:
    """One command result consumed by BOT and Web renderers.

    Application and transport fields share one class definition and are
    normalized in ``__post_init__``.
    """

    handled: bool = True
    status: str = ""
    code: str = ""
    message: str = ""
    recovery: str = ""
    correlation_id: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    view: Mapping[str, Any] | None = None
    player_message: Any = None
    delivery: Any = ()
    error: CommandError | None = None
    committed_revision: int | None = None
    latest_seq: int | None = None

    # Transport-facing fields normalized into the same result contract.
    ok: bool = True
    next_action: str = ""
    text: str | None = None
    parts: tuple[str, ...] | None = None
    send_strategy: str = "message"
    engine_requests: list[Any] | None = None
    vote_casts: list[Any] | None = None
    broker_events: list[dict[str, Any]] | None = None
    fallback_requests: list[Any] | None = None

    def __post_init__(self) -> None:
        if self.error is not None:
            self.ok = False
            if not self.code:
                self.code = self.error.code
            if not self.message:
                self.message = self.error.message
            if not self.recovery:
                self.recovery = self.error.recovery
            if not self.correlation_id:
                self.correlation_id = self.error.correlation_id
        if self.text is None and self.message:
            self.text = self.message
        elif not self.message and self.text:
            self.message = self.text
        if (
            self.error is None
            and self.player_message is None
            and (self.text or self.message)
        ):
            from ..messaging.player import PlayerMessage

            self.player_message = PlayerMessage.from_text(
                self.message or self.text,
                default_title=(
                    "操作已完成"
                    if self.ok
                    else "操作未完成"
                ),
                audience=(
                    self.error.audience
                    if self.error is not None
                    else "public"
                ),
            )
        if self.next_action and not self.recovery:
            self.recovery = self.next_action
        elif self.recovery and not self.next_action:
            self.next_action = self.recovery
        if not self.status:
            if not self.handled:
                self.status = "ignored"
            elif self.ok:
                self.status = "success"
            else:
                self.status = "failed"
        if not self.code:
            self.code = (
                self.error.code
                if self.error is not None
                else ("command.ok" if self.ok else "command.failed")
            )
        self.data = _readonly_mapping(self.data)
        if self.view is not None:
            self.view = _readonly_mapping(self.view)

    @classmethod
    def reply(
        cls,
        text: str,
        *,
        delivery: Any = (),
        status: str = "success",
        code: str = "command.ok",
        data: Mapping[str, Any] | None = None,
    ) -> "CommandResult":
        return cls(
            handled=True,
            ok=True,
            status=status,
            code=code,
            message=str(text),
            text=str(text),
            delivery=delivery,
            data=data or {},
        )

    @classmethod
    def message_reply(
        cls,
        message_type: str,
        data: Mapping[str, Any] | None = None,
        *,
        audience: str = "",
        delivery: Any = (),
        status: str = "success",
        code: str = "command.ok",
    ) -> "CommandResult":
        """Return a registered player message without assembling layout text."""

        from ..messaging.player import PlayerMessage

        message = PlayerMessage.registered(
            message_type,
            data=data,
            audience=audience,
        )
        return cls(
            handled=True,
            ok=True,
            status=status,
            code=code,
            player_message=message,
            delivery=delivery,
            data=data or {},
        )

    @classmethod
    def ignored(cls) -> "CommandResult":
        return cls(
            handled=False,
            ok=True,
            status="ignored",
            code="command.ignored",
            text=None,
        )

    @classmethod
    def failed(
        cls,
        error: CommandError,
        *,
        handled: bool = True,
    ) -> "CommandResult":
        return cls(
            handled=handled,
            ok=False,
            status="failed",
            code=error.code,
            error=error,
        )

    def public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": bool(self.ok),
            "handled": bool(self.handled),
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "recovery": self.recovery,
            "correlation_id": self.correlation_id,
            "data": dict(self.data),
            "view": dict(self.view) if self.view is not None else None,
            "committed_revision": self.committed_revision,
            "latest_seq": self.latest_seq,
        }
        if self.error is not None:
            payload["error"] = self.error.public_dict(
                include_correlation=bool(self.correlation_id)
            )
        return payload


__all__ = [
    "CommandError",
    "CommandResult",
    "DeliveryIntent",
]
