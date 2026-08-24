"""Explicit console authoring mutations for opaque intent dispatch.

These functions are deliberately not registered as legacy dashboard paths.
The console intent layer resolves an opaque target first, then calls one of these
semantic operations with the internal world reference.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from . import (
    WebRouteError,
    actor_id,
    ok,
    require_admin,
    require_author,
    route_errors,
    text,
)
from .world_packages import _lazy, _require_service


def _revision(value: Any) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebRouteError(
            409,
            "authoring.revision_required",
            "当前世界的状态版本无法确认。",
            "请刷新作者工作区后重新提交；你的表单内容仍会保留。",
        ) from exc
    if revision < 1:
        raise WebRouteError(
            409,
            "authoring.revision_required",
            "当前世界的状态版本无法确认。",
            "请刷新作者工作区后重新提交；你的表单内容仍会保留。",
        )
    return revision


def _request_key(value: Any) -> str:
    key = text(value)
    if not key or len(key) > 200:
        raise WebRouteError(
            400,
            "authoring.idempotency_required",
            "本次作者操作缺少有效的防重复凭证。",
            "请保持编辑窗口打开并重新提交。",
        )
    return key


def _health_failure(report: Mapping[str, Any]) -> WebRouteError:
    messages = [
        text(item.get("message"))
        for item in (report.get("errors") or [])[:5]
        if isinstance(item, Mapping)
    ]
    return WebRouteError(
        400,
        "authoring.health_failed",
        "编辑后模板体检未通过："
        + ("；".join(filter(None, messages)) or "存在未通过项。"),
        "请修正标记的字段后重试；尚未覆盖现有世界草稿。",
    )


@route_errors
async def designer_field_save_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    field: Mapping[str, Any],
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    upsert: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """E01: save one card-field definition with CAS and durable replay."""
    require_author(principal)
    world_ref = text(world_ref)
    if not world_ref:
        raise WebRouteError(
            404,
            "authoring.world_missing",
            "要编辑的世界已经不存在。",
            "请刷新作者实验室并重新选择世界。",
        )
    revision = _revision(expected_revision)
    request_key = _request_key(idempotency_key)
    field_input = dict(field) if isinstance(field, Mapping) else {}
    if not field_input:
        raise WebRouteError(
            400,
            "authoring.field_missing",
            "没有可保存的角色卡字段。",
            "请补全字段名称、类型与规则后重试。",
        )
    if upsert is None:
        upsert = _lazy("tavern.twp.designer", "upsert_field")
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "check_template")

    def transform(world: dict[str, Any]) -> Mapping[str, Any]:
        return upsert(world, field_input)

    def validate(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        report = check(candidate)
        if not isinstance(report, Mapping) or not bool(report.get("compatible")):
            raise _health_failure(
                report if isinstance(report, Mapping) else {}
            )
        return report

    result = await repos.apply_world_edit_intent(
        world_ref,
        actor or actor_id(principal),
        expected_revision=revision,
        idempotency_key=request_key,
        operation_type="designer.field_save",
        request_payload={"field": field_input},
        transform=transform,
        validate=validate,
    )
    if publish is not None and not bool(result.get("replayed")):
        publish(
            {
                "type": "world",
                "action": "designer_field_save",
                "world_id": world_ref,
            }
        )
    return ok(result)


@route_errors
async def designer_preset_save_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    set_key: str,
    preset: Mapping[str, Any],
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    upsert: Callable[..., Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """E03: save one preset with CAS and durable replay."""
    require_author(principal)
    world_ref = text(world_ref)
    set_key = text(set_key)
    preset_input = dict(preset) if isinstance(preset, Mapping) else {}
    if not world_ref:
        raise WebRouteError(
            404,
            "authoring.world_missing",
            "要编辑的世界已经不存在。",
            "请刷新作者实验室并重新选择世界。",
        )
    if not set_key or not preset_input:
        raise WebRouteError(
            400,
            "authoring.preset_missing",
            "没有可保存的预设内容。",
            "请选择预设分组并补全预设内容后重试。",
        )
    revision = _revision(expected_revision)
    request_key = _request_key(idempotency_key)
    if upsert is None:
        upsert = _lazy("tavern.twp.designer", "upsert_preset")
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "check_template")

    def transform(world: dict[str, Any]) -> Mapping[str, Any]:
        return upsert(world, set_key, preset_input)

    def validate(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        report = check(candidate)
        if not isinstance(report, Mapping) or not bool(report.get("compatible")):
            raise _health_failure(
                report if isinstance(report, Mapping) else {}
            )
        return report

    result = await repos.apply_world_edit_intent(
        world_ref,
        actor or actor_id(principal),
        expected_revision=revision,
        idempotency_key=request_key,
        operation_type="designer.preset_save",
        request_payload={"set_key": set_key, "preset": preset_input},
        transform=transform,
        validate=validate,
    )
    if publish is not None and not bool(result.get("replayed")):
        publish(
            {
                "type": "world",
                "action": "designer_preset_save",
                "world_id": world_ref,
            }
        )
    return ok(result)


def _module_enabled(package: Mapping[str, Any], module_id: str) -> bool:
    overrides = package.get("module_overrides")
    if isinstance(overrides, Mapping) and module_id in overrides:
        return bool(overrides[module_id])
    for entry in package.get("modules") or []:
        if not isinstance(entry, Mapping):
            continue
        if text(entry.get("module_id") or entry.get("id")) == module_id:
            return bool(entry.get("enabled", True))
    raise WebRouteError(
        400,
        "authoring.module_missing",
        "所选模块已经不存在。",
        "请刷新模块列表后重新选择。",
    )


@route_errors
async def twp_module_toggle_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    package_ref: str,
    module_id: str,
    enabled: bool,
    expected_revision: int,
    idempotency_key: str,
    world_twp: Any = None,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """E25: staged TWP module toggle with compensation and durable replay."""
    require_admin(principal)
    world_ref = text(world_ref)
    package_ref = text(package_ref)
    module_id = text(module_id)
    if not world_ref or not package_ref or not module_id:
        raise WebRouteError(
            400,
            "authoring.module_fields_missing",
            "模块切换缺少世界、世界包或模块。",
            "请刷新模块列表后重新选择。",
        )
    revision = _revision(expected_revision)
    request_key = _request_key(idempotency_key)
    request = {
        "package_ref": package_ref,
        "module_id": module_id,
        "enabled": bool(enabled),
    }
    prepared = await repos.prepare_world_write_intent(
        world_ref,
        actor or actor_id(principal),
        expected_revision=revision,
        idempotency_key=request_key,
        operation_type="world.module_toggle",
        request_payload=request,
    )
    if prepared.get("state") == "completed":
        result = dict(prepared)
        result.pop("state", None)
        result.pop("operation_id", None)
        result.pop("input_hash", None)
        return ok(result)

    package_id = ""
    before_record: dict[str, Any] = {}
    previous_enabled = False
    previous_known = False
    changed_external = False
    try:
        world_twp = _require_service(
            world_twp,
            "authoring.service_unavailable",
            "世界包服务不可用。",
            "请检查插件运行状态后重试。",
        )
        package_id = world_twp.resolve_reference(package_ref)
        current_world = await repos.get_world(world_ref)
        compiled_before = world_twp.compiled_world(package_id)
        if text(compiled_before.get("slug")) != text(current_world.get("slug")):
            raise WebRouteError(
                409,
                "authoring.package_world_mismatch",
                "所选世界包不属于当前世界。",
                "请刷新世界与模块列表后重新选择。",
            )
        before_record = dict(world_twp.get(package_id))
        previous_enabled = _module_enabled(before_record, module_id)
        previous_known = True
        await world_twp.set_module(
            package_id,
            module_id,
            bool(enabled),
            actor or actor_id(principal),
        )
        changed_external = True
        compiled = dict(world_twp.compiled_world(package_id))
        if text(compiled.get("slug")) != text(current_world.get("slug")):
            raise WebRouteError(
                409,
                "authoring.package_world_mismatch",
                "模块编译结果不属于当前世界。",
                "系统将恢复原模块状态；请刷新世界包后重试。",
            )
        compiled["id"] = world_ref
        compiled["revision"] = revision
        result = await repos.commit_world_write_intent(
            compiled,
            actor or actor_id(principal),
            expected_revision=revision,
            idempotency_key=request_key,
            operation_type="world.module_toggle",
            request_payload=request,
        )
    except Exception as exc:
        if not changed_external and previous_known:
            try:
                changed_external = (
                    _module_enabled(world_twp.get(package_id), module_id)
                    != previous_enabled
                )
            except Exception:
                changed_external = True
        compensated = not changed_external
        if changed_external and previous_known:
            try:
                await world_twp.set_module(
                    package_id,
                    module_id,
                    previous_enabled,
                    actor or actor_id(principal),
                )
                restore_record = getattr(
                    world_twp,
                    "restore_package_record",
                    None,
                )
                if callable(restore_record):
                    await restore_record(package_id, before_record)
                compensated = True
            except Exception:
                compensated = False
        elif changed_external:
            compensated = False
        failure_status = (
            int(getattr(exc, "status_code", 500) or 500)
            if isinstance(exc, WebRouteError)
            else (400 if isinstance(exc, (TypeError, ValueError)) else 500)
        )
        retryable = not changed_external and failure_status >= 500
        try:
            await repos.fail_world_write_intent(
                world_ref,
                expected_revision=revision,
                idempotency_key=request_key,
                operation_type="world.module_toggle",
                request_payload=request,
                error_code=(
                    "module.write_failed"
                    if compensated
                    else "module.compensation_failed"
                ),
                retryable=retryable,
            )
        except Exception:
            pass
        if not compensated:
            raise WebRouteError(
                500,
                "authoring.module_compensation_failed",
                "模块切换失败，且世界包文件未能自动恢复。",
                "请停止使用该世界并让管理员重新导入原世界包。",
            ) from exc
        raise

    if publish is not None and not bool(result.get("replayed")):
        publish(
            {
                "type": "world_twp",
                "action": "module",
                "package_ref": world_twp.package_reference(package_id),
            }
        )
    return ok(result)


__all__ = [
    "designer_field_save_intent",
    "designer_preset_save_intent",
    "twp_module_toggle_intent",
]
