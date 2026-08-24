from .common import *

def _route_error(
    status: int,
    code: str,
    message: str,
    recovery: str,
) -> WebApiError:
    return WebApiError(status, code, message, recovery)


def _resolved_target(
    principal: Mapping[str, Any],
    workspace: str,
    key: str,
    *,
    kind: str,
) -> str:
    value = text(resolve_surface_key(principal, workspace, key, kind=kind))
    if not value:
        raise _route_error(
            404,
            "intent.target_expired",
            "所选项目已经失效或不属于当前账号。",
            "请刷新当前工作区后重新选择。",
        )
    return value


def _checked_input(
    body: Mapping[str, Any],
    *,
    allowed: frozenset[str],
) -> dict[str, Any]:
    value = mapping(body.get("input"))
    unknown = set(value) - allowed
    if unknown:
        raise _route_error(
            400,
            "intent.input_invalid",
            "提交内容包含当前动作不接受的字段。",
            "请关闭操作窗口，刷新后重新提交。",
        )
    return value


def _safe_success(
    *,
    action_id: str,
    intent: str,
    label: str,
    state: str,
    revision: int | str | None,
    replayed: bool,
) -> dict[str, Any]:
    outcome = "已返回此前结果" if replayed else "操作已经生效"
    envelope = visual_envelope(
        kind="action_intent",
        data={
            "action_id": action_id,
            "intent": intent,
            "outcome": outcome,
            "target": {"label": label, "state": state},
        },
        revision=revision,
        summary={
            "label": state,
            "summary": outcome,
            "state": "已完成",
            "count": 1,
        },
        permissions={"can_view": True, "can_manage": True},
    )
    return {"status": 200, "body": envelope.to_dict()}


