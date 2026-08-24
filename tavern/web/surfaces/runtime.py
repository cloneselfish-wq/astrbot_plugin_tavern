from __future__ import annotations

from .registry import *
from .dashboard import *



def _stage_label(value: Any) -> str:
    return {
        "a": "阶段 A · 核心资料",
        "b": "阶段 B · 构筑选择",
        "c": "阶段 C · 完成确认",
        "core_ready": "阶段 A · 核心资料完成",
        "staged_pending": "阶段 B · 等待补充",
        "stage_locked": "阶段 C · 等待确认",
        "complete": "建卡完成",
        "approved": "审核通过",
        "incomplete": "资料待补充",
    }.get(_text(value, limit=40).lower(), "阶段待确认")


def _character_status(value: Any, projected_label: Any) -> str:
    status = _text(value, limit=50).lower()
    known = {
        "uncreated": "未建卡",
        "draft": "填写中",
        "pending": "待审核",
        "pending_review": "待审核",
        "approved": "已批准",
        "rejected": "已退回",
    }
    if status in known:
        return known[status]
    label = _public_text(projected_label, limit=80)
    return label if label and label != "状态解析失败" else "审核状态待确认"


async def _characters_surface(context: SurfaceContext) -> SurfaceProjection:
    from ..routes.characters import character_list_view
    from ...repositories.workflow_support import participant_revision

    session_id = _resolve_session_context(context, required=False)
    if not session_id:
        offset, page_size = context.page(default=20)
        return SurfaceProjection(
            data={
                "items": [],
                "queue": [
                    {"label": "待审核", "value": 0},
                    {"label": "已批准", "value": 0},
                    {"label": "填写中", "value": 0},
                    {"label": "可以开演", "value": 0},
                ],
                "privacy": {
                    "label": "角色隐私边界",
                    "summary": "选择副本后才会读取该副本的角色审核摘要；平台账号和私密方向不会进入列表。",
                    "state": "等待选择副本",
                },
                "filters": {
                    "sessions": await _session_filter_options(context),
                    "statuses": [],
                    "search": True,
                },
                "detail": [],
                "pagination": _pagination(
                    context,
                    offset=offset,
                    page_size=page_size,
                    returned=0,
                    total=0,
                    has_more=False,
                ),
            },
            summary={
                "label": "请先选择副本",
                "summary": "请从副本运行进入一个故事，再查看该副本的角色与审核。",
                "state": "等待选择",
                "count": 0,
            },
            permissions={"can_view": True, "can_manage": False},
            empty=True,
        )
    source = _route_body(
        await character_list_view(
            context.principal,
            context.database,
            session_id,
            query={},
        ),
        operation="读取角色审核队列",
    )
    source_permissions = _mapping(source.get("permissions"))
    privacy = {
        "label": "角色隐私边界",
        "summary": (
            "当前身份可查看本副本的角色审核摘要；私密方向、平台账号和完整角色字段不会进入列表。"
            if bool(source_permissions.get("can_view_all"))
            else "当前只显示本人的角色摘要；其他玩家的角色字段、平台账号和私密方向不会进入页面。"
        ),
        "state": (
            "主持审核视图"
            if bool(source_permissions.get("can_view_all"))
            else "本人视图"
        ),
    }
    session = _mapping(await context.database.get_session(session_id))
    session_options = await _session_filter_options(
        context,
        selected_session_id=session_id,
        selected_session=session,
    )
    readonly = bool(session.get("readonly")) or _text(
        session.get("state"), limit=50
    ) == "finished"
    protocol_archive = _mapping(session.get("protocol_archive"))
    roster = [
        dict(item)
        for item in await context.database.list_roster(session_id) or ()
        if isinstance(item, Mapping)
    ]
    source_items = _sequence(source.get("items"))
    query = _text(context.query.get("q"), limit=200).casefold()
    wanted_state = _text(context.query.get("status"), limit=50).lower()
    status_options: dict[str, str] = {}
    projected: list[dict[str, Any]] = []
    for index, raw in enumerate(source_items):
        item = _mapping(raw)
        roster_item = roster[index] if index < len(roster) else {}
        actor = _mapping(item.get("actor_view"))
        semantic_values = _mapping(actor.get("semantic_values"))
        technical = _mapping(item.get("technical"))
        internal = _text(
            roster_item.get("id") or technical.get("participant_id"),
            limit=300,
        )
        name = _safe_label(
            item.get("name") or actor.get("title") or item.get("display_name"),
            "角色名称缺失",
        )
        card_status = _text(item.get("card_status"), limit=50)
        status_label = _character_status(
            card_status,
            item.get("card_status_label"),
        )
        if card_status:
            status_options[card_status.lower()] = status_label
        if query and query not in name.casefold():
            continue
        if wanted_state and card_status.lower() != wanted_state:
            continue
        stage = _mapping(item.get("stage"))
        revision = _integer(
            roster_item.get("card_version_no")
            or technical.get("card_version_no"),
            0,
        )
        available_actions: list[dict[str, Any]] = []
        detail_groups: list[dict[str, Any]] = []
        for section in _sequence(actor.get("sections"))[:3]:
            section_view = _mapping(section)
            detail_values: list[str] = []
            for raw_value in _sequence(section_view.get("items"))[:4]:
                value = _mapping(raw_value)
                item_label = _public_text(value.get("label"), limit=60)
                item_value = _public_text(
                    value.get("display_value")
                    or value.get("value")
                    or value.get("summary"),
                    limit=80,
                )
                if item_label and item_value:
                    detail_values.append(f"{item_label}：{item_value}")
            section_label = _public_text(
                section_view.get("label") or section_view.get("title"), limit=80
            )
            if section_label and detail_values:
                detail_groups.append(
                    {
                        "label": section_label,
                        "summary": " · ".join(detail_values),
                        "state": "可见摘要",
                    }
                )
        if (
            internal
            and revision > 0
            and not readonly
            and card_status.lower() in {"pending", "pending_review"}
        ):
            available_actions.append(
                _available_action(
                    "C28",
                    "card.review.approve",
                    "通过角色审核",
                    target_kind="character",
                    expected_revision=revision,
                    description="确认当前版本可进入副本；初始物品与就绪计时会原子建立。",
                )
            )
        retirement_revision = participant_revision(
            roster_item,
            _integer(session.get("revision"), 0),
        )
        participation_status = _text(
            roster_item.get("participation_status"), limit=50
        ).lower()
        if (
            internal
            and not readonly
            and context.roles & {"admin", "host"}
            and participation_status
            in {"reserved", "active", "standby", "away"}
        ):
            available_actions.append(
                _available_action(
                    "C19",
                    "participant.retire",
                    "安排角色安全退场",
                    target_kind="character",
                    expected_revision=retirement_revision,
                    description="取消该角色未完成选择、计时与委托，并写入退场幕间；已结算事实会保留。",
                    fields=[
                        {
                            "name": "reason",
                            "type": "textarea",
                            "labelKey": "action.field.reason",
                            "required": True,
                        },
                        {
                            "name": "acknowledge_departure",
                            "type": "checkbox",
                            "labelKey": "action.field.acknowledge_departure",
                            "required": True,
                        },
                    ],
                )
            )
        if (
            index == 0
            and internal
            and not readonly
            and context.roles & {"admin", "host"}
            and _text(session.get("state"), limit=50).lower() == "preparing"
        ):
            available_actions.append(
                _available_action(
                    "C10",
                    "participants.force_ready",
                    "将合格角色全部设为已准备",
                    target_kind="character",
                    expected_revision=_integer(session.get("revision"), 0),
                    description="只处理已审核且仍在本副本的角色；资料不完整或已经退场的角色会跳过。",
                )
            )
        projected.append(
            {
                "key": context.key(
                    "character",
                    (
                        f"{session_id}\x1f{internal}"
                        if internal
                        else f"{session_id}:{index}:{name}"
                    ),
                ),
                "object_kind": "character",
                "label": name,
                "summary": " · ".join(
                    value
                    for value in (
                        _public_text(item.get("display_name"), limit=80),
                        _public_text(actor.get("subtitle"), limit=120),
                        _safe_label(item.get("participation_label"), "参与状态待确认"),
                    )
                    if value
                ),
                "player_label": _public_text(item.get("display_name"), limit=80),
                "concept_label": _public_text(
                    semantic_values.get("actor.identity.profession")
                    or semantic_values.get("actor.identity.species")
                    or actor.get("subtitle"),
                    limit=120,
                ),
                "strengths_summary": _public_text(
                    semantic_values.get("actor.capability.list"),
                    limit=180,
                ),
                "limits_summary": _public_text(
                    semantic_values.get("actor.weakness.list"),
                    limit=180,
                ),
                "submitted_at": _text(roster_item.get("updated_at"), limit=80),
                "state": status_label,
                "stage": _stage_label(stage.get("stage")),
                "ready": bool(item.get("ready")),
                "pending_fields": _integer(stage.get("pending_count"), 0),
                "detail": detail_groups,
                "revision": revision,
                "readonly": readonly,
                "readonly_reason": (
                    _public_text(
                        protocol_archive.get("reason"),
                        limit=240,
                        default="副本已经归档，角色审核保持只读。",
                    )
                    if readonly
                    else ""
                ),
                "readonly_recovery": (
                    _public_text(
                        protocol_archive.get("next_step"),
                        limit=240,
                        default="如需继续游玩，请在当前 RC10 世界中新建副本。",
                    )
                    if readonly
                    else ""
                ),
                "available_actions": available_actions,
            }
        )
    offset, page_size = context.page(default=12)
    total = len(projected)
    items = projected[offset : offset + page_size]
    pending = sum(1 for item in projected if item["state"] == "待审核")
    queue = [
        {"label": state, "value": count}
        for state in ("待审核", "已退回", "填写中", "已批准")
        if (count := sum(1 for item in projected if item["state"] == state))
    ][:4]
    return SurfaceProjection(
        data={
            "items": items,
            "queue": queue,
            "privacy": privacy,
            "filters": {
                "sessions": session_options,
                "statuses": [
                    {"value": value, "label": label}
                    for value, label in status_options.items()
                ],
                "search": True,
            },
            "detail": items[0].get("detail", []) if items else [],
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=total,
                has_more=offset + len(items) < total,
            ),
        },
        summary={
            "label": items[0]["label"] if items else "当前没有待处理角色",
            "summary": (
                f"有 {pending} 个角色等待审核。"
                if pending
                else "当前筛选下没有等待审核的角色。"
            ),
            "state": items[0]["state"] if items else "空",
            "count": total,
        },
        updated_at="",
        permissions={"can_view": True, "can_manage": not readonly},
        empty=not items,
        readonly=readonly,
    )


