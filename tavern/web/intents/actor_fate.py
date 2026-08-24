"""Allow-listed WebUI intents for actor-owned lethal fate previews."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ...database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)
from ..routes.sessions import require_member, resolve_viewer_participant
from ..surfaces.registry import resolve_surface_key
from .common import actor_id, flag, mapping, text
from .sessions import _checked_input, _route_error, _safe_success


def _fate_intent_error(
    status: int,
    code: str,
    *,
    operation: str,
    reason: str,
    next_step: str,
):
    return _route_error(
        status,
        code,
        f"失败操作：{operation}\n原因：{reason}",
        (
            "自动处理：系统没有修改角色命运或救援窗口。\n"
            f"下一步：{next_step}"
        ),
    )


async def _actor_fate_preview_action(
    principal: Mapping[str, Any],
    database: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved_target = resolve_surface_key(
        principal,
        "sessions",
        target_key,
        kind="fate-preview",
    )
    try:
        resolved = mapping(json.loads(text(resolved_target)))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _fate_intent_error(
            409,
            "actor_fate.preview_key_invalid",
            operation="处理本人致命命运预览",
            reason="所选预览已失效或不属于当前角色。",
            next_step="请刷新跑团现场后重新选择。",
        ) from exc
    session_id = text(resolved.get("session_id"))
    preview_operation_id = text(resolved.get("preview_operation_id"))
    if not session_id or not preview_operation_id:
        raise _fate_intent_error(
            409,
            "actor_fate.preview_key_invalid",
            operation="处理本人致命命运预览",
            reason="所选预览已失效或不属于当前角色。",
            next_step="请刷新跑团现场后重新选择。",
        )
    role = await require_member(database, session_id, principal)
    if role != "player" or bool(principal.get("is_admin")):
        raise _fate_intent_error(
            403,
            "actor_fate.actor_required",
            operation="确认或拒绝致命命运预览",
            reason="主持人或管理员不能代替角色本人确认。",
            next_step="请让目标角色本人从已认证账号处理该预览。",
        )
    auth_source = text(principal.get("auth_source"))
    participant = await resolve_viewer_participant(
        database,
        session_id,
        "" if auth_source == "miniprogram_binding" else text(
            principal.get("username")
        ),
        text(principal.get("participant_ref"))
        if auth_source == "miniprogram_binding"
        else "",
    )
    if not participant:
        raise _fate_intent_error(
            403,
            "actor_fate.participant_required",
            operation="处理本人致命命运预览",
            reason="当前账号没有可确认的出场角色身份。",
            next_step="请检查玩家绑定后重新打开跑团现场。",
        )
    values = _checked_input(
        body,
        allowed=frozenset({"acknowledge_fate"}),
    )
    if not flag(values.get("acknowledge_fate")):
        raise _fate_intent_error(
            400,
            "actor_fate.acknowledgement_required",
            operation="提交本人命运预览选择",
            reason="尚未确认已阅读致命原因、替代方案与救援影响。",
            next_step="请勾选确认框后重新提交。",
        )
    previews = await database.list_actor_fate_previews(
        session_id,
        text(participant.get("id")),
        status="",
    )
    selected = next(
        (
            dict(item)
            for item in previews
            if text(item.get("operation_id")) == preview_operation_id
        ),
        {},
    )
    if not selected:
        raise _fate_intent_error(
            409,
            "actor_fate.preview_missing",
            operation="处理本人致命命运预览",
            reason="所选预览已失效或不属于当前角色。",
            next_step="请刷新跑团现场后重新选择。",
        )
    decision = (
        "accept" if intent == "actor_fate.preview.accept" else "refuse"
    )
    try:
        result = await database.resolve_actor_fate_preview(
            session_id=session_id,
            preview_operation_id=preview_operation_id,
            participant_id=text(participant.get("id")),
            decision=decision,
            expected_revision=expected_revision,
            actor_id=text(participant.get("group_user_id")) or actor_id(principal),
            idempotency_key=idempotency_key,
        )
    except PermissionError as exc:
        raise _fate_intent_error(
            403,
            "actor_fate.actor_required",
            operation="处理本人致命命运预览",
            reason="该预览不属于当前角色。",
            next_step="请返回自己的跑团现场重新选择。",
        ) from exc
    except DatabaseNotFoundError as exc:
        raise _fate_intent_error(
            409,
            "actor_fate.preview_missing",
            operation="处理本人致命命运预览",
            reason="预览或角色命运状态已不存在。",
            next_step="请刷新跑团现场后重新选择。",
        ) from exc
    except (
        DatabaseConflictError,
        InvalidTransitionError,
        ValueError,
    ) as exc:
        raise _fate_intent_error(
            409,
            "actor_fate.preview_conflict",
            operation="处理本人致命命运预览",
            reason="预览、角色状态或冻结世界规则已经变化。",
            next_step="请刷新跑团现场后重新选择。",
        ) from exc
    if text(result.get("status")) == "expired":
        raise _fate_intent_error(
            409,
            "actor_fate.preview_expired",
            operation="处理本人致命命运预览",
            reason="该预览已经超过世界规则声明的有效期。",
            next_step="请等待规则重新生成可确认的预览。",
        )
    return _safe_success(
        action_id="actor-fate-preview",
        intent=intent,
        label=text(selected.get("actor_name"), "当前角色"),
        state=text(result.get("message"), "命运预览已经处理"),
        revision=expected_revision,
        replayed=bool(result.get("replayed")),
    )


__all__ = ["_actor_fate_preview_action"]
