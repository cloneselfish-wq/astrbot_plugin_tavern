"""平台无关请求上下文（D1_PLAN 02 D1-ARC-001 §2.2）。

``RequestContext`` 是命令处理链的唯一输入载体：群、平台、用户、权限、
correlation ID 等字段在进入业务层之前已完成归一化。任何业务模块
（命令、仓储、服务、引擎）只依赖本模块，不得 import AstrBot 事件
对象；平台事件 → 上下文的唯一适配入口位于
``tavern/entry/event_context.py``。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Iterable, Mapping

# 角色词汇与 main.py / repositories 权限判定保持一致：
# admin 来自插件配置，host/moderator/player 来自副本权限投影。
ROLE_ADMIN = "admin"
ROLE_HOST = "host"
ROLE_MODERATOR = "moderator"
ROLE_PLAYER = "player"

ROLE_NAMES = (ROLE_ADMIN, ROLE_HOST, ROLE_MODERATOR, ROLE_PLAYER)


@dataclass(frozen=True)
class RequestContext:
    """一次平台消息的归一化上下文（纯数据，无 I/O、无平台依赖）。

    字段含义：
    - ``correlation_id``：稳定关联 ID，同一事件多次转换结果一致；
    - ``platform``：平台实例标识（如 qq / wechat / discord）；
    - ``user_id``：发送者用户标识；
    - ``group_id``：群标识（私聊为空串）；
    - ``session_id``：由宿主解析注入的副本标识，事件本身不携带；
    - ``origin``：平台统一消息来源字符串（仅用于投递回源，不解析）；
    - ``private``：是否私聊消息；
    - ``text``：归一化后的消息文本；
    - ``roles``：权限角色集合（admin/host/moderator/player 等）；
    - ``metadata``：只读附加信息（如 sender_name、message_id），
      由宿主注入安全标量，绝不包含原始平台事件对象。
    """

    correlation_id: str = ""
    platform: str = ""
    user_id: str = ""
    group_id: str = ""
    session_id: str = ""
    origin: str = ""
    private: bool = False
    text: str = ""
    roles: frozenset[str] = field(default_factory=frozenset)
    capabilities: frozenset[str] = field(default_factory=frozenset)
    request_id: str = ""
    idempotency_key: str = ""
    expected_revision: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    # Host-facing constructor fields are normalized into
    # ``platform``/``roles``/``metadata`` and are not a second request model.
    platform_id: str = ""
    user_name: str = ""
    is_admin: bool = False
    is_moderator: bool = False
    is_active_dm: bool = False
    trigger_prefix: str = "t"

    def __post_init__(self) -> None:
        platform = str(self.platform or self.platform_id or "").strip()
        roles = {str(r) for r in self.roles if str(r)}
        if self.is_admin:
            roles.add(ROLE_ADMIN)
        if self.is_moderator:
            roles.add(ROLE_MODERATOR)
        if self.is_active_dm:
            roles.add(ROLE_HOST)
        capabilities = frozenset(
            str(item) for item in self.capabilities if str(item)
        )
        metadata = dict(self.metadata)
        if self.user_name:
            metadata.setdefault("sender_name", str(self.user_name))
        request_id = str(
            self.request_id
            or metadata.get("transport_event_id")
            or metadata.get("message_id")
            or self.correlation_id
            or ""
        )
        idempotency_key = str(self.idempotency_key or request_id or "")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "platform_id", platform)
        object.__setattr__(self, "roles", frozenset(roles))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "is_admin", ROLE_ADMIN in roles)
        object.__setattr__(self, "is_moderator", ROLE_MODERATOR in roles)
        object.__setattr__(self, "is_active_dm", ROLE_HOST in roles)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "idempotency_key", idempotency_key)
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(metadata),
        )

    @property
    def user(self) -> str:
        """发送者用户标识别名。"""

        return self.user_id

    @property
    def group(self) -> str:
        """群标识别名。"""

        return self.group_id

    @property
    def is_private(self) -> bool:
        """是否私聊消息。"""

        return self.private

    def has_role(self, role: str) -> bool:
        """当前上下文是否具备指定角色。"""

        return role in self.roles

    def with_roles(self, *roles: str) -> "RequestContext":
        """返回追加角色后的新上下文，原对象保持不变。"""

        return replace(
            self,
            roles=frozenset(self.roles) | frozenset(str(r) for r in roles),
        )

    def to_dict(self) -> dict[str, Any]:
        """导出为可序列化的普通字典（供审计、日志与持久化）。"""

        return {
            "correlation_id": self.correlation_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "group_id": self.group_id,
            "session_id": self.session_id,
            "origin": self.origin,
            "private": self.private,
            "text": self.text,
            "roles": sorted(self.roles),
            "capabilities": sorted(self.capabilities),
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "expected_revision": self.expected_revision,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "ROLE_ADMIN",
    "ROLE_HOST",
    "ROLE_MODERATOR",
    "ROLE_PLAYER",
    "ROLE_NAMES",
    "RequestContext",
]
