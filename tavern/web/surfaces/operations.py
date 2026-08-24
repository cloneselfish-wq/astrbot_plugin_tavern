from __future__ import annotations
from .registry import *
from .dashboard import *
from .runtime import *
from .worlds import *
from .author_jobs_support import _job_type
async def _author_jobs_surface(context: SurfaceContext) -> SurfaceProjection:
    from ..routes.tendencies import author_jobs_view
    offset, page_size = context.page(default=20)
    world_ref = _resolve_world_context(context, required=False)
    source = _route_body(
        await author_jobs_view(
            context.principal,
            context.database,
            world_ref=world_ref,
            limit=min(500, offset + page_size + 1),
        ),
        operation="读取作者任务",
    )
    rows = [
        dict(item)
        for item in _sequence(source.get("jobs"))
        if isinstance(item, Mapping)
    ]
    status_options = sorted(
        {
            _text(item.get("status"), limit=50).lower()
            for item in rows
            if _text(item.get("status"), limit=50)
        }
    )
    type_options = sorted(
        {
            _text(item.get("job_type"), limit=80).lower()
            for item in rows
            if _text(item.get("job_type"), limit=80)
        }
    )
    query = _text(context.query.get("q"), limit=200).casefold()
    wanted_state = _text(context.query.get("status"), limit=50).lower()
    wanted_type = _text(context.query.get("type"), limit=80).lower()
    wanted_time = _text(context.query.get("time"), limit=40).lower()
    if query:
        rows = [
            item
            for item in rows
            if query in _job_type(item.get("job_type")).casefold()
            or query in _job_state(item.get("status")).casefold()
        ]
    if wanted_state:
        rows = [
            item
            for item in rows
            if _text(item.get("status"), limit=50).lower() == wanted_state
        ]
    if wanted_type:
        rows = [
            item
            for item in rows
            if _text(item.get("job_type"), limit=80).lower() == wanted_type
        ]
    if wanted_time:
        now = datetime.now(timezone.utc)
        rows = [
            item
            for item in rows
            if _matches_time_filter(
                item.get("updated_at") or item.get("created_at"),
                wanted_time,
                now=now,
            )
        ]
    selected = rows[offset : offset + page_size]
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(selected, start=offset):
        internal = _text(raw.get("job_ref"), limit=300)
        revision = _integer(raw.get("revision"), 0)
        raw_status = _text(raw.get("status"), limit=50).lower()
        state = _job_state(raw.get("status"))
        if raw_status == "permanently_failed":
            state = "永久失败"
        current = _integer(raw.get("progress_current"), 0)
        total = _integer(raw.get("progress_total"), 0)
        if total > 0:
            progress = f"进度 {current}/{total}"
        else:
            progress = "尚未提供阶段总数"
        summary = (
            "任务未完成，输入与已有产物保持不变。"
            if state in {"可重试失败", "已停止重试"}
            else progress
        )
        available_actions: list[dict[str, Any]] = []
        if internal and revision > 0 and raw_status in {"queued", "leased", "running"}:
            available_actions.append(
                _available_action(
                    "C32",
                    "author_job.cancel",
                    "停止作者任务",
                    target_kind="job",
                    expected_revision=revision,
                    description="请求安全停止当前任务；已生成的有效产物不会被静默删除。",
                )
            )
        elif internal and revision > 0 and raw_status == "permanently_failed":
            available_actions.append(
                _available_action(
                    "C32",
                    "author_job.retry",
                    "重新执行作者任务",
                    target_kind="job",
                    expected_revision=revision,
                    description="沿用原输入创建新的重试任务；世界修订变化时会拒绝。",
                )
            )
        failure_reason = _public_text(raw.get("last_error"), limit=240)
        automatic_action = {
            "queued": "系统等待安全工作者接手，任务输入保持不变。",
            "leased": "系统正在执行当前任务并持续检查进度。",
            "running": "系统继续执行当前步骤，并保存已经确认的阶段结果。",
            "retry_wait": "系统正在等待下一次安全重试，不重复已提交结果。",
            "permanently_failed": "系统已停止自动重试，并保留原输入、失败记录和已有产物。",
            "succeeded": "系统已保存任务结果和可用报告。",
            "completed": "系统已保存任务结果和可用报告。",
            "cancelled": "系统已停止后续步骤，并保留已经确认的有效产物。",
        }.get(raw_status, "系统保留当前任务状态，等待下一次状态检查。")
        next_step = {
            "queued": "等待任务开始；无需重复提交。",
            "leased": "等待当前步骤完成；长时间无变化时刷新状态。",
            "running": "等待当前步骤完成；长时间无变化时刷新状态。",
            "retry_wait": "等待自动重试；达到上限后页面会转为永久失败。",
            "permanently_failed": "查看失败原因后，使用“重新执行作者任务”创建新尝试。",
            "succeeded": "按需查看任务报告或下载已生成产物。",
            "completed": "按需查看任务报告或下载已生成产物。",
            "cancelled": "如仍需执行，请从原作者流程重新触发任务。",
        }.get(raw_status, "刷新任务状态后再决定下一步。")
        items.append(
            {
                "key": context.key("job", internal or f"job:{index}"),
                "object_kind": "job",
                "label": _job_type(raw.get("job_type")),
                "type_label": _job_type(raw.get("job_type")),
                "summary": summary,
                "state": state,
                "progress_current": current,
                "progress_total": total,
                "attempts": _integer(raw.get("attempts"), 0),
                "max_attempts": _integer(raw.get("max_attempts"), 0),
                "failure_reason": (
                    failure_reason
                    if failure_reason
                    else "未提供可公开的失败原因。"
                    if raw_status in {"failed", "retry_wait", "permanently_failed"}
                    else ""
                ),
                "automatic_action": automatic_action,
                "next_step": next_step,
                "artifacts": [
                    {
                        "label": _safe_label(
                            _mapping(artifact).get("label"),
                            "任务报告",
                        ),
                        "summary": _public_text(
                            _mapping(artifact).get("summary"),
                            limit=180,
                            default="任务报告已生成。",
                        ),
                        "state": _safe_label(
                            _mapping(artifact).get("state"),
                            "已生成",
                        ),
                        "updated_at": _text(
                            _mapping(artifact).get("updated_at"),
                            limit=80,
                        ),
                    }
                    for artifact in _sequence(raw.get("artifacts"))
                ],
                "revision": revision,
                "readonly": not bool(available_actions),
                "readonly_reason": (
                    "当前任务状态没有可执行的人工动作。"
                    if not available_actions
                    else ""
                ),
                "available_actions": available_actions,
                "updated_at": _text(raw.get("updated_at"), limit=80),
            }
        )
    has_more = len(rows) > offset + len(items)
    counts = [
        {
            "label": _job_state(value),
            "value": sum(
                1
                for item in rows
                if _text(item.get("status"), limit=50).lower() == value
            ),
        }
        for value in status_options
    ]
    counts = [item for item in counts if item["value"]][:4]
    world_options: list[dict[str, Any]] = []
    for item in await context.database.list_worlds(False) or ():
        if not isinstance(item, Mapping):
            continue
        option = _project_world(context, item)
        option["value"] = option["key"]
        world_options.append(option)
    artifacts = [
        {**artifact, "job_label": item["label"]}
        for item in items
        for artifact in _sequence(item.get("artifacts"))
    ]
    available_actions: list[dict[str, Any]] = []
    if world_ref:
        selected_revision = _integer(_mapping(await context.database.get_world(world_ref)).get("revision"), 0)
        if selected_revision > 0:
            available_actions.append(
                _available_action(
                    "E15", "author_job.create", "运行世界完整验证", target_kind="world",
                    expected_revision=selected_revision,
                    description="登记发布前完整检查任务；后台会验证当前世界修订并保留可重放回执。",
                ))
    return SurfaceProjection(
        data={
            "items": items,
            "counts": counts,
            "filters": {
                "statuses": [
                    {"value": value, "label": _job_state(value)}
                    for value in status_options
                ],
                "types": [
                    {"value": value, "label": _job_type(value)}
                    for value in type_options
                ],
                "times": [
                    {"value": value, "label": label}
                    for value, label in _TIME_FILTER_OPTIONS
                ],
                "search": True,
            },
            "world_options": world_options,
            "available_actions": available_actions,
            "artifacts": artifacts,
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=None,
                has_more=has_more,
            ),
        },
        summary={
            "label": "后台任务队列",
            "summary": items[0]["summary"] if items else "当前队列为空。",
            "state": items[0]["state"] if items else "空",
            "count": len(items),
        },
        revision=_integer(source.get("revision"), 0),
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={"can_view": True, "can_manage": True},
        empty=not items,
    )

