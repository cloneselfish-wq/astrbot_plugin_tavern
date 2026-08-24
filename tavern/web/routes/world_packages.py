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

def _lazy(module: str, attr: str) -> Any:
    """延迟导入真实实现，保持模块加载轻量且便于测试注入。"""
    if module.startswith("tavern."):
        module = "..." + module[len("tavern.") :]
    return getattr(importlib.import_module(module, package=__package__), attr)


def _require_service(service: Any, code: str, message: str, recovery: str) -> Any:
    if service is None:
        raise WebRouteError(503, code, message, recovery)
    return service


def _require_package_path(package_path: Any) -> Path:
    if package_path is None or str(package_path or "").strip() == "":
        raise WebRouteError(
            400,
            "world.package.missing",
            "请选择一个 TWP ZIP 世界包。",
            "请选择 .zip 世界包后重试。",
        )
    path = Path(str(package_path))
    if not str(path).lower().endswith(".zip"):
        raise WebRouteError(
            400,
            "world.package.invalid_type",
            "世界包只接受 .zip 整包导入，不再接受旧版 JSON。",
            "请重新选择 .zip 世界包后重试。",
        )
    return path


async def _install_twp_zip(
    repos: Any,
    world_twp: Any,
    temp_path: Path,
    actor: str,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """安装一个已就绪的 TWP ZIP 临时文件（install -> install_package_world）。"""
    result = await world_twp.install(temp_path, actor)
    report = dict(result.get("report") or {})
    compiled = dict(report.get("compiled_world") or {})
    installed = await repos.install_package_world(
        compiled,
        package=result.get("package"),
        actor_id=f"package:{actor}",
    )
    item = dict(installed.get("item") or {})
    if publish is not None:
        publish(
            {
                "type": "world_twp",
                "action": "import",
                "package_id": (result.get("package") or {}).get("id"),
            }
        )
    return {
        "item": item,
        "package": result.get("package"),
        "preflight": report,
        "mode": installed.get("mode"),
    }


async def _resolve_world(
    data: Mapping[str, Any],
    repos: Any,
) -> dict[str, Any]:
    """按 payload 解析作者工具使用的世界快照（world_ref 优先，其次 world JSON）。"""
    world_ref = text(data.get("world_ref"))
    world = data.get("world", data.get("world_snapshot", {}))
    if world_ref:
        world = await repos.get_world(world_ref)
    if not isinstance(world, Mapping) or not world:
        raise WebRouteError(
            400,
            "authoring.world.missing",
            "缺少世界内容：请提供 world_ref 或 world JSON。",
            "请重新选择世界或粘贴世界 JSON 后重试。",
        )
    return dict(world)


async def _resolve_character_fields(
    data: Mapping[str, Any],
    repos: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """加载可信角色卡/草稿用于模拟；无副本请求时接受浏览器字段做模板预览。"""
    session_id = text(data.get("session_id"))
    participant_id = text(data.get("participant_id"))
    if not session_id and not participant_id:
        fields = data.get("fields", {})
        return (
            dict(fields) if isinstance(fields, Mapping) else {},
            {"source": "authoring_preview", "trusted": False},
        )
    if not session_id or not participant_id:
        raise WebRouteError(
            400,
            "authoring.simulation.incomplete",
            "真实角色模拟必须同时提供 session_id 与 participant_id。",
            "请选择副本与角色后重试。",
        )
    roster = await repos.list_roster(session_id)
    participant = next(
        (
            item
            for item in roster
            if str(item.get("id") or "") == participant_id
        ),
        None,
    )
    if not isinstance(participant, Mapping):
        raise WebRouteError(
            400,
            "authoring.simulation.unknown_participant",
            "所选角色不属于当前副本。",
            "请重新选择角色后重试。",
        )
    expected = data.get("expected_card_version")
    actual_version = to_int(participant.get("card_version_no"), 0) or 0
    if expected not in (None, "") and to_int(expected, -1) != actual_version:
        raise WebRouteError(
            409,
            "authoring.card.version_changed",
            f"角色卡版本已变化：期望 v{to_int(expected, 0)}，当前 v{actual_version}。",
            "请刷新角色卡后重试。",
        )
    card_profile = participant.get("card_profile")
    draft_profile = participant.get("draft_profile")
    if isinstance(card_profile, Mapping) and card_profile:
        fields = card_profile
        source = "approved_card"
    elif isinstance(draft_profile, Mapping) and draft_profile:
        fields = draft_profile
        source = "active_draft"
    else:
        raise WebRouteError(
            400,
            "authoring.simulation.no_card",
            "所选角色没有可模拟的已保存角色卡或建卡草稿。",
            "请先完成角色卡后重试。",
        )
    character_name = text(
        participant.get("character_name") or participant.get("display_name")
    )
    if not character_name:
        raise WebRouteError(
            409,
            "authoring.simulation.name_missing",
            "所选角色缺少可公开显示的名称，无法安全生成模拟结果。",
            "请先修复角色名称并刷新角色列表后重试。",
        )
    return (
        dict(fields),
        {
            "source": source,
            "trusted": True,
            "session_id": session_id,
            "participant_id": participant_id,
            "character_name": character_name,
            "card_version": actual_version,
            "draft_status": text(participant.get("draft_status")),
            "card_status": text(participant.get("card_status")),
        },
    )


async def _persist_designer_edit(
    repos: Any,
    candidate: Mapping[str, Any],
    actor: str,
    *,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
    check: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存前体检 + 可回滚的作者编辑落库。"""
    if check is None:
        check = _lazy("tavern.twp.validation.privacy", "check_template")
    report = check(candidate)
    if not bool(report.get("compatible")):
        messages = [
            str(item.get("message") or "")
            for item in (report.get("errors") or [])[:5]
        ]
        raise WebRouteError(
            400,
            "authoring.edit.health_failed",
            "编辑后模板体检未通过："
            + ("；".join(messages) or "存在未通过项。"),
            "请根据体检错误修正后重试。",
        )
    item = await repos.save_world_edit(candidate, actor)
    if publish is not None:
        publish(
            {
                "type": "world",
                "action": "designer_edit",
                "world_id": item.get("id"),
            }
        )
    return {"item": item, "report": report}


def _preset_issue_set_id(problem: Mapping[str, Any]) -> str:
    """从契约问题 path 中解析预设库稳定 id（无则返回空串）。"""
    path = str(problem.get("path") or "")
    marker = "preset_libraries."
    if marker in path:
        rest = path.split(marker, 1)[1]
        candidate = rest.split(".", 1)[0]
        if candidate:
            return candidate
    return ""


@route_errors
async def twp_protocol(
    principal: Mapping[str, Any],
    *,
    world_twp: Any = None,
) -> dict[str, Any]:
    """TWP 协议目录（只读）。"""
    require_login(principal)
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    return ok(world_twp.protocol_info())


@route_errors
async def twp_packages(
    principal: Mapping[str, Any],
    *,
    world_twp: Any = None,
) -> dict[str, Any]:
    """已安装 TWP 包列表（只读）。"""
    require_login(principal)
    world_twp = _require_service(
        world_twp,
        "authoring.service_unavailable",
        "世界包服务不可用。",
        "请检查插件运行状态后重试。",
    )
    return ok({"items": world_twp.public_packages()})


__all__ = [name for name in globals() if not name.startswith('__')]


