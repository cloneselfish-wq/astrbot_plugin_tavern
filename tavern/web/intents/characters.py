from .common import *
from .sessions import *

async def _card_review(
    principal: Mapping[str, Any],
    database: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    action_id, approved, result_state = _CARD_ACTIONS[intent]
    resolved = _resolved_target(
        principal, "characters", target_key, kind="character"
    )
    session_id, participant_ref = _split_character_target(resolved)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.card_review_forbidden",
            "只有当前副本主持人或管理员可以审核角色。",
            "请联系主持人处理角色审核。",
        )
    values = _checked_input(body, allowed=frozenset({"note"}))
    try:
        item = await database.review_character_card(
            session_id,
            participant_ref,
            approved,
            f"console:{actor_id(principal)}",
            text(values.get("note")),
            expected_revision,
            idempotency_key,
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.character_missing",
            "审核角色失败：角色已经不存在。",
            "系统没有修改数据；请刷新角色审核页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.card_review_conflict",
            "审核角色失败：角色卡版本或审核状态已经变化。",
            "系统没有覆盖新结果；请刷新后重新审核。",
        ) from exc
    item = mapping(item)
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label=text(
            item.get("character_name") or item.get("display_name"),
            "当前角色",
        ),
        state=result_state,
        revision=to_int(item.get("card_version"), expected_revision),
        replayed=bool(item.get("idempotent")),
    )


def _author_job_state(value: Any) -> str:
    return {
        "queued": "等待执行",
        "leased": "正在执行",
        "running": "正在执行",
        "retry_wait": "等待自动重试",
        "permanently_failed": "已停止重试",
        "cancel_requested": "正在停止",
        "cancelled": "已取消",
        "succeeded": "已完成",
        "completed": "已完成",
    }.get(text(value).lower(), "状态已经更新")