async def _todo_surface(context: SurfaceContext) -> SurfaceProjection:
    raw_sessions: list[dict[str, Any]] = []
    session_offset = 0
    session_truncated = False
    while len(raw_sessions) < 500:
        page, _visible_total, has_more, _state_counts = await _visible_session_page(
            context,
            offset=session_offset,
            page_size=min(100, 500 - len(raw_sessions)),
        )
        raw_sessions.extend(page)
        session_offset += len(page)
        if not has_more:
            break
        if not page:
            session_truncated = True
            break
    else:
        session_truncated = True
    session_ids = [
        _text(item.get("id"), limit=300)
        for item in raw_sessions
        if isinstance(item, Mapping) and _text(item.get("id"), limit=300)
    ]
    operation_loader = getattr(context.database, "active_operations", None)
    active_operations = (
        _mapping(await operation_loader(session_ids))
        if callable(operation_loader)
        else {}
    )
    todos: list[dict[str, Any]] = []
    priorities = {
        "阻止开演": 0,
        "需要审核": 1,
        "投递失败": 2,
        "系统异常": 3,
        "一般提醒": 4,
    }
    for raw in raw_sessions or ():
        if not isinstance(raw, Mapping):
            continue
        item = _mapping(raw)
        internal = _text(item.get("id"), limit=300)
        if not internal:
            continue
        waiting = waiting_summary(
            item.get("waiting_for"), unknown="等待完成当前业务步骤。"
        )
        state = _text(item.get("state"), limit=50).lower()
        if not waiting and state != "maintenance":
            continue
        label = _safe_label(
            item.get("instance_name") or item.get("name"), "副本名称缺失"
        )
        category = "系统异常" if state == "maintenance" else "阻止开演"
        operation = _mapping(active_operations.get(internal))
        operation_internal = _text(operation.get("operation_id"), limit=300)
        available_actions: list[dict[str, Any]] = []
        target = f"session:{internal}"
        if operation_internal and context.roles & {"admin", "host"}:
            target = f"operation:{internal}\x1f{operation_internal}"
            available_actions.append(
                _available_action(
                    "C08",
                    "operation.cancel.request",
                    "请求停止当前事务",
                    target_kind="todo",
                    expected_revision=_integer(operation.get("revision"), 0),
                    description="只请求当前副本的事务进入安全取消握手；已经结算的事实不会回滚。",
                    fields=[
                        {
                            "name": "reason",
                            "type": "textarea",
                            "labelKey": "action.field.reason",
                            "required": True,
                        }
                    ],
                )
            )
        todos.append(
            {
                "key": context.key("todo", target),
                "object_kind": "todo",
                "label": label,
                "summary": (
                    waiting
                    if waiting
                    else "副本处于维护状态，新的业务写入保持暂停。"
                ),
                "state": category,
                "priority": priorities[category],
                "runtime": True,
                "revision": _integer(operation.get("revision"), 0),
                "available_actions": available_actions,
                "updated_at": _text(item.get("updated_at"), limit=80),
                "navigation": {
                    "workspace": "session_detail",
                    "label": "打开副本",
                    "context": {
                        "objectKey": context.key("session", internal),
                    },
                },
            }
        )

    overview_counts: dict[str, Any] = {}
    if "admin" in context.roles:
        overview = _mapping(await context.database.overview())
        counts = _mapping(overview.get("counts"))
        overview_counts = counts
        pending = _integer(counts.get("pending_deliveries"), 0)
        if pending:
            todos.append(
                {
                    "key": context.key("todo", "global:delivery"),
                    "object_kind": "todo",
                    "label": "消息仍在等待投递",
                    "summary": f"有 {pending} 项投递等待自动处理或人工恢复。",
                    "state": "投递失败",
                    "priority": priorities["投递失败"],
                    "runtime": False,
                    "updated_at": "",
                }
            )
        storage_errors = _integer(overview.get("storage_errors"), 0)
        if storage_errors:
            todos.append(
                {
                    "key": context.key("todo", "global:storage"),
                    "object_kind": "todo",
                    "label": "存储检查发现异常",
                    "summary": "系统已暂停依赖可靠保存的危险操作。",
                    "state": "系统异常",
                    "priority": priorities["系统异常"],
                    "runtime": False,
                    "updated_at": "",
                }
            )
    status_options = [
        label for label in priorities if any(item["state"] == label for item in todos)
    ]
    query = _text(context.query.get("q"), limit=200).casefold()
    wanted_state = _text(context.query.get("status"), limit=50)
    if query:
        todos = [
            item
            for item in todos
            if query in (item["label"] + " " + item["summary"]).casefold()
        ]
    if wanted_state:
        todos = [item for item in todos if item["state"] == wanted_state]
    todos.sort(key=lambda item: (item["priority"], item["updated_at"]))
    offset, page_size = context.page(default=20)
    total = len(todos)
    items = todos[offset : offset + page_size]
    problems: list[VisualProblem | Mapping[str, Any]] = []
    category_counts = [
        {"label": label, "value": sum(1 for item in todos if item["state"] == label)}
        for label in priorities
        if any(item["state"] == label for item in todos)
    ]
    operation_statuses = [
        _text(item.get("status"), limit=50).lower()
        for item in active_operations.values()
        if isinstance(item, Mapping)
    ]
    queue_counts: dict[str, int] = {
        "准备中": sum(
            1 for status in operation_statuses if status in {"pending", "reserved"}
        ),
        "运行中": sum(
            1
            for status in operation_statuses
            if status in {"generating", "dice_locked", "ready_to_commit"}
        ),
    }
    if "admin" in context.roles:
        queue_counts["等待投递"] = _integer(
            overview_counts.get("pending_deliveries"), 0
        )
    health_loader = getattr(context.database, "health_summary", None)
    if callable(health_loader) and "admin" in context.roles:
        try:
            health = _mapping(await health_loader())
            retrying = 0
            permanent = 0
            for raw in _sequence(health.get("components")):
                component = _mapping(raw)
                if _text(component.get("code"), limit=80) not in {
                    "delivery_outbox",
                    "storage_outbox",
                    "event_outbox",
                    "author_jobs",
                }:
                    continue
                metrics = _mapping(component.get("metrics"))
                retrying += _integer(metrics.get("retry_wait"), 0)
                retrying += _integer(metrics.get("expired_leases"), 0)
                permanent += _integer(metrics.get("permanently_failed"), 0)
                permanent += _integer(metrics.get("stale_leases"), 0)
            queue_counts["恢复中"] = retrying
            queue_counts["永久失败"] = permanent
        except Exception as exc:
            _status, problem = problem_from_exception(exc)
            problems.append(problem)
    queue_groups = [
        {
            "label": label,
            "value": value,
            "filter_state": (
                "投递失败"
                if label in {"等待投递", "恢复中", "永久失败"}
                else "阻止开演"
            ),
        }
        for label, value in queue_counts.items()
    ]
    for item in items:
        item["actionable"] = bool(item.get("available_actions"))
        item["notice_kind"] = "需要处理" if item["actionable"] else "只读通知"
    permanent_count = queue_counts.get("永久失败")
    delivery_boundary = {
        "label": "投递与恢复边界",
        "summary": (
            "系统只续行仍可安全重试的项目；已完成结果不会重放，永久失败会保留等待人工检查。"
        ),
        "state": (
            f"{permanent_count} 项永久失败"
            if permanent_count
            else "自动恢复按安全边界运行"
        ),
        "queue": queue_groups,
    }
    if session_truncated:
        problems.append(
            VisualProblem(
                code="tavern.surface.todo_scan_limited",
                message="待办聚合已达到本次安全扫描上限。",
                recovery="请使用副本或状态筛选缩小范围后重试。",
                retryable=True,
            )
        )
    return SurfaceProjection(
        data={
            "items": items,
            "groups": category_counts,
            "category_counts": category_counts,
            "context": delivery_boundary,
            "filters": {
                "statuses": [
                    {"value": value, "label": value}
                    for value in status_options
                ],
                "search": True,
            },
            "blockers": [
                item
                for item in items
                if item.get("runtime") and not item.get("actionable")
            ][:5],
            "selected": items[0] if items else None,
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=None if session_truncated else total,
                has_more=offset + len(items) < total,
            ),
        },
        summary={
            "label": items[0]["label"] if items else "当前没有待处理事项",
            "summary": items[0]["summary"] if items else "没有会阻止玩家的事项。",
            "state": items[0]["state"] if items else "空",
            "count": total,
        },
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={"can_view": True, "can_manage": True},
        problems=problems,
        state="partial" if problems else None,
        empty=not items,
    )

