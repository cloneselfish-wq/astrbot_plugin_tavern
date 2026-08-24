"""opening recommendation and pre-performance override routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import (
    WebRouteError,
    actor_id,
    mapping,
    ok,
    require_admin,
    require_login,
    route_errors,
    text,
    to_int,
)

__all__ = ["opening_view", "override_opening"]


@route_errors
async def opening_view(
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
            "opening.session_required",
            "缺少要查看的故事副本。",
            "请返回副本列表重新打开。",
        )
    result = await repos.opening_decision(session_id)
    if result is None:
        result = await repos.prepare_opening_decision(session_id)
    return ok(
        {
            **result,
            "permissions": {
                "can_override": bool(principal.get("is_admin"))
                and not bool(result.get("frozen")),
            },
        }
    )


@route_errors
async def override_opening(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    require_admin(principal)
    data = mapping(payload)
    session_id = text(data.get("session_id"))
    option_ref = text(data.get("option_ref"))
    expected_revision = to_int(data.get("expected_revision"))
    if not session_id or not option_ref or expected_revision is None:
        raise WebRouteError(
            400,
            "opening.missing_fields",
            "开局覆盖缺少副本、选项或最新修订号。",
            "请刷新开局建议后重新选择。",
        )
    current = await repos.opening_decision(session_id)
    if current is None:
        current = await repos.prepare_opening_decision(session_id)
    selected = next(
        (
            item
            for item in current.get("candidates") or []
            if text(item.get("option_ref")) == option_ref
        ),
        None,
    )
    if selected is None:
        raise WebRouteError(
            422,
            "opening.option_invalid",
            "所选开局不属于当前世界或已经失效。",
            "请刷新开局建议后重新选择。",
        )
    result = await repos.override_opening_decision(
        session_id,
        text(selected.get("scene_ref")),
        actor_id(principal),
        expected_revision,
    )
    return ok(result)

