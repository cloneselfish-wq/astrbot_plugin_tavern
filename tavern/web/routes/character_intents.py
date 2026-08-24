"""Principal-scoped RC8 resident-character mutations.

The public console route resolves opaque handles before entering this module.  No
browser request supplies a database id, slug, or raw profile document.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from . import WebRouteError, actor_id, mapping, ok, require_author, route_errors, text


def _revision(value: Any) -> int:
    try:
        revision = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise WebRouteError(
            409,
            "character.revision_required",
            "当前常驻角色的状态版本无法确认。",
            "请刷新作者实验室后重新提交；表单内容会保留。",
        ) from exc
    if revision < 1:
        raise WebRouteError(
            409,
            "character.revision_required",
            "当前常驻角色的状态版本无法确认。",
            "请刷新作者实验室后重新提交；表单内容会保留。",
        )
    return revision


def _request_key(value: Any) -> str:
    key = text(value)
    if not key or len(key) > 200:
        raise WebRouteError(
            400,
            "character.idempotency_required",
            "本次常驻角色操作缺少有效的防重复凭证。",
            "请保持操作窗口打开并重新提交。",
        )
    return key


def _safe_item(result: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    item = mapping(result.get("item"))
    return {
        "operation": operation,
        "state": "已退役" if operation == "retire" else "已保存",
        "label": text(item.get("name"), "常驻角色"),
        "revision": int(item.get("revision") or 0),
        "replayed": bool(result.get("replayed")),
    }


@route_errors
async def save_resident_character_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    character_ref: str = "",
    values: Mapping[str, Any] | None = None,
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """E08 replacement: structured create/update with CAS and replay."""

    require_author(principal)
    world_ref = text(world_ref)
    if not world_ref:
        raise WebRouteError(
            404,
            "character.world_missing",
            "要编辑的世界已经不存在。",
            "请刷新作者实验室并重新选择世界。",
        )
    safe_values = mapping(values)
    allowed = {"name", "role", "description", "private_direction", "enabled"}
    if set(safe_values) - allowed:
        raise WebRouteError(
            400,
            "character.fields_invalid",
            "常驻角色表单包含未登记字段。",
            "请关闭操作窗口，刷新作者实验室后重试。",
        )
    if not text(safe_values.get("name")):
        raise WebRouteError(
            400,
            "character.name_required",
            "常驻角色名称不能为空。",
            "请填写玩家可辨认的名称后重试。",
        )
    result = await repos.save_character_intent(
        world_ref,
        actor or actor_id(principal),
        character_id=text(character_ref),
        values=safe_values,
        expected_revision=_revision(expected_revision),
        idempotency_key=_request_key(idempotency_key),
    )
    if publish is not None and not bool(result.get("replayed")):
        publish({"type": "world", "action": "resident_character_saved"})
    return ok(_safe_item(result, operation="update" if character_ref else "create"))


@route_errors
async def retire_resident_character_intent(
    principal: Mapping[str, Any],
    repos: Any,
    *,
    world_ref: str,
    character_ref: str,
    reason: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str = "",
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """C06 replacement: disable the definition and preserve every reference."""

    require_author(principal)
    world_ref = text(world_ref)
    character_ref = text(character_ref)
    reason = text(reason)
    if not world_ref or not character_ref:
        raise WebRouteError(
            404,
            "character.target_missing",
            "要退役的常驻角色已经不存在。",
            "请刷新内容树后重新选择。",
        )
    if not reason:
        raise WebRouteError(
            400,
            "character.retire_reason_required",
            "退役常驻角色前需要说明原因。",
            "请填写对其他作者可读的退役原因后重试。",
        )
    result = await repos.retire_character_intent(
        world_ref,
        character_ref,
        actor or actor_id(principal),
        expected_revision=_revision(expected_revision),
        idempotency_key=_request_key(idempotency_key),
        reason=reason,
    )
    if publish is not None and not bool(result.get("replayed")):
        publish({"type": "world", "action": "resident_character_retired"})
    return ok(_safe_item(result, operation="retire"))


__all__ = [
    "retire_resident_character_intent",
    "save_resident_character_intent",
]