def _memory_scope(value: Any) -> str:
    return {
        "party": "小队共享",
        "session": "当前副本",
        "character": "角色范围",
        "private": "主持范围",
        "world": "世界事实",
    }.get(_text(value, limit=40).lower(), "可见范围待确认")


def _memory_importance(value: Any) -> str:
    number = number_or_none(value)
    if number is None:
        return "未记录"
    if number >= 3:
        return "高"
    if number >= 1:
        return "中"
    return "低"


def _memory_type(value: Any) -> str:
    return {
        "fact": "事实",
        "event": "事件",
        "clue": "线索",
        "decision": "决定",
        "relationship": "关系",
        "quest": "任务",
        "preference": "倾向",
        "summary": "摘要",
    }.get(_text(value, limit=50).lower(), "类型待确认")


def _memory_governance(raw: Mapping[str, Any]) -> list[str]:
    item = _mapping(raw)
    values: list[str] = []
    conflict = _text(item.get("conflict_status"), limit=50).lower()
    if bool(item.get("invalidated")):
        values.append("已失效")
    else:
        values.append("当前有效")
    if conflict not in {"", "clear", "resolved"}:
        values.append("存在冲突")
    if bool(item.get("pinned")):
        values.append("已置顶")
    if bool(item.get("locked")):
        values.append("已锁定")
    return values


