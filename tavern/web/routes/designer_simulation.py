from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ...protocol.errors import TwpPackageError
from ...storage import unlink_with_retry
from . import (
    WebRouteError,
    actor_id,
    mapping,
    ok,
    require_admin,
    require_author,
    require_login,
    route_errors,
    text,
    to_int,
)
from .world_packages import *
from .world_imports import *
from .designer_content import *
from .designer_validation import *

@route_errors
async def designer_preset_save(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    upsert: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存预设（作者）。"""
    require_author(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if upsert is None:
        upsert = _lazy("tavern.twp.designer", "upsert_preset")
    candidate = upsert(
        world,
        text(data.get("set_key")),
        mapping(data.get("preset")),
    )
    return ok(
        await _persist_designer_edit(
            repos,
            candidate,
            actor or actor_id(principal),
            publish=publish,
            check=check,
        )
    )


@route_errors
async def designer_field_save(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    upsert: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存角色卡字段定义（作者）。"""
    require_author(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if upsert is None:
        upsert = _lazy("tavern.twp.designer", "upsert_field")
    candidate = upsert(world, mapping(data.get("field")))
    return ok(
        await _persist_designer_edit(
            repos,
            candidate,
            actor or actor_id(principal),
            publish=publish,
            check=check,
        )
    )


@route_errors
async def designer_reorder(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    reorder: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """预设排序并落库（作者）。"""
    require_author(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if reorder is None:
        reorder = _lazy("tavern.twp.designer", "reorder_presets")
    candidate = reorder(
        world,
        text(data.get("set_key")),
        data.get("order", []),
    )
    return ok(
        await _persist_designer_edit(
            repos,
            candidate,
            actor or actor_id(principal),
            publish=publish,
            check=check,
        )
    )


@route_errors
async def designer_revert(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """回滚最近一次作者编辑（作者）。"""
    require_author(principal)
    data = mapping(payload)
    world_id = text(data.get("world_ref")) or text(data.get("id"))
    if not world_id:
        raise WebRouteError(
            400,
            "authoring.revert.missing_world",
            "缺少 world_ref。",
            "请选择要回滚的世界后重试。",
        )
    item = await repos.revert_world_edit(
        world_id,
        actor or actor_id(principal),
    )
    if publish is not None:
        publish(
            {
                "type": "world",
                "action": "designer_revert",
                "world_id": item.get("id"),
            }
        )
    return ok({"item": item})


__all__ = [name for name in globals() if not name.startswith('__')]


