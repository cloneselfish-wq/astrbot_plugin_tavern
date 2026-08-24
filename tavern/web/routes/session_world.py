from .visual_support import *
from .session_summary import *
from .session_party import *
from ...visualization.public_states import public_state_fields

@_visual_route("party")
async def session_party_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session, role, principal, keys = await _context(principal, database, query)
    conflict = _check_expected_revision("party", session, role, principal, query)
    if conflict:
        return conflict
    return _response(
        await build_session_party(
            database,
            session,
            role=role,
            is_admin=bool(principal.get("is_admin")),
            keys=keys,
        )
    )


@_visual_route("world_visuals")
async def session_world_visuals_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    route_principal = dict(principal)
    if text(route_principal.get("auth_source")) != "miniprogram_binding":
        # participant_ref is an authenticated identity attribute only for a
        # miniprogram binding.  Platform/console callers are resolved by their
        # own principal and cannot borrow another roster row through this key.
        route_principal.pop("participant_ref", None)
    session, role, principal, keys = await _context(
        route_principal,
        database,
        query,
    )
    conflict = _check_expected_revision(
        "world_visuals", session, role, principal, query
    )
    if conflict:
        return conflict
    viewer = None
    runtime_principal_ref = text(principal.get("username"))
    if role == "player":
        auth_source = text(principal.get("auth_source"))
        # A miniprogram username is an opaque binding handle, not the actor
        # identity stored in the frozen tactical roster.  Conversely, a
        # platform principal must not be able to override its platform user
        # id by supplying another participant reference.
        viewer_username = (
            "" if auth_source == "miniprogram_binding"
            else text(principal.get("username"))
        )
        viewer_participant_ref = (
            text(principal.get("participant_ref"))
            if auth_source == "miniprogram_binding"
            else ""
        )
        viewer = await resolve_viewer_participant(
            database,
            text(session.get("id")),
            viewer_username,
            viewer_participant_ref,
        )
        # Fail closed when the verified participant cannot be resolved: the
        # projector may still return public/party state, but no row is marked
        # as self and no actor-owned tactical choices are exposed.
        runtime_principal_ref = text(mapping(viewer).get("group_user_id"))
    envelope = await build_session_world_visuals(
        database,
        session,
        role=role,
        is_admin=bool(principal.get("is_admin")),
        viewer_participant=viewer,
        keys=keys,
        requested_surface_key=text(mapping(query).get("surface_key")),
        placement=text(mapping(query).get("placement"), "live_session"),
        principal_ref=runtime_principal_ref,
    )
    body = envelope.to_dict()
    body_data = mapping(body.get("data"))
    profile = mapping(body_data.get("ui_profile"))
    declared_lenses = {
        text(mapping(item).get("key"))
        for item in profile.get("live_lenses") or ()
        if isinstance(item, Mapping)
    }
    if (
        role in {"dm", "admin"}
        and body.get("state") != "readonly"
        and "clocks" in declared_lenses
    ):
        timer_rows = await database.list_timers(text(session.get("id")))
        timer_items: list[dict[str, Any]] = []
        labels = {
            "turn": "当前回合倒计时",
            "vote": "集体表决倒计时",
            "preparation": "准备阶段倒计时",
            "ready": "角色准备倒计时",
            "standby": "暂离保留倒计时",
            "card_completion": "角色填写倒计时",
        }
        for raw in timer_rows or ():
            timer = mapping(raw)
            status = text(timer.get("status")).lower()
            internal = text(timer.get("id"))
            if status not in {"active", "paused"} or not internal:
                continue
            options = (
                [
                    {"value": "pause", "label": "暂停"},
                    {"value": "extend", "label": "延长"},
                    {"value": "expire", "label": "立即到期"},
                    {"value": "disable", "label": "停用"},
                ]
                if status == "active"
                else [
                    {"value": "resume", "label": "恢复"},
                    {"value": "extend", "label": "延长"},
                    {"value": "expire", "label": "立即到期"},
                    {"value": "disable", "label": "停用"},
                ]
            )
            remaining = max(0, to_int(timer.get("remaining_seconds"), 0) or 0)
            timer_items.append(
                {
                    "key": issue_surface_key(
                        principal,
                        "sessions",
                        "timer",
                        f"{text(session.get('id'))}\x1f{internal}",
                    ),
                    "object_kind": "timer",
                    "label": labels.get(
                        text(timer.get("timer_type")).lower(),
                        "运行倒计时",
                    ),
                    "type": "time",
                    "remaining_seconds": remaining,
                    "remaining_label": f"剩余 {remaining} 秒",
                    **public_state_fields(
                        status,
                        family="timer",
                        problem_code="visual.world.timer_state_unknown",
                    ),
                    "updated_at": text(timer.get("updated_at")),
                    "revision": timer_revision(timer),
                    "available_actions": [
                        {
                            "action_id": "C24",
                            "intent": "timer.control",
                            "label": "调整倒计时",
                            "target_kind": "timer",
                            "expected_revision": timer_revision(timer),
                            "description": "调整只作用于当前副本的这一个倒计时；立即到期可能触发既定超时规则。",
                            "transportReady": True,
                            "focus_return": "opener",
                            "fields": [
                                {
                                    "name": "operation",
                                    "type": "select",
                                    "labelKey": "action.field.timer_operation",
                                    "required": True,
                                    "options": options,
                                },
                                {
                                    "name": "seconds",
                                    "type": "number",
                                    "labelKey": "action.field.timer_seconds",
                                },
                            ],
                        }
                    ],
                }
            )
        surfaces = mapping(body_data.get("surfaces"))
        clocks = mapping(surfaces.get("clocks"))
        clock_data = mapping(clocks.get("data"))
        current_items = [
            dict(item)
            for item in clock_data.get("items", [])
            if isinstance(item, Mapping)
        ]
        combined = (timer_items + current_items)[:6]
        clock_data["items"] = combined
        clock_data["total_items"] = len(timer_items) + len(current_items)
        clock_data["truncated"] = clock_data["total_items"] > len(combined)
        clocks["data"] = clock_data
        clocks["empty"] = not combined
        clocks["state"] = "empty" if not combined else "ready"
        clocks["summary"] = {"label": "clocks", "count": len(combined)}
        surfaces["clocks"] = clocks
        body_data["surfaces"] = surfaces
        body["data"] = body_data
    return {"status": 200, "body": body}


