"""D1-ARC-003：操作与诊断纯路由。

只消费 ``TavernDatabase`` 公开接口、投递服务与既有语义投影，不接触
AstrBot、Web 框架或平台消息对象；返回值是可 JSON 序列化的路由信封
``{"status": ..., "body": {...}}``，错误统一由 ``route_errors`` 转成
标准信封（code / message / recovery / correlation_id）。

边界（D1-ARC-003 §4.2 / §4.3）：

- 普通玩家只见“恢复摘要 + 待处理数量”，看不到 operation_id、
  operation_type、request/result 等内部字段；
- 取消卡住任务、投递重试/取消仅限 DM / 管理员，普通成员一律 403；
- 投递状态按投递服务自身可见性过滤：普通玩家只传入自己的身份
  （``player:<身份>``），DM / 管理员传入特权视角；
- 诊断报告单独授权：仅管理员可读，报告内容已经 ``redact`` 脱敏；
- 归档副本（``finished``）拒绝全部写操作并标记 ``readonly``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from ...diagnostics import build_diagnostic_report
from ...operations import recovery_summary
from ...database_support import DatabaseNotFoundError
from ...turn_budget import player_generation_stage_label

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
from .sessions import require_member, resolve_viewer_participant

__all__ = [
    "deliveries_act",
    "deliveries_view",
    "diagnostics_view",
    "operation_cancel",
    "operations_view",
]

#: 生成任务类型的中文展示名（DM / 管理员可见；玩家不可见任何类型标识）。
_OPERATION_TYPE_LABELS = {
    "generate_choices": "生成选项",
    "generate_vote": "生成投票",
    "generate_narrative": "生成叙事",
    "turn_commit": "回合提交",
    "choice_resolution": "选项结算",
    "vote_resolution": "投票结算",
    "story_pacing": "剧情节奏",
    "finalization": "终局归档",
}

_DIAGNOSTIC_ONLY_OPERATION_TYPES = frozenset(
    {
        "economy.seed",
    }
)

_ACTION_KIND_LABELS = {
    "forced_choose": "后台代选",
    "forced_reroll": "后台重整",
    "staged_supplement": "分阶段建卡",
    "delegation_choose": "托管代选",
    "delegation_reroll": "托管重整",
}

#: 操作回执状态 → 中文状态名（只输出语义，不输出内部状态机字段）。
_RECEIPT_STATUS_LABELS = {
    "reserved": "已预留",
    "validated": "已校验",
    "committed": "已提交",
    "delivery_pending": "等待投递",
    "delivered": "已送达",
    "delivery_failed": "投递失败",
    "rejected": "已拒绝",
    "rolled_back": "已回滚",
    "pending": "处理中",
    "running": "执行中",
    "failed": "失败",
    "cancelled": "已取消",
}

#: 待处理行动任务状态 → 中文状态名。
_PENDING_STATUS_LABELS = {
    "pending": "等待执行",
    "committed": "已执行",
    "failed": "失败",
    "cancelled": "已取消",
}

_DELIVERY_STATUS_MAP = {
    "pending": "waiting",
    "leased": "waiting",
    "partially_sent": "partial",
    "retry_wait": "retry_wait",
    "delivered": "delivered",
    "permanently_failed": "failed",
    "cancelled": "cancelled",
    "webui_only": "waiting",
}


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def _require_session(database: Any, session_id: str) -> dict[str, Any]:
    session_id = _text(session_id)
    if not session_id:
        raise WebRouteError(
            400,
            "operations.session_missing",
            "缺少 session_id。",
            "请先选择一个要查看的副本。",
        )
    try:
        session = await database.get_session(session_id)
    except DatabaseNotFoundError:
        raise WebRouteError(
            404,
            "operations.session_not_found",
            "副本不存在或已删除。",
            "请刷新副本列表后重新选择。",
        ) from None
    if session is None:
        raise WebRouteError(
            404,
            "operations.session_not_found",
            "副本不存在或已删除。",
            "请刷新副本列表后重新选择。",
        )
    return dict(session) if isinstance(session, Mapping) else {}


def _is_readonly(session: Mapping[str, Any]) -> bool:
    return _text(session.get("state")) == "finished"


def _raise_if_readonly(session: Mapping[str, Any]) -> None:
    if _is_readonly(session):
        raise WebRouteError(
            409,
            "operations.readonly",
            "该副本已归档并处于只读状态，无法执行操作。",
            "请选择其他副本，或从最终存档克隆新副本后继续。",
        )


async def _privileged_role(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> str:
    """返回 dm / admin；无权限直接抛 403（fail closed）。"""
    role = await require_member(database, session_id, principal)
    if role in {"dm", "admin"}:
        return role
    raise WebRouteError(
        403,
        "operations.forbidden",
        "你没有执行该操作状态的权限。",
        "请确认当前账号是副本 DM 或管理员。",
    )


def _status_label(status: str, labels: Mapping[str, str]) -> str:
    return labels.get(_text(status), "状态异常")


def _pick(source: Any, key: str, default: Any = "") -> Any:
    """同时兼容映射与 dataclass 风格的对象（如 DeliveryOutcome）。"""
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _generation_progress(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = raw.get("result")
    result = result if isinstance(result, Mapping) else {}
    stages = [
        item
        for item in result.get("generation_stages") or []
        if isinstance(item, Mapping)
    ]
    latest = stages[-1] if stages else {}
    phase = _text(
        raw.get("last_progress_stage")
        or raw.get("phase")
        or latest.get("stage")
    )
    if not phase and not stages:
        return {}
    providers = [
        _text(item.get("provider_id"))
        for item in stages
        if _text(item.get("provider_id"))
    ]
    return {
        "phase": player_generation_stage_label(phase),
        "elapsed_seconds": max(
            0.0,
            float(latest.get("elapsed") or 0.0),
        ),
        "remaining_seconds": max(
            0.0,
            float(latest.get("remaining_seconds") or 0.0),
        ),
        "provider_switched": len(dict.fromkeys(providers)) > 1
        or any(
            _text(item.get("result")) == "fallback" for item in stages
        ),
        "repair_used": any(
            "repair" in _text(item.get("result"))
            or "repair" in _text(item.get("stage"))
            for item in stages
        ),
        "fallback_used": any(
            _text(item.get("result")) == "fallback" for item in stages
        ),
        "convergence": _text(latest.get("result")),
        "updated_at": _text(
            raw.get("last_progress_at") or raw.get("updated_at")
        ),
    }


def _strip_system_prompt(value: Any) -> Any:
    """递归移除诊断报告中的 ``system_prompt`` 字面键（值已脱敏，键也不外露）。"""
    if isinstance(value, Mapping):
        return {
            (
                "prompt_material" if str(key) == "system_prompt" else str(key)
            ): _strip_system_prompt(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_strip_system_prompt(item) for item in value]
    return value


@route_errors
async def operations_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """副本操作状态：恢复摘要 + 最近操作（玩家只见安全摘要）。"""
    require_login(principal)
    session_id = _text(mapping(query).get("session_id"))
    session = await _require_session(database, session_id)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    is_admin = bool(principal.get("is_admin"))
    limit = max(1, min(200, to_int(mapping(query).get("limit"), 50) or 50))

    operations = [
        item
        for item in await database.list_session_operations(
            session_id,
            limit,
        )
        if isinstance(item, Mapping)
        and _text(item.get("operation_type"))
        not in _DIAGNOSTIC_ONLY_OPERATION_TYPES
    ]
    pending = await database.pending_operations(session_id)
    try:
        has_choices = bool(await database.active_choice_set(session_id))
    except Exception:
        has_choices = False
    try:
        has_vote = bool(await database.active_vote(session_id))
    except Exception:
        has_vote = False
    recovery = recovery_summary(
        operations,
        session_state=_text(session.get("state")),
        has_active_choices=has_choices,
        has_active_vote=has_vote,
    )
    if isinstance(recovery, Mapping):
        recovery = dict(recovery)
        # 原始回执列表（operation_id / request / result）绝不进入普通视图；
        # 语义恢复摘要保留计数、阶段与建议动作。
        recovery.pop("operations", None)

    items: list[dict[str, Any]] = []
    pending_items: list[dict[str, Any]] = []
    if is_privileged:
        for raw in operations:
            if not isinstance(raw, Mapping):
                continue
            operation_type = _text(raw.get("operation_type"))
            item: dict[str, Any] = {
                "operation_type_label": _OPERATION_TYPE_LABELS.get(
                    operation_type, "副本任务"
                ),
                "status": _text(raw.get("status")),
                "status_label": _status_label(
                    _text(raw.get("status")), _RECEIPT_STATUS_LABELS
                ),
                "phase": _text(raw.get("phase")),
                "created_at": _text(raw.get("created_at")),
                "updated_at": _text(raw.get("updated_at")),
            }
            progress = _generation_progress(raw)
            if progress:
                item["generation_progress"] = progress
            if is_admin:
                item["technical_details"] = {
                    "operation_id": _text(raw.get("operation_id")),
                    "retry_count": to_int(raw.get("retry_count"), 0) or 0,
                    "last_error_code": _text(raw.get("last_error_code")),
                }
            items.append(item)
        for raw in pending:
            if not isinstance(raw, Mapping):
                continue
            kind = _text(raw.get("kind"))
            item = {
                "kind_label": _ACTION_KIND_LABELS.get(kind, "副本任务"),
                "status": _text(raw.get("status")),
                "status_label": _status_label(
                    _text(raw.get("status")), _PENDING_STATUS_LABELS
                ),
                "created_at": _text(raw.get("created_at")),
                "updated_at": _text(raw.get("updated_at")),
            }
            if is_admin:
                item["technical_details"] = {
                    "operation_id": _text(raw.get("id")),
                    "actor_id": _text(raw.get("actor_id")),
                }
            pending_items.append(item)

    readonly = _is_readonly(session)
    return ok(
        {
            "session_id": session_id,
            "recovery": recovery,
            "pending_count": len(pending),
            "pending_items": pending_items,
            "items": items,
            "readonly": readonly,
            "permissions": {
                "can_cancel": is_privileged and not readonly,
                "can_manage_deliveries": is_privileged and not readonly,
                "role_source": _text(principal.get("role_source"), "unmapped"),
            },
        }
    )


@route_errors
async def operation_cancel(
    principal: Mapping[str, Any],
    database: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    audit: Callable[..., Any] | None = None,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """取消卡住的生成任务（DM / 管理员；幂等）。"""
    require_login(principal)
    data = mapping(payload)
    session_id = _text(data.get("session_id"))
    session = await _require_session(database, session_id)
    await _privileged_role(database, session_id, principal)
    _raise_if_readonly(session)
    operation_id = _text(data.get("operation_id"))
    if not operation_id:
        raise WebRouteError(
            400,
            "operations.cancel.missing_target",
            "缺少要取消的操作。",
            "请从操作列表中选择目标后重试。",
        )
    known = [
        _text(item.get("operation_id"))
        for item in (await database.list_session_operations(session_id, 200))
        if isinstance(item, Mapping)
    ]
    if operation_id not in known:
        raise WebRouteError(
            404,
            "operations.cancel.not_found",
            "该操作不存在或不属于当前副本。",
            "请刷新操作列表后重新选择。",
        )
    reason = _text(data.get("reason")) or "WebUI 取消卡住任务"
    actor_name = _text(actor) or actor_id(principal)
    result = await database.update_operation(
        operation_id,
        status="cancel_requested",
        phase="cancel_requested",
        result={"reason": reason},
        actor_id=actor_name,
    )
    if audit is not None:
        try:
            await audit(
                session_id,
                f"web:{_text(principal.get('username'))}",
                "operation.cancel",
                operation_id,
                {"reason": reason},
            )
        except Exception:
            pass
    if publish is not None:
        publish(
            {
                "type": "session",
                "action": "operation_cancel_requested",
                "session_id": session_id,
            }
        )
    return ok(
        {
            "session_id": session_id,
            "cancelled": False,
            "cancel_requested": True,
            "status": "cancel_requested",
            "operation_id": operation_id,
            "phase": _text(
                dict(result).get("phase") if isinstance(result, Mapping) else ""
            ),
            "message": "已请求取消；系统会在下一个安全检查点丢弃尚未提交的结果。",
        }
    )


async def _resolve_delivery_viewer(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
    role: str,
) -> str:
    """构造投递服务可见性参数；普通玩家解析本人身份，失败 fail closed。"""
    if bool(principal.get("is_admin")):
        return "admin"
    if role == "dm":
        return "dm"
    participant = await resolve_viewer_participant(
        database, session_id, _text(principal.get("username"))
    )
    if not participant:
        raise WebRouteError(
            403,
            "operations.deliveries.participant_unresolved",
            "无法解析你的角色身份。",
            "请先由主持人绑定角色卡后重试。",
        )
    identity = _text(
        participant.get("group_user_id") or participant.get("id")
    )
    if not identity:
        raise WebRouteError(
            403,
            "operations.deliveries.identity_missing",
            "无法确定你的投递身份。",
            "请联系主持人补全账号绑定后重试。",
        )
    return f"player:{identity}"


def _project_delivery_item(
    view: Mapping[str, Any],
    *,
    privileged: bool,
) -> dict[str, Any]:
    status = _text(view.get("status"))
    status_view = _DELIVERY_STATUS_MAP.get(status, "waiting")
    item: dict[str, Any] = {
        "recipient_name": _text(
            view.get("recipient_label"), "收件人名称缺失"
        ),
        "verified": bool(view.get("verified")),
        "status": status_view,
        "status_label": _text(view.get("status_label"), "等待投递"),
        "channel_label": _text(view.get("channel"), "平台消息"),
        "sensitive": _text(view.get("message_type")) in {
            "dm_whisper",
            "death_confirm",
        },
        "sent_parts": to_int(view.get("sent_parts"), 0) or 0,
        "total_parts": to_int(view.get("total_parts"), 0) or 0,
        "attempts": to_int(view.get("attempts"), 0) or 0,
        "next_retry_at": _text(view.get("next_retry_at")),
    }
    if privileged:
        item["id"] = _text(view.get("delivery_id"))
        item["last_error"] = _text(view.get("last_error_message"))
        item["can_retry"] = status in {
            "pending",
            "partially_sent",
            "retry_wait",
        }
        item["can_cancel"] = status in {
            "pending",
            "leased",
            "partially_sent",
            "retry_wait",
        }
        preview = _text(view.get("text_preview"))
        if preview:
            item["text_preview"] = preview
    else:
        item["can_retry"] = False
        item["can_cancel"] = False
    return item


def _project_turn_delivery_item(view: Mapping[str, Any]) -> dict[str, Any]:
    """Project synchronous turn receipts without exposing stable identifiers."""

    raw_status = _text(view.get("status"))
    parts = [
        item for item in (view.get("parts") or ()) if isinstance(item, Mapping)
    ]
    sent_parts = sum(
        1 for item in parts if _text(item.get("status")) in {"delivered", "skipped"}
    )
    labels = {
        "pending": "等待发送",
        "sending": "正在发送",
        "partially_sent": "部分送达",
        "retry_wait": "等待重试",
        "delivered": "已送达",
        "cancelled": "已取消",
    }
    return {
        "recipient_name": "当前副本群聊",
        "verified": True,
        "status": {
            "pending": "waiting",
            "sending": "waiting",
            "partially_sent": "partial",
            "retry_wait": "retry_wait",
            "delivered": "delivered",
            "cancelled": "cancelled",
        }.get(raw_status, "failed"),
        "status_label": labels.get(raw_status, "状态解析失败"),
        "channel_label": "同步回复",
        "sensitive": False,
        "sent_parts": sent_parts,
        "total_parts": to_int(view.get("total_parts"), len(parts)) or len(parts),
        "attempts": to_int(view.get("attempt_count"), 0) or 0,
        "next_retry_at": "",
        "last_error": _text(view.get("last_error")),
        "can_retry": False,
        "can_cancel": False,
        "sequence_delivery": True,
    }


def _outcome_payload(outcome: Any) -> dict[str, Any]:
    return {
        "ok": bool(_pick(outcome, "ok", False)),
        "status": _text(_pick(outcome, "status")),
        "delivery_id": _text(_pick(outcome, "delivery_id")),
        "reason": _text(_pick(outcome, "reason")),
        "method": _text(_pick(outcome, "method")),
        "attempts": to_int(_pick(outcome, "attempts"), 0) or 0,
        "sent_parts": to_int(_pick(outcome, "sent_parts"), 0) or 0,
        "total_parts": to_int(_pick(outcome, "total_parts"), 0) or 0,
        "next_retry_at": _text(_pick(outcome, "next_retry_at")),
    }


@route_errors
async def deliveries_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    delivery_service: Any = None,
) -> dict[str, Any]:
    """投递状态列表：玩家只见本人收件，DM / 管理员见全副本（含操作入口）。"""
    require_login(principal)
    session_id = _text(mapping(query).get("session_id"))
    session = await _require_session(database, session_id)
    role = await require_member(database, session_id, principal)
    privileged = role in {"dm", "admin"}
    if delivery_service is None:
        raise WebRouteError(
            503,
            "operations.deliveries.unavailable",
            "投递服务当前不可用。",
            "请稍后重试，或联系管理员检查插件状态。",
        )
    limit = max(1, min(200, to_int(mapping(query).get("limit"), 100) or 100))
    viewer = await _resolve_delivery_viewer(
        database, session_id, principal, role
    )
    views = await delivery_service.list_status(
        session_id,
        viewer=viewer,
        limit=limit,
    )
    items = [
        _project_delivery_item(item, privileged=privileged)
        for item in views
        if isinstance(item, Mapping)
    ]
    turn_sequences: list[dict[str, Any]] = []
    if privileged and callable(getattr(database, "list_turn_delivery_runs", None)):
        runs = await database.list_turn_delivery_runs(session_id, limit=limit)
        turn_sequences = [
            _project_turn_delivery_item(item)
            for item in runs
            if isinstance(item, Mapping)
        ]
        items.extend(turn_sequences)
    return ok(
        {
            "session_id": session_id,
            "schema": "DeliveryStatusView",
            "items": items,
            "turn_sequences": turn_sequences,
            "count": len(items),
            "readonly": _is_readonly(session),
            "permissions": {
                "can_manage_deliveries": privileged and not _is_readonly(session),
                "role_source": _text(principal.get("role_source"), "unmapped"),
            },
        }
    )


@route_errors
async def deliveries_act(
    principal: Mapping[str, Any],
    database: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    actor: str = "",
    delivery_service: Any = None,
    audit: Callable[..., Any] | None = None,
    publish: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """重试或取消投递（DM / 管理员；校验目标归属当前副本）。"""
    require_login(principal)
    data = mapping(payload)
    session_id = _text(data.get("session_id"))
    session = await _require_session(database, session_id)
    await _privileged_role(database, session_id, principal)
    _raise_if_readonly(session)
    if delivery_service is None:
        raise WebRouteError(
            503,
            "operations.deliveries.unavailable",
            "投递服务当前不可用。",
            "请稍后重试，或联系管理员检查插件状态。",
        )
    delivery_id = _text(data.get("delivery_id"))
    action = _text(data.get("action"), "retry").lower()
    if not delivery_id:
        raise WebRouteError(
            400,
            "operations.deliveries.missing_target",
            "缺少要操作的投递记录。",
            "请从投递列表中选择目标后重试。",
        )
    if action not in {"retry", "cancel"}:
        raise WebRouteError(
            400,
            "operations.deliveries.unsupported_action",
            "投递操作只支持重试或取消。",
            "请选择“重试”或“取消”后重试。",
        )
    record = await database.get(delivery_id)
    if not record:
        raise WebRouteError(
            404,
            "operations.deliveries.not_found",
            "投递记录不存在。",
            "请刷新投递列表后重新选择。",
        )
    if _text(record.get("session_id")) != session_id:
        raise WebRouteError(
            404,
            "operations.deliveries.not_in_session",
            "投递记录不属于当前副本。",
            "请选择对应副本后重试。",
        )
    actor_name = _text(actor) or actor_id(principal)
    if action == "cancel":
        outcome = await delivery_service.cancel(
            delivery_id,
            actor=actor_name,
            reason=_text(data.get("reason")) or "web_cancelled",
        )
    else:
        outcome = await delivery_service.deliver(
            delivery_id,
            actor=actor_name,
        )
    if audit is not None:
        try:
            await audit(
                session_id,
                f"web:{_text(principal.get('username'))}",
                f"delivery.{action}",
                delivery_id,
                {},
            )
        except Exception:
            pass
    if publish is not None:
        publish(
            {
                "type": "delivery",
                "action": action,
                "session_id": session_id,
            }
        )
    return ok(
        {
            "session_id": session_id,
            "delivery_id": delivery_id,
            "action": action,
            "outcome": _outcome_payload(outcome),
        }
    )


@route_errors
async def diagnostics_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """脱敏诊断报告（仅管理员；普通页面不得复用该 DTO）。"""
    require_admin(principal)
    session_id = _text(mapping(query).get("session_id"))
    await _require_session(database, session_id)
    report = _strip_system_prompt(
        await build_diagnostic_report(database, session_id)
    )
    return ok(
        {
            "session_id": session_id,
            "report": report,
            "redacted": True,
            "note": "诊断报告已脱敏：用户 ID 已哈希，密钥、私聊来源、"
            "私人字段与完整系统提示词未导出。",
        }
    )

__all__ = [name for name in globals() if not name.startswith('__')]

