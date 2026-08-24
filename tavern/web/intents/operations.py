from .common import *
from .sessions import *
from .characters import *
from .author_jobs import *

async def _session_clone(
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
        body, allowed=frozenset({"instance_name", "instance_slug"})
    )
    name = text(values.get("instance_name"))
    slug = text(values.get("instance_slug")).lower()
    if not name or not slug:
        raise _route_error(
            400,
            "intent.session_clone_input_required",
            "克隆副本需要填写新分支名称与短标识。",
            "请使用当前群内唯一、可读的名称和短标识后重试。",
        )
    try:
        result = mapping(
            await database.clone_session(
                session_id,
                f"console:{actor_id(principal)}",
                instance_slug=slug,
                instance_name=name,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.session_clone_missing",
            "克隆副本失败：源副本或保护点已经不存在。",
            "系统没有创建残留分支；请刷新副本运行页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError, ValueError) as exc:
        raise _route_error(
            409,
            "intent.session_clone_conflict",
            "克隆副本失败：源状态已经变化，或名称与现有分支冲突。",
            "系统没有覆盖任何副本；请刷新并更换短标识后重试。",
        ) from exc
    return _safe_success(
        action_id="E10",
        intent="session.clone",
        label=text(result.get("instance_name") or result.get("name"), name),
        state="独立关闭分支已经建立",
        revision=to_int(result.get("revision"), 1),
        replayed=bool(result.get("replayed")),
    )


def _set_nested(root: dict[str, Any], path: str, value: Any) -> None:
    section, field = path.split(".", 1)
    current = root.get(section)
    target = dict(current) if isinstance(current, Mapping) else {}
    target[field] = value
    root[section] = target


async def _settings_group_save(
    principal: Mapping[str, Any],
    database: Any,
    services: Any,
    *,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    resolved = _resolved_target(
        principal, "settings", target_key, kind="setting"
    )
    require_admin(principal)
    if not resolved.startswith("group:"):
        raise _route_error(
            404,
            "intent.settings_group_expired",
            "所选设置组已经失效。",
            "请刷新设置页后重新选择当前分组。",
        )
    group = resolved[len("group:") :]
    fields = _SETTINGS_FIELDS.get(group)
    if not fields:
        raise _route_error(
            404,
            "intent.settings_group_expired",
            "所选设置组已经失效。",
            "请刷新设置页后重新选择当前分组。",
        )
    values = _checked_input(body, allowed=frozenset(fields))
    if not values:
        raise _route_error(
            400,
            "intent.settings_values_required",
            "当前设置组没有可保存的修改。",
            "请修改至少一个可编辑字段后再保存。",
        )
    plugin_config = _service(services, "plugin_config")
    if not isinstance(plugin_config, Mapping):
        raise _route_error(
            503,
            "intent.settings_service_unavailable",
            "宿主设置服务当前不可用。",
            "系统没有修改设置；请稍后刷新设置页。",
        )
    lock = _service(services, "config_lock")

    async def commit() -> dict[str, Any]:
        current = TavernConfig.from_mapping(plugin_config).to_mapping()
        candidate = json.loads(json.dumps(current, ensure_ascii=False))
        for name, raw in values.items():
            path, value_type = fields[name]
            if value_type == "bool":
                converted: Any = flag(raw)
            elif value_type == "int":
                converted = to_int(raw)
                if converted is None:
                    raise _route_error(
                        400,
                        "intent.settings_value_invalid",
                        "设置值不是有效整数。",
                        "请修正标红字段后重新保存。",
                    )
            elif value_type == "float":
                try:
                    converted = float(raw)
                except (TypeError, ValueError) as exc:
                    raise _route_error(
                        400,
                        "intent.settings_value_invalid",
                        "设置值不是有效数字。",
                        "请修正标红字段后重新保存。",
                    ) from exc
            elif value_type == "text":
                converted = text(raw)
            elif value_type == "csv":
                source = raw if isinstance(raw, list) else str(raw or "").replace("，", ",").split(",")
                converted = list(dict.fromkeys(text(item) for item in source if text(item)))
            else:
                converted = text(raw).lower()
                if converted not in {"deny", "silent"}:
                    raise _route_error(
                        400,
                        "intent.settings_value_invalid",
                        "未授权请求处理方式无效。",
                        "请选择明确拒绝或静默忽略。",
                    )
            _set_nested(candidate, path, converted)
        try:
            normalized = TavernConfig.from_mapping(candidate).to_mapping()
        except (TypeError, ValueError) as exc:
            raise _route_error(
                400,
                "intent.settings_validation_failed",
                "保存设置失败：字段组合不符合当前配置约束。",
                "系统保留原设置；请修正页面提示后重试。",
            ) from exc
        default_world = mapping(
            await database.get_world(
                text(mapping(normalized.get("runtime")).get("default_world_slug"))
            )
        )
        if not default_world or flag(default_world.get("archived")):
            raise _route_error(
                409,
                "intent.settings_default_world_invalid",
                "保存设置失败：当前默认世界不存在或已经归档。",
                "系统保留原设置；请先在世界库选择可用世界。",
            )
        try:
            prepared = mapping(
                await database.prepare_configuration_update(
                    idempotency_key,
                    expected_revision,
                    current,
                    normalized,
                    f"console:{actor_id(principal)}",
                )
            )
        except DatabaseConflictError as exc:
            raise _route_error(
                409,
                "intent.settings_conflict",
                "保存设置失败：宿主设置或修订已经变化。",
                "系统没有覆盖新设置；请刷新当前分组并比较后重试。",
            ) from exc
        if prepared.get("revision") is not None:
            return prepared
        missing = object()
        previous = {
            section: plugin_config.get(section, missing)
            for section in normalized
        }
        try:
            for section, value in normalized.items():
                plugin_config[section] = value
            save_async = getattr(plugin_config, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                save = getattr(plugin_config, "save_config", None)
                if callable(save):
                    save()
            persisted = TavernConfig.from_mapping(plugin_config).to_mapping()
            if persisted != normalized:
                raise RuntimeError("配置保存后回读校验不一致")
        except Exception:
            for section, value in previous.items():
                if value is missing:
                    plugin_config.pop(section, None)
                else:
                    plugin_config[section] = value
            raise
        return mapping(
            await database.complete_configuration_update(
                idempotency_key,
                normalized,
                f"console:{actor_id(principal)}",
            )
        )

    try:
        if lock is not None and hasattr(lock, "__aenter__"):
            async with lock:
                result = await commit()
        else:
            result = await commit()
    except WebApiError:
        raise
    except Exception as exc:
        raise _route_error(
            500,
            "intent.settings_save_failed",
            "保存设置失败：宿主没有确认写入结果。",
            "系统已恢复内存中的原设置；请刷新页面核对持久配置后重试。",
        ) from exc
    return _safe_success(
        action_id="settings.save",
        intent="settings.group.save",
        label="当前设置组",
        state="设置已经保存并回读确认",
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


async def _session_token_quota(
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
        allowed=frozenset({"window_seconds", "token_limit", "enabled"}),
    )
    window_seconds = to_int(values.get("window_seconds"))
    token_limit = to_int(values.get("token_limit"))
    if (
        window_seconds is None
        or window_seconds < 60
        or window_seconds > 365 * 24 * 60 * 60
        or token_limit is None
        or token_limit < 1
        or token_limit > 1_000_000_000
    ):
        raise _route_error(
            400,
            "intent.token_quota_invalid",
            "Token 限额或统计窗口不在安全范围内。",
            "窗口请输入 60 秒至 365 天，限额请输入 1 至 10 亿。",
        )
    try:
        result = mapping(
            await database.set_token_quota(
                session_id,
                "group",
                window_seconds=window_seconds,
                token_limit=token_limit,
                enabled=flag(values.get("enabled")),
                actor_id=f"console:{actor_id(principal)}",
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
        )
    except DatabaseNotFoundError as exc:
        raise _route_error(
            404,
            "intent.token_quota_session_missing",
            "调整 Token 限额失败：副本已经不存在。",
            "系统没有修改其他群；请刷新副本运行页。",
        ) from exc
    except (DatabaseConflictError, InvalidTransitionError, ValueError) as exc:
        raise _route_error(
            409,
            "intent.token_quota_conflict",
            "调整 Token 限额失败：限额或副本状态已经变化。",
            "系统没有覆盖新策略；请刷新副本运行页后重试。",
        ) from exc
    return _safe_success(
        action_id="E16",
        intent="session.token_quota.set",
        label="当前群 Token 限额",
        state="限额策略已经保存，历史用量保持不变",
        revision=to_int(result.get("revision"), expected_revision),
        replayed=bool(result.get("replayed")),
    )


def _snapshot_route_body(
    result: Mapping[str, Any],
    *,
    fallback_code: str,
    fallback_message: str,
    fallback_recovery: str,
) -> dict[str, Any]:
    value = mapping(result)
    status = int(value.get("status") or 500)
    if status >= 400:
        error = mapping(value.get("error"))
        raise _route_error(
            status,
            text(error.get("code"), fallback_code),
            text(
                error.get("reason") or error.get("message"),
                fallback_message,
            ),
            text(
                error.get("next_command") or error.get("recovery"),
                fallback_recovery,
            ),
        )
    return mapping(value.get("body"))


__all__ = [name for name in globals() if not name.startswith('__')]