async def _collect_visible_memories(
    context: SurfaceContext,
    *,
    session_id: str,
    viewer_role: str,
    query: str,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], bool]:
    loader = getattr(context.database, "list_visible_memories_page", None)
    if callable(loader):
        rows: list[dict[str, Any]] = []
        offset = 0
        truncated = False
        while len(rows) < limit:
            page_size = min(100, limit - len(rows))
            page = _mapping(
                await loader(
                    session_id,
                    viewer_role=viewer_role,
                    viewer_id=_text(context.principal.get("username"), limit=300),
                    viewer_participant_ref=_text(
                        context.principal.get("participant_ref"), limit=300
                    ),
                    query=query,
                    offset=offset,
                    page_size=page_size,
                    include_invalidated=True,
                )
            )
            items = [
                dict(item)
                for item in _sequence(page.get("items"))
                if isinstance(item, Mapping)
            ]
            rows.extend(items)
            offset += len(items)
            if not bool(page.get("has_more")) or not items:
                break
            if len(rows) >= limit:
                truncated = True
                break
        return rows, truncated

    raw_rows = await context.database.list_memories(
        session_id,
        query,
        limit,
        include_invalidated=True,
    )
    rows = [
        dict(item) for item in raw_rows or () if isinstance(item, Mapping)
    ]
    if "admin" not in context.roles:
        rows = [
            item
            for item in rows
            if _text(item.get("visibility"), limit=40, default="public").lower()
            in {"public", "party", "session", "world", "host", "dm", "moderator"}
        ]
    return rows[:limit], len(rows) > limit


