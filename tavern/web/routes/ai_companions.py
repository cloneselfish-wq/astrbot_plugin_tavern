"""AI companion management routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import (
    WebRouteError,
    mapping,
    ok,
    require_admin,
    require_login,
    route_errors,
    text,
    to_int,
)

__all__ = [
    "act_on_ai_decision",
    "configure_ai_companions",
    "list_ai_companions",
]


@route_errors
async def list_ai_companions(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_login(principal)
    session_id = text(mapping(query).get("session_id"))
    if not session_id:
        raise WebRouteError(
            400,
            "ai_companion.session_required",
            "缺少要查看的副本。",
            "请返回副本列表重新打开。",
        )
    result = await repos.list_ai_companions(session_id)
    return ok(
        {
            **result,
            "permissions": {
                "can_manage": bool(principal.get("is_admin")),
                "maximum_active": 8,
                "default_visible_limit": 3,
            },
        }
    )


@route_errors
async def configure_ai_companions(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(principal)
    data = mapping(payload)
    session_id = text(data.get("session_id"))
    idempotency_key = text(data.get("idempotency_key"))
    expected_revision = to_int(data.get("expected_revision"))
    count = to_int(data.get("count"))
    mode = text(data.get("mode"), "confirm")
    if (
        not session_id
        or not idempotency_key
        or expected_revision is None
        or count is None
    ):
        raise WebRouteError(
            400,
            "ai_companion.missing_fields",
            "AI 队友配置缺少副本、数量、修订号或幂等键。",
            "请刷新副本后重新提交配置。",
        )
    result = await repos.configure_ai_companions(
        session_id=session_id,
        count=count,
        mode=mode,
        expected_session_revision=expected_revision,
        idempotency_key=idempotency_key,
    )
    return ok(result)


@route_errors
async def act_on_ai_decision(
    principal: Mapping[str, Any],
    runner: Any,
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(principal)
    data = mapping(payload)
    session_id = text(data.get("session_id"))
    operation_ref = text(data.get("operation_ref"))
    action = text(data.get("action"))
    expected_revision = to_int(data.get("expected_session_revision"))
    if not session_id or not operation_ref or action not in {
        "confirm",
        "reselect",
        "pause",
    }:
        raise WebRouteError(
            400,
            "ai_companion.decision_missing_fields",
            "AI 队友操作缺少副本、待确认项或合法动作。",
            "请刷新副本后重新选择确认、重选或暂停。",
        )
    if action == "confirm":
        if expected_revision is None:
            raise WebRouteError(
                400,
                "ai_companion.revision_required",
                "确认 AI 行动需要最新副本修订号。",
                "请刷新副本后重新确认。",
            )
        result = await runner.confirm_pending(
            session_id=session_id,
            operation_ref=operation_ref,
            expected_session_revision=expected_revision,
        )
    elif action == "reselect":
        result = await runner.reselect_pending(
            session_id=session_id,
            operation_ref=operation_ref,
        )
    else:
        result = await runner.pause_pending(
            session_id=session_id,
            operation_ref=operation_ref,
        )
    return ok(result)

