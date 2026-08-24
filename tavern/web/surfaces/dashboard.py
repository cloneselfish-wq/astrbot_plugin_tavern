from __future__ import annotations

from .registry import *
from .health_support import health_state, health_summary
from .capabilities import capability_panel_projection

async def _dashboard_surface(context: SurfaceContext) -> SurfaceProjection:
    rows, visible_total, _has_more, state_counts = await _visible_session_page(
        context,
        offset=0,
        page_size=4,
    )
    list_sessions = getattr(context.database, "list_sessions", None)
    raw_session_rows = await list_sessions() if callable(list_sessions) else ()
    raw_sessions = {
        _text(item.get("id"), limit=300): dict(item)
        for item in raw_session_rows or ()
        if isinstance(item, Mapping) and _text(item.get("id"), limit=300)
    }
    raw_timestamps = {
        _text(item.get("id"), limit=300): _text(item.get("updated_at"), limit=80)
        for item in raw_session_rows or ()
        if isinstance(item, Mapping) and _text(item.get("id"), limit=300)
    }
    rich_rows = [raw_sessions.get(_text(_mapping(raw).get("id"), limit=300), dict(raw)) for raw in rows]
    try:
        enriched_rows = await enrich_session_display_labels(
            context.database,
            rich_rows,
        )
    except Exception:
        enriched_rows = rich_rows
    session_items: list[dict[str, Any]] = []
    for raw, rich in zip(rows, enriched_rows):
        combined = {**dict(raw), **{
            key: rich.get(key)
            for key in (
                "world_state",
                "player_count",
                "ready_count",
                "progress",
                "waiting_for",
                "revision",
                "updated_at",
            )
            if rich.get(key) is not None
        }}
        projected = _project_session(context, combined)
        projected["updated_at"] = raw_timestamps.get(
            _text(_mapping(raw).get("id"), limit=300),
            projected.get("updated_at", ""),
        )
        session_items.append(projected)
    metrics: list[dict[str, Any]] = []
    density: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    urgent: list[dict[str, Any]] = []
    problems: list[VisualProblem | Mapping[str, Any]] = []
    items: list[dict[str, Any]] = session_items
    updated_at = latest_timestamp(
        *(item.get("updated_at") for item in session_items)
    )

    if "admin" in context.roles:
        overview = _mapping(await context.database.overview())
        counts = _mapping(overview.get("counts"))
        storage_errors = _integer(overview.get("storage_errors"), 0)
        pending_actions = (
            _integer(counts.get("pending_deliveries"), 0)
            + _integer(counts.get("open_votes"), 0)
        )
        player_count = counts.get("players")
        metric_specs = (
            ("active_sessions", "正在运行", counts.get("running"), "当前已开演副本", "amber"),
            (
                "online_players",
                "在线玩家",
                player_count if player_count is not None else "待确认",
                "当前参与中的玩家" if player_count is not None else "当前服务未提供在线人数",
                "blue",
            ),
            ("pending_actions", "等待处理", pending_actions, "投递与表决", "danger" if pending_actions else "amber"),
            (
                "service_health",
                "数据写入",
                "需检查" if storage_errors else "正常",
                f"{storage_errors} 项存储异常" if storage_errors else "持久化检查通过",
                "danger" if storage_errors else "jade",
            ),
        )
        for key, label, metric_value, detail, tone in metric_specs:
            metrics.append(
                {
                    "key": key,
                    "label": label,
                    "value": metric_value,
                    "detail": detail,
                    "tone": tone,
                }
            )
        density_specs = (
            ("worlds", "世界包", "worlds", "个可用世界", "worlds"),
            ("sessions_total", "全部副本", "sessions", "个故事副本", "sessions"),
            ("preparing", "准备中", "preparing", "个副本待开演", "sessions"),
            ("memories", "长期记忆", "memories", "条安全记忆", "memories"),
            ("open_votes", "进行中投票", "open_votes", "项表决待完成", "todo"),
            ("active_timers", "活跃倒计时", "active_timers", "个计时正在运行", "session_detail"),
            ("recovery_points", "恢复点", "snapshots", "个保护点", "audit"),
        )
        for key, label, count_key, detail, navigate_to in density_specs:
            if count_key not in counts:
                continue
            density.append(
                {
                    "key": key,
                    "label": label,
                    "value": _integer(counts.get(count_key), 0),
                    "detail": detail,
                    "navigate_to": navigate_to,
                }
            )
        token_loader = getattr(context.database, "global_token_usage", None)
        if callable(token_loader):
            try:
                token_usage = max(0, int(await token_loader(86400)))
            except Exception:
                token_usage = None
            if token_usage is not None:
                density.append(
                    {
                        "key": "token_usage",
                        "label": "Token 用量",
                        "value": f"{token_usage:,}",
                        "detail": "最近 24 小时",
                    }
                )
        if storage_errors:
            urgent.append(
                {
                    "key": context.key("todo", "dashboard:storage"),
                    "label": "存储检查需要处理",
                    "summary": "部分玩家操作可能无法可靠保存。",
                    "state": "阻止开演",
                }
            )
        pending = _integer(counts.get("pending_deliveries"), 0)
        if pending:
            urgent.append(
                {
                    "key": context.key("todo", "dashboard:delivery"),
                    "label": "消息仍在等待投递",
                    "summary": f"有 {pending} 项投递等待系统继续处理。",
                    "state": "投递恢复",
                }
            )
        try:
            health = _mapping(await context.database.health_summary())
            for raw in _sequence(health.get("components")):
                component = _mapping(raw)
                label = _safe_label(component.get("label"), "服务名称缺失")
                state = health_state(component.get("state"))
                services.append(
                    {
                        "label": label,
                        "summary": health_summary(label, state),
                        "state": state,
                        "updated_at": _text(component.get("checked_at"), limit=80),
                    }
                )
            priority = {
                "不可用": 0,
                "维护中": 1,
                "正在恢复": 2,
                "尚未确认": 3,
                "正常": 4,
            }
            services.sort(
                key=lambda item: (priority.get(item["state"], 3), item["label"])
            )
            updated_at = latest_timestamp(
                updated_at,
                _text(health.get("generated_at"), limit=80),
            )
        except Exception as exc:
            _status, problem = problem_from_exception(exc)
            problems.append(problem)
    elif "author" in context.roles:
        worlds = await context.database.list_worlds(False)
        jobs = await context.database.list_author_jobs(limit=3)
        failed = [
            item
            for item in jobs or ()
            if _text(_mapping(item).get("status"), limit=40)
            in {"failed", "retry_wait", "permanently_failed"}
        ]
        if failed:
            urgent.append(
                {
                    "key": context.key("todo", "dashboard:author-jobs"),
                    "label": "作者任务需要处理",
                    "summary": f"有 {len(failed)} 项任务没有完成。",
                    "state": "需要检查",
                }
            )
        metrics.extend(
            [
                {"key": "available_worlds", "label": "可用世界", "value": len(worlds or ()), "tone": "amber"},
                {"key": "unfinished_jobs", "label": "未完成任务", "value": len(failed), "tone": "danger" if failed else "jade"},
            ]
        )
    else:
        metrics.extend(
            [
                {
                    "key": "active_sessions",
                    "label": "正在运行",
                    "value": _integer(state_counts.get("running"), 0),
                    "detail": "当前角色可见",
                    "tone": "amber",
                },
                {
                    "key": "visible_sessions",
                    "label": "可见副本",
                    "value": visible_total,
                    "detail": "按当前权限统计",
                    "tone": "blue",
                },
            ]
        )

    if urgent:
        stage_label = urgent[0]["label"]
        stage_summary = urgent[0]["summary"]
        stage_state = urgent[0]["state"]
    elif session_items:
        stage_label = session_items[0]["label"]
        stage_summary = "继续处理当前最相关的副本。"
        stage_state = session_items[0]["state"]
    elif "author" in context.roles:
        stage_label = "选择一个世界开始创作"
        stage_summary = "当前没有阻塞中的作者任务。"
        stage_state = "可开始"
    else:
        stage_label = "当前没有可见副本"
        stage_summary = "加入副本后，这里会显示与你相关的下一步。"
        stage_state = "空"

    readonly = context.roles == frozenset({"readonly"})
    current_story: dict[str, Any] | None = None
    story_loader = getattr(context.database, "latest_public_story_event", None)
    if callable(story_loader):
        for raw, item in zip(rows, session_items):
            if _text(_mapping(raw).get("state"), limit=40) != "running":
                continue
            internal_session = _text(_mapping(raw).get("id"), limit=300)
            if not internal_session:
                continue
            try:
                story_event = _mapping(await story_loader(internal_session))
            except Exception as exc:
                _status, problem = problem_from_exception(exc)
                problems.append(problem)
                break
            story_text = _public_text(story_event.get("content"), limit=360)
            if story_text:
                current_story = {
                    "key": context.key("story", f"{internal_session}:current"),
                    "session_key": item["key"],
                    "label": item["label"],
                    "summary": story_text,
                    "round_label": (
                        f"第 {item['round']} 轮"
                        if item.get("round") is not None
                        else ""
                    ),
                    "scene_label": item.get("scene_label") or "",
                    "actor_label": (
                        f"下一位：{item['actor_label']}"
                        if item.get("actor_label")
                        else ""
                    ),
                }
            break
    recent_changes = [
        {
            "label": item["label"],
            "summary": f"副本状态快照已更新：{item['state']}。",
            "state": item["state"],
            "created_at": item.get("updated_at"),
        }
        for item in session_items
        if item.get("updated_at")
    ][:4]
    capability_panels = await capability_panel_projection(
        context,
        visible_sessions=visible_total,
        running_sessions=_integer(state_counts.get("running"), 0),
        attention_services=sum(
            1 for item in services if item.get("state") != "正常"
        ),
        visible_services=len(services),
    )
    return SurfaceProjection(
        data={
            "stage": {
                "label": stage_label,
                "summary": stage_summary,
                "state": stage_state,
            },
            "items": items,
            "urgent": urgent[:3],
            "metrics": metrics[:4],
            "density": density[:8],
            "services": services[:3],
            "recent_changes": recent_changes,
            "capability_panels": capability_panels,
            **({"current_story": current_story} if current_story else {}),
        },
        summary={
            "label": stage_label,
            "summary": stage_summary,
            "state": stage_state,
            "count": len(items),
        },
        revision=None,
        updated_at=updated_at,
        permissions={
            "can_view": True,
            "can_manage": bool(context.roles & {"admin", "host", "author"})
            and not readonly,
        },
        empty=not items and not urgent,
        readonly=readonly,
        problems=problems,
        state="partial" if problems else None,
    )

