"""D1-ARC-003 平台无关 Web 主体与授权边界。

``Principal`` 只携带平台无关的身份与角色信息；授权函数是纯函数，
不访问数据库、不读取 AstrBot 事件对象。角色判定失败一律关闭
（fail closed）：缺少身份、身份无法映射、字段缺失均视为无权限。

角色等级：``guest < player < dm < admin``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional

from .errors import ForbiddenError, UnauthorizedError

__all__ = [
    "Role",
    "Principal",
    "build_principal",
    "with_role",
    "require_authenticated",
    "require_admin",
    "require_dm",
    "require_member",
    "require_player_own",
    "authorize_diagnostics",
]


class Role(str, Enum):
    """主体角色（按权限从低到高）。"""

    GUEST = "guest"
    PLAYER = "player"
    DM = "dm"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]


_ROLE_RANK = {
    Role.GUEST: 0,
    Role.PLAYER: 1,
    Role.DM: 2,
    Role.ADMIN: 3,
}


@dataclass(frozen=True)
class Principal:
    """当前 Web 请求主体的平台无关描述。"""

    username: str = ""
    role: Role = Role.GUEST
    role_source: str = "unmapped"
    session_id: str = ""
    participant_id: str = ""
    user_id: str = ""
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_authenticated(self) -> bool:
        return bool(str(self.username or "").strip())

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_dm(self) -> bool:
        return self.role.rank >= Role.DM.rank

    @property
    def is_member(self) -> bool:
        return self.role.rank >= Role.PLAYER.rank

    def at_least(self, role: Role) -> bool:
        return self.role.rank >= role.rank

    def owns(self, participant: Any) -> bool:
        """判定主体是否拥有该参与者数据（player-own）。

        participant 可以是 Mapping 或带属性的对象；身份缺失时关闭
        （返回 False），绝不默认放行。
        """
        if participant is None:
            return False
        if isinstance(participant, Mapping):
            pid = participant.get("id") or participant.get("participant_id") or ""
            uid = (
                participant.get("group_user_id")
                or participant.get("user_id")
                or ""
            )
        else:
            pid = (
                getattr(participant, "participant_id", None)
                or getattr(participant, "id", None)
                or ""
            )
            uid = (
                getattr(participant, "group_user_id", None)
                or getattr(participant, "user_id", None)
                or ""
            )
        own_pid = str(self.participant_id or "")
        own_uid = str(self.user_id or "")
        if own_pid and str(pid) and own_pid == str(pid):
            return True
        if own_uid and str(uid) and own_uid == str(uid):
            return True
        return False


def build_principal(
    username: str,
    *,
    is_admin: bool = False,
    role_source: str = "unmapped",
    session_id: str = "",
    participant_id: str = "",
    user_id: str = "",
    capabilities: Optional[Iterable[str]] = None,
) -> Principal:
    """从 Web 登录名与宿主声明构造主体。

    与现有 ``web_console._web_principal`` 的判定语义一致：只有显式
    管理员声明才授予 admin；无法映射的角色默认为 guest。
    """
    return Principal(
        username=str(username or "").strip(),
        role=Role.ADMIN if is_admin else Role.GUEST,
        role_source=role_source,
        session_id=str(session_id or ""),
        participant_id=str(participant_id or ""),
        user_id=str(user_id or ""),
        capabilities=frozenset(capabilities or ()),
    )


def with_role(principal: Principal, role: Role) -> Principal:
    """按新角色重建主体（用于权限提升/降级后的重新判定）。"""
    return Principal(
        username=principal.username,
        role=role,
        role_source=principal.role_source,
        session_id=principal.session_id,
        participant_id=principal.participant_id,
        user_id=principal.user_id,
        capabilities=principal.capabilities,
    )


def require_authenticated(principal: Principal) -> Principal:
    if not isinstance(principal, Principal) or not principal.is_authenticated:
        raise UnauthorizedError(detail="缺少登录身份")
    return principal


def require_admin(principal: Principal) -> Principal:
    require_authenticated(principal)
    if not principal.is_admin:
        raise ForbiddenError(detail=f"需要管理员权限（当前角色 {principal.role.value}）")
    return principal


def require_dm(principal: Principal) -> Principal:
    require_authenticated(principal)
    if not principal.at_least(Role.DM):
        raise ForbiddenError(detail="需要副本主持或管理员权限")
    return principal


def require_member(principal: Principal) -> Principal:
    require_authenticated(principal)
    if not principal.is_member:
        raise ForbiddenError(detail="需要副本成员身份")
    return principal


def require_player_own(principal: Principal, participant: Any) -> Principal:
    """普通玩家只能查看/操作属于本人身份的数据。"""
    require_member(principal)
    if not principal.owns(participant):
        raise ForbiddenError(detail="只能查看或操作本人的数据")
    return principal


def authorize_diagnostics(principal: Principal) -> Principal:
    """诊断接口单独授权：仅管理员可用，普通页面不得复用诊断 DTO。"""
    return require_admin(principal)
