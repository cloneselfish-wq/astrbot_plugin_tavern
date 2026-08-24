from .common import *
from .sessions import *
from .characters import *

async def _memory_governance(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    memory_id = _resolved_target(
        principal, "memories", target_key, kind="memory"
    )
    try:
        context = mapping(await database.memory_action_context(memory_id))
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.memory_missing",
            "治理事实失败：这条事实已经不存在。",
            "系统没有修改数据；请刷新长期记忆页。",
        ) from exc
    session_id = text(context.get("session_id"))
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.memory_governance_forbidden",
            "只有当前副本主持人或管理员可以治理事实。",
            "请联系主持人处理这条事实。",
        )
    if bool(context.get("locked")) and role != "admin":
        raise _route_error(
            403,
            "intent.memory_locked",
            "这条事实已经锁定，主持人不能修改其治理状态。",
            "请联系管理员核对锁定原因。",
        )
    values = _checked_input(body, allowed=frozenset({"operation", "reason"}))
    operation = text(values.get("operation")).lower()
    labels = {
        "pin": "事实已经置顶",
        "unpin": "事实已取消置顶",
        "invalidate": "事实已经标记为失效",
        "restore": "事实已经恢复为有效",
        "resolve": "事实冲突已经处理",
    }
    if operation not in labels:
        raise _route_error(
            400,
            "intent.memory_operation_invalid",
            "没有选择可用的事实治理动作。",
            "请从页面列出的置顶、失效、恢复或冲突处理中重新选择。",
        )
    try:
        result = mapping(
            await database.govern_memory(
                memory_id,
                operation,
                f"console:{actor_id(principal)}",
                expected_revision=expected_revision,
                operation_id=idempotency_key,
                reason=text(values.get("reason")),
                allow_locked=role == "admin",
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.memory_missing",
            "治理事实失败：这条事实已经不存在。",
            "系统没有修改数据；请刷新长期记忆页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.memory_conflict",
            "治理事实失败：事实状态或副本生命周期已经变化。",
            "系统没有覆盖新状态；请刷新长期记忆页后重试。",
        ) from exc
    return _safe_success(
        action_id="memory.govern",
        intent="memory.govern",
        label="当前事实",
        state=text(result.get("state"), labels[operation]),
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


def _split_operation_target(value: str) -> tuple[str, str]:
    if not value.startswith("operation:") or "\x1f" not in value:
        raise _route_error(
            404,
            "intent.operation_expired",
            "所选待办事务已经失效。",
            "请刷新待办页后重新选择。",
        )
    session_id, operation_id = value[len("operation:") :].split("\x1f", 1)
    if not session_id or not operation_id:
        raise _route_error(
            404,
            "intent.operation_expired",
            "所选待办事务已经失效。",
            "请刷新待办页后重新选择。",
        )
    return session_id, operation_id


async def _operation_cancel_request(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(principal, "todo", target_key, kind="todo")
    session_id, operation_id = _split_operation_target(resolved)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.operation_cancel_forbidden",
            "只有当前副本主持人或管理员可以请求停止事务。",
            "请联系主持人处理这项运行阻塞。",
        )
    values = _checked_input(body, allowed=frozenset({"reason"}))
    reason = text(values.get("reason"))
    if not reason:
        raise _route_error(
            400,
            "intent.operation_cancel_reason_required",
            "停止事务前需要填写原因。",
            "请说明当前阻塞或风险后重新提交。",
        )
    try:
        result = mapping(
            await database.request_session_operation_cancel(
                session_id,
                f"console:{actor_id(principal)}",
                operation_id=operation_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                reason=reason,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.operation_missing",
            "停止事务失败：事务已经结束或不属于当前副本。",
            "系统没有修改其他事务；请刷新待办页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.operation_cancel_conflict",
            "停止事务失败：事务阶段已经变化，不能覆盖当前结果。",
            "系统保留已完成事实；请刷新待办页查看最新状态。",
        ) from exc
    if not bool(result.get("found", True)):
        raise _route_error(
            404,
            "intent.operation_missing",
            "停止事务失败：当前没有可取消的事务。",
            "请刷新待办页；若仍阻塞，请查看健康中心。",
        )
    return _safe_success(
        action_id="C08",
        intent="operation.cancel.request",
        label="当前运行事务",
        state=(
            "已请求安全停止"
            if bool(result.get("changed"))
            else "停止请求已经存在"
        ),
        revision=expected_revision,
        replayed=bool(result.get("replayed")),
    )


def _split_timer_target(value: str) -> tuple[str, str]:
    if "\x1f" not in value:
        raise _route_error(
            404,
            "intent.timer_expired",
            "所选倒计时已经失效。",
            "请刷新世界 Lens 后重新选择。",
        )
    session_id, timer_id = value.split("\x1f", 1)
    if not session_id or not timer_id:
        raise _route_error(
            404,
            "intent.timer_expired",
            "所选倒计时已经失效。",
            "请刷新世界 Lens 后重新选择。",
        )
    return session_id, timer_id


async def _timer_control(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(principal, "sessions", target_key, kind="timer")
    session_id, timer_id = _split_timer_target(resolved)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.timer_forbidden",
            "只有当前副本主持人或管理员可以调整倒计时。",
            "请联系主持人处理当前时间压力。",
        )
    values = _checked_input(body, allowed=frozenset({"operation", "seconds"}))
    operation = text(values.get("operation")).lower()
    if operation not in {"pause", "resume", "extend", "expire", "disable"}:
        raise _route_error(
            400,
            "intent.timer_operation_invalid",
            "没有选择可用的倒计时动作。",
            "请从页面列出的暂停、恢复、延长、到期或停用中重新选择。",
        )
    seconds = to_int(values.get("seconds"), 0) or 0
    if operation == "extend" and (seconds < 1 or seconds > 86400):
        raise _route_error(
            400,
            "intent.timer_seconds_invalid",
            "延长倒计时需要 1—86400 秒。",
            "请填写安全范围内的秒数后重新提交。",
        )
    try:
        result = mapping(
            await database.control_timer(
                timer_id,
                operation,
                f"console:{actor_id(principal)}",
                seconds=seconds,
                session_id=session_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.timer_missing",
            "调整倒计时失败：倒计时已经不存在或不属于当前副本。",
            "系统没有修改其他倒计时；请刷新世界 Lens。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError, ValueError) as exc:
        raise _route_error(
            409,
            "intent.timer_conflict",
            "调整倒计时失败：倒计时状态已经变化或不允许该动作。",
            "系统没有覆盖最新状态；请刷新世界 Lens 后重试。",
        ) from exc
    states = {
        "pause": "倒计时已经暂停",
        "resume": "倒计时已经恢复",
        "extend": "倒计时已经延长",
        "expire": "倒计时已按既定规则到期",
        "disable": "倒计时已经停用",
    }
    from ...repositories.timers_support import timer_revision

    return _safe_success(
        action_id="C24",
        intent="timer.control",
        label="当前倒计时",
        state=states[operation],
        revision=timer_revision(result),
        replayed=bool(result.get("replayed")),
    )


async def _participant_action(
    principal: Mapping[str, Any],
    database: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(
        principal, "characters", target_key, kind="character"
    )
    session_id, participant_ref = _split_character_target(resolved)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.participant_forbidden",
            "只有当前副本主持人或管理员可以管理队伍参与状态。",
            "请联系主持人处理角色准备或退场。",
        )
    try:
        if intent == "participants.force_ready":
            _checked_input(body, allowed=frozenset())
            result = mapping(
                await database.force_all_ready(
                    session_id,
                    f"console:{actor_id(principal)}",
                    expected_revision=expected_revision,
                    idempotency_key=idempotency_key,
                )
            )
            return _safe_success(
                action_id="C10",
                intent=intent,
                label="当前准备大厅",
                state=f"{to_int(result.get('ready_count'), 0) or 0} 个合格角色已准备",
                revision=expected_revision,
                replayed=bool(result.get("replayed")),
            )
        values = _checked_input(
            body,
            allowed=frozenset({"reason", "acknowledge_departure"}),
        )
        reason = text(values.get("reason"))
        if not reason or not flag(values.get("acknowledge_departure")):
            raise _route_error(
                400,
                "intent.participant_retire_confirmation_required",
                "安排角色退场需要填写原因并确认影响。",
                "请说明退场原因，阅读保留项后重新确认。",
            )
        result = mapping(
            await database.retire_participant(
                session_id,
                participant_ref,
                f"console:{actor_id(principal)}",
                forced=True,
                reason=reason,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        )
        return _safe_success(
            action_id="C19",
            intent=intent,
            label="当前角色",
            state="角色已经安全退场，已结算事实保持不变",
            revision=expected_revision,
            replayed=bool(result.get("replayed")),
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.participant_missing",
            "管理队伍状态失败：角色或副本已经不存在。",
            "系统没有修改其他角色；请刷新角色审核页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError, ValueError) as exc:
        raise _route_error(
            409,
            "intent.participant_conflict",
            "管理队伍状态失败：角色、卡片或副本阶段已经变化。",
            "系统没有覆盖最新状态；请刷新角色审核页后重试。",
        ) from exc


__all__ = [name for name in globals() if not name.startswith('__')]

