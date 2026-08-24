"""平台传输适配（D1_PLAN 15 §5）：send_by_session 优先、send_message 回退。

适配差异（平台实例解析、会话构造、消息链构造）全部收敛在本模块与
``session_factory`` 中。返回 ``None``/``False`` 一律不算成功；分片中途失败
返回已发送片数与下一片游标，由服务层持久化。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable

from ..platform_delivery import capabilities_for, split_text
from .session_factory import MessageSessionFactory
from .target import DeliveryTarget

# 会话优先路径未触达平台时允许回退到 context.send_message 的状态集合。
# 平台已实际拒绝或抛出异常时不允许回退，避免重复投递。
_FALLBACK_STATUSES = frozenset({"unsupported", "unavailable", "invalid_target", "empty"})


@dataclass(frozen=True, slots=True)
class PlatformResolveResult:
    ok: bool
    platform: Any = None
    status: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SendAttemptResult:
    ok: bool
    status: str  # sent|unsupported|unavailable|invalid_target|rejected|exception|empty
    reason: str = ""
    method: str = ""  # send_by_session | send_message | none
    attempted_parts: int = 0
    sent_parts: int = 0
    next_part_index: int = 0
    parts: tuple[str, ...] = ()

    @property
    def fallback_allowed(self) -> bool:
        return self.method != "send_message" and self.status in _FALLBACK_STATUSES


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
        return await value
    return value


def _as_parts(text_or_parts: Any, splitter: Callable[[str], list[str]]) -> list[str]:
    """接受已切分列表或原始文本；原始文本按平台能力切分。"""

    if isinstance(text_or_parts, str):
        return [part for part in splitter(text_or_parts) if part]
    if isinstance(text_or_parts, Sequence) and not isinstance(text_or_parts, (bytes, bytearray)):
        return [str(part or "").strip() for part in text_or_parts if str(part or "").strip()]
    return []


class DeliveryAdapter:
    def __init__(self, session_factory: MessageSessionFactory | None = None) -> None:
        self.session_factory = session_factory or MessageSessionFactory()

    def split_for(self, platform_or_origin: Any, text: Any) -> list[str]:
        """按平台能力切分消息为物理分片（移动端规则：不打断候选块）。"""

        capabilities = capabilities_for(platform_or_origin)
        return [
            part
            for part in split_text(str(text or "").strip(), capabilities.max_text_length)
            if part
        ]

    async def resolve_platform(
        self,
        context: Any,
        platform_instance_id: Any,
    ) -> PlatformResolveResult:
        """多签名解析平台实例：platform_manager → context → context.bot。"""

        platform_id = str(platform_instance_id or "").strip()
        if not platform_id:
            return PlatformResolveResult(
                False,
                status="invalid_target",
                reason="缺少平台实例",
            )
        if context is None:
            return PlatformResolveResult(
                False,
                status="unavailable",
                reason="没有可用宿主上下文",
            )
        manager = getattr(context, "platform_manager", None)
        for method_name in ("get_platform", "get_platform_instance"):
            getter = getattr(manager, method_name, None) if manager is not None else None
            if not callable(getter):
                continue
            try:
                platform = await _maybe_await(getter(platform_id))
            except Exception:  # noqa: BLE001
                continue
            if platform is not None:
                return PlatformResolveResult(True, platform=platform, status="ok")
        for method_name in ("get_platform_instance", "get_platform"):
            getter = getattr(context, method_name, None)
            if not callable(getter):
                continue
            try:
                platform = await _maybe_await(getter(platform_id))
            except Exception:  # noqa: BLE001
                continue
            if platform is not None:
                return PlatformResolveResult(True, platform=platform, status="ok")
        bot = getattr(context, "bot", None)
        if bot is not None:
            bot_id = getattr(bot, "platform_name", None) or getattr(bot, "platform_id", None)
            if str(bot_id or "") == platform_id:
                return PlatformResolveResult(True, platform=bot, status="ok")
        return PlatformResolveResult(
            False,
            status="unavailable",
            reason=f"无法解析平台实例 {platform_id}",
        )

    async def send_by_session(
        self,
        context: Any,
        target: DeliveryTarget,
        text_or_parts: Any,
        *,
        start_index: int = 0,
        use_markdown: bool = False,
    ) -> SendAttemptResult:
        """优先路径：解析平台实例 → 构造会话 → 逐片 send_by_session。"""

        parts = _as_parts(text_or_parts, lambda text: self.split_for(target.platform_instance_id, text))
        if not parts:
            return SendAttemptResult(False, "empty", reason="消息内容为空", method="send_by_session")
        resolved = await self.resolve_platform(context, target.platform_instance_id)
        if not resolved.ok:
            return SendAttemptResult(
                False,
                resolved.status or "unavailable",
                reason=resolved.reason,
                method="send_by_session",
            )
        sender = getattr(resolved.platform, "send_by_session", None)
        if not callable(sender):
            return SendAttemptResult(
                False,
                "unsupported",
                reason="平台未提供 send_by_session",
                method="send_by_session",
            )
        built = self.session_factory.build(target)
        if not built.ok:
            return SendAttemptResult(
                False,
                built.status or "unavailable",
                reason=built.reason,
                method="send_by_session",
            )
        session = built.session
        return await self._send_parts(
            send=lambda chain: sender(session, chain),
            build_chain=lambda text: self.session_factory.build_chain(
                text,
                use_markdown=use_markdown,
            ),
            parts=parts,
            start_index=start_index,
            method="send_by_session",
        )

    async def send_message(
        self,
        context: Any,
        origin: Any,
        text_or_parts: Any,
        *,
        start_index: int = 0,
        use_markdown: bool = False,
    ) -> SendAttemptResult:
        """兼容回退：context.send_message(origin, chain)，None/False 不算成功。"""

        target = str(origin or "").strip()
        if not target:
            return SendAttemptResult(
                False,
                "invalid_target",
                reason="没有可用会话来源",
                method="send_message",
            )
        sender = getattr(context, "send_message", None) if context is not None else None
        if not callable(sender):
            return SendAttemptResult(
                False,
                "unavailable",
                reason="context.send_message 不可用",
                method="send_message",
            )
        parts = _as_parts(text_or_parts, lambda text: self.split_for(target, text))
        if not parts:
            return SendAttemptResult(False, "empty", reason="消息内容为空", method="send_message")
        return await self._send_parts(
            send=lambda chain: sender(target, chain),
            build_chain=lambda text: self.session_factory.build_chain(
                text,
                use_markdown=use_markdown,
            ),
            parts=parts,
            start_index=start_index,
            method="send_message",
        )

    async def deliver(
        self,
        context: Any,
        target: DeliveryTarget,
        text_or_parts: Any,
        *,
        start_index: int = 0,
        allow_fallback: bool = True,
        use_markdown: bool = False,
    ) -> SendAttemptResult:
        """按 D1-DEL-004 顺序：send_by_session → send_message 回退。"""

        session_result = await self.send_by_session(
            context,
            target,
            text_or_parts,
            start_index=start_index,
            use_markdown=use_markdown,
        )
        if session_result.ok or not allow_fallback or not session_result.fallback_allowed:
            return session_result
        # 会话路径未触达平台：用同一分片列表回退（会话路径没有发送任何片）。
        fallback_parts = session_result.parts or text_or_parts
        return await self.send_message(
            context,
            target.unified_origin,
            fallback_parts,
            start_index=0,
            use_markdown=use_markdown,
        )

    async def _send_parts(
        self,
        *,
        send: Callable[[Any], Any],
        build_chain: Callable[[str], Any],
        parts: list[str],
        start_index: int,
        method: str,
    ) -> SendAttemptResult:
        total = len(parts)
        index = max(0, min(int(start_index or 0), total))
        sent_parts = 0
        for current in range(index, total):
            chain_result = build_chain(parts[current])
            if not chain_result.ok:
                return SendAttemptResult(
                    False,
                    chain_result.status or "unavailable",
                    reason=chain_result.reason,
                    method=method,
                    attempted_parts=total,
                    sent_parts=sent_parts,
                    next_part_index=current,
                    parts=tuple(parts),
                )
            try:
                result = await send(chain_result.chain)
            except Exception as exc:  # noqa: BLE001
                return SendAttemptResult(
                    False,
                    "exception",
                    reason=f"{type(exc).__name__}: {str(exc)[:160]}",
                    method=method,
                    attempted_parts=total,
                    sent_parts=sent_parts,
                    next_part_index=current,
                    parts=tuple(parts),
                )
            if result is False or result is None:
                return SendAttemptResult(
                    False,
                    "rejected",
                    reason="平台没有确认消息已发送",
                    method=method,
                    attempted_parts=total,
                    sent_parts=sent_parts,
                    next_part_index=current,
                    parts=tuple(parts),
                )
            sent_parts += 1
        return SendAttemptResult(
            True,
            "sent",
            method=method,
            attempted_parts=total,
            sent_parts=sent_parts,
            next_part_index=total,
            parts=tuple(parts),
        )


__all__ = [
    "DeliveryAdapter",
    "PlatformResolveResult",
    "SendAttemptResult",
]
