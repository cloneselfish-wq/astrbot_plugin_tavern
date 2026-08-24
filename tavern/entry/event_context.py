"""AstrBot 平台事件 → RequestContext 唯一适配层。

职责（D1_PLAN 02 D1-ARC-001 §2.2）：
- 把 AstrBot 事件对象转换为平台无关的 ``RequestContext``；
- 提取完成后不再持有原始事件对象，原始事件绝不进入
  ``metadata`` 或其它字段；
- 模块导入不依赖 astrbot（鸭子类型适配），无宿主环境可直接导入；
- 适配器不访问数据库与配置；副本标识、角色、附加元数据等由宿主
  在调用时显式注入（``session_id`` / ``roles`` / ``metadata``）。
"""
from __future__ import annotations

import inspect
import uuid
from typing import Any, Iterable, Mapping

from ..runtime.request import ROLE_NAMES, RequestContext

__all__ = ["from_astrbot_event"]

_CORRELATION_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_MAX_CORRELATION_LENGTH = 160

_PRIVATE_MESSAGE_TYPES = {
    "private",
    "friend",
    "friendmessage",
    "c2c",
    "c2cmessage",
    "privatemessage",
    "private_message",
}
_GROUP_MESSAGE_TYPES = {
    "group",
    "groupmessage",
    "groupmsg",
    "group_message",
}


def _attr(event: Any, name: str) -> Any:
    """缺字段或属性读取抛错时一律视为缺失，保证适配永不崩溃。"""

    try:
        return getattr(event, name, None)
    except Exception:
        return None


def _call(event: Any, *names: str) -> Any:
    """按顺序尝试调用取数方法；不存在或抛错则继续下一个。"""

    for name in names:
        method = _attr(event, name)
        if callable(method):
            try:
                value = method()
                if inspect.isawaitable(value):
                    # This adapter is deliberately synchronous.  Newer AstrBot
                    # exposes a few async accessors (notably get_group); never
                    # stringify their coroutine objects or leave them unclosed.
                    closer = getattr(value, "close", None)
                    if callable(closer):
                        closer()
                    continue
                if value is not None and value != "":
                    return value
            except Exception:
                continue
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _origin(event: Any) -> str:
    value = _attr(event, "unified_msg_origin") or _call(
        event, "get_unified_msg_origin"
    )
    return _text(value)


def _platform(event: Any, origin: str, *, override: str | None) -> str:
    if override is not None:
        return _text(override)
    value = _call(event, "get_platform_id", "get_platform")
    if value is not None:
        return _text(value)
    if ":" in origin:
        return origin.split(":", 1)[0]
    if origin:
        return "qq"
    if any(
        callable(_attr(event, name))
        for name in ("get_platform_id", "get_platform", "get_sender_id")
    ):
        return "qq"
    return ""


def _user(event: Any, *, override: str | None) -> str:
    if override is not None:
        return _text(override)
    value = (
        _call(event, "get_sender_id", "get_user_id")
        or _attr(event, "sender_id")
        or _attr(event, "user_id")
    )
    if value is not None:
        return _text(value)
    message_obj = _attr(event, "message_obj")
    return _text(getattr(message_obj, "sender_id", "") or "")


def _group(event: Any, *, override: str | None) -> str:
    if override is not None:
        return _text(override)
    value = (
        _call(event, "get_group_id", "get_group")
        or _attr(event, "group_id")
        or _attr(event, "group")
    )
    if value is not None:
        return _text(value)
    message_obj = _attr(event, "message_obj")
    return _text(getattr(message_obj, "group_id", "") or "")


def _message_type(event: Any) -> str:
    value = _call(event, "get_message_type") or _attr(event, "message_type")
    return _text(value).lower().replace("_", "")