def _safe_continuation(
    *,
    action_id: str,
    label: str,
    state: str,
    revision: int,
    intent: str,
    target_key: str,
    target_kind: str,
    description: str,
    fields: list[Mapping[str, Any]],
    details: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project the next confirmed step without exposing its server token."""

    envelope = visual_envelope(
        kind="action_intent",
        data={
            "action_id": action_id,
            "outcome": "预览已经完成，等待确认",
            "target": {"label": label, "state": state},
            "details": [dict(item) for item in (details or [])][:12],
            "continuation": {
                "action_id": action_id,
                "intent": intent,
                "target_key": target_key,
                "target_kind": target_kind,
                "expected_revision": revision,
                "description": description,
                "fields": [dict(item) for item in fields],
                "transportReady": True,
                "focus_return": "opener",
            },
        },
        revision=revision,
        summary={
            "label": label,
            "summary": description,
            "state": "等待确认",
            "count": 1,
        },
        permissions={"can_view": True, "can_manage": True},
    )
    return {"status": 200, "body": envelope.to_dict()}


def _safe_inspection(
    *,
    action_id: str,
    intent: str,
    label: str,
    state: str,
    revision: int | str | None,
    summary: str,
    details: list[Mapping[str, Any]],
) -> dict[str, Any]:
    envelope = visual_envelope(
        kind="action_intent",
        data={
            "action_id": action_id,
            "intent": intent,
            "outcome": "预览已经完成",
            "target": {"label": label, "state": state},
            "details": [dict(item) for item in details][:12],
        },
        revision=revision,
        summary={
            "label": state,
            "summary": summary,
            "state": "已完成",
            "count": len(details),
        },
        permissions={"can_view": True, "can_manage": True},
    )
    return {"status": 200, "body": envelope.to_dict()}


def _service(services: Any, name: str) -> Any:
    if isinstance(services, Mapping):
        return services.get(name)
    return getattr(services, name, None) if services is not None else None


def _resolved_session_target(
    principal: Mapping[str, Any],
    target_key: str,
) -> str:
    for workspace in ("dashboard", "sessions"):
        resolved = text(
            resolve_surface_key(
                principal,
                workspace,
                target_key,
                kind="session",
            )
        )
        if resolved:
            return resolved
    raise _route_error(
        404,
        "intent.session_target_expired",
        "所选副本已经失效或不属于当前账号。",
        "请刷新跑团现场后重新选择。",
    )


def _reminder_public(value: Mapping[str, Any]) -> dict[str, Any]:
    source = text(value.get("source")).lower()
    return {
        "enabled": bool(value.get("enabled")),
        "interval_seconds": to_int(value.get("interval_seconds"), 60) or 60,
        "source": {
            "global_default": "全局默认",
            "session_override": "副本覆盖",
            "implicit_default": "安全默认",
        }.get(source, "安全默认"),
        "updated_at": text(value.get("updated_at")),
        "applies_to": "next_generation",
    }


def _session_config_conflict(
    *,
    code: str,
    message: str,
    current: Mapping[str, Any],
    revision: int,
) -> dict[str, Any]:
    problem = {
        "code": code,
        "message": message,
        "recovery": "系统保留页面草稿；请比较当前值后重新提交。",
        "retryable": True,
        "preserved_revision": revision,
    }
    envelope = visual_envelope(
        kind="action_intent",
        data={"preserved": True, "current": dict(current)},
        revision=revision,
        summary={
            "label": "设置已经变化",
            "summary": message,
            "state": "需要比较",
            "count": 1,
        },
        permissions={"can_view": True, "can_manage": True},
        problems=[problem],
        state="conflict",
    )
    return {"status": 409, "body": envelope.to_dict()}


def _validate_global_reminder_settings_input(
    body: Mapping[str, Any],
) -> None:
    values = mapping(body.get("input"))
    enabled_name = "story_generation_reminder_enabled"
    interval_name = "story_generation_reminder_interval_seconds"
    if enabled_name in values and type(values[enabled_name]) is not bool:
        raise _route_error(
            400,
            "intent.settings_reminder_enabled_invalid",
            "故事生成提醒开关必须为布尔值。",
            "请重新切换开关后保存。",
        )
    if interval_name in values:
        try:
            validate_reminder_interval(values[interval_name])
        except GenerationReminderConfigError as exc:
            raise _route_error(
                400,
                "intent.settings_reminder_interval_invalid",
                "故事生成提醒间隔无效。",
                "请输入 30—600 秒，并使用 15 秒步长。",
            ) from exc


async def _session_generation_reminder_save(
    principal: Mapping[str, Any],
    database: Any,
    services: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _resolved_session_target(principal, target_key)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.generation_reminder_forbidden",
            "只有当前副本主持人或管理员可以调整故事生成提醒。",
            "你仍可查看当前有效设置；请联系主持人修改。",
        )
    values = _checked_input(
        body,
        allowed=frozenset({"enabled", "interval_seconds", "inherit_global"}),
    )
    enabled = values.get("enabled")
    inherit_global = values.get("inherit_global")
    if type(enabled) is not bool or type(inherit_global) is not bool:
        raise _route_error(
            400,
            "intent.generation_reminder_boolean_invalid",
            "故事生成提醒开关与继承选项必须为布尔值。",
            "请重新选择开关和继承方式后提交。",
        )
    try:
        interval_seconds = validate_reminder_interval(
            values.get("interval_seconds")
        )
    except GenerationReminderConfigError as exc:
        raise _route_error(
            400,
            "intent.generation_reminder_interval_invalid",
            "故事生成提醒间隔无效。",
            "请输入 30—600 秒，并使用 15 秒步长。",
        ) from exc
    global_config: dict[str, Any] = {}
    if inherit_global:
        plugin_config = _service(services, "plugin_config")
        recorder = getattr(database, "record_configuration_revision", None)
        if not isinstance(plugin_config, Mapping) or not callable(recorder):
            raise _route_error(
                503,
                "intent.generation_reminder_global_unavailable",
                "当前全局提醒设置无法读取。",
                "系统没有修改副本；请刷新后重试。",
            )
        effective = TavernConfig.from_mapping(plugin_config)
        full = effective.to_mapping()
        recorded = mapping(
            await recorder(full, f"console:{actor_id(principal)}")
        )
        global_revision = to_int(recorded.get("revision"))
        if global_revision is None:
            raise _route_error(
                503,
                "intent.generation_reminder_global_revision_missing",
                "当前全局提醒版本无法确认。",
                "系统没有修改副本；请刷新全局设置后重试。",
            )
        global_config = {
            "enabled": effective.story_generation_reminder_enabled,
            "interval_seconds": (
                effective.story_generation_reminder_interval_seconds
            ),
            "revision": global_revision,
        }
    try:
        result = mapping(
            await database.save_session_generation_reminder(
                session_id,
                enabled=enabled,
                interval_seconds=interval_seconds,
                inherit_global=inherit_global,
                expected_revision=expected_revision,
                actor_id=f"console:{actor_id(principal)}",
                idempotency_key=idempotency_key,
                global_config=global_config,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.generation_reminder_missing",
            "故事生成提醒设置不存在。",
            "请刷新跑团现场后重试。",
        ) from exc
    except DatabaseConflictError:
        current = mapping(
            await database.get_session_generation_reminder(session_id)
        )
        return _session_config_conflict(
            code="intent.generation_reminder_conflict",
            message="故事生成提醒设置在你编辑期间已经变化。",
            current=_reminder_public(current),
            revision=to_int(current.get("revision"), expected_revision)
            or expected_revision,
        )
    except (GenerationReminderConfigError, TypeError, ValueError) as exc:
        raise _route_error(
            400,
            "intent.generation_reminder_invalid",
            "故事生成提醒设置不符合当前约束。",
            "请检查开关、30—600 秒间隔和继承方式。",
        ) from exc
    return _safe_success(
        action_id="session-generation-reminder-save",
        intent="session.generation_reminder.save",
        label="故事生成提醒",
        state=(
            "已恢复全局默认；下次故事生成生效"
            if inherit_global
            else "副本提醒设置已保存；下次故事生成生效"
        ),
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


async def _session_narrative_mode_save(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _resolved_session_target(principal, target_key)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.narrative_mode_forbidden",
            "只有当前副本主持人或管理员可以调整正文模式。",
            "你仍可查看当前模式；请联系主持人修改。",
        )
    values = _checked_input(body, allowed=frozenset({"mode"}))
    mode = text(values.get("mode")).lower()
    if not mode:
        raise _route_error(
            400,
            "intent.narrative_mode_required",
            "请选择正文模式。",
            "请选择极简、平衡或史诗模式后提交。",
        )
    try:
        result = mapping(
            await database.set_narrative_mode(
                session_id,
                mode,
                expected_revision=expected_revision,
                actor_id=f"console:{actor_id(principal)}",
                idempotency_key=idempotency_key,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.narrative_mode_missing",
            "正文模式设置不存在。",
            "请刷新跑团现场后重试。",
        ) from exc
    except DatabaseConflictError:
        current = mapping(await database.get_narrative_mode(session_id))
        revision = to_int(current.get("revision"), expected_revision) or expected_revision
        safe_current = {
            key: value
            for key, value in current.items()
            if key in {"mode", "label", "minimum", "maximum", "description", "updated_at", "applies_to", "options"}
        }
        return _session_config_conflict(
            code="intent.narrative_mode_conflict",
            message="正文模式在你编辑期间已经变化。",
            current=safe_current,
            revision=revision,
        )
    except (TypeError, ValueError) as exc:
        raise _route_error(
            400,
            "intent.narrative_mode_invalid",
            "正文模式无效。",
            "请选择极简、平衡或史诗模式。",
        ) from exc
    return _safe_success(
        action_id="session-narrative-mode-save",
        intent="session.narrative_mode.save",
        label="正文模式",
        state="正文模式已保存；下次故事生成生效",
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


async def _session_generation_cancel_request(
    principal: Mapping[str, Any],
    database: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _resolved_session_target(principal, target_key)
    role = await require_member(database, session_id, principal)
    if role not in {"dm", "admin"}:
        raise _route_error(
            403,
            "intent.operation_cancel_forbidden",
            "只有当前副本主持人或管理员可以请求停止事务。",
            "请联系主持人处理当前故事生成。",
        )
    values = _checked_input(body, allowed=frozenset({"reason"}))
    reason = text(values.get("reason"))
    if not reason:
        raise _route_error(
            400,
            "intent.operation_cancel_reason_required",
            "停止故事生成前需要填写原因。",
            "请说明当前阻塞或风险后重新提交。",
        )
    try:
        result = mapping(
            await database.request_session_operation_cancel(
                session_id,
                f"console:{actor_id(principal)}",
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                reason=reason,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.operation_missing",
            "当前故事生成已经结束或不存在。",
            "请刷新生成进度后查看最新状态。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.operation_cancel_conflict",
            "停止请求未提交：故事生成阶段已经变化。",
            "系统没有回滚已完成事实；请刷新后重试。",
        ) from exc
    return _safe_success(
        action_id="generation-cancel",
        intent="operation.cancel.request",
        label="当前故事生成",
        state=(
            "已请求安全停止"
            if result.get("changed")
            else "停止请求已经存在"
            if result.get("found")
            else "故事生成已经结束，无需停止"
        ),
        revision=expected_revision,
        replayed=bool(result.get("replayed")),
    )


def project_recovery_preview(
    principal: Mapping[str, Any],
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    """Turn a verified backup preview into an opaque confirmed continuation."""

    require_admin(principal)
    value = mapping(preview)
    token = text(value.get("token")).lower()
    if len(token) != 32 or any(character not in "0123456789abcdef" for character in token):
        raise _route_error(
            400,
            "intent.recovery_preview_invalid",
            "备份预览没有生成有效的恢复凭据。",
            "系统没有替换任何数据；请重新上传并校验备份。",
        )
    revision = int(token[:13], 16)
    internal = json.dumps(
        {"token": token, "revision": revision},
        sort_keys=True,
        separators=(",", ":"),
    )
    target_key = issue_surface_key(
        principal,
        "settings",
        "recovery",
        internal,
    )
    counts = mapping(value.get("counts"))
    total_objects = sum(
        max(0, to_int(item, 0) or 0)
        for item in counts.values()
        if isinstance(item, (int, float, str))
    )
    confirmation = text(value.get("confirm_text"))
    return _safe_continuation(
        action_id="E26",
        label="完整备份恢复",
        state="已通过完整性预览",
        revision=revision,
        intent="backup.restore.execute",
        target_key=target_key,
        target_kind="recovery",
        description=(
            f"将替换当前目录数据并先建立回退点。请输入“{confirmation}”确认。"
        ),
        fields=[
            {
                "name": "confirm_text",
                "type": "text",
                "labelKey": "action.field.restore_confirmation",
                "required": True,
            }
        ],
        details=[
            {
                "label": "Schema 迁移",
                "summary": f"{to_int(value.get('source_schema'), 0)} → {to_int(value.get('target_schema'), 0)}",
                "state": "已校验",
            },
            {
                "label": "可恢复对象",
                "summary": f"共检查 {total_objects} 项目录记录。",
                "state": "等待确认",
            },
            {
                "label": "回退保护",
                "summary": text(value.get("rollback"), "执行前建立完整回退点。"),
                "state": "将自动执行",
            },
        ],
    )


def _safe_failure(
    exc: BaseException,
    *,
    preserved_revision: int | str | None,
) -> dict[str, Any]:
    status, problem = problem_from_exception(
        exc,
        preserved_revision=preserved_revision,
    )
    state = (
        "permission"
        if status in {401, 403}
        else "conflict"
        if status == 409
        else "error"
    )
    envelope = visual_envelope(
        kind="action_intent",
        data={"preserved": status in {409, 429}},
        revision=problem.preserved_revision,
        summary={
            "label": "操作未完成",
            "summary": problem.message,
            "state": "需要处理",
            "count": 1,
        },
        permissions={
            "can_view": state != "permission",
            "can_manage": False,
        },
        problems=[problem],
        state=state,
    )
    return {"status": status, "body": envelope.to_dict()}


async def _session_lifecycle(
    principal: Mapping[str, Any],
    database: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    action_id, action, result_state = _SESSION_ACTIONS[intent]
    session_id = _resolved_target(
        principal, "sessions", target_key, kind="session"
    )
    # Resolve inside the principal scope before role enforcement so a caller
    # cannot use this endpoint as an oracle for another principal's handle.
    # The existing lifecycle route is console-admin-only; console preserves it.
    require_admin(principal)
    values = _checked_input(
        body,
        allowed=frozenset(
            {"reason", "confirmation_name", "acknowledge_archive"}
        ),
    )
    confirmation_name = text(values.get("confirmation_name"))
    requires_archive_confirmation = action in {"finish", "abort"}
    if requires_archive_confirmation and not confirmation_name:
        raise _route_error(
            400,
            "intent.confirmation_required",
            "缺少当前副本名称确认。",
            "请按页面显示的副本名称重新输入后提交。",
        )
    try:
        result = await database.apply_session_lifecycle(
            session_id,
            action,
            f"console:{actor_id(principal)}",
            reason=text(values.get("reason")),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            confirmation_name=confirmation_name,
            acknowledge_archive=flag(values.get("acknowledge_archive")),
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.session_missing",
            "处理副本失败：副本已经不存在。",
            "系统没有修改数据；请刷新副本列表。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError) as exc:
        raise _route_error(
            409,
            "intent.session_conflict",
            "处理副本失败：当前状态已经变化或不允许该动作。",
            "系统没有覆盖新状态；请刷新后重新确认。",
        ) from exc
    session = mapping(mapping(result).get("session"))
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label=text(session.get("instance_name") or session.get("name"), "当前副本"),
        state=result_state,
        revision=to_int(session.get("revision")),
        replayed=bool(
            mapping(result).get("idempotent_replay")
            or mapping(mapping(result).get("result")).get("idempotent_replay")
        ),
    )


def _split_character_target(value: str) -> tuple[str, str]:
    if "\x1f" not in value:
        raise _route_error(
            404,
            "intent.character_expired",
            "所选角色已经失效。",
            "请刷新角色审核页后重新选择。",
        )
    session_id, participant_ref = value.split("\x1f", 1)
    if not session_id or not participant_ref:
        raise _route_error(
            404,
            "intent.character_expired",
            "所选角色已经失效。",
            "请刷新角色审核页后重新选择。",
        )
    return session_id, participant_ref


__all__ = [name for name in globals() if not name.startswith('__')]

