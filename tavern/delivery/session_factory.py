"""AstrBot 会话工厂：惰性导入 + 多签名构造尝试（D1_PLAN 15 §5.1、§17）。

插件导入阶段不触碰 ``astrbot``；每次构建会话时才尝试导入。导入失败或所有
构造签名都不匹配时返回结构化 ``unavailable``，调用方回退到
``context.send_message`` 或持久化 outbox。具体枚举名与构造参数只收敛在本
文件，不散落到业务命令。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .target import (
    TARGET_KIND_CHANNEL,
    TARGET_KIND_GROUP,
    TARGET_KIND_PRIVATE,
    TARGET_KIND_THREAD,
    DeliveryTarget,
    origin_type_name,
)

# 目标种类 → MessageType 枚举候选名（按版本尝试）。
_ENUM_NAMES = {
    TARGET_KIND_GROUP: ("GROUP_MESSAGE",),
    TARGET_KIND_PRIVATE: ("FRIEND_MESSAGE", "PRIVATE_MESSAGE"),
    TARGET_KIND_CHANNEL: ("CHANNEL_MESSAGE",),
    TARGET_KIND_THREAD: ("THREAD_MESSAGE",),
}


@dataclass(frozen=True, slots=True)
class SessionAvailability:
    available: bool
    status: str  # ok | unavailable
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SessionBuildResult:
    ok: bool
    status: str  # ok | unavailable | invalid_target | unsupported | exception
    session: Any = None
    reason: str = ""
    signature: str = ""


@dataclass(frozen=True, slots=True)
class MessageChainResult:
    ok: bool
    status: str  # ok | unavailable | empty | exception
    chain: Any = None
    reason: str = ""


class MessageSessionFactory:
    """多版本兼容的 MessageSession / MessageChain 构造器。"""

    def __init__(self) -> None:
        self._loaded = False
        self._session_type: Any = None
        self._message_type_enum: Any = None
        self._chain_type: Any = None
        self._session_error = ""
        self._chain_error = ""

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from astrbot.api.message import MessageSession, MessageType  # type: ignore

            self._session_type = MessageSession
            self._message_type_enum = MessageType
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        try:
            from astrbot.api.event import MessageChain  # type: ignore

            self._chain_type = MessageChain
        except Exception:  # noqa: BLE001
            try:
                from astrbot.api.message import MessageChain  # type: ignore

                self._chain_type = MessageChain
            except Exception as exc:  # noqa: BLE001
                self._chain_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    def availability(self) -> SessionAvailability:
        """结构化能力查询：宿主缺失时返回 unavailable，不抛异常。"""

        self._load()
        if self._session_type is None:
            return SessionAvailability(
                False,
                "unavailable",
                self._session_error or "astrbot MessageSession 不可用",
            )
        return SessionAvailability(True, "ok", "")

    def _enum_value(self, target: DeliveryTarget) -> Any:
        if self._message_type_enum is None:
            return None
        for name in _ENUM_NAMES.get(target.message_type, ()):
            value = getattr(self._message_type_enum, name, None)
            if value is not None:
                return value
        return None

    def build(self, target: DeliveryTarget) -> SessionBuildResult:
        """按目标构造 MessageSession，尝试全部已知签名。"""

        self._load()
        session_type = self._session_type
        if session_type is None:
            return SessionBuildResult(
                False,
                "unavailable",
                reason=self._session_error or "MessageSession 不可用",
            )
        platform_id = target.platform_instance_id
        target_id = target.target_id
        candidates: list[tuple[str, dict[str, Any] | None]] = []
        if target.unified_origin:
            candidates.append(("from_str", None))
        enum_value = self._enum_value(target)
        if enum_value is not None:
            candidates.append(
                (
                    "kwargs_full",
                    {
                        "platform_name": platform_id,
                        "message_type": enum_value,
                        "session_id": target_id,
                    },
                )
            )
            candidates.append(
                (
                    "kwargs_order2",
                    {
                        "session_id": target_id,
                        "platform_name": platform_id,
                        "message_type": enum_value,
                    },
                )
            )
            candidates.append(
                (
                    "kwargs_target_id",
                    {
                        "target_id": target_id,
                        "platform_name": platform_id,
                        "message_type": enum_value,
                    },
                )
            )
        else:
            origin_type = origin_type_name(target.target_kind)
            if origin_type:
                candidates.append(
                    (
                        "kwargs_string_type",
                        {
                            "platform_name": platform_id,
                            "message_type": origin_type,
                            "session_id": target_id,
                        },
                    )
                )
        candidates.append(
            ("kwargs_minimal", {"platform_name": platform_id, "session_id": target_id})
        )
        errors: list[str] = []
        for signature, kwargs in candidates:
            try:
                if kwargs is None:
                    from_str = getattr(session_type, "from_str", None)
                    if not callable(from_str):
                        errors.append(f"{signature}:missing")
                        continue
                    session = from_str(target.unified_origin)
                else:
                    session = session_type(**kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{signature}:{type(exc).__name__}")
                continue
            if session is None:
                errors.append(f"{signature}:None")
                continue
            return SessionBuildResult(True, "ok", session=session, signature=signature)
        return SessionBuildResult(
            False,
            "unavailable",
            reason="；".join(errors[:4]) or "没有可用构造签名",
        )

    def build_chain(
        self,
        text: Any,
        *,
        use_markdown: bool = False,
    ) -> MessageChainResult:
        """构造 MessageChain；宿主缺失或内容为空时返回结构化失败。"""

        self._load()
        chain_type = self._chain_type
        if chain_type is None:
            return MessageChainResult(
                False,
                "unavailable",
                reason=self._chain_error or "MessageChain 不可用",
            )
        content = str(text or "").strip()
        if not content:
            return MessageChainResult(False, "empty", reason="消息内容为空")
        try:
            chain = chain_type().message(content)
            if use_markdown and callable(getattr(chain, "use_markdown", None)):
                chain = chain.use_markdown(True)
        except Exception as exc:  # noqa: BLE001
            return MessageChainResult(
                False,
                "exception",
                reason=f"{type(exc).__name__}: {str(exc)[:160]}",
            )
        return MessageChainResult(True, "ok", chain=chain)


__all__ = [
    "MessageChainResult",
    "MessageSessionFactory",
    "SessionAvailability",
    "SessionBuildResult",
]
