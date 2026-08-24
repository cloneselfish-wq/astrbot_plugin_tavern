"""D1 Web 纯路由服务的公共边界原语。"""

from __future__ import annotations

import functools
import inspect
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Callable

from ...database_support import DatabaseConflictError
from ...errors import PolicyRejection
from ..errors import WebApiError, build_envelope


logger = logging.getLogger(__name__)


class WebRouteError(WebApiError):
    """返回 ``status/body|error`` 信封的纯路由错误。"""


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def to_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return text(value).lower() in {"1", "true", "yes", "on", "是", "开启"}


def actor_id(principal: Mapping[str, Any]) -> str:
    return text(principal.get("username")) or "web:anonymous"


def ok(body: Mapping[str, Any] | None = None, *, status: int = 200) -> dict[str, Any]:
    return {"status": int(status), "body": mapping(body)}


def _capability(principal: Mapping[str, Any], name: str) -> bool:
    return bool(mapping(principal.get("capabilities")).get(name))


def require_login(principal: Mapping[str, Any]) -> None:
    if not text(principal.get("username")):
        raise WebRouteError(
            401,
            "auth.login_required",
            "请先登录后再执行该操作。",
            "请重新登录，然后重试。",
        )


def require_admin(principal: Mapping[str, Any]) -> None:
    require_login(principal)
    if not (bool(principal.get("is_admin")) or _capability(principal, "admin")):
        raise WebRouteError(
            403,
            "auth.admin_required",
            "该操作仅限管理员执行。",
            "请联系管理员处理。",
        )


def require_author(principal: Mapping[str, Any]) -> None:
    require_login(principal)
    if not (
        bool(principal.get("is_admin"))
        or _capability(principal, "admin")
        or _capability(principal, "author")
    ):
        raise WebRouteError(
            403,
            "auth.admin_required",
            "该操作需要世界作者或管理员权限。",
            "请联系管理员授予作者权限。",
        )


def error_from_exception(exc: BaseException) -> dict[str, Any]:
    envelope = build_envelope(exc, correlation_id=uuid.uuid4().hex)
    logger.error(
        "Tavern Web route failed: correlation_id=%s status=%s",
        envelope.correlation_id,
        envelope.status_code,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    error = envelope.to_payload()["error"]
    return {
        "status": int(envelope.status_code),
        "ok": False,
        "error": error,
    }


def route_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    async def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            result = func(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001
            return error_from_exception(exc)

    return wrapped


from .narrative_control import (  # noqa: E402
    DM_POLICY_KEYS,
    SUPPORTED_COMMANDS,
    narrative_control_view,
    resolve_participant_ref,
    validate_dm_command,
)
from .sessions import (  # noqa: E402
    require_member,
    resolve_viewer_participant,
    resolve_viewer_role,
    session_changes_view,
    session_detail_view,
    session_list_view,
    session_shell_view,
)

__all__ = [
    "DM_POLICY_KEYS",
    "SUPPORTED_COMMANDS",
    "WebRouteError",
    "actor_id",
    "error_from_exception",
    "flag",
    "mapping",
    "narrative_control_view",
    "ok",
    "require_admin",
    "require_author",
    "require_login",
    "require_member",
    "resolve_participant_ref",
    "resolve_viewer_participant",
    "resolve_viewer_role",
    "route_errors",
    "session_changes_view",
    "session_detail_view",
    "session_list_view",
    "session_shell_view",
    "text",
    "to_int",
    "validate_dm_command",
]