async def _memories_surface(context: SurfaceContext) -> SurfaceProjection:
    from ...repositories.story_support import memory_revision
    from ..routes.sessions import require_member

    session_id = _resolve_session_context(context, required=False)
    if not session_id:
        offset, page_size = context.page(default=20)
        return SurfaceProjection(
            data={
                "items": [],
                "scope_map": [],
                "filters": {
                    "search": True,
                    "scopes": [],
                    "importances": [],
                    "tags": [],
                    "governances": [],
                },
                "pagination": _pagination(
                    context,
                    offset=offset,
                    page_size=page_size,
                    returned=0,
                    total=0,
                    has_more=False,
                ),
            },
            summary={
                "label": "事实与治理",
                "summary": "请从副本运行进入一个故事，再查看该副本中当前身份可见的长期事实。",
                "state": "等待选择",
                "count": 0,
            },
            permissions={"can_view": True, "can_manage": False},
            empty=True,
        )
    viewer_role = await require_member(
        context.database,
        session_id,
        context.principal,
    )
    offset, page_size = context.page(default=20)
    query = _text(context.query.get("q"), limit=200)
    visible_rows, facet_truncated = await _collect_visible_memories(
        context,
        session_id=session_id,
        viewer_role=viewer_role,
        query=query,
    )
    scope_values = sorted(
        {
            _text(item.get("scope"), limit=40)
            for item in visible_rows
            if _text(item.get("scope"), limit=40)
        },
        key=_memory_scope,
    )
    importance_values = sorted(
        {_memory_importance(item.get("importance")) for item in visible_rows}
    )
    tag_values = sorted(
        {
            tag
            for item in visible_rows
            for tag in (
                _public_text(value, limit=40)
                for value in _sequence(item.get("tags"))
            )
            if tag
        }
    )
    governance_values = sorted(
        {value for item in visible_rows for value in _memory_governance(item)}
    )
    selected_scope = _resolve_filter_value(
        context,
        "memory-scope-filter",
        context.query.get("scope"),
        label="记忆范围",
    )
    selected_importance = _resolve_filter_value(
        context,
        "memory-importance-filter",
        context.query.get("importance"),
        label="重要度",
    )
    selected_tag = _resolve_filter_value(
        context,
        "memory-tag-filter",
        context.query.get("tag"),
        label="标签",
    )
    selected_governance = _resolve_filter_value(
        context,
        "memory-governance-filter",
        context.query.get("governance"),
        label="治理状态",
    )
    filtered_rows = [
        item
        for item in visible_rows
        if (
            not selected_scope
            or _text(item.get("scope"), limit=40) == selected_scope
        )
        and (
            not selected_importance
            or _memory_importance(item.get("importance")) == selected_importance
        )
        and (
            not selected_tag
            or selected_tag
            in {
                _public_text(value, limit=40)
                for value in _sequence(item.get("tags"))
            }
        )
        and (
            not selected_governance
            or selected_governance in _memory_governance(item)
        )
    ]
    visible_total: int | None = None if facet_truncated else len(filtered_rows)
    selected = filtered_rows[offset : offset + page_size]
    has_more = offset + len(selected) < len(filtered_rows)
    session = _mapping(await context.database.get_session(session_id))
    readonly = bool(session.get("readonly")) or _text(
        session.get("state"), limit=50
    ) == "finished"
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(selected, start=offset):
        internal = _text(raw.get("id"), limit=300)
        invalidated = bool(raw.get("invalidated"))
        conflict = _text(raw.get("conflict_status"), limit=50).lower()
        state = (
            "已失效"
            if invalidated
            else "存在冲突"
            if conflict not in {"", "clear", "resolved"}
            else "当前有效"
        )
        tags = [
            _public_text(value, limit=40)
            for value in _sequence(raw.get("tags"))
            if _public_text(value, limit=40)
        ][:4]
        revision = memory_revision(raw)
        operations = [
            {
                "value": "unpin" if bool(raw.get("pinned")) else "pin",
                "label": "取消置顶" if bool(raw.get("pinned")) else "置顶事实",
            },
            {
                "value": "restore" if invalidated else "invalidate",
                "label": "恢复事实" if invalidated else "标记失效",
            },
        ]
        if conflict not in {"", "clear", "resolved"}:
            operations.append({"value": "resolve", "label": "确认冲突已处理"})
        can_govern = (
            viewer_role in {"admin", "dm"}
            and not readonly
            and (not bool(raw.get("locked")) or viewer_role == "admin")
        )
        items.append(
            {
                "key": context.key("memory", internal or f"{session_id}:{index}"),
                "object_kind": "memory",
                "label": _public_text(
                    raw.get("content"),
                    limit=160,
                    default="事实内容读取失败",
                ),
                "summary": " · ".join(
                    [_memory_scope(raw.get("scope")), *tags]
                ),
                "scope": _memory_scope(raw.get("scope")),
                "type": _memory_type(raw.get("kind")),
                "tags": tags,
                "tag_summary": "、".join(tags) if tags else "无标签",
                "state": state,
                "importance": number_or_none(raw.get("importance")),
                "importance_label": _memory_importance(raw.get("importance")),
                "salience": number_or_none(raw.get("salience")),
                "locked": bool(raw.get("locked")),
                "pinned": bool(raw.get("pinned")),
                "revision": revision,
                "available_actions": (
                    [
                        _available_action(
                            "memory.govern",
                            "memory.govern",
                            "治理这条事实",
                            target_kind="memory",
                            expected_revision=revision,
                            description="置顶、失效、恢复或处理冲突；不会硬删除事实与来源链。",
                            fields=[
                                {
                                    "name": "operation",
                                    "type": "select",
                                    "labelKey": "action.field.memory_operation",
                                    "required": True,
                                    "options": operations,
                                },
                                {
                                    "name": "reason",
                                    "type": "textarea",
                                    "labelKey": "action.field.reason",
                                    "required": False,
                                },
                            ],
                        )
                    ]
                    if can_govern
                    else []
                ),
                "updated_at": _text(raw.get("updated_at"), limit=80),
            }
        )
    scope_counts: dict[str, int] = {}
    for item in filtered_rows:
        scope_label = _memory_scope(item.get("scope"))
        scope_counts[scope_label] = scope_counts.get(scope_label, 0) + 1
    problems: list[VisualProblem] = []
    if facet_truncated:
        problems.append(
            VisualProblem(
                code="tavern.surface.memory_filter_scan_truncated",
                message="可见事实较多，当前筛选只检查了前 500 项。",
                recovery="请增加搜索词、范围、标签或治理条件后重试。",
                retryable=False,
            )
        )
    return SurfaceProjection(
        data={
            "items": items,
            "scope_map": [
                {
                    "label": label,
                    "value": value,
                    "summary": "当前页且仅限本人可见范围。",
                }
                for label, value in scope_counts.items()
            ],
            "filters": {
                "search": True,
                "scopes": [
                    {
                        "value": _opaque_filter_value(
                            context, "memory-scope-filter", value
                        ),
                        "label": _memory_scope(value),
                    }
                    for value in scope_values
                ],
                "importances": [
                    {
                        "value": _opaque_filter_value(
                            context, "memory-importance-filter", value
                        ),
                        "label": value,
                    }
                    for value in importance_values
                ],
                "tags": [
                    {
                        "value": _opaque_filter_value(
                            context, "memory-tag-filter", value
                        ),
                        "label": value,
                    }
                    for value in tag_values
                ],
                "governances": [
                    {
                        "value": _opaque_filter_value(
                            context, "memory-governance-filter", value
                        ),
                        "label": value,
                    }
                    for value in governance_values
                ],
            },
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=visible_total,
                has_more=has_more,
            ),
        },
        summary={
            "label": "事实与治理",
            "summary": (
                "选择一条事实后再按需加载来源和取代关系。"
                if items
                else "当前筛选没有可见事实。"
            ),
            "state": items[0]["state"] if items else "空",
            "count": visible_total if visible_total is not None else len(items),
        },
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={"can_view": True, "can_manage": not readonly},
        problems=problems,
        state="partial" if problems else None,
        empty=not items,
        readonly=readonly,
    )


_WORLD_CAPABILITY_LABELS = {
    "actor": "角色创建",
    "card_wizard": "分步建卡",
    "scene_graph": "场景路径",
    "quests": "任务追踪",
    "clocks": "场景时钟",
    "relations": "人物关系",
    "ai_companions": "AI 队友",
    "memory": "长期记忆",
    "memories": "长期记忆",
    "timers": "场景计时",
    "modules": "可选模块",
}



__all__ = [name for name in globals() if not name.startswith('__')]
