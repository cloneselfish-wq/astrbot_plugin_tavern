"""D1-ARC-003：世界库路由（纯函数，不依赖 Web 框架与宿主）。

世界库接口边界（D1-ARC-003 §4.2）：

- 只处理世界包目录、排序、归档/恢复与内置世界安装状态；
- 不返回任何会话运行状态（session runtime / world_state / 计时器）；
- 技术字段（slug / package_id / revision / artifact 哈希等）只进入
  ``technical_details`` 且仅管理员视角可见（复用领域投影）。

所有函数签名：
``(principal, repos, ...) -> {"status": int, "body": dict}``，
错误统一走 ``tavern.web.routes`` 的错误 envelope。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, Callable

from ...builtin_worlds import (
    merge_builtin_world_statuses,
    project_world_catalog,
)
from ...constants import DEFAULT_WORLD_SLUG
from ...projections.character import VIEWER_ROLES

from . import (
    WebRouteError,
    actor_id,
    flag,
    mapping,
    ok,
    require_admin,
    require_login,
    route_errors,
    text,
    to_int,
)

__all__ = [
    "archive_world",
    "archive_world_intent",
    "builtin_world_status",
    "list_worlds",
    "reorder_world",
    "restore_world",
    "retry_builtin_world",
]

# 世界库接口不得泄漏的会话运行状态字段（防御性剥离）。
_SESSION_STATE_KEYS = (
    "session_state",
    "world_state",
    "world_state_json",
    "runtime",
    "live_state",
    "current_turn",
    "turn_no",
    "session_status",
    "clocks",
    "timers",
)


async def _builtin_status_list(
    builtin_statuses: Callable[[], Any] | list[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """读取内置世界安装状态；没有注入时安全返回空列表。"""
    if builtin_statuses is None:
        return []
    value = builtin_statuses() if callable(builtin_statuses) else builtin_statuses
    if inspect.isawaitable(value):
        value = await value
    if isinstance(value, list):
        return [
            dict(item)
            for item in value
            if isinstance(item, Mapping)
        ]
    return []


def _merge_builtin_statuses(
    items: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    *,
    can_retry: bool,
) -> None:
    """把内置世界安装状态合并进世界卡（就地修改 items）。"""
    merge_builtin_world_statuses(
        items,
        statuses,
        can_retry=can_retry,
        viewer_role="admin" if can_retry else "player",
        include_technical_refs=can_retry,
    )


@route_errors
async def list_worlds(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    query: Mapping[str, Any] | None = None,
    builtin_statuses: Callable[[], Any] | list[Mapping[str, Any]] | None = None,
    default_slug: str | None = None,
    viewer_role: str | None = None,
) -> dict[str, Any]:
    """世界库列表：玩家视角隐藏技术字段，管理员视角包含 technical_details。"""
    require_login(principal)
    is_admin = bool(principal.get("is_admin"))
    include_archived = flag(mapping(query).get("include_archived"))
    rows = await repos.list_worlds(include_archived=include_archived)
    role = str(viewer_role or ("admin" if is_admin else "player")).strip().lower()
    if role not in VIEWER_ROLES:
        raise WebRouteError(
            400,
            "world.library.invalid_viewer",
            "未知世界投影视角。",
            "请刷新页面后重试。",
        )
    items = project_world_catalog(
        rows,
        default_slug=default_slug or DEFAULT_WORLD_SLUG,
        viewer_role=role,
        include_technical_refs=is_admin,
    )
    # 世界库接口不得返回任何会话运行状态（D1-ARC-003 §4.2）。
    for item in items:
        for key in _SESSION_STATE_KEYS:
            item.pop(key, None)
    _merge_builtin_statuses(
        items,
        await _builtin_status_list(builtin_statuses),
        can_retry=is_admin,
    )
    return ok(
        {
            "items": items,
            "permissions": {
                "can_install_worlds": is_admin,
                "can_manage_worlds": is_admin,
                "role_source": str(principal.get("role_source") or "unmapped"),
            },
        }
    )


@route_errors
async def reorder_world(
    principal: Mapping[str, Any],
    repos: Any,
    payload: Mapping[str, Any] | None = None,
    *,
    actor: str = "",
) -> dict[str, Any]:
    """调整世界库排序（管理员）。"""
    require_admin(principal)
    data = mapping(payload)
    world_id = text(data.get("id"))
    if not world_id:
        raise WebRouteError(
            400,
            "world.order.missing_target",
            "缺少要排序的世界。",
            "请选择世界后再试。",
        )
    sort_order = to_int(data.get("sort_order"), 1) or 1
    item = await repos.set_world_sort_order(
        world_id,
        sort_order,
        actor or actor_id(principal),
    )
    return ok({"item": item})


@route_errors
async def archive_world_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    expected_revision: int,
    idempotency_key: str,
    default_world_slug: str = "",
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """C05: archive one resolved world with CAS and durable replay."""
    require_admin(principal)
    world_ref = text(world_ref)
    request_key = text(idempotency_key)
    try:
        revision = int(expected_revision)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebRouteError(
            409,
            "world.revision_required",
            "当前世界的状态版本无法确认。",
            "请刷新世界库后重新确认归档。",
        ) from exc
    if not world_ref:
        raise WebRouteError(
            404,
            "world.missing",
            "要归档的世界已经不存在。",
            "请刷新世界库后重新选择。",
        )
    if revision < 1:
        raise WebRouteError(
            409,
            "world.revision_required",
            "当前世界的状态版本无法确认。",
            "请刷新世界库后重新确认归档。",
        )
    if not request_key or len(request_key) > 200:
        raise WebRouteError(
            400,
            "world.idempotency_required",
            "本次归档缺少有效的防重复凭证。",
            "请保持确认窗口打开并重新提交。",
        )
    current = await repos.get_world(world_ref)
    protected_slug = text(default_world_slug)
    if protected_slug and text(current.get("slug")) == protected_slug:
        raise WebRouteError(
            400,
            "world.default_protected",
            "默认世界不能归档。",
            "请先在设置中更换默认世界，再重新归档。",
        )
    result = await repos.archive_world_intent(
        world_ref,
        actor or actor_id(principal),
        expected_revision=revision,
        idempotency_key=request_key,
    )
    if publish is not None and not bool(result.get("replayed")):
        publish(
            {
                "type": "world",
                "action": "archive",
                "world_id": world_ref,
            }
        )
    return ok(result)


@route_errors
async def archive_world(
    principal: Mapping[str, Any],
    repos: Any,
    payload: Mapping[str, Any] | None = None,
    *,
    actor: str = "",
    default_slug: str | None = None,
) -> dict[str, Any]:
    """归档世界（管理员）；默认世界不允许归档。"""
    require_admin(principal)
    data = mapping(payload)
    world_id = text(data.get("id"))
    if not world_id:
        raise WebRouteError(
            400,
            "world.archive.missing_target",
            "缺少要归档的世界。",
            "请选择世界后再试。",
        )
    world = await repos.get_world(world_id)
    if str(world.get("slug") or "") == str(default_slug or DEFAULT_WORLD_SLUG):
        raise WebRouteError(
            400,
            "world.archive.default_protected",
            "该世界是当前默认世界，请先在设置中更换默认世界。",
            "请先在设置中更换默认世界后再归档。",
        )
    item = await repos.archive_world(world_id, actor or actor_id(principal))
    return ok({"item": item})


@route_errors
async def restore_world(
    principal: Mapping[str, Any],
    repos: Any,
    payload: Mapping[str, Any] | None = None,
    *,
    actor: str = "",
) -> dict[str, Any]:
    """恢复已归档世界（管理员）。"""
    require_admin(principal)
    data = mapping(payload)
    world_id = text(data.get("id"))
    if not world_id:
        raise WebRouteError(
            400,
            "world.restore.missing_target",
            "缺少要恢复的世界。",
            "请选择世界后再试。",
        )
    item = await repos.restore_world(world_id, actor or actor_id(principal))
    return ok({"item": item})


@route_errors
async def builtin_world_status(
    principal: Mapping[str, Any],
    *,
    builtin_statuses: Callable[[], Any] | list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """内置世界安装状态（GET，只读）。"""
    require_login(principal)
    items = await _builtin_status_list(builtin_statuses)
    return ok(
        {
            "items": items,
            "permissions": {
                "can_retry": bool(principal.get("is_admin")),
                "role_source": str(principal.get("role_source") or "unmapped"),
            },
        }
    )


@route_errors
async def retry_builtin_world(
    principal: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    retry: Callable[[str], Any] | None = None,
    audit: Callable[..., Any] | None = None,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """重试失败的内置世界安装（管理员）。"""
    require_admin(principal)
    data = mapping(payload)
    key = text(data.get("key"))
    if not key:
        raise WebRouteError(
            400,
            "world.builtin_retry.missing_key",
            "缺少内置世界标识。",
            "请选择要重试的内置世界后重试。",
        )
    if retry is None:
        raise WebRouteError(
            403,
            "world.builtin_retry.unavailable",
            "当前宿主未提供内置世界重试能力。",
            "请确认插件运行环境支持后重试。",
        )
    result = retry(key)
    if inspect.isawaitable(result):
        result = await result
    if audit is not None:
        try:
            await audit(
                "",
                f"web:{text(principal.get('username'))}",
                "builtin_world.retry",
                key,
                {},
            )
        except Exception:
            pass
    item = (
        dict(result)
        if isinstance(result, Mapping)
        else {"key": key, "state": "requested"}
    )
    if publish is not None:
        publish(
            {
                "type": "world",
                "action": "builtin_retry",
                "key": key,
                "state": str(item.get("state") or ""),
            }
        )
    return ok({"item": item})

__all__ = [name for name in globals() if not name.startswith('__')]