_AUDIT_ACTIONS = {
    "session.pause": "暂停副本",
    "session.resume": "继续副本",
    "session.finish": "归档副本",
    "card.review.approve": "通过角色审核",
    "card.review.reject": "退回角色审核",
    "plugin.module.toggle": "调整系统模块",
    "settings.save": "保存系统设置",
}

async def _audit_mode_surface(
    context: SurfaceContext, *, mode: str
) -> SurfaceProjection:
    offset, page_size = context.page(default=20)
    items: list[dict[str, Any]] = []
    counts: list[dict[str, Any]] = []
    status_options: list[dict[str, str]] = []
    extra_filters: dict[str, list[dict[str, str]]] = {}
    wanted_state = _text(context.query.get("status"), limit=50).lower()
    if mode == "delivery":
        session_id = _resolve_session_context(context, required=False)
        row_limit = min(100, offset + page_size + 1)
        source_rows: list[dict[str, Any]] = []
        if session_id:
            rows = await context.database.list_turn_delivery_runs(
                session_id,
                limit=row_limit,
            )
            source_rows = [
                dict(item) for item in rows or () if isinstance(item, Mapping)
            ]
        else:
            list_sessions = getattr(context.database, "list_sessions", None)
            raw_sessions = await list_sessions() if callable(list_sessions) else ()
            for raw_session in list(raw_sessions or ())[:50]:
                if not isinstance(raw_session, Mapping):
                    continue
                candidate_id = _text(raw_session.get("id"), limit=300)
                if not candidate_id:
                    continue
                candidate_label = _safe_label(
                    raw_session.get("instance_name") or raw_session.get("name"),
                    "授权副本",
                )
                rows = await context.database.list_turn_delivery_runs(
                    candidate_id,
                    limit=row_limit,
                )
                for raw in rows or ():
                    if not isinstance(raw, Mapping):
                        continue
                    projected = dict(raw)
                    projected["_session_label"] = candidate_label
                    source_rows.append(projected)
            source_rows.sort(
                key=lambda item: _text(item.get("updated_at"), limit=80),
                reverse=True,
            )
            source_rows = source_rows[:row_limit]
        raw_states = sorted(
            {
                _text(item.get("status"), limit=50).lower()
                for item in source_rows
                if _text(item.get("status"), limit=50)
            }
        )
        status_options = [
            {"value": value, "label": _delivery_state(value)}
            for value in raw_states
        ]
        counts = [
            {
                "label": _delivery_state(value),
                "value": sum(
                    1
                    for item in source_rows
                    if _text(item.get("status"), limit=50).lower() == value
                ),
            }
            for value in raw_states
        ][:4]
        if wanted_state:
            source_rows = [
                item
                for item in source_rows
                if _text(item.get("status"), limit=50).lower() == wanted_state
            ]
        selected = source_rows[offset : offset + page_size]
        for index, raw in enumerate(selected, start=offset):
            total_parts = _integer(raw.get("total_parts"), 0)
            next_index = _integer(raw.get("next_part_index"), 0)
            delivered = min(max(next_index, 0), total_parts)
            remaining = max(0, total_parts - delivered)
            parts: list[dict[str, Any]] = []
            for part_index, part_raw in enumerate(_sequence(raw.get("parts"))):
                part = _mapping(part_raw)
                parts.append(
                    {
                        "sequence": part_index + 1,
                        "label": {
                            "story": "故事正文",
                            "choice": "本轮选择",
                            "result": "行动结果",
                            "status": "状态更新",
                        }.get(
                            _text(part.get("kind"), limit=40).lower(),
                            "消息分段",
                        ),
                        "state": _delivery_state(part.get("status")),
                        "attempts": _integer(part.get("attempts"), 0),
                    }
                )
            internal = _text(
                raw.get("run_id") or raw.get("operation_id"), limit=300
            )
            raw_status = _text(raw.get("status"), limit=50).lower()
            next_step = {
                "pending": "等待系统开始发送；无需重复提交。",
                "sending": "等待当前分段发送完成。",
                "partially_sent": "系统只会续发尚未确认的分段。",
                "retry_wait": "等待系统按安全间隔继续发送。",
                "delivered": "无需处理；所有分段已确认。",
                "cancelled": "投递已经取消；已送达分段不会撤回。",
            }.get(raw_status, "刷新后重新检查投递状态。")
            items.append(
                {
                    "key": context.key(
                        "delivery", internal or f"{session_id}:{index}"
                    ),
                    "object_kind": "delivery",
                    "label": (
                        f"{raw['_session_label']} · 本轮分段投递"
                        if raw.get("_session_label")
                        else "本轮分段投递"
                    ),
                    "summary": (
                        f"已送达 {delivered}/{total_parts} 段，"
                        f"还剩 {remaining} 段。"
                    ),
                    "state": _delivery_state(raw.get("status")),
                    "attempts": _integer(raw.get("attempt_count"), 0),
                    "delivered_parts": delivered,
                    "remaining_parts": remaining,
                    "parts": parts,
                    "parts_summary": "；".join(
                        f"{part['label']}：{part['state']}（{part['attempts']} 次）"
                        for part in parts
                    ) or "没有可显示的分段回执。",
                    "next_step": next_step,
                    "failure_reason": _public_text(raw.get("last_error"), limit=200),
                    "actions_enabled": False,
                    "action_boundary": "同步回复投递尚未提供能校验目标归属、最新状态和重复提交的人工重试或取消入口，因此操作保持禁用。",
                    "available_actions": [],
                    "updated_at": _text(raw.get("updated_at"), limit=80),
                }
            )
        has_more = len(source_rows) > offset + len(items)
    else:
        rows = await context.database.list_audit("", 501, 0)
        source_rows = [
            dict(item) for item in rows or () if isinstance(item, Mapping)
        ]
        audit_truncated = len(source_rows) > 500
        source_rows = source_rows[:500]
        list_sessions = getattr(context.database, "list_sessions", None)
        raw_sessions = await list_sessions() if callable(list_sessions) else ()
        session_labels = {
            _text(item.get("id"), limit=300): _safe_label(
                item.get("instance_name") or item.get("name"), "授权对象"
            )
            for item in raw_sessions or ()
            if isinstance(item, Mapping) and _text(item.get("id"), limit=300)
        }
        actor_labels: dict[str, str] = {}
        object_labels: dict[str, str] = {}
        action_labels: dict[str, str] = {}
        records: list[dict[str, Any]] = []
        for raw in source_rows:
            actor_ref = _text(raw.get("actor_id") or raw.get("actor"), limit=300)
            if actor_ref and actor_ref not in actor_labels:
                actor_labels[actor_ref] = _public_text(
                    raw.get("actor_name") or raw.get("actor_label"),
                    limit=100,
                    default=f"授权操作者 {len(actor_labels) + 1}",
                )
            object_ref = _text(
                raw.get("session_id") or raw.get("target"), limit=300
            )
            if object_ref and object_ref not in object_labels:
                object_labels[object_ref] = session_labels.get(
                    object_ref, f"授权对象 {len(object_labels) + 1}"
                )
            action_ref = _text(raw.get("action"), limit=120)
            if action_ref and action_ref not in action_labels:
                action_labels[action_ref] = _AUDIT_ACTIONS.get(
                    action_ref, "一次授权操作"
                )
            detail = _mapping(raw.get("detail"))
            records.append(
                {
                    "raw": raw,
                    "actor_ref": actor_ref,
                    "actor_label": actor_labels.get(actor_ref, "授权操作者"),
                    "object_ref": object_ref,
                    "object_label": object_labels.get(object_ref, "授权对象"),
                    "action_ref": action_ref,
                    "action_label": action_labels.get(action_ref, "一次授权操作"),
                    "search_text": " ".join(
                        value
                        for value in (
                            action_labels.get(action_ref, "一次授权操作"),
                            object_labels.get(object_ref, "授权对象"),
                            actor_labels.get(actor_ref, "授权操作者"),
                            _public_text(detail.get("message"), limit=160),
                        )
                        if value
                    ),
                }
            )
        object_options = [
            {
                "value": _opaque_filter_value(
                    context, "audit-object-filter", value
                ),
                "label": label,
            }
            for value, label in sorted(object_labels.items(), key=lambda item: item[1])
        ]
        actor_options = [
            {
                "value": _opaque_filter_value(
                    context, "audit-actor-filter", value
                ),
                "label": label,
            }
            for value, label in sorted(actor_labels.items(), key=lambda item: item[1])
        ]
        action_options = [
            {
                "value": _opaque_filter_value(
                    context, "audit-action-filter", value
                ),
                "label": label,
            }
            for value, label in sorted(action_labels.items(), key=lambda item: item[1])
        ]
        wanted_object = _resolve_filter_value(
            context,
            "audit-object-filter",
            context.query.get("object"),
            label="对象",
        )
        wanted_actor = _resolve_filter_value(
            context,
            "audit-actor-filter",
            context.query.get("actor"),
            label="操作者",
        )
        wanted_action = _resolve_filter_value(
            context,
            "audit-action-filter",
            context.query.get("action"),
            label="操作",
        )
        wanted_time = _text(context.query.get("time"), limit=40).lower()
        search = _text(context.query.get("q"), limit=200).casefold()
        now = datetime.now(timezone.utc)
        records = [
            record
            for record in records
            if (not search or search in record["search_text"].casefold())
            and (not wanted_object or record["object_ref"] == wanted_object)
            and (not wanted_actor or record["actor_ref"] == wanted_actor)
            and (not wanted_action or record["action_ref"] == wanted_action)
            and (
                not wanted_time
                or _matches_time_filter(
                    record["raw"].get("created_at"), wanted_time, now=now
                )
            )
        ]
        status_options = [{"value": "recorded", "label": "已记录"}]
        selected_records = records[offset : offset + page_size]
        for index, record in enumerate(selected_records, start=offset):
            raw = record["raw"]
            internal = raw.get("id")
            items.append(
                {
                    "key": context.key("audit", internal or f"audit:{index}"),
                    "object_kind": "audit",
                    "label": record["action_label"],
                    "summary": _public_text(
                        _mapping(raw.get("detail")).get("message"),
                        limit=180,
                        default="系统记录了操作结果；技术差异需按权限查看。",
                    ),
                    "actor_label": record["actor_label"],
                    "object_label": record["object_label"],
                    "state": "已记录",
                    "updated_at": _text(raw.get("created_at"), limit=80),
                }
            )
        if wanted_state and wanted_state != "recorded":
            items = []
        has_more = offset + len(selected_records) < len(records)
        extra_filters = {
            "objects": object_options,
            "actors": actor_options,
            "actions": action_options,
            "times": [
                {"value": value, "label": label}
                for value, label in _TIME_FILTER_OPTIONS
            ],
        }
        if audit_truncated:
            counts.append(
                {
                    "label": "记录较多",
                    "value": 500,
                    "summary": "请增加对象、操作者、动作或时间条件。",
                }
            )
    mode_label = "投递恢复" if mode == "delivery" else "审计记录"
    return SurfaceProjection(
        data={
            "items": items,
            "mode": mode_label,
            "mode_context": {
                "label": mode_label,
                "summary": (
                    "只加载当前副本的分段投递生命周期。"
                    if mode == "delivery"
                    else "只加载已授权操作的脱敏记录。"
                ),
                "state": "当前模式",
            },
            "counts": counts if mode == "delivery" else [],
            "deliveries": items if mode == "delivery" else [],
            "selected_delivery": items[0] if mode == "delivery" and items else None,
            "records": items if mode == "audit" else [],
            "filters": {
                "statuses": status_options,
                "search": mode == "audit",
                **extra_filters,
            },
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=len(items),
                total=None,
                has_more=has_more,
            ),
        },
        summary={
            "label": items[0]["label"] if items else "当前没有记录",
            "summary": items[0]["summary"] if items else "当前模式下没有可见记录。",
            "state": items[0]["state"] if items else "空",
            "count": len(items),
        },
        updated_at=latest_timestamp(*(item.get("updated_at") for item in items)),
        permissions={
            "can_view": True,
            "can_manage": mode == "delivery",
            "can_view_private": True,
        },
        empty=not items,
    )

