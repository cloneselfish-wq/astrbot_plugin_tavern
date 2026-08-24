from .common import *
from .sessions import *
from .characters import *
from .author_jobs import *
from .operations import *

def _split_snapshot_target(value: str, *, prefix: str) -> tuple[str, str]:
    if "\x1f" not in value:
        raise _route_error(
            404,
            f"intent.{prefix}_expired",
            "所选存档已经失效。",
            "请刷新回放 Lens 后重新选择。",
        )
    session_id, target = value.split("\x1f", 1)
    if not session_id or not target:
        raise _route_error(
            404,
            f"intent.{prefix}_expired",
            "所选存档已经失效。",
            "请刷新回放 Lens 后重新选择。",
        )
    return session_id, target


async def _snapshot_action(
    principal: Mapping[str, Any],
    database: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    if intent == "snapshot.create":
        session_id = _resolved_target(
            principal, "sessions", target_key, kind="session"
        )
        values = _checked_input(body, allowed=frozenset({"name"}))
        result = await create_snapshot_intent(
            principal,
            database,
            session_ref=session_id,
            name=text(values.get("name")),
            replace=False,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        action_id = "E30"
        state = "命名存档已经创建"
    elif intent == "archive.trash":
        resolved = _resolved_target(
            principal, "sessions", target_key, kind="archive"
        )
        session_id, filename = _split_snapshot_target(
            resolved, prefix="archive"
        )
        values = _checked_input(
            body, allowed=frozenset({"acknowledge_trash"})
        )
        if not flag(values.get("acknowledge_trash")):
            raise _route_error(
                400,
                "intent.archive_confirmation_required",
                "移动独立存档前需要确认可恢复回收影响。",
                "请阅读说明并勾选确认后重新提交。",
            )
        result = await trash_archive_intent(
            principal,
            database,
            session_ref=session_id,
            filename=filename,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        action_id = "C22"
        state = "独立存档已经移入可恢复回收区"
    else:
        resolved = _resolved_target(
            principal, "sessions", target_key, kind="snapshot"
        )
        session_id, snapshot_ref = _split_snapshot_target(
            resolved, prefix="snapshot"
        )
        if intent == "snapshot.replace":
            values = _checked_input(
                body,
                allowed=frozenset({"name", "acknowledge_replace"}),
            )
            if not flag(values.get("acknowledge_replace")):
                raise _route_error(
                    400,
                    "intent.snapshot_replace_confirmation_required",
                    "更新命名存档前需要确认覆盖影响。",
                    "请阅读说明并勾选确认后重新提交。",
                )
            context = mapping(
                await database.snapshot_action_context(
                    session_id, snapshot_ref
                )
            )
            if to_int(context.get("revision")) != expected_revision:
                raise _route_error(
                    409,
                    "intent.snapshot_conflict",
                    "更新命名存档失败：副本或存档已经变化。",
                    "系统没有覆盖存档；请刷新回放 Lens 后重试。",
                )
            session = mapping(await database.get_session(session_id))
            snapshot = mapping(context.get("snapshot"))
            result = await create_snapshot_intent(
                principal,
                database,
                session_ref=session_id,
                name=text(values.get("name")),
                replace=True,
                expected_revision=to_int(session.get("revision"), 0) or 0,
                expected_snapshot_revision=to_int(snapshot.get("revision")),
                idempotency_key=idempotency_key,
            )
            action_id = "E30"
            state = "命名存档已经更新"
        elif intent == "snapshot.restore":
            values = _checked_input(
                body, allowed=frozenset({"acknowledge_restore"})
            )
            if not flag(values.get("acknowledge_restore")):
                raise _route_error(
                    400,
                    "intent.snapshot_restore_confirmation_required",
                    "恢复存档前需要确认当前状态会先建立保护点。",
                    "请阅读说明并勾选确认后重新提交。",
                )
            result = await restore_snapshot_intent(
                principal,
                database,
                session_ref=session_id,
                snapshot_ref=snapshot_ref,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
            action_id = "C20"
            state = "存档已经恢复，副本保持暂停"
        else:
            values = _checked_input(
                body, allowed=frozenset({"acknowledge_delete"})
            )
            if not flag(values.get("acknowledge_delete")):
                raise _route_error(
                    400,
                    "intent.snapshot_delete_confirmation_required",
                    "删除命名存档前需要确认目标。",
                    "请阅读说明并勾选确认后重新提交。",
                )
            result = await delete_snapshot_intent(
                principal,
                database,
                session_ref=session_id,
                snapshot_ref=snapshot_ref,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )
            action_id = "C21"
            state = "命名存档已经删除"
    output = _snapshot_route_body(
        result,
        fallback_code="intent.snapshot_failed",
        fallback_message="存档操作未能完成。",
        fallback_recovery="系统保留原状态；请刷新回放 Lens 后重试。",
    )
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label="当前副本存档",
        state=text(output.get("state"), state),
        revision=to_int(output.get("revision"), expected_revision),
        replayed=bool(output.get("replayed")),
    )


def _split_scoped_target(
    value: str,
    *,
    parts: int,
    label: str,
) -> tuple[str, ...]:
    values = tuple(text(item) for item in text(value).split("\x1f"))
    if len(values) != parts or not all(values):
        raise _route_error(
            404,
            "intent.authoring_target_expired",
            f"所选{label}已经失效。",
            "请刷新作者工作区后重新选择。",
        )
    return values


async def _world_authoring_action(
    principal: Mapping[str, Any],
    database: Any,
    services: Any,
    *,
    intent: str,
    target_key: str,
    expected_revision: int,
    idempotency_key: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    publish = _service(services, "publish")
    if intent in {"designer.field.save", "designer.preset.save"}:
        workspace = "designer"
    else:
        workspace = "worlds"
    world_ref = _resolved_target(
        principal, workspace, target_key, kind="world"
    )
    if intent == "designer.field.save":
        values = _checked_input(
            body,
            allowed=frozenset(
                {"field_key", "label", "help", "type", "required"}
            ),
        )
        resolved = _resolved_target(
            principal,
            "designer",
            text(values.get("field_key")),
            kind="designer-field",
        )
        target_world, field_key = _split_scoped_target(
            resolved, parts=2, label="角色卡字段"
        )
        if target_world != world_ref:
            raise _route_error(
                404,
                "intent.authoring_target_mismatch",
                "所选角色卡字段不属于当前世界。",
                "请刷新作者实验室后重新选择。",
            )
        world = mapping(await database.get_world(world_ref))
        actor = mapping(mapping(world.get("rules")).get("actor"))
        current = next(
            (
                mapping(item)
                for item in actor.get("fields", [])
                if isinstance(item, Mapping) and text(item.get("key")) == field_key
            ),
            {},
        )
        if not current:
            raise _route_error(
                404,
                "intent.authoring_field_missing",
                "所选角色卡字段已经不存在。",
                "请刷新建卡流程后重新选择。",
            )
        field_type = text(values.get("type"), text(current.get("type"), "text"))
        allowed_types = {
            "text", "textarea", "integer", "select", "preset_select",
            "multi_select", "boolean", "derived",
        }
        if field_type not in allowed_types or not text(values.get("label")):
            raise _route_error(
                400,
                "intent.authoring_field_invalid",
                "角色卡字段缺少名称或使用了不支持的类型。",
                "请填写字段名称并从页面列出的类型中选择。",
            )
        field = dict(current)
        field.update(
            {
                "key": field_key,
                "label": text(values.get("label")),
                "help": text(values.get("help")),
                "type": field_type,
                "required": flag(values.get("required")),
            }
        )
        result = await designer_field_save_intent(
            principal,
            database,
            world_ref=world_ref,
            field=field,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            publish=publish,
        )
        action_id = "E01"
        state = "角色卡字段已经保存"
    elif intent == "designer.preset.save":
        values = _checked_input(
            body,
            allowed=frozenset({"preset_key", "label", "description"}),
        )
        resolved = _resolved_target(
            principal,
            "designer",
            text(values.get("preset_key")),
            kind="designer-preset",
        )
        target_world, set_key, preset_id = _split_scoped_target(
            resolved, parts=3, label="角色预设"
        )
        if target_world != world_ref:
            raise _route_error(
                404,
                "intent.authoring_target_mismatch",
                "所选角色预设不属于当前世界。",
                "请刷新作者实验室后重新选择。",
            )
        world = mapping(await database.get_world(world_ref))
        actor = mapping(mapping(world.get("rules")).get("actor"))
        raw_set = mapping(actor.get("preset_sets")).get(set_key)
        candidates = (
            list(raw_set.values())
            if isinstance(raw_set, Mapping)
            else list(raw_set or [])
        )
        current = next(
            (
                mapping(item)
                for item in candidates
                if isinstance(item, Mapping) and text(item.get("id")) == preset_id
            ),
            {},
        )
        if not current or not text(values.get("label")):
            raise _route_error(
                400 if current else 404,
                "intent.authoring_preset_invalid",
                "所选角色预设不存在或缺少名称。",
                "请刷新预设列表并填写可辨认的名称。",
            )
        preset = dict(current)
        preset.update(
            {
                "id": preset_id,
                "label": text(values.get("label")),
                "description": text(values.get("description")),
            }
        )
        result = await designer_preset_save_intent(
            principal,
            database,
            world_ref=world_ref,
            set_key=set_key,
            preset=preset,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            publish=publish,
        )
        action_id = "E03"
        state = "角色预设已经保存"
    elif intent == "world.module.toggle":
        values = _checked_input(
            body, allowed=frozenset({"module_key", "enabled"})
        )
        resolved = _resolved_target(
            principal,
            "worlds",
            text(values.get("module_key")),
            kind="world-module",
        )
        target_world, package_ref, module_id = _split_scoped_target(
            resolved, parts=3, label="可选世界模块"
        )
        if target_world != world_ref:
            raise _route_error(
                404,
                "intent.module_target_mismatch",
                "所选模块不属于当前世界。",
                "请刷新世界库后重新选择。",
            )
        result = await twp_module_toggle_intent(
            principal,
            database,
            world_ref=world_ref,
            package_ref=package_ref,
            module_id=module_id,
            enabled=flag(values.get("enabled")),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            world_twp=_service(services, "world_twp"),
            publish=publish,
        )
        action_id = "E25"
        state = "世界模块状态已经更新"
    else:
        values = _checked_input(
            body, allowed=frozenset({"acknowledge_archive"})
        )
        if not flag(values.get("acknowledge_archive")):
            raise _route_error(
                400,
                "intent.world_archive_confirmation_required",
                "归档世界前需要确认运行影响。",
                "请确认没有活动副本仍依赖该世界后重新提交。",
            )
        result = await archive_world_intent(
            principal,
            database,
            world_ref=world_ref,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            default_world_slug=text(_service(services, "default_world_slug")),
            publish=publish,
        )
        action_id = "C05"
        state = "世界已经归档"
    output = _snapshot_route_body(
        result,
        fallback_code="intent.world_authoring_failed",
        fallback_message="世界或作者内容操作未能完成。",
        fallback_recovery="系统保留原状态；请刷新当前工作区后重试。",
    )
    item = mapping(output.get("item"))
    return _safe_success(
        action_id=action_id,
        intent=intent,
        label=text(item.get("name"), "当前世界"),
        state=state,
        revision=to_int(item.get("revision"), expected_revision),
        replayed=bool(output.get("replayed")),
    )


__all__ = [name for name in globals() if not name.startswith('__')]

