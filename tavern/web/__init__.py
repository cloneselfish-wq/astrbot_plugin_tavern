"""D1-ARC-003 Web 边界层（平台无关）。

本包是 Web API 的统一授权与错误边界：

- ``auth.py``：平台无关的 ``Principal``（admin/dm/member/player-own）与
  授权函数，不依赖 AstrBot 事件对象或数据库实现。
- ``errors.py``：统一 ``ErrorEnvelope``（code / message / recovery /
  correlation_id），技术详情仅对授权角色输出，普通用户不泄漏异常类型、
  路径与内部 ID。

路由模块（``routes/``）和视图（``views/``）由后续拆分任务接入；本包
只提供边界原语，不注册任何路由。
"""

from __future__ import annotations

from .auth import (
    Principal,
    Role,
    authorize_diagnostics,
    build_principal,
    require_admin,
    require_authenticated,
    require_dm,
    require_member,
    require_player_own,
    with_role,
)
from .errors import (
    BadRequestError,
    ConflictError,
    ErrorEnvelope,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    UnauthorizedError,
    WebApiError,
    WebBoundaryError,
    build_envelope,
    classify_status,
    error_payload,
    new_correlation_id,
)

__all__ = [
    "Principal",
    "Role",
    "authorize_diagnostics",
    "build_principal",
    "require_admin",
    "require_authenticated",
    "require_dm",
    "require_member",
    "require_player_own",
    "with_role",
    "BadRequestError",
    "ConflictError",
    "ErrorEnvelope",
    "ForbiddenError",
    "InternalServerError",
    "NotFoundError",
    "UnauthorizedError",
    "WebApiError",
    "WebBoundaryError",
    "build_envelope",
    "classify_status",
    "error_payload",
    "new_correlation_id",
]
