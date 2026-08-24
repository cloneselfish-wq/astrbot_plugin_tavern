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

@route_errors
async def designer_simulate(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    template: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    build: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """角色构筑模拟（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if template is None:
        template = _lazy("tavern.lifecycle", "card_template")
    if build is None:
        build = _lazy("tavern.twp.designer", "build_simulation")
    fields, input_meta = await _resolve_character_fields(data, repos)
    result = build(template(world), fields, world)
    if not isinstance(result, Mapping):
        raise WebRouteError(
            500,
            "authoring.simulation.invalid_result",
            "模拟结果无效。",
            "请检查输入后重试。",
        )
    body = dict(result)
    body["input"] = input_meta
    return ok(body)


@route_errors
async def designer_effects(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    reducer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """效果归约预览（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if reducer is None:
        reducer = _lazy("tavern.twp.designer", "effect_reducer")
    fields, input_meta = await _resolve_character_fields(data, repos)
    result = reducer(
        world,
        fields,
        dry_run=bool(data.get("dry_run", True)),
    )
    if not isinstance(result, Mapping):
        raise WebRouteError(
            500,
            "authoring.effects.invalid_result",
            "效果预览结果无效。",
            "请检查输入后重试。",
        )
    body = dict(result)
    body["input"] = input_meta
    return ok(body)


@route_errors
async def designer_template_diff(
    principal: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    diff: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """模板差异对比（只读）。"""
    require_login(principal)
    data = mapping(payload)
    if diff is None:
        diff = _lazy("tavern.twp.designer", "template_diff")
    return ok(
        diff(
            data.get("base", {}),
            data.get("candidate", {}),
        )
    )


@route_errors
async def designer_card_groups(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    template: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    groups: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """角色卡分组预览（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if template is None:
        template = _lazy("tavern.lifecycle", "card_template")
    if groups is None:
        groups = _lazy("tavern.twp.designer", "card_groups")
    fields = data.get("fields", {})
    fields = dict(fields) if isinstance(fields, Mapping) else {}
    return ok({"groups": groups(template(world), fields)})


@route_errors
async def designer_card_diff(
    principal: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    diff: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """角色卡差异对比（只读）。"""
    require_login(principal)
    data = mapping(payload)
    if diff is None:
        diff = _lazy("tavern.twp.designer", "card_diff")
    return ok(
        diff(
            data.get("current", {}),
            data.get("candidate", {}),
        )
    )


@route_errors
async def designer_preset_references(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    refs: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """预设引用查询（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if refs is None:
        refs = _lazy("tavern.twp.designer", "preset_references")
    references = refs(
        world,
        text(data.get("set_key")),
        text(data.get("preset_id")),
    )
    return ok({"references": references, "count": len(references)})


@route_errors
async def twp_l10n_report(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    report: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """本地化缺口报告（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if report is None:
        report = _lazy("tavern.twp.localization", "localization_report")
    return ok(
        report(
            world,
            requested_locale=text(data.get("requested_locale")) or None,
        )
    )


@route_errors
async def designer_distribution(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    summary: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """分发统计（只读）。"""
    require_login(principal)
    world = await _resolve_world(mapping(payload), repos)
    if summary is None:
        summary = _lazy("tavern.twp.designer", "distribution_summary")
    return ok(summary(world))


@route_errors
async def twp_simulate(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    simulate: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """确定性冒烟模拟（只读）。"""
    require_login(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if simulate is None:
        simulate = _lazy("tavern.twp.simulation", "run_smoke_simulation")
    return ok(
        simulate(
            world,
            turns=to_int(data.get("turns"), 30) or 30,
            party_sizes=data.get("party_sizes") or [1, 4, 8],
        )
    )


@route_errors
async def twp_commands(
    principal: Mapping[str, Any],
    repos: Any,
) -> dict[str, Any]:
    """世界命令目录（只读，世界级元数据）。"""
    require_login(principal)
    catalog = await repos.world_command_catalog()
    if not isinstance(catalog, list):
        catalog = []
    return ok({"items": catalog, "count": len(catalog)})


@route_errors
async def designer_preset_delete(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    delete: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """删除预设并落库（作者）；保存前体检。"""
    require_author(principal)
    data = mapping(payload)
    world = await _resolve_world(data, repos)
    if delete is None:
        delete = _lazy("tavern.twp.designer", "delete_preset")
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "check_template")
    candidate = delete(
        world,
        text(data.get("set_key")),
        text(data.get("preset_id")),
    )
    report = check(candidate)
    if not bool(report.get("compatible")):
        messages = [
            str(item.get("message") or "")
            for item in (report.get("errors") or [])[:5]
        ]
        raise WebRouteError(
            400,
            "authoring.edit.health_failed",
            "删除后模板体检未通过："
            + ("；".join(messages) or "存在未通过项。"),
            "请根据体检错误修正后重试。",
        )
    item = await repos.save_world(
        candidate,
        actor or actor_id(principal),
    )
    if publish is not None:
        publish(
            {
                "type": "world",
                "action": "preset_delete",
                "world_id": item.get("id"),
            }
        )
    return ok({"item": item, "report": report})


__all__ = [name for name in globals() if not name.startswith('__')]


