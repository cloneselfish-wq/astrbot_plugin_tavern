"""D1-ARC-003 统一 Web 错误边界。

所有 Web API 错误对外统一为：:

    {
      "error": {
        "code": "tavern.web.forbidden",
        "message": "权限不足，无法执行此操作。",
        "recovery": "请确认账号具备所需权限，或联系管理员。"
      }
    }

边界规则：

- 普通用户（非管理员）只能看到按错误类别映射的受控中文文案，绝不回显
  异常类型、堆栈、文件路径、内部 ID 或原始异常文本。
- 关联编号、跟踪编号与技术详情只允许通过独立授权的诊断接口取得；
  通用错误响应即使由管理员触发也不得携带这些字段。
- 内部异常与堆栈只进入日志，由调用方负责记录。

本模块不依赖 AstrBot、数据库连接或任何具体路由实现。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)
from ..errors import PolicyRejection

__all__ = [
    "WebApiError",
    "WebBoundaryError",
    "BadRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "NotFoundError",
    "ConflictError",
    "SemanticContractError",
    "RateLimitError",
    "ServiceUnavailableError",
    "InternalServerError",
    "ErrorEnvelope",
    "new_correlation_id",
    "classify_status",
    "build_envelope",
    "error_payload",
    "bad_request",
    "forbidden",
    "not_found",
]


def new_correlation_id() -> str:
    """生成一次请求的关联编号（供日志与技术支持排查使用）。"""
    return uuid.uuid4().hex


class WebBoundaryError(Exception):
    """Web 边界层错误基类（携带对外契约字段）。"""

    status_code: int = 500
    code: str = "tavern.web.internal_error"
    message: str = "服务器内部错误。"
    recovery: str = "请稍后重试；若仍失败，请联系管理员并说明发生时间与操作。"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        recovery: Optional[str] = None,
        status_code: Optional[int] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.message = message or self.message
        super().__init__(self.message)
        self.status_code = status_code or self.status_code
        self.code = code or self.code
        self.recovery = recovery or self.recovery
        # 仅进入技术详情，绝不进入普通用户可见文案。
        self.detail = detail or ""


class WebApiError(WebBoundaryError):
    """纯路由服务使用的兼容错误类型。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        recovery: str = "",
    ) -> None:
        super().__init__(
            message,
            code=code,
            recovery=recovery,
            status_code=status_code,
        )


def bad_request(message: str, *, recovery: str = "") -> WebApiError:
    return WebApiError(400, "bad_request", message, recovery)


def forbidden(
    message: str = "权限不足，无法执行该操作。",
    *,
    recovery: str = "",
    code: str = "forbidden",
) -> WebApiError:
    return WebApiError(403, code, message, recovery)


def not_found(message: str, *, recovery: str = "") -> WebApiError:
    return WebApiError(404, "not_found", message, recovery)


class BadRequestError(WebBoundaryError):
    status_code = 400
    code = "tavern.web.bad_request"
    message = "请求参数不正确。"
    recovery = "请检查输入内容后重试。"


class UnauthorizedError(WebBoundaryError):
    status_code = 401
    code = "tavern.web.unauthorized"
    message = "登录状态无效或已过期。"
    recovery = "请重新登录后重试。"


class ForbiddenError(WebBoundaryError):
    status_code = 403
    code = "tavern.web.forbidden"
    message = "权限不足，无法执行此操作。"
    recovery = "请确认账号具备所需权限，或联系管理员。"


class NotFoundError(WebBoundaryError):
    status_code = 404
    code = "tavern.web.not_found"
    message = "请求的资源不存在。"
    recovery = "请检查输入内容后重试。"


class ConflictError(WebBoundaryError):
    status_code = 409
    code = "tavern.web.conflict"
    message = "数据冲突，操作无法完成。"
    recovery = "请刷新后重试；若仍失败，请联系管理员。"


class SemanticContractError(WebBoundaryError):
    status_code = 422
    code = "tavern.web.semantic_contract"
    message = "提交的内容不符合世界规则。"
    recovery = "系统没有应用本次内容；请修正后重试。"


