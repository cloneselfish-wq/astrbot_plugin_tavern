from .common import *
from .sessions import *
from .characters import *
from .author_jobs import *
from .operations import *
from .settings import *
from .snapshots import *
from .actor_fate import *

async def _session_pacing_commit(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(
        principal, "sessions", target_key, kind="pacing"
    )
    try:
        continuation = mapping(json.loads(resolved))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _route_error(
            404,
            "intent.pacing_preview_expired",
            "剧情节奏预览已经失效。",
            "请返回现场重新预览。",
        ) from exc
    session_id = text(continuation.get("session_id"))
    plan_revision = to_int(continuation.get("revision"))
    if not session_id or plan_revision != expected_revision:
        raise _route_error(
            409,
            "intent.pacing_preview_changed",
            "剧情节奏预览版本与当前确认不一致。",
            "系统没有改变世界；请重新预览。",
        )
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.pacing_forbidden",
            "只有当前副本主持人或管理员可以提交剧情节奏。",
            "请联系主持人处理当前停滞。",
        )
    values = _checked_input(body, allowed=frozenset({"acknowledge_pacing"}))
    if not flag(values.get("acknowledge_pacing")):
        raise _route_error(
            400,
            "intent.pacing_confirmation_required",
            "提交剧情节奏前需要确认影响。",
            "请阅读预览并勾选确认后重新提交。",
        )
    try:
        result = mapping(
            await database.commit_story_pacing(
                session_id=session_id,
                plan_id=text(continuation.get("plan_id")),
                preview_hash=text(continuation.get("preview_hash")),
                expected_session_revision=expected_revision,
                idempotency_key=idempotency_key,
                actor_id=f"console:{actor_id(principal)}",
                source="console_webui",
                reason=text(continuation.get("reason"), "console 剧情节奏确认"),
            )
        )
    except DatabaseConflictError as exc:
        raise _route_error(
            409,
            "intent.pacing_commit_conflict",
            "提交剧情节奏失败：现场或预览已经变化。",
            "系统没有覆盖新状态；请刷新现场并重新预览。",
        ) from exc
    session = mapping(await database.get_session(session_id))
    return _safe_success(
        action_id="C26",
        intent="session.pacing.commit",
        label=text(session.get("instance_name") or session.get("name"), "当前副本"),
        state=text(result.get("summary"), "剧情节奏已经更新"),
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


async def _designer_simulation(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    world_ref = _resolved_target(
        principal, "designer", target_key, kind="world"
    )
    require_author(principal)
    values = _checked_input(body, allowed=frozenset({"turns", "party_size"}))
    turns = to_int(values.get("turns"), 30) or 30
    party_size = to_int(values.get("party_size"), 4) or 4
    if turns < 1 or turns > 200 or party_size not in {1, 2, 4, 6, 8}:
        raise _route_error(
            400,
            "intent.simulation_input_invalid",
            "流程模拟参数不在安全范围内。",
            "轮数请选择 1—200，队伍人数请选择 1、2、4、6 或 8。",
        )
    world = mapping(await database.get_world(world_ref))
    if not world:
        raise _route_error(
            404,
            "intent.world_missing",
            "流程模拟失败：世界已经不存在。",
            "请刷新作者实验室并重新选择世界。",
        )
    actual_revision = to_int(world.get("revision"), 0) or 0
    if actual_revision != expected_revision:
        raise _route_error(
            409,
            "intent.simulation_revision_conflict",
            "流程模拟失败：世界草稿在你打开后已经变化。",
            "系统没有保存任何内容；请刷新后重新模拟。",
        )
    from ...twp.simulation import run_smoke_simulation

    report = mapping(
        await asyncio.to_thread(
            run_smoke_simulation,
            world,
            turns=turns,
            party_sizes=[party_size],
        )
    )
    errors = [item for item in report.get("errors", []) if isinstance(item, Mapping)]
    state = "模拟通过" if bool(report.get("ok")) else "模拟发现问题"
    return _safe_inspection(
        action_id="E14",
        intent="designer.simulate",
        label=text(world.get("name"), "当前世界"),
        state=state,
        revision=actual_revision,
        summary=(
            "这次模拟只使用确定性夹具，没有修改世界草稿或运行中的副本。"
        ),
        details=[
            {"label": "模拟轮数", "summary": str(turns), "state": "夹具"},
            {"label": "队伍人数", "summary": str(party_size), "state": "夹具"},
            {
                "label": "场景覆盖",
                "summary": f"访问 {len(report.get('scenes_visited', []))} 个场景",
                "state": "已计算",
            },
            {
                "label": "发现的问题",
                "summary": f"{len(errors)} 项",
                "state": "需要处理" if errors else "无阻塞",
            },
        ],
    )


async def _backup_restore_execute(
    principal: Mapping[str, Any],
    services: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(
        principal, "settings", target_key, kind="recovery"
    )
    require_admin(principal)
    try:
        continuation = mapping(json.loads(resolved))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _route_error(
            404,
            "intent.recovery_preview_expired",
            "备份恢复预览已经失效。",
            "系统没有替换任何数据；请重新上传备份。",
        ) from exc
    if to_int(continuation.get("revision")) != expected_revision:
        raise _route_error(
            409,
            "intent.recovery_preview_changed",
            "备份恢复预览与当前确认不一致。",
            "系统没有替换任何数据；请重新上传并预览。",
        )
    values = _checked_input(body, allowed=frozenset({"confirm_text"}))
    recovery = _service(services, "backup_recovery_service")
    if recovery is None or not callable(getattr(recovery, "execute", None)):
        raise _route_error(
            503,
            "intent.recovery_service_unavailable",
            "完整备份恢复服务当前不可用。",
            "系统没有替换任何数据；请稍后刷新设置页重试。",
        )
    try:
        result = mapping(
            await asyncio.to_thread(
                recovery.execute,
                text(continuation.get("token")),
                confirm_text=text(values.get("confirm_text")),
                operation_id=idempotency_key,
                actor_id=f"console:{actor_id(principal)}",
            )
        )
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.recovery_conflict",
            "完整备份恢复未能提交：预览或当前数据已经变化。",
            "系统保留原数据与回退点；请重新上传并预览。",
        ) from exc
    return _safe_success(
        action_id="E26",
        intent="backup.restore.execute",
        label="完整备份恢复",
        state=text(result.get("summary"), "备份恢复已经完成"),
        revision=expected_revision,
        replayed=bool(result.get("replayed")),
    )


async def execute_intent(
    principal: Mapping[str, Any],
    database: Any,
    router: Any,
    *,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
    services: Any = None,
) -> dict[str, Any]:
    """Execute one fixed console semantic intent and return a safe envelope."""

    expected_revision: int | None = None
    try:
        require_login(principal)
        body = mapping(payload)
        supplied = set(body)
        if supplied.intersection(_FORBIDDEN_TRANSPORT_FIELDS):
            raise _route_error(
                400,
                "intent.internal_reference_rejected",
                "当前操作不能接收内部地址或稳定标识。",
                "请从当前 console 工作区重新打开该操作。",
            )
        if supplied - _REQUEST_FIELDS:
            raise _route_error(
                400,
                "intent.request_invalid",
                "当前操作包含未登记的请求字段。",
                "请关闭操作窗口，刷新后重新提交。",
            )
        intent = text(body.get("intent")).lower()
        if intent not in INTENT_ALLOWLIST:
            raise _route_error(
                400,
                "intent.not_allowed",
                "当前页面动作尚未迁移或已经停用。",
                "请刷新页面并使用仍然可见的操作。",
            )
        target_key = text(body.get("target_key"))
        if not target_key:
            raise _route_error(
                400,
                "intent.target_required",
                "缺少要处理的页面项目。",
                "请从当前列表重新选择。",
            )
        expected_revision = to_int(body.get("expected_revision"))
        if expected_revision is None or expected_revision < 0:
            raise _route_error(
                409,
                "intent.revision_required",
                "当前页面项目的状态版本无法确认。",
                "请刷新当前工作区后重新提交。",
            )
        request_key = text(idempotency_key)
        if not request_key or len(request_key) > 200:
            raise _route_error(
                400,
                "intent.idempotency_required",
                "本次操作缺少有效的防重复凭证。",
                "请保持操作窗口打开并重新提交。",
            )

        if intent == "session.generation_reminder.save":
            return await _session_generation_reminder_save(
                principal,
                database,
                services,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent == "session.narrative_mode.save":
            return await _session_narrative_mode_save(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _ACTOR_FATE_PREVIEW_ACTIONS:
            return await _actor_fate_preview_action(
                principal,
                database,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )

        if intent in _SESSION_ACTIONS:
            return await _session_lifecycle(
                principal,
                database,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _WORLD_MIGRATION_ACTIONS:
            return await _session_world_migrate(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent == "session.pacing.preview":
            return await _session_pacing_preview(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                body=body,
            )
        if intent == "session.pacing.commit":
            return await _session_pacing_commit(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _DESIGNER_ACTIONS:
            return await _designer_simulation(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                body=body,
            )
        if intent in _AUTHOR_JOB_CREATE_ACTIONS:
            return await _author_job_create(
                principal,
                database,
                router,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _RECOVERY_ACTIONS:
            return await _backup_restore_execute(
                principal,
                services,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _MEMORY_ACTIONS:
            return await _memory_governance(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _OPERATION_ACTIONS:
            if any(
                resolve_surface_key(
                    principal, workspace, target_key, kind="session"
                )
                for workspace in ("dashboard", "sessions")
            ):
                return await _session_generation_cancel_request(
                    principal,
                    database,
                    target_key=target_key,
                    expected_revision=expected_revision,
                    idempotency_key=request_key,
                    body=body,
                )
            return await _operation_cancel_request(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _TIMER_ACTIONS:
            return await _timer_control(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _PARTICIPANT_ACTIONS:
            return await _participant_action(
                principal,
                database,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _SESSION_CLONE_ACTIONS:
            return await _session_clone(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _SETTINGS_ACTIONS:
            _validate_global_reminder_settings_input(body)
            return await _settings_group_save(
                principal,
                database,
                services,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _TOKEN_QUOTA_ACTIONS:
            return await _session_token_quota(
                principal,
                database,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _SNAPSHOT_ACTIONS:
            return await _snapshot_action(
                principal,
                database,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _WORLD_AUTHORING_ACTIONS:
            return await _world_authoring_action(
                principal,
                database,
                services,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _RESIDENT_CHARACTER_ACTIONS:
            return await _resident_character_action(
                principal,
                database,
                services,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _GITHUB_IMPORT_ACTIONS:
            return await _github_import_action(
                principal,
                database,
                services,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _CARD_ACTIONS:
            return await _card_review(
                principal,
                database,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _TENDENCY_ACTIONS:
            return await _tendency_evidence(
                principal,
                database,
                router,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        if intent in _HEALTH_ACTIONS:
            return await _health_recovery(
                principal,
                database,
                router,
                intent=intent,
                target_key=target_key,
                expected_revision=expected_revision,
                idempotency_key=request_key,
                body=body,
            )
        return await _author_job(
            principal,
            database,
            router,
            intent=intent,
            target_key=target_key,
            expected_revision=expected_revision,
            idempotency_key=request_key,
            body=body,
        )
    except Exception as exc:  # noqa: BLE001 - safe Web boundary
        return _safe_failure(exc, preserved_revision=expected_revision)


__all__ = [name for name in globals() if not name.startswith('__')]