async def _session_filter_options(
    context: SurfaceContext,
    *,
    selected_session_id: str = "",
    selected_session: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Project only principal-visible sessions into opaque selector choices."""

    rows: list[dict[str, Any]] = []
    if callable(getattr(context.database, "list_visible_sessions_page", None)):
        rows, _total, _has_more, _counts = await _visible_session_page(
            context,
            offset=0,
            page_size=100,
        )
    selected_id = _text(selected_session_id, limit=300)
    if selected_id and not any(
        _text(item.get("id"), limit=300) == selected_id for item in rows
    ):
        current = _mapping(selected_session)
        if not current:
            loader = getattr(context.database, "get_session", None)
            current = _mapping(await loader(selected_id)) if callable(loader) else {}
        if current:
            rows.append({"id": selected_id, **dict(current)})
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in rows:
        internal = _text(raw.get("id"), limit=300)
        if not internal or internal in seen:
            continue
        seen.add(internal)
        projected = _project_session(context, raw)
        choices.append(
            {
                "value": projected["key"],
                "label": projected["label"],
            }
        )
    return choices


async def _tendencies_surface(context: SurfaceContext) -> SurfaceProjection:
    from ..routes.tendencies import tendency_view

    session_id = _resolve_session_context(context, required=False)
    if not session_id:
        return SurfaceProjection(
            data={
                "items": [],
                "trends": [],
                "session": {},
                "effects": [],
                "privacy": "选择副本后，只会读取本人在该副本中可见且已经提交的选择。",
                "ignored_count": 0,
                "filters": {
                    "sessions": await _session_filter_options(context),
                },
            },
            summary={
                "label": "请先选择副本",
                "summary": "请从总览进入一个故事副本，再查看本人在该副本中的倾向与形成依据。",
                "state": "等待选择",
                "count": 0,
            },
            revision=0,
            permissions={"can_view": True, "can_manage": False},
            empty=True,
        )
    source = _route_body(
        await tendency_view(
            context.principal,
            context.database,
            session_id=session_id,
        ),
        operation="读取本人倾向",
    )
    view = _mapping(source.get("view"))
    session_raw = _mapping(await context.database.get_session(session_id))
    session_context = {
        "label": _safe_label(
            session_raw.get("instance_name") or session_raw.get("name"),
            "副本名称缺失",
        ),
        "summary": _safe_label(
            session_raw.get("world_name") or session_raw.get("world"),
            "世界资料缺失",
        ),
        "state": _session_state(session_raw.get("state")),
        "updated_at": _text(session_raw.get("updated_at"), limit=80),
    }
    session_options = await _session_filter_options(
        context,
        selected_session_id=session_id,
        selected_session=session_raw,
    )
    observations: list[dict[str, Any]] = []
    for raw in _sequence(view.get("observations"))[:5]:
        item = _mapping(raw)
        label = _safe_label(item.get("label"), "观察名称缺失")
        observations.append(
            {
                "label": label,
                "summary": "由本人已提交选择形成的当前观察。",
                "state": _safe_label(item.get("confidence_label"), "证据不足"),
            }
        )
    revision = _integer(view.get("revision"), 0)
    evidence: list[dict[str, Any]] = []
    for state, operation, rows in (
        ("可用依据", "ignore", view.get("active_evidence")),
        ("已忽略", "restore", view.get("revoked_evidence")),
    ):
        for index, raw in enumerate(_sequence(rows)):
            item = _mapping(raw)
            number = _integer(item.get("number"), index + 1)
            summary = _public_text(
                item.get("summary"),
                limit=140,
                default="依据内容暂时不可用。",
            )
            can_act = operation == "ignore" or bool(item.get("restorable"))
            evidence.append(
                {
                    "key": context.key(
                        "evidence",
                        f"{session_id}\x1f{operation}\x1f{number}",
                    ),
                    "object_kind": "evidence",
                    "label": summary,
                    "summary": _public_text(item.get("rationale"), limit=180),
                    "state": state,
                    "updated_at": _text(item.get("created_at"), limit=80),
                    "restorable": bool(item.get("restorable")),
                    "revision": revision,
                    "available_actions": (
                        [
                            _available_action(
                                "tendency.evidence.visibility",
                                f"tendency.evidence.{operation}",
                                "恢复这条依据" if operation == "restore" else "忽略这条依据",
                                target_kind="evidence",
                                expected_revision=revision,
                                description=(
                                    "恢复后，这条本人可见依据会重新参与倾向解释。"
                                    if operation == "restore"
                                    else "忽略后，这条依据不再参与倾向解释，仍可恢复。"
                                ),
                            )
                        ]
                        if can_act
                        else []
                    ),
                }
            )
    privacy = _public_text(
        view.get("privacy_notice"),
        limit=240,
        default="只使用本人在当前副本中可见且已经提交的选择。",
    )
    declared_effects = view.get("effects") or view.get("effect_summary")
    if isinstance(declared_effects, Mapping):
        declared_effects = _mapping(declared_effects).get("items")
    effects: list[dict[str, Any]] = []
    for raw in _sequence(declared_effects)[:5]:
        item = _mapping(raw)
        label = _public_text(item.get("label") or item.get("name"), limit=100)
        summary_text = _public_text(
            item.get("summary") or item.get("description"), limit=180
        )
        if not label or not summary_text:
            continue
        effects.append(
            {
                "label": label,
                "summary": summary_text,
                "state": _public_text(
                    item.get("state") or item.get("reason"), limit=80
                ),
            }
        )
    empty = not observations and not evidence
    return SurfaceProjection(
        data={
            "items": evidence[:20],
            "trends": observations,
            "session": session_context,
            "effects": effects,
            "privacy": privacy,
            "ignored_count": len(_sequence(view.get("revoked_evidence"))),
            "filters": {"sessions": session_options},
        },
        summary={
            "label": observations[0]["label"] if observations else "当前观察不足",
            "summary": (
                "查看形成这项观察的本人可见依据。"
                if observations
                else "还需要更多已提交选择，系统不会据此替你决定角色。"
            ),
            "state": "当前观察" if observations else "证据不足",
            "count": len(observations),
        },
        revision=revision,
        updated_at=max(
            (_text(item.get("updated_at"), limit=80) for item in evidence),
            default="",
        ),
        permissions={"can_view": True, "can_manage": True},
        empty=empty,
    )


async def _sessions_surface(context: SurfaceContext) -> SurfaceProjection:
    offset, page_size = context.page(default=16)
    query = _text(context.query.get("q"), limit=200)
    wanted_state = _text(context.query.get("status"), limit=50).lower()
    visible_rows, facet_truncated, state_counts = await _collect_visible_session_rows(
        context,
        query=query,
    )
    world_options: list[dict[str, str]] = []
    group_options: list[dict[str, str]] = []
    seen_worlds: set[str] = set()
    seen_groups: set[str] = set()
    for raw in visible_rows:
        world_ref = _session_world_ref(raw)
        if world_ref and world_ref not in seen_worlds:
            seen_worlds.add(world_ref)
            world_options.append(
                {
                    "value": _opaque_filter_value(
                        context, "session-world-filter", world_ref
                    ),
                    "label": _session_world_label(raw),
                }
            )
        group_ref = _session_group_ref(raw)
        if group_ref and group_ref not in seen_groups:
            seen_groups.add(group_ref)
            group_options.append(
                {
                    "value": _opaque_filter_value(
                        context, "session-group-filter", group_ref
                    ),
                    "label": _session_group_label(raw, len(group_options) + 1),
                }
            )
    selected_world = _resolve_filter_value(
        context,
        "session-world-filter",
        context.query.get("world"),
        label="世界",
    )
    selected_group = _resolve_filter_value(
        context,
        "session-group-filter",
        context.query.get("group"),
        label="群组",
    )
    filtered_rows = [
        item
        for item in visible_rows
        if (
            not wanted_state
            or _text(item.get("state"), limit=50).lower() == wanted_state
        )
        and (not selected_world or _session_world_ref(item) == selected_world)
        and (not selected_group or _session_group_ref(item) == selected_group)
    ]
    total = len(filtered_rows)
    rows = filtered_rows[offset : offset + page_size]
    has_more = offset + len(rows) < total
    problems: list[VisualProblem] = []
    if facet_truncated:
        problems.append(
            VisualProblem(
                code="tavern.surface.session_filter_scan_truncated",
                message="可见副本较多，当前筛选只检查了前 500 项。",
                recovery="请增加搜索词、世界或群组条件后重试。",
                retryable=False,
            )
        )
    world_rows = (
        [
            dict(item)
            for item in (await context.database.list_worlds(False)) or ()
            if isinstance(item, Mapping)
        ]
        if "admin" in context.roles
        else []
    )
    quota_loader = getattr(context.database, "token_quota_contexts", None)
    quota_contexts = (
        _mapping(
            await quota_loader(
                [
                    _text(item.get("id"), limit=300)
                    for item in rows
                    if isinstance(item, Mapping)
                    and _text(item.get("id"), limit=300)
                ]
            )
        )
        if callable(quota_loader) and "admin" in context.roles
        else {}
    )
    group_summaries: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(filtered_rows):
        raw_map = _mapping(raw)
        group_ref = _session_group_ref(raw_map) or (
            "visible-group:"
            + _session_group_label(raw_map, index + 1)
        )
        group = group_summaries.setdefault(
            group_ref,
            {
                "label": _session_group_label(raw_map, index + 1),
                "platform_label": {
                    "qq": "QQ 群聊",
                    "onebot": "QQ 群聊",
                    "aiocqhttp": "QQ 群聊",
                    "kook": "KOOK 频道",
                    "discord": "Discord 频道",
                    "telegram": "Telegram 群聊",
                }.get(
                    _text(raw_map.get("platform_id"), limit=80).lower(),
                    "群聊平台",
                ),
                "visible_count": 0,
                "running_count": 0,
            },
        )
        group["visible_count"] += 1
        if _text(raw_map.get("state"), limit=40).lower() == "running":
            group["running_count"] += 1

    items: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}
    for raw in rows:
        item = _project_session(context, raw)
        raw_map = _mapping(raw)
        item["group_label"] = _session_group_label(raw_map, len(items) + 1)
        group_ref = _session_group_ref(raw_map) or (
            "visible-group:" + item["group_label"]
        )
        group_summary = group_summaries.get(group_ref, {})
        internal_session = _text(raw_map.get("id"), limit=300)
        quota = _mapping(quota_contexts.get(internal_session))
        quota_summary = ""
        if quota:
            if bool(quota.get("enabled")):
                window_seconds = max(60, _integer(quota.get("window_seconds"), 86400))
                window_label = (
                    f"{window_seconds // 86400} 天"
                    if window_seconds % 86400 == 0
                    else f"{window_seconds // 3600} 小时"
                    if window_seconds % 3600 == 0
                    else f"{window_seconds // 60} 分钟"
                )
                quota_summary = (
                    f"{_integer(quota.get('token_limit'), 400000):,} Token / "
                    f"{window_label}"
                )
            else:
                quota_summary = "群 Token 限额未启用"
        item.update(
            {
                "group_platform_label": group_summary.get(
                    "platform_label", "群聊平台"
                ),
                "group_visible_count": _integer(
                    group_summary.get("visible_count"), 1
                ),
                "group_running_count": _integer(
                    group_summary.get("running_count"), 0
                ),
                "group_quota_summary": quota_summary,
            }
        )
        group_view = groups.setdefault(
            group_ref,
            {
                "key": context.key("session-group", group_ref),
                "label": item["group_label"],
                "platform_label": item["group_platform_label"],
                "running_count": item["group_running_count"],
                "visible_count": item["group_visible_count"],
                "quota_summary": item["group_quota_summary"],
                "items": [],
            },
        )
        if (
            "admin" in context.roles
            and not bool(item.get("readonly"))
            and quota
        ):
            item.setdefault("available_actions", []).append(
                _available_action(
                    "E16",
                    "session.token_quota.set",
                    "调整当前群 Token 限额",
                    target_kind="session",
                    expected_revision=_integer(quota.get("revision"), 0),
                    description="调整当前副本所属群的滑动窗口限额；不会删除历史用量或剧情内容。",
                    fields=[
                        {
                            "name": "window_seconds",
                            "type": "number",
                            "labelKey": "action.field.window",
                            "required": True,
                            "value": _integer(quota.get("window_seconds"), 86400),
                        },
                        {
                            "name": "token_limit",
                            "type": "number",
                            "labelKey": "action.field.limit",
                            "required": True,
                            "value": _integer(quota.get("token_limit"), 400000),
                        },
                        {
                            "name": "enabled",
                            "type": "checkbox",
                            "labelKey": "action.field.enabled",
                            "value": bool(quota.get("enabled")),
                        },
                    ],
                )
            )
        if (
            "admin" in context.roles
            and _text(raw_map.get("state"), limit=40).lower()
            not in {"running", "finished"}
        ):
            current_world = _text(raw_map.get("world_id"), limit=300)
            options = [
                {
                    "value": context.key(
                        "world",
                        _text(world.get("id") or world.get("slug"), limit=300),
                    ),
                    "label": _safe_label(world.get("name"), "世界名称缺失"),
                }
                for world in world_rows
                if _text(world.get("id") or world.get("slug"), limit=300)
                and _text(world.get("id"), limit=300) != current_world
            ]
            if options:
                item.setdefault("available_actions", []).append(
                    _available_action(
                        "C12",
                        "session.world.migrate",
                        "迁移冻结世界",
                        target_kind="session",
                        expected_revision=_integer(item.get("revision"), 0),
                        description="仅用于未运行或已暂停副本；执行前建立不可覆盖备份并检查角色契约。",
                        fields=[
                            {
                                "name": "candidate_key",
                                "type": "select",
                                "labelKey": "action.field.candidate_world",
                                "required": True,
                                "options": options,
                            },
                            {
                                "name": "acknowledge_migration",
                                "type": "checkbox",
                                "labelKey": "action.field.acknowledge_migration",
                                "required": True,
                            },
                        ],
                    )
                )
        items.append(item)
        group_view["items"].append(item)
    distribution: dict[str, int] = {
        _session_state(key): value
        for key, value in state_counts.items()
        if value
    }
    counts = [
        {"label": label, "value": value}
        for label, value in distribution.items()
        if value
    ][:4]
    capability_panels = [
        panel
        for panel in await capability_panel_projection(
            context,
            visible_sessions=total,
            running_sessions=sum(
                count
                for state, count in state_counts.items()
                if _text(state, limit=40).lower() == "running"
            ),
            attention_services=0,
            visible_services=0,
            groups={"session"},
        )
        if panel.get("group") == "session"
    ]
    return SurfaceProjection(
        data={
            "items": items,
            "groups": list(groups.values()),
            "stage_counts": counts,
            "capability_panels": capability_panels,
            "filters": {
                "statuses": [
                    {"value": raw_state, "label": _session_state(raw_state)}
                    for raw_state, count in state_counts.items()
                    if count
                ],
                "worlds": sorted(world_options, key=lambda item: item["label"]),
                "groups": sorted(group_options, key=lambda item: item["label"]),
                "search": True,
            },
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=None if facet_truncated else total,
                has_more=has_more,
            ),
        },
        summary={
            "label": items[0]["label"] if items else "没有符合条件的副本",
            "summary": (
                items[0]["summary"]
                if items
                else "调整状态或搜索条件后重试。"
            ),
            "state": items[0]["state"] if items else "空",
            "count": total if not facet_truncated else len(items),
        },
        revision=None,
        permissions={
            "can_view": True,
            "can_manage": bool(context.roles & {"admin", "host"}),
        },
        problems=problems,
        state="partial" if problems else None,
        empty=not items,
    )

__all__ = [name for name in globals() if not name.startswith('__')]