class RateLimitError(WebBoundaryError):
    status_code = 429
    code = "tavern.web.rate_limited"
    message = "操作过于频繁。"
    recovery = "请稍后再试。"


class ServiceUnavailableError(WebBoundaryError):
    status_code = 503
    code = "tavern.web.service_unavailable"
    message = "相关服务暂时不可用。"
    recovery = "系统已保留当前状态；请稍后重试。"


class InternalServerError(WebBoundaryError):
    status_code = 500
    code = "tavern.web.internal_error"
    message = "服务器内部错误。"
    recovery = "请稍后重试；若仍失败，请联系管理员并说明发生时间与操作。"


_KNOWN = (
    (
        WebApiError,
        500,
        "tavern.web.internal_error",
        "服务器内部错误。",
        "请稍后重试；若仍失败，请联系管理员并说明发生时间与操作。",
    ),
    # (异常类型, 状态码, 错误码, 中文文案, 恢复指引)
    (
        BadRequestError,
        400,
        "tavern.web.bad_request",
        "请求参数不正确。",
        "请检查输入内容后重试。",
    ),
    (
        UnauthorizedError,
        401,
        "tavern.web.unauthorized",
        "登录状态无效或已过期。",
        "请重新登录后重试。",
    ),
    (
        ForbiddenError,
        403,
        "tavern.web.forbidden",
        "权限不足，无法执行此操作。",
        "请确认账号具备所需权限，或联系管理员。",
    ),
    (
        NotFoundError,
        404,
        "tavern.web.not_found",
        "请求的资源不存在。",
        "请检查输入内容后重试。",
    ),
    (
        ConflictError,
        409,
        "tavern.web.conflict",
        "数据冲突，操作无法完成。",
        "请刷新后重试；若仍失败，请联系管理员。",
    ),
    (
        SemanticContractError,
        422,
        "tavern.web.semantic_contract",
        "提交的内容不符合世界规则。",
        "系统没有应用本次内容；请修正后重试。",
    ),
    (
        RateLimitError,
        429,
        "tavern.web.rate_limited",
        "操作过于频繁。",
        "请稍后再试。",
    ),
    (
        ServiceUnavailableError,
        503,
        "tavern.web.service_unavailable",
        "相关服务暂时不可用。",
        "系统已保留当前状态；请稍后重试。",
    ),
    (
        InternalServerError,
        500,
        "tavern.web.internal_error",
        "服务器内部错误。",
        "请稍后重试；若仍失败，请联系管理员并说明发生时间与操作。",
    ),
)


def classify_status(exc: BaseException) -> int:
    """把领域/平台异常映射到 HTTP 状态码（与现有处理器语义一致）。"""
    from ..protocol.errors import TwpPackageError

    if isinstance(exc, TwpPackageError):
        code = str(exc.issue.code or "")
        if code in {
            "world.slug_duplicate",
            "world.package_identity_conflict",
            "world.package_revision_conflict",
            "world.package_idempotency_conflict",
        }:
            return 409
        return 422
    for cls, status, _code, _msg, _rec in _KNOWN:
        if isinstance(exc, cls):
            return status
    if isinstance(exc, PermissionError):
        return 401
    if isinstance(exc, PolicyRejection):
        return 403
    if isinstance(exc, DatabaseNotFoundError):
        return 404
    if isinstance(exc, LookupError):
        return 404
    if isinstance(exc, (DatabaseConflictError, sqlite3.IntegrityError)):
        return 409
    if isinstance(exc, (InvalidTransitionError, ValueError, TypeError)):
        return 400
    return 500