def _is_private(
    event: Any,
    origin: str,
    *,
    override: bool | None,
) -> bool:
    if override is not None:
        return bool(override)
    message_type = _message_type(event)
    if message_type:
        if message_type in _PRIVATE_MESSAGE_TYPES:
            return True
        if message_type in _GROUP_MESSAGE_TYPES:
            return False
    normalized = origin.lower()
    return (
        "friendmessage" in normalized
        or ":private" in normalized
        or normalized.endswith(":friend")
    )


def _text_content(event: Any) -> str:
    value = _attr(event, "message_str") or _call(event, "get_message_str")
    return _text(value)


def _message_id(event: Any) -> str:
    value = _attr(event, "message_id") or _call(event, "get_message_id")
    if value is not None:
        return _text(value)
    message_obj = _attr(event, "message_obj")
    return _text(getattr(message_obj, "id", "") or "")


def _sender_name(event: Any) -> str:
    value = (
        _call(event, "get_sender_name")
        or _attr(event, "sender_name")
        or _attr(event, "sender_nickname")
    )
    return _text(value)


def _event_role_hint(event: Any) -> str:
    """平台事件可选的发送者角色提示（仅接受字符串，避免对象泄漏）。"""

    for name in ("sender_role", "role"):
        value = _attr(event, name)
        if isinstance(value, str):
            role = value.strip()
            if role in ROLE_NAMES:
                return role
    return ""


def _correlation_id(
    event: Any,
    platform: str,
    user: str,
    group: str,
    origin: str,
    message_id: str,
    text: str,
    *,
    override: str | None,
) -> str:
    """稳定关联 ID：同一事件多次转换结果一致，无随机成分。

    优先级：宿主显式覆盖 > 事件自带 correlation_id > 按平台/用户/
    群/来源/消息 ID/文本的确定性 UUID5 复合值。
    """

    if override is not None:
        value = str(override).strip()
        if value:
            return value[:_MAX_CORRELATION_LENGTH]
    explicit = _attr(event, "correlation_id") or _call(
        event, "get_correlation_id"
    )
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:_MAX_CORRELATION_LENGTH]
    if not any((platform, user, group, origin, message_id, text)):
        return ""
    composite = "\x1f".join(
        [platform, user, group, origin, message_id, text]
    )
    return str(uuid.uuid5(_CORRELATION_NAMESPACE, composite))


def from_astrbot_event(
    event: Any,
    *,
    session_id: str = "",
    roles: Iterable[str] = (),
    metadata: Mapping[str, Any] | None = None,
    correlation_id: str | None = None,
    platform_id: str | None = None,
    group_id: str | None = None,
    user_id: str | None = None,
    is_private: bool | None = None,
) -> RequestContext:
    """把 AstrBot 平台事件转换为平台无关的 ``RequestContext``。

    ``session_id`` / ``roles`` / ``metadata`` 由宿主在调用时注入；
    其余字段从事件提取，缺字段时使用安全默认值（空串 / False），
    绝不抛出异常。返回的上下文不持有原始事件对象。
    """

    origin = _origin(event)
    platform = _platform(event, origin, override=platform_id)
    user = _user(event, override=user_id)
    group = _group(event, override=group_id)
    text = _text_content(event)
    private = _is_private(event, origin, override=is_private)
    message_id = _message_id(event)
    sender_name = _sender_name(event)

    merged_roles: set[str] = {
        str(role).strip() for role in roles if str(role).strip()
    }
    role_hint = _event_role_hint(event)
    if role_hint:
        merged_roles.add(role_hint)

    meta: dict[str, Any] = {}
    if metadata:
        for key, value in metadata.items():
            meta[str(key)] = value
    if sender_name:
        meta.setdefault("sender_name", sender_name)
    if message_id:
        meta.setdefault("message_id", message_id)

    correlation = _correlation_id(
        event,
        platform,
        user,
        group,
        origin,
        message_id,
        text,
        override=correlation_id,
    )
    return RequestContext(
        correlation_id=correlation,
        platform=platform,
        user_id=user,
        group_id=group,
        session_id=_text(session_id),
        origin=origin,
        private=private,
        text=text,
        roles=frozenset(merged_roles),
        metadata=meta,
    )
