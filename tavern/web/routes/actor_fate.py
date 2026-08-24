"""Principal-scoped lethal fate preview and consent route."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ...database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)
from ...visualization import visual_envelope
from ..surfaces.registry import issue_surface_key, resolve_surface_key
from . import (
    WebRouteError,
    flag,
    mapping,
    require_login,
    route_errors,
    text,
    to_int,
)
from .sessions import require_member, resolve_viewer_participant


def _viewer_lookup(principal: Mapping[str, Any]) -> tuple[str, str]:
    if text(principal.get("auth_source")) == "miniprogram_binding":
        return "", text(principal.get("participant_ref"))
    return text(principal.get("username")), ""


def _route_failure(
    status: int,
    code: str,
    *,
    operation: str,
    reason: str,
    next_step: str,
) -> WebRouteError:
    return WebRouteError(
        status,
        code,
        f"失败操作：{operation}\n原因：{reason}",
        (
            "自动处理：系统没有修改角色命运或救援窗口。\n"
            f"下一步：{next_step}"
        ),
    )


def _safe_preview(
    preview: Mapping[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "actor": text(preview.get("actor_name"), "当前角色"),
        "source": text(preview.get("source"), "未说明危险来源"),
        "reason": text(preview.get("reason"), "未说明致命原因"),
        "alternatives": [
            text(item)
            for item in preview.get("alternatives") or ()
            if text(item)
        ][:8],
        "rescue_window": "确认后进入世界声明的救援窗口",
        "expires_on": text(preview.get("expires_on")),
        "expected_revision": int(
            preview.get("expected_fate_revision") or 0
        ),
        "status": "等待本人确认",
    }


@route_errors
async def actor_fate_consent_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    method: str = "GET",
    idempotency_key: str = "",
) -> dict[str, Any]:
    require_login(principal)
    query_map = mapping(query)
    body = mapping(payload)
    session_id = text(query_map.get("session_id") or body.get("session_id"))
    if not session_id:
        raise _route_failure(
            400,
            "actor_fate.session_required",
            operation="打开本人命运预览",
            reason="当前请求没有关联可用副本。",
            next_step="请返回跑团现场后重新打开自己的命运预览。",
        )
    try:
        role = await require_member(database, session_id, principal)
    except Exception as exc:
        raise _route_failure(
            403,
            "actor_fate.member_required",
            operation="验证命运预览所属角色",
            reason="当前账号不是该副本的有效成员。",
            next_step="请检查账号与玩家席位绑定后重试。",
        ) from exc
    if role != "player" or bool(principal.get("is_admin")):
        raise _route_failure(
            403,
            "actor_fate.actor_required",
            operation="确认或拒绝致命命运预览",
            reason="主持人或管理员不能代替角色本人确认。",
            next_step="请让目标角色本人从已认证账号处理该预览。",
        )
    username, participant_ref = _viewer_lookup(principal)
    participant = await resolve_viewer_participant(
        database,
        session_id,
        username,
        participant_ref,
    )
    if not participant:
        raise _route_failure(
            403,
            "actor_fate.participant_required",
            operation="读取本人致命命运预览",
            reason="当前账号没有可确认的出场角色身份。",
            next_step="请检查玩家绑定后重新打开自己的角色状态。",
        )
    participant_id = text(participant.get("id"))
    all_previews = await database.list_actor_fate_previews(
        session_id,
        participant_id,
        status="",
    )

    def token_for(preview: Mapping[str, Any]) -> str:
        target = json.dumps(
            {
                "session_id": session_id,
                "preview_operation_id": text(preview.get("operation_id")),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return issue_surface_key(
            principal,
            "sessions",
            "fate-preview",
            target,
        )

    if str(method or "GET").upper() == "POST":
        intent = text(body.get("intent"))
        decision = {
            "actor_fate.preview.accept": "accept",
            "actor_fate.preview.refuse": "refuse",
        }.get(intent, "")
        if not decision:
            raise _route_failure(
                400,
                "actor_fate.intent_invalid",
                operation="处理本人致命命运预览",
                reason="提交的预览操作未注册。",
                next_step="请刷新后使用“确认进入救援”或“拒绝本次预览”。",
            )
        preview_key = text(body.get("preview_key") or body.get("target_key"))
        resolved_target = resolve_surface_key(
            principal,
            "sessions",
            preview_key,
            kind="fate-preview",
        )
        try:
            resolved = mapping(json.loads(text(resolved_target)))
        except (TypeError, ValueError, json.JSONDecodeError):
            resolved = {}
        resolved_operation = (
            text(resolved.get("preview_operation_id"))
            if text(resolved.get("session_id")) == session_id
            else ""
        )
        selected: dict[str, Any] = {}
        for preview in all_previews:
            if text(preview.get("operation_id")) == resolved_operation:
                selected = dict(preview)
                break
        if not selected:
            raise _route_failure(
                409,
                "actor_fate.preview_key_invalid",
                operation="处理所选致命命运预览",
                reason="所选预览已失效或不属于当前角色。",
                next_step="请刷新自己的命运预览后重试。",
            )
        input_values = mapping(body.get("input"))
        unknown_inputs = set(input_values) - {"acknowledge_fate"}
        if unknown_inputs:
            raise _route_failure(
                400,
                "actor_fate.input_invalid",
                operation="提交本人命运预览选择",
                reason="提交内容包含当前操作不接受的字段。",
                next_step="请关闭操作窗口，刷新后重新提交。",
            )
        if not flag(input_values.get("acknowledge_fate")):
            raise _route_failure(
                400,
                "actor_fate.acknowledgement_required",
                operation="提交本人命运预览选择",
                reason="尚未确认已阅读致命原因、替代方案与救援影响。",
                next_step="请勾选确认框后重新提交。",
            )
        expected = to_int(body.get("expected_revision"))
        if expected is None:
            raise _route_failure(
                400,
                "actor_fate.revision_required",
                operation="确认致命命运预览版本",
                reason="提交内容缺少当前角色状态版本。",
                next_step="请刷新预览后重新确认。",
            )
        request_key = text(idempotency_key or body.get("idempotency_key"))
        if not request_key:
            raise _route_failure(
                400,
                "actor_fate.idempotency_required",
                operation="提交本人命运预览选择",
                reason="请求缺少防重复凭证。",
                next_step="请保留页面并重新提交。",
            )
        try:
            result = await database.resolve_actor_fate_preview(
                session_id=session_id,
                preview_operation_id=text(selected.get("operation_id")),
                participant_id=participant_id,
                decision=decision,
                expected_revision=int(expected),
                actor_id=text(participant.get("group_user_id")),
                idempotency_key=request_key,
            )
        except PermissionError as exc:
            raise _route_failure(
                403,
                "actor_fate.actor_required",
                operation="处理本人致命命运预览",
                reason="该预览不属于当前角色。",
                next_step="请返回自己的角色状态重新选择。",
            ) from exc
        except DatabaseNotFoundError as exc:
            raise _route_failure(
                409,
                "actor_fate.preview_missing",
                operation="处理本人致命命运预览",
                reason="预览或角色命运状态已不存在。",
                next_step="请刷新自己的命运预览后重试。",
            ) from exc
        except (
            DatabaseConflictError,
            InvalidTransitionError,
            ValueError,
        ) as exc:
            raise _route_failure(
                409,
                "actor_fate.preview_conflict",
                operation="处理本人致命命运预览",
                reason="预览、角色状态或冻结世界规则已经变化。",
                next_step="请刷新自己的命运预览后重新选择。",
            ) from exc
        if text(result.get("status")) == "expired":
            raise _route_failure(
                409,
                "actor_fate.preview_expired",
                operation="处理本人致命命运预览",
                reason="该预览已经超过世界规则声明的有效期。",
                next_step="请等待规则重新生成可确认的预览。",
            )
        data = {
            "state": text(result.get("status")),
            "message": text(result.get("message")),
            "state_changed": bool(result.get("state_changed")),
            "replayed": bool(result.get("replayed")),
            "items": [],
            "available_actions": [],
        }
    else:
        pending = [
            preview
            for preview in all_previews
            if text(preview.get("status")) == "pending_consent"
        ]
        items: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        for index, preview in enumerate(all_previews):
            if text(preview.get("status")) != "pending_consent":
                continue
            token = token_for(preview)
            item = _safe_preview(preview, key=token)
            items.append(item)
            actor_label = text(item.get("actor"), "当前角色")
            alternatives = "；".join(
                text(value)
                for value in item.get("alternatives") or ()
                if text(value)
            ) or "由世界规则声明的救援操作"
            preview_detail = (
                f"危险来源：{text(item.get('source'), '未说明')}；"
                f"致命原因：{text(item.get('reason'), '未说明')}；"
                f"替代方案：{alternatives}。"
            )
            for intent, label, description, acknowledgement in (
                (
                    "actor_fate.preview.accept",
                    f"确认「{actor_label}」进入救援窗口",
                    preview_detail
                    + "确认后仅进入世界声明的非终态救援窗口；仍需队伍完成救援。",
                    "我已阅读原因与替代方案，确认进入救援窗口",
                ),
                (
                    "actor_fate.preview.refuse",
                    f"拒绝「{actor_label}」本次致命命运",
                    preview_detail
                    + "拒绝后保留当前命运状态，不开启本次救援窗口。",
                    "我确认拒绝本次致命命运并保留当前状态",
                ),
            ):
                actions.append(
                    {
                        "action_id": f"{intent.replace('.', '-')}-{index}",
                        "intent": intent,
                        "label": label,
                        "target_kind": "fate-preview",
                        "target_key": token,
                        "expected_revision": item["expected_revision"],
                        "description": description,
                        "transportReady": True,
                        "focus_return": "opener",
                        "fields": [
                            {
                                "name": "acknowledge_fate",
                                "type": "checkbox",
                                "label": acknowledgement,
                                "required": True,
                            }
                        ],
                    }
                )
        data = {
            "state": "pending" if items else "empty",
            "message": (
                "致命命运尚未生效；请阅读原因与替代方案后由本人确认或拒绝。"
                if items
                else "当前没有等待本人确认的致命命运预览。"
            ),
            "items": items,
            "available_actions": actions,
        }
    envelope = visual_envelope(
        kind="actor_fate_consent",
        data=data,
        revision=max(
            (
                int(item.get("expected_revision") or 0)
                for item in data.get("items") or ()
            ),
            default=0,
        ),
        summary={
            "label": "角色命运确认",
            "count": len(data.get("items") or ()),
            "state": text(data.get("state")),
        },
        permissions={
            "can_view": True,
            "can_confirm_own": True,
            "can_confirm_others": False,
        },
        state="ready" if data.get("items") else "empty",
        empty=not bool(data.get("items")),
    )
    return {"status": 200, "body": envelope.to_dict()}


__all__ = ["actor_fate_consent_view"]