_PUBLIC_BY_STATUS = {
    400: ("tavern.web.bad_request", "请求参数不正确。", "请检查输入内容后重试。"),
    401: ("tavern.web.unauthorized", "登录状态无效或已过期。", "请重新登录后重试。"),
    403: ("tavern.web.forbidden", "权限不足，无法执行此操作。", "请确认账号具备所需权限，或联系管理员。"),
    404: ("tavern.web.not_found", "请求的资源不存在。", "请检查输入内容后重试。"),
    409: ("tavern.web.conflict", "数据冲突，操作无法完成。", "请刷新后重试；若仍失败，请联系管理员。"),
    422: ("tavern.web.semantic_contract", "提交的内容不符合世界规则。", "系统没有应用本次内容；请修正后重试。"),
    429: ("tavern.web.rate_limited", "操作过于频繁。", "请稍后再试。"),
    503: ("tavern.web.service_unavailable", "相关服务暂时不可用。", "系统已保留当前状态；请稍后重试。"),
    500: ("tavern.web.internal_error", "服务器内部错误。", "请稍后重试；若仍失败，请联系管理员并说明发生时间与操作。"),
}


@dataclass(frozen=True)
class ErrorEnvelope:
    """统一错误信封。

    ``include_technical`` 是独立授权诊断接口的显式开关。通用路由必须
    使用默认值，使关联编号、跟踪编号与技术详情全部留在内部边界。
    """

    code: str
    message: str
    recovery: str
    correlation_id: str
    status_code: int = 500
    technical: Optional[Mapping[str, Any]] = field(default=None)

    def to_payload(self, *, include_technical: bool = False) -> dict[str, Any]:
        next_actions = [self.recovery] if self.recovery else []
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "recovery": self.recovery,
            "operation": self.message,
            "reason": self.message,
            "automatic_action": "系统未修改任何数据。",
            "next_actions": next_actions,
            "retryable": self.status_code in {409, 429, 503},
        }
        if include_technical:
            body["correlation_id"] = self.correlation_id
            body["trace_id"] = self.correlation_id
            if self.technical:
                body["technical"] = dict(self.technical)
        return {"error": body}


def build_envelope(
    exc: BaseException,
    correlation_id: Optional[str] = None,
    *,
    include_technical: bool = False,
    context: Optional[Mapping[str, Any]] = None,
) -> ErrorEnvelope:
    """把异常转换为统一信封。

    - 已知边界异常：沿用其受控文案与恢复指引。
    - 已知领域异常：按类型映射为受控文案，原始信息只进技术详情。
    - 未知异常：一律 500，公开文案固定为“服务器内部错误。”，
      异常类型/原文只进技术详情。
    """
    cid = correlation_id or new_correlation_id()
    for cls, status, code, message, recovery in _KNOWN:
        if isinstance(exc, cls):
            technical = None
            if include_technical:
                technical = {
                    "exception_type": _type_name(exc),
                    "detail": str(exc.detail) if exc.detail else str(exc) if str(exc) else "",
                }
                if context:
                    technical["context"] = dict(context)
            return ErrorEnvelope(
                # 实例显式传入的契约字段优先；否则回退类默认值。
                code=getattr(exc, "code", None) or code,
                message=str(getattr(exc, "message", None) or message),
                recovery=str(getattr(exc, "recovery", None) or recovery),
                correlation_id=cid,
                status_code=int(getattr(exc, "status_code", None) or status),
                technical=technical,
            )
    status = classify_status(exc)
    code, message, recovery = _PUBLIC_BY_STATUS.get(
        status,
        _PUBLIC_BY_STATUS[500],
    )
    technical = None
    if include_technical:
        technical = {
            "exception_type": _type_name(exc),
            "detail": str(exc) or "",
        }
        if context:
            technical["context"] = dict(context)
    return ErrorEnvelope(
        code=code,
        message=message,
        recovery=recovery,
        correlation_id=cid,
        status_code=status,
        technical=technical,
    )


def error_payload(
    exc: BaseException,
    correlation_id: Optional[str] = None,
    *,
    include_technical: bool = False,
    context: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """便捷入口：直接返回可直接序列化的 JSON 负载。"""
    return build_envelope(
        exc,
        correlation_id,
        include_technical=include_technical,
        context=context,
    ).to_payload(include_technical=include_technical)


def _type_name(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__qualname__}"
