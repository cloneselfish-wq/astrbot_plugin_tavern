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

@route_errors
async def twp_preflight(
    principal: Mapping[str, Any],
    *,
    package_path: Any = None,
    inspect: Callable[[Path], Any] | None = None,
) -> dict[str, Any]:
    """预检 TWP ZIP 世界包；不可兼容时返回 200 + compatible=False 报告。"""
    require_login(principal)
    path = _require_package_path(package_path)
    if inspect is None:
        inspect = _lazy("tavern.protocol.references", "inspect_twp_archive")
    try:
        result = inspect(path)
    except TwpPackageError as exc:
        return ok(
            {
                "compatible": False,
                "issues": [item.export() for item in exc.issues],
                "summary": {},
            }
        )
    if not isinstance(result, Mapping):
        raise WebRouteError(
            500,
            "authoring.preflight.invalid_report",
            "世界包预检结果无效。",
            "请重新上传世界包后重试。",
        )
    return ok(dict(result))


@route_errors
async def twp_import(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    package_path: Any = None,
    world_twp: Any = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """导入 TWP ZIP 世界包（管理员）。"""
    require_admin(principal)
    path = _require_package_path(package_path)
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    return ok(
        await _install_twp_zip(
            repos,
            world_twp,
            path,
            actor or actor_id(principal),
            publish,
        )
    )


@route_errors
async def twp_export(
    principal: Mapping[str, Any],
    *,
    package_id: str = "",
    world_twp: Any = None,
) -> dict[str, Any]:
    """导出原始 TWP ZIP 包（返回文件元数据；文件流由 web 层负责）。"""
    require_login(principal)
    package_id = text(package_id)
    if not package_id:
        raise WebRouteError(
            400,
            "world.package.missing",
            "缺少世界包标识。",
            "请选择要导出的世界包后重试。",
        )
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    resolved_id = world_twp.resolve_reference(package_id)
    item = world_twp.get(resolved_id)
    version = text(item.get("version"), "unknown")
    return ok(
        {
            "package_ref": world_twp.package_reference(resolved_id),
            "version": version,
            "filename": f"tavern-world-{version}.zip",
            "content_type": "application/zip",
        }
    )


@route_errors
async def twp_module_toggle(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    world_twp: Any = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """切换 TWP 世界包模块并落库（管理员）。"""
    require_admin(principal)
    data = mapping(payload)
    package_id = text(data.get("package_ref") or data.get("package_id"))
    module_id = text(data.get("module_id"))
    if not package_id or not module_id:
        raise WebRouteError(
            400,
            "world.module.missing_fields",
            "需要 package_id 与 module_id。",
            "请选择世界包与模块后重试。",
        )
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    actor = actor or actor_id(principal)
    result = await world_twp.set_module(
        package_id,
        module_id,
        bool(data.get("enabled")),
        actor,
    )
    compiled = dict(world_twp.compiled_world(package_id))
    current = await repos.get_world(text(compiled.get("slug")))
    compiled["id"] = current.get("id")
    compiled["revision"] = current.get("revision")
    item = await repos.save_world(compiled, actor)
    if publish is not None:
        publish(
            {
                "type": "world_twp",
                "action": "module",
                "package_ref": world_twp.package_reference(
                    world_twp.resolve_reference(package_id)
                ),
            }
        )
    body = dict(result) if isinstance(result, Mapping) else {}
    body["item"] = item
    return ok(body)


@route_errors
async def twp_preset_libraries(
    principal: Mapping[str, Any],
    *,
    package_id: str = "",
    world_twp: Any = None,
    normalize: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """TWP 演员预设库视图；管理员才可编辑。"""
    require_login(principal)
    package_id = text(package_id)
    if not package_id:
        raise WebRouteError(
            400,
            "world.package.missing",
            "缺少世界包标识。",
            "请选择世界包后重试。",
        )
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    world = dict(world_twp.compiled_world(package_id))
    rules = mapping(world.get("rules"))
    actor = mapping(rules.get("actor"))
    if normalize is None:
        normalize = _lazy("tavern.presets", "normalize_preset_libraries")
    try:
        normalized = mapping(normalize(actor))
    except Exception as exc:
        from ...presets import PresetLibraryContractError

        if isinstance(exc, PresetLibraryContractError):
            raise WebRouteError(
                400,
                "authoring.preset_library.contract",
                f"预设库契约错误（{exc.code}）：{exc}",
                "请修正预设库内容后重试。",
            ) from exc
        raise
    issues = [
        {
            "code": str(
                problem.get("code")
                or "actor.preset_library.problem"
            ),
            "set_id": _preset_issue_set_id(problem),
            "path": str(problem.get("path") or ""),
            "message": str(
                problem.get("message") or "预设库信息不完整"
            ),
            "severity": str(problem.get("severity") or "error"),
        }
        for problem in (normalized.get("problems") or [])
        if isinstance(problem, Mapping)
    ]
    return ok(
        {
            "package_id": package_id,
            "candidate_contract": text(
                actor.get("candidate_contract"),
                "twp-actor-candidate/1.0.0-rc10",
            ),
            "items": normalized.get("items") or [],
            "count": to_int(normalized.get("count"), 0) or 0,
            "referenced_library_ids": list(
                normalized.get("referenced_library_ids") or []
            ),
            "metadata_complete": bool(
                normalized.get("metadata_complete")
            ),
            "issues": issues,
            "permissions": {
                "can_view": True,
                "can_edit": bool(principal.get("is_admin")),
                "role_source": str(
                    principal.get("role_source") or "unmapped"
                ),
            },
        }
    )


__all__ = [name for name in globals() if not name.startswith('__')]