async def _audit_surface(context: SurfaceContext) -> SurfaceProjection:
    delivery = await _audit_mode_surface(context, mode="delivery")
    audit = await _audit_mode_surface(context, mode="audit")
    delivery_data = _mapping(delivery.data)
    audit_data = _mapping(audit.data)
    deliveries = [
        dict(item)
        for item in _sequence(delivery_data.get("deliveries"))
        if isinstance(item, Mapping)
    ]
    records = [
        dict(item)
        for item in _sequence(audit_data.get("records"))
        if isinstance(item, Mapping)
    ]
    audit_filters = _mapping(audit_data.get("filters"))
    delivery_filters = _mapping(delivery_data.get("filters"))
    statuses: list[dict[str, Any]] = []
    seen_statuses: set[str] = set()
    for option in (
        *_sequence(delivery_filters.get("statuses")),
        *_sequence(audit_filters.get("statuses")),
    ):
        if not isinstance(option, Mapping):
            continue
        value = _text(option.get("value"), limit=80)
        if not value or value in seen_statuses:
            continue
        seen_statuses.add(value)
        statuses.append(dict(option))
    offset, page_size = context.page(default=20)
    delivery_page = _mapping(delivery_data.get("pagination"))
    audit_page = _mapping(audit_data.get("pagination"))
    items = [*deliveries, *records]
    problems = [*delivery.problems, *audit.problems]
    first = items[0] if items else None
    return SurfaceProjection(
        data={
            "items": items,
            "mode": "投递与审计",
            "mode_context": {
                "label": "投递与审计",
                "summary": "同时加载分段投递生命周期与已授权操作记录。",
                "state": "同屏查看",
            },
            "counts": list(_sequence(delivery_data.get("counts"))),
            "deliveries": deliveries,
            "selected_delivery": deliveries[0] if deliveries else None,
            "records": records,
            "filters": {
                "statuses": statuses,
                "search": True,
                **{
                    key: value
                    for key, value in audit_filters.items()
                    if key not in {"statuses", "search"}
                },
            },
            "pagination": _pagination(
                context,
                offset=offset,
                page_size=page_size,
                returned=max(len(deliveries), len(records)),
                total=None,
                has_more=bool(
                    delivery_page.get("has_more") or audit_page.get("has_more")
                ),
            ),
            "delivery_pagination": delivery_page,
            "audit_pagination": audit_page,
        },
        summary={
            "label": first["label"] if first else "当前没有记录",
            "summary": (
                first["summary"]
                if first
                else "当前没有可见投递或授权操作记录。"
            ),
            "state": first["state"] if first else "空",
            "count": len(items),
        },
        updated_at=latest_timestamp(delivery.updated_at, audit.updated_at),
        permissions={
            "can_view": True,
            "can_manage": bool(delivery.permissions.get("can_manage")),
            "can_view_private": bool(audit.permissions.get("can_view_private")),
        },
        problems=problems,
        state="partial" if problems else None,
        empty=not items,
    )

__all__ = [name for name in globals() if not name.startswith('__')]