@_visual_route("history")
async def session_history_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    delivery_service: Any = None,
) -> dict[str, Any]:
    session, role, principal, keys = await _context(principal, database, query)
    conflict = _check_expected_revision("history", session, role, principal, query)
    if conflict:
        return conflict
    values = mapping(query)
    page_size = max(1, min(100, to_int(values.get("page_size"), 20) or 20))
    session_id = text(session.get("id"))
    actor_names = await _history_actor_names(database, session_id)
    facet_rows = await _history_visible_rows(
        database,
        session_id,
        role=role,
        limit=_HISTORY_SCAN_LIMIT + 1,
    )
    facet_truncated = len(facet_rows) > _HISTORY_SCAN_LIMIT
    facet_projected = _history_project_rows(
        facet_rows[:_HISTORY_SCAN_LIMIT],
        keys=keys,
        actor_names=actor_names,
    )
    filter_options = _history_filter_options(facet_projected)
    filters = _validate_history_filters(values, filter_options)
    scoped_keys = _history_filter_keys(keys, filters)
    after_sequence = (
        scoped_keys.read_cursor("historyseq", text(values.get("cursor")))
        if text(values.get("cursor"))
        else 0
    )
    page_rows = (
        facet_rows
        if after_sequence == 0
        else await _history_visible_rows(
            database,
            session_id,
            role=role,
            after_sequence=after_sequence,
            limit=_HISTORY_SCAN_LIMIT + 1,
        )
    )
    page_scan_truncated = len(page_rows) > _HISTORY_SCAN_LIMIT
    page_projected = _history_project_rows(
        page_rows[:_HISTORY_SCAN_LIMIT],
        keys=keys,
        actor_names=actor_names,
    )
    matching = [
        (item, meta)
        for item, meta in page_projected
        if _history_matches(item, meta, filters)
    ]
    selected = matching[:page_size]
    has_more = len(matching) > page_size or page_scan_truncated
    if len(matching) > page_size and selected:
        next_position = to_int(selected[-1][0].get("sequence"), 0) or 0
    elif page_scan_truncated and page_projected:
        next_position = to_int(page_projected[-1][0].get("sequence"), 0) or 0
    else:
        next_position = 0
    delivery_viewer = "admin" if role == "admin" else "dm" if role == "dm" else ""
    if role == "player":
        participant = await resolve_viewer_participant(
            database,
            text(session.get("id")),
            text(principal.get("username")),
            text(principal.get("participant_ref")),
        )
        identity = text(
            mapping(participant).get("group_user_id")
            or mapping(participant).get("id")
        )
        if identity:
            delivery_viewer = f"player:{identity}"
    envelope = await build_session_history(
        database,
        session,
        role=role,
        is_admin=bool(principal.get("is_admin")),
        keys=keys,
        cursor="",
        page_size=1,
        delivery_cursor=text(values.get("delivery_cursor")),
        delivery_service=delivery_service,
        delivery_viewer=delivery_viewer,
    )
    body = envelope.to_dict()
    data = mapping(body.get("data"))
    items = [item for item, _meta in selected]
    visible_latest = max(
        (to_int(item.get("sequence"), 0) or 0 for item, _meta in facet_projected),
        default=0,
    )
    timeline = {
        "items": items,
        "next_cursor": (
            scoped_keys.cursor("historyseq", next_position)
            if has_more and next_position > 0
            else ""
        ),
        "has_more": bool(has_more and next_position > 0),
        "page_size": page_size,
        "latest_sequence": visible_latest,
        "state": "ready" if items else "empty",
        "problems": [],
    }
    data["timeline"] = timeline
    data["filters"] = {
        "search": True,
        **filter_options,
        "active_count": sum(
            bool(filters[field]) for field in ("q", "round", "actor", "type")
        ),
    }
    if facet_truncated or page_scan_truncated:
        body.setdefault("problems", []).append(
            {
                "code": "visual.history.filter_scan_truncated",
                "message": "回放筛选选项只覆盖了当前可安全扫描的可见记录。",
                "recovery": "可继续翻页，或缩小搜索范围后重试。",
                "retryable": False,
            }
        )
        if body.get("state") not in {"readonly", "stale", "permission", "error"}:
            body["state"] = "partial"
    deliveries = mapping(data.get("deliveries"))
    result_count = len(items) + len(deliveries.get("items") or ())
    body["summary"] = {"label": "副本回放与投递", "count": result_count}
    if body.get("state") not in {"readonly", "stale", "partial", "permission", "error"}:
        body["state"] = "ready" if result_count else "empty"
    if items and text(items[-1].get("created_at")):
        body["updated_at"] = text(items[-1].get("created_at"))
    readonly = body.get("state") == "readonly" or text(session.get("state")) == "finished"
    can_manage = role in {"dm", "admin"} and not readonly
    snapshots: list[dict[str, Any]] = []
    if callable(getattr(database, "list_snapshots", None)):
        for raw in (await database.list_snapshots(session_id) or ())[:20]:
            snapshot = mapping(raw)
            internal = text(snapshot.get("id") or snapshot.get("name"))
            name = text(snapshot.get("name"), "命名存档")
            if not internal:
                continue
            context: dict[str, Any] = {}
            if can_manage and callable(
                getattr(database, "snapshot_action_context", None)
            ):
                context = mapping(
                    await database.snapshot_action_context(
                        session_id, internal
                    )
                )
            revision = to_int(context.get("revision"), 0) or 0
            actions: list[dict[str, Any]] = []
            if can_manage and revision > 0:
                actions.extend(
                    [
                        {
                            "action_id": "C20",
                            "intent": "snapshot.restore",
                            "label": "恢复到这个存档",
                            "target_kind": "snapshot",
                            "expected_revision": revision,
                            "description": "先建立当前状态保护点，再恢复所选存档并暂停副本。",
                            "transportReady": True,
                            "focus_return": "opener",
                            "fields": [
                                {
                                    "name": "acknowledge_restore",
                                    "type": "checkbox",
                                    "labelKey": "action.field.acknowledge_restore",
                                    "required": True,
                                }
                            ],
                        },
                        {
                            "action_id": "E30",
                            "intent": "snapshot.replace",
                            "label": "更新这个命名存档",
                            "target_kind": "snapshot",
                            "expected_revision": revision,
                            "description": "以当前状态替换这个命名存档；保护点和最终存档不能替换。",
                            "transportReady": True,
                            "focus_return": "opener",
                            "fields": [
                                {
                                    "name": "name",
                                    "type": "text",
                                    "labelKey": "action.field.name",
                                    "required": True,
                                    "value": name,
                                },
                                {
                                    "name": "acknowledge_replace",
                                    "type": "checkbox",
                                    "labelKey": "action.field.acknowledge_replace",
                                    "required": True,
                                },
                            ],
                        },
                    ]
                )
                if text(snapshot.get("kind")).lower() == "manual":
                    actions.append(
                        {
                            "action_id": "C21",
                            "intent": "snapshot.delete",
                            "label": "删除这个命名存档",
                            "target_kind": "snapshot",
                            "expected_revision": revision,
                            "description": "只删除这个手动存档；安全点、回滚点和最终存档不会开放删除。",
                            "transportReady": True,
                            "focus_return": "opener",
                            "fields": [
                                {
                                    "name": "acknowledge_delete",
                                    "type": "checkbox",
                                    "labelKey": "action.field.acknowledge_delete",
                                    "required": True,
                                }
                            ],
                        }
                    )
            snapshots.append(
                {
                    "key": issue_surface_key(
                        principal,
                        "sessions",
                        "snapshot",
                        f"{session_id}\x1f{internal}",
                    ),
                    "object_kind": "snapshot",
                    "label": name,
                    "summary": f"第 {to_int(snapshot.get('turn_no'), 0) or 0} 轮",
                    "state": text(snapshot.get("kind"), "manual"),
                    "created_at": text(snapshot.get("created_at")),
                    "revision": revision,
                    "available_actions": actions,
                }
            )
    archives: list[dict[str, Any]] = []
    storage = getattr(database, "storage", None)
    if storage is not None and callable(getattr(storage, "list_archives", None)):
        from .snapshot_intents import archive_item_revision

        try:
            archive_rows = await asyncio.to_thread(
                storage.list_archives, session_id, kind="save"
            )
        except Exception:
            archive_rows = []
        for raw in (archive_rows or ())[:20]:
            archive = mapping(raw)
            filename = text(archive.get("filename"))
            if not filename:
                continue
            revision = archive_item_revision(archive)
            archives.append(
                {
                    "key": issue_surface_key(
                        principal,
                        "sessions",
                        "archive",
                        f"{session_id}\x1f{filename}",
                    ),
                    "object_kind": "archive",
                    "label": "独立存档",
                    "summary": "可恢复的副本存档文件",
                    "state": "可用",
                    "created_at": text(archive.get("created_at")),
                    "revision": revision,
                    "available_actions": (
                        [
                            {
                                "action_id": "C22",
                                "intent": "archive.trash",
                                "label": "移入可恢复回收区",
                                "target_kind": "archive",
                                "expected_revision": revision,
                                "description": "先持久化移动计划，再核验归档身份与哈希；失败可重试对账。",
                                "transportReady": True,
                                "focus_return": "opener",
                                "fields": [
                                    {
                                        "name": "acknowledge_trash",
                                        "type": "checkbox",
                                        "labelKey": "action.field.acknowledge_trash",
                                        "required": True,
                                    }
                                ],
                            }
                        ]
                        if can_manage
                        else []
                    ),
                }
            )
    data["snapshot_session"] = {
        "key": issue_surface_key(
            principal, "sessions", "session", session_id
        ),
        "object_kind": "session",
        "revision": to_int(session.get("revision"), 0) or 0,
        "available_actions": (
            [
                {
                    "action_id": "E30",
                    "intent": "snapshot.create",
                    "label": "创建命名存档",
                    "target_kind": "session",
                    "expected_revision": to_int(session.get("revision"), 0) or 0,
                    "description": "从当前已提交状态建立命名存档；不会覆盖已有同名存档。",
                    "transportReady": True,
                    "focus_return": "opener",
                    "fields": [
                        {
                            "name": "name",
                            "type": "text",
                            "labelKey": "action.field.name",
                            "required": True,
                        }
                    ],
                }
            ]
            if can_manage
            else []
        ),
    }
    data["snapshots"] = snapshots
    data["archives"] = archives
    body["data"] = data
    return {"status": 200, "body": body}


__all__ = [name for name in globals() if not name.startswith('__')]