async def _author_job_create(
    principal: Mapping[str, Any],
    database: Any,
    router: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    world_ref = next(
        (
            text(resolve_surface_key(principal, workspace, target_key, kind="world"))
            for workspace in ("designer", "author_jobs")
            if resolve_surface_key(principal, workspace, target_key, kind="world")
        ),
        "",
    )
    if not world_ref:
        raise _route_error(
            404,
            "intent.target_expired",
            "所选世界已经失效或不属于当前账号。",
            "请刷新作者工作区后重新选择世界。",
        )
    require_author(principal)
    values = _checked_input(body, allowed=frozenset({"job_type"}))
    requested_type = text(values.get("job_type"), "full_preflight").lower()
    if requested_type != "full_preflight":
        raise _route_error(
            400,
            "intent.author_job_type_invalid",
            "当前入口只登记发布前完整检查。",
            "请刷新作者工作区并使用页面提供的世界验证动作。",
        )
    world = mapping(await database.get_world(world_ref))
    if not world:
        raise _route_error(
            404,
            "intent.world_missing",
            "创建世界验证任务失败：世界已经不存在。",
            "系统没有创建任务；请刷新作者工作区并重新选择世界。",
        )
    actual_revision = to_int(world.get("revision"), 0) or 0
    if actual_revision != expected_revision:
        raise _route_error(
            409,
            "intent.author_job_revision_conflict",
            "创建世界验证任务失败：世界草稿在你打开后已经变化。",
            "系统没有创建任务；请刷新作者工作区后重新提交。",
        )
    result = await author_job_create(
        principal,
        router,
        payload={
            "job_type": "full_preflight",
            "world_ref": world_ref,
            "request": {},
            "max_attempts": 3,
        },
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
    )
    status = int(mapping(result).get("status") or 500)
    if status >= 400:
        raw = mapping(mapping(result).get("error"))
        raise _route_error(
            status,
            text(raw.get("code"), "intent.author_job_create_failed"),
            text(
                raw.get("reason") or raw.get("message"),
                "世界验证任务未能建立。",
            ),
            text(
                raw.get("next_command") or raw.get("recovery"),
                "请刷新作者工作区后重试。",
            ),
        )
    response_body = mapping(mapping(result).get("body"))
    job = mapping(response_body.get("job"))
    replayed = (
        status == 200
        or text(response_body.get("status")).lower() == "replayed"
        or bool(job.get("replayed"))
    )
    return _safe_success(
        action_id="E15",
        intent="author_job.create",
        label=text(world.get("name"), "当前世界"),
        state=(
            "已返回原世界验证任务"
            if replayed
            else "世界验证任务已经排队"
        ),
        revision=actual_revision,
        replayed=replayed,
    )


async def _author_job(
    principal: Mapping[str, Any],
    database: Any,
    router: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    action_id, operation, result_state = _AUTHOR_JOB_ACTIONS[intent]
    job_ref = _resolved_target(
        principal, "author_jobs", target_key, kind="job"
    )
    require_author(principal)
    _checked_input(body, allowed=frozenset())
    result = await author_job_action(
        principal,
        router,
        payload={
            "job_ref": job_ref,
            "action": operation,
            "expected_revision": expected_revision,
        },
        idempotency_key=idempotency_key,
    )
    status = int(mapping(result).get("status") or 500)
    if status >= 400:
        raw = mapping(mapping(result).get("error"))
        raise _route_error(
            status,
            text(raw.get("code"), "intent.author_job_failed"),
            text(
                raw.get("reason") or raw.get("message"),
                "作者任务操作未完成。",
            ),
            text(
                raw.get("next_command") or raw.get("recovery"),
                "请刷新作者任务列表后重试。",
            ),
        )
    response_body = mapping(mapping(result).get("body"))
    job = mapping(response_body.get("job"))
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label="作者任务",
        state=(
            result_state
            if operation == "retry"
            else _author_job_state(job.get("status"))
        ),
        revision=to_int(job.get("revision"), expected_revision),
        replayed=(
            text(response_body.get("status")).lower() == "replayed"
            or bool(job.get("replayed"))
        ),
    )


def _split_tendency_target(value: str) -> tuple[str, str, int]:
    parts = value.split("\x1f")
    if len(parts) != 3:
        raise _route_error(
            404,
            "intent.evidence_expired",
            "所选倾向依据已经失效。",
            "请刷新“我的倾向”后重新选择。",
        )
    session_id, operation, raw_number = parts
    number = to_int(raw_number, 0) or 0
    if not session_id or operation not in {"ignore", "restore"} or number < 1:
        raise _route_error(
            404,
            "intent.evidence_expired",
            "所选倾向依据已经失效。",
            "请刷新“我的倾向”后重新选择。",
        )
    return session_id, operation, number


async def _tendency_evidence(
    principal: Mapping[str, Any],
    database: Any,
    router: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    action_id, expected_operation, result_state = _TENDENCY_ACTIONS[intent]
    resolved = _resolved_target(
        principal, "tendencies", target_key, kind="evidence"
    )
    session_id, operation, number = _split_tendency_target(resolved)
    if operation != expected_operation:
        raise _route_error(
            409,
            "intent.evidence_changed",
            "这条依据的当前状态已经变化。",
            "系统没有覆盖新状态；请刷新“我的倾向”后重试。",
        )
    _checked_input(body, allowed=frozenset())
    result = await tendency_action(
        principal,
        database,
        router,
        payload={
            "session_id": session_id,
            "action": operation,
            "number": number,
            "expected_revision": expected_revision,
        },
        idempotency_key=idempotency_key,
    )
    status = int(mapping(result).get("status") or 500)
    if status >= 400:
        raw = mapping(mapping(result).get("error"))
        raise _route_error(
            status,
            text(raw.get("code"), "intent.tendency_failed"),
            text(
                raw.get("reason") or raw.get("message"),
                "调整倾向依据未能完成。",
            ),
            text(
                raw.get("next_command") or raw.get("recovery"),
                "请刷新“我的倾向”后重试。",
            ),
        )
    response_body = mapping(mapping(result).get("body"))
    view = mapping(response_body.get("view"))
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label="本人可见倾向依据",
        state=result_state,
        revision=to_int(view.get("revision"), expected_revision),
        replayed=text(response_body.get("status")).lower() == "replayed",
    )


def _health_component(
    summary: Mapping[str, Any],
    code: str,
) -> dict[str, Any]:
    components = mapping(summary).get("components")
    if not isinstance(components, list):
        return {}
    return next(
        (
            mapping(item)
            for item in components
            if isinstance(item, Mapping) and text(item.get("code")) == code
        ),
        {},
    )


async def _health_recovery(
    principal: Mapping[str, Any],
    database: Any,
    router: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    action_id, result_state = _HEALTH_ACTIONS[intent]
    component = _resolved_target(
        principal, "health", target_key, kind="health"
    )
    require_admin(principal)
    _checked_input(body, allowed=frozenset())
    compatible = {
        "health.backup.create": {"backup"},
        "health.outbox.retry": {
            "delivery_outbox",
            "storage_outbox",
            "event_outbox",
        },
        "health.lease.release_expired": {"author_jobs", "operations"},
    }
    if component not in compatible[intent]:
        raise _route_error(
            409,
            "intent.health_target_changed",
            "当前健康项目已不再适用这个恢复动作。",
            "系统没有修改数据；请刷新健康中心后重新选择。",
        )
    before = _health_component(
        await database.health_summary(),
        component,
    )
    if not before:
        raise _route_error(
            404,
            "intent.health_target_missing",
            "当前健康项目已经不存在。",
            "请刷新健康中心后重新选择。",
        )
    if health_component_revision(before) != expected_revision:
        raise _route_error(
            409,
            "intent.health_revision_conflict",
            "健康状态在你打开操作后已经变化。",
            "系统没有执行恢复；请刷新后重新确认。",
        )
    action_payload: dict[str, Any] = {"action": intent}
    if intent == "health.backup.create":
        action_payload["label"] = "health-recovery"
    elif intent == "health.outbox.retry":
        action_payload.update({"component": component, "number": 1})
    else:
        action_payload["component"] = component
    result = await health_action(
        principal,
        router,
        payload=action_payload,
        idempotency_key=idempotency_key,
    )
    status = int(mapping(result).get("status") or 500)
    if status >= 400:
        raw = mapping(mapping(result).get("error"))
        raise _route_error(
            status,
            text(raw.get("code"), "intent.health_failed"),
            text(
                raw.get("reason") or raw.get("message"),
                "健康恢复未能完成。",
            ),
            text(
                raw.get("next_command") or raw.get("recovery"),
                "请刷新健康中心后重试。",
            ),
        )
    response_body = mapping(mapping(result).get("body"))
    outcome = mapping(response_body.get("result"))
    after = _health_component(
        await database.health_summary(),
        component,
    )
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label=text(before.get("label"), "健康项目"),
        state=text(outcome.get("summary"), result_state),
        revision=(
            health_component_revision(after)
            if after
            else expected_revision
        ),
        replayed=(
            text(response_body.get("status")).lower() == "replayed"
            or bool(outcome.get("replayed"))
        ),
    )


async def _session_world_migrate(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _resolved_target(
        principal, "sessions", target_key, kind="session"
    )
    require_admin(principal)
    values = _checked_input(
        body,
        allowed=frozenset({"candidate_key", "acknowledge_migration"}),
    )
    candidate_key = text(values.get("candidate_key"))
    candidate_world_ref = text(
        resolve_surface_key(
            principal, "worlds", candidate_key, kind="world"
        )
    )
    if not candidate_world_ref:
        raise _route_error(
            404,
            "intent.world_candidate_expired",
            "选择的目标世界已经失效或不属于当前账号。",
            "请刷新副本管理页后重新选择目标世界。",
        )
    if not flag(values.get("acknowledge_migration")):
        raise _route_error(
            400,
            "intent.world_migration_confirmation_required",
            "迁移冻结世界缺少明确确认。",
            "请阅读影响说明并按页面提示重新确认。",
        )
    session = mapping(await database.get_session(session_id))
    if not session:
        raise _route_error(
            404,
            "intent.session_missing",
            "迁移冻结世界失败：副本已经不存在。",
            "系统没有修改数据；请刷新副本列表。",
        )
    try:
        result = mapping(
            await database.migrate_session_world(
                session_id,
                candidate_world_ref,
                f"console:{actor_id(principal)}",
                expected_revision=expected_revision,
                operation_id=idempotency_key,
                confirmation="MIGRATE_FROZEN_WORLD",
            )
        )
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.world_migration_conflict",
            "迁移冻结世界失败：副本状态、目标世界或预期修订已经变化。",
            "系统保留原世界和不可覆盖备份；请刷新后重新预览。",
        ) from exc
    updated_session = mapping(await database.get_session(session_id))
    return _safe_success(
        action_id="C12",
        intent="session.world.migrate",
        label=text(session.get("instance_name") or session.get("name"), "当前副本"),
        state="冻结世界已经迁移",
        revision=to_int(updated_session.get("revision"), expected_revision),
        replayed=bool(result.get("replayed") or result.get("idempotent_replay")),
    )


__all__ = [name for name in globals() if not name.startswith('__')]

