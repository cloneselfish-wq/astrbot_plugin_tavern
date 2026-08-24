"""Web 身份主体与路由授权策略。

早期实现曾混用两类 Web 主体：AstrBot 原生后台登录身份被当作
消息平台用户 ID 解析，导致作者任务、健康中心、作者实验室等纯控制台入口
对后台用户返回 403。本模块把两类主体显式建模，并为每个 Web 路由声明
授权策略。

契约要点：

- ``ConsolePrincipal`` 只代表 AstrBot 后台登录身份，绝不自动携带
  QQ/OpenID，也不写入玩家成员表。
- ``PlatformPrincipal`` 只由玩家门户或显式绑定后的控制台“以玩家身份查看”
  能力产生，携带真实平台 ID 与副本成员/主持角色。
- 每个 Web 路由必须声明一种授权策略；未声明时 ``route_policy`` 返回
  ``None``（fail closed），测试会断言全部已注册路由都有显式策略。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class AuthSource(str, Enum):
    ASTRBOT_CONSOLE = "astrbot_console"
    PLATFORM_BINDING = "platform_binding"
    MINIPROGRAM_BINDING = "miniprogram_binding"


class RoutePolicy(str, Enum):
    CONSOLE_ADMIN = "console_admin"
    CONSOLE_OR_PLUGIN_ADMIN = "console_or_plugin_admin"
    AUTHENTICATED = "authenticated"
    SESSION_DM = "session_dm"
    SESSION_MEMBER = "session_member"
    PUBLIC_READ = "public_read"


@dataclass(frozen=True, slots=True)
class ConsolePrincipal:
    """AstrBot 原生管理页主体。"""

    username: str
    correlation_id: str = ""
    auth_source: str = AuthSource.ASTRBOT_CONSOLE.value
    is_console_admin: bool = True

    def to_mapping(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "correlation_id": self.correlation_id,
            "auth_source": self.auth_source,
            "is_admin": self.is_console_admin,
            "role_source": "astrbot_console",
            "capabilities": {
                "admin": self.is_console_admin,
                "author": self.is_console_admin,
                "world_install": self.is_console_admin,
                "economy": self.is_console_admin,
                "dm": self.is_console_admin,
            },
        }


@dataclass(frozen=True, slots=True)
class PlatformPrincipal:
    """绑定真实消息平台用户的副本主体。"""

    platform: str
    platform_user_id: str
    is_plugin_admin: bool = False
    member_role: str = ""
    is_dm: bool = False
    correlation_id: str = ""
    auth_source: str = AuthSource.PLATFORM_BINDING.value

    def to_mapping(self) -> dict[str, Any]:
        return {
            "username": self.platform_user_id,
            "platform": self.platform,
            "platform_user_id": self.platform_user_id,
            "auth_source": self.auth_source,
            "is_admin": self.is_plugin_admin,
            "member_role": self.member_role,
            "is_dm": self.is_dm or self.is_plugin_admin,
            "role_source": (
                "config_admin_ids" if self.is_plugin_admin else "platform_binding"
            ),
            "capabilities": {
                "admin": self.is_plugin_admin,
                "dm": self.is_dm or self.is_plugin_admin,
                "member": bool(self.member_role),
            },
        }


@dataclass(frozen=True, slots=True)
class MiniprogramPrincipal:
    """Bound WeChat/QQ miniprogram identity.

    The public binding reference is opaque and never contains openid/unionid.
    """

    provider: str
    binding_ref: str
    participant_ref: str = ""
    member_role: str = ""
    correlation_id: str = ""
    auth_source: str = AuthSource.MINIPROGRAM_BINDING.value

    def to_mapping(self) -> dict[str, Any]:
        return {
            "username": self.binding_ref,
            "provider": self.provider,
            "binding_ref": self.binding_ref,
            "participant_ref": self.participant_ref,
            "auth_source": self.auth_source,
            "is_admin": False,
            "is_dm": self.member_role in {"dm", "host", "moderator"},
            "member_role": self.member_role,
            "role_source": "miniprogram_binding",
            "capabilities": {
                "admin": False,
                "dm": self.member_role in {"dm", "host", "moderator"},
                "member": bool(self.member_role),
            },
        }

    def to_public_view(
        self,
        *,
        session_ref: str = "",
    ) -> dict[str, Any]:
        role = (
            "dm"
            if self.member_role in {"dm", "host", "moderator"}
            else ("player" if self.participant_ref else "guest")
        )
        capabilities = []
        if self.participant_ref:
            capabilities.extend(("session.read", "session.joined"))
        if role == "dm":
            capabilities.append("session.moderate")
        return {
            "schema": "tavern-principal-view/1.0.0-rc10",
            "principal_ref": self.binding_ref,
            "principal_kind": "miniprogram",
            "role": role,
            "role_source": "miniprogram_binding",
            "binding_state": "active",
            "authenticated": True,
            "session_ref": str(session_ref or "") or None,
            "participant_ref": self.participant_ref or None,
            "capabilities": capabilities,
        }


def _policy_for_path(path: str) -> RoutePolicy | None:
    """按路径前缀返回路由授权策略。

    未匹配任何前缀返回 ``None``（fail closed）。这是有意为之：新增路由
    必须显式加入策略表，避免隐式落入平台主体解析。
    """
    p = str(path or "").strip("/")
    if not p:
        return None

    # 必须代表真实平台 DM 的写操作（含其子路径）。
    session_dm = (
        "sessions/card-review",
        "sessions/card-revisions",
        "sessions/action",
        "sessions/state",
        "sessions/turn-order",
        "sessions/time-rules",
        "sessions/rules",
        "sessions/npc",
        "sessions/timer",
        "sessions/timer-policy",
        "sessions/token-quota",
        "sessions/token-reset",
        "sessions/permission",
        "sessions/participant",
        "sessions/rescue",
        "sessions/inject-fact",
        "sessions/apply-effect",
        "sessions/advance-clock",
        "sessions/pacing/commit",
        "sessions/turn-command",
        "sessions/narrative-control",
        "sessions/narrative-mode",
        "sessions/economy/set-enabled",
        "sessions/economy/adjust",
        "sessions/operations/cancel",
        "sessions/deliveries/action",
        "economy/set-enabled",
        "economy/adjust",
    )

    # 必须代表真实副本成员（含 DM/管理员）的读写。
    session_member = (
        "supplements",
        "dashboard/session-summary",
        "dashboard/session-party",
        "dashboard/session-world-visuals",
        "dashboard/session-history",
        "dashboard/session-generation",
        "sessions/tendencies/me",
        "sessions/tendencies/action",
        "sessions/characters",
        "sessions/world-state",
        "sessions/actor-fate",
        "sessions/assets",
        "sessions/economy",
        "sessions/operations",
        "sessions/deliveries/view",
        "sessions/diagnostics/view",
        "sessions/growth",
        "sessions/changes",
        "sessions/card-source",
        "sessions/recovery",
        "sessions/diagnostics",
        "sessions/turn-preflight",
        "sessions/context-compile",
        "sessions/pacing/preview",
    )

    def _matches(candidate: str, prefixes: tuple[str, ...]) -> bool:
        if candidate in prefixes:
            return True
        return any(candidate.startswith(prefix + "/") for prefix in prefixes)

    surface_routes = frozenset(
        {
            "dashboard/surfaces/dashboard",
            "dashboard/surfaces/tendencies",
            "dashboard/surfaces/sessions",
            "dashboard/surfaces/characters",
            "dashboard/surfaces/memories",
            "dashboard/surfaces/worlds",
            "dashboard/surfaces/designer",
            "dashboard/surfaces/author_jobs",
            "dashboard/surfaces/todo",
            "dashboard/surfaces/audit",
            "dashboard/surfaces/health",
            "dashboard/surfaces/settings",
            "dashboard/surfaces/modules",
            "dashboard/surfaces/about",
        }
    )
    if p.startswith("dashboard/surfaces/"):
        return RoutePolicy.AUTHENTICATED if p in surface_routes else None
    if p == "dashboard/events":
        # The handler resolves a principal-scoped opaque session key and then
        # performs the real membership check.  Generic route middleware does
        # not have the internal session id and must not guess it here.
        return RoutePolicy.AUTHENTICATED
    if p in {"dashboard/intents", "dashboard/recovery-preview"}:
        # The handler accepts only allow-listed semantic intents and resolves
        # principal-scoped opaque handles before repeating domain permission
        # checks. Middleware must not guess a stable object identifier.
        return RoutePolicy.AUTHENTICATED
    if _matches(p, session_dm):
        return RoutePolicy.SESSION_DM
    if _matches(p, session_member):
        return RoutePolicy.SESSION_MEMBER
    # 其余 WebUI 路由均为原生后台控制台管理入口。
    return RoutePolicy.CONSOLE_ADMIN


def route_policy(path: str) -> RoutePolicy | None:
    """返回路由的授权策略；未知路由返回 ``None``。"""
    return _policy_for_path(path)


def classify_principal(source: str) -> AuthSource:
    """把字符串来源规范化为 AuthSource。"""
    if source == AuthSource.ASTRBOT_CONSOLE.value:
        return AuthSource.ASTRBOT_CONSOLE
    if source == AuthSource.MINIPROGRAM_BINDING.value:
        return AuthSource.MINIPROGRAM_BINDING
    return AuthSource.PLATFORM_BINDING


def principal_from_mapping(
    value: Mapping[str, Any],
) -> ConsolePrincipal | PlatformPrincipal | MiniprogramPrincipal:
    """从现有字典主体构造规范主体对象（向后兼容）。"""
    auth_source = str(value.get("auth_source") or "")
    if classify_principal(auth_source) is AuthSource.ASTRBOT_CONSOLE:
        return ConsolePrincipal(
            username=str(value.get("username") or ""),
            correlation_id=str(value.get("correlation_id") or ""),
            auth_source=AuthSource.ASTRBOT_CONSOLE.value,
            is_console_admin=bool(value.get("is_admin", True)),
        )
    if classify_principal(auth_source) is AuthSource.MINIPROGRAM_BINDING:
        return MiniprogramPrincipal(
            provider=str(value.get("provider") or ""),
            binding_ref=str(value.get("binding_ref") or value.get("username") or ""),
            participant_ref=str(value.get("participant_ref") or ""),
            member_role=str(value.get("member_role") or ""),
            correlation_id=str(value.get("correlation_id") or ""),
        )
    return PlatformPrincipal(
        platform=str(value.get("platform") or ""),
        platform_user_id=str(
            value.get("platform_user_id") or value.get("username") or ""
        ),
        is_plugin_admin=bool(value.get("is_admin")),
        member_role=str(value.get("member_role") or ""),
        is_dm=bool(value.get("is_dm")),
        correlation_id=str(value.get("correlation_id") or ""),
    )


__all__ = [
    "AuthSource",
    "ConsolePrincipal",
    "MiniprogramPrincipal",
    "PlatformPrincipal",
    "RoutePolicy",
    "classify_principal",
    "principal_from_mapping",
    "route_policy",
]
