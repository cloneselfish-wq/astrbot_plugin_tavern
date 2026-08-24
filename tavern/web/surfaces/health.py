from __future__ import annotations

from .registry import *
from .health_support import health_state, health_summary


async def _health_surface(context: SurfaceContext) -> SurfaceProjection:
    summary = _mapping(await context.database.health_summary())
    raw_components = [
        _mapping(item)
        for item in _sequence(summary.get("components"))
        if isinstance(item, Mapping)
    ]
    items: list[dict[str, Any]] = []
    component_keys: dict[str, str] = {}
    for index, item in enumerate(raw_components):
        code = _text(item.get("code"), limit=100)
        label = _safe_label(item.get("label"), "服务名称缺失")
        state = health_state(item.get("state"))
        metrics = _mapping(item.get("metrics"))
        next_action = _mapping(item.get("next_action"))
        revision = health_component_revision(item)
        condition = "service"
        if code == "release_artifact":
            condition = {
                ("development_source", "not_applicable"): "development",
                ("installation", "verified"): "pure_install",
                ("installation", "missing"): "manifest_missing",
                ("installation", "corrupt"): "manifest_corrupt",
            }.get(
                (
                    _text(metrics.get("runtime_kind"), limit=40),
                    _text(metrics.get("integrity"), limit=40),
                ),
                "manifest_corrupt",
            )
        elif code == "backup":
            condition = {
                "missing": "backup_missing",
                "corrupt": "backup_corrupt",
                "verified": "backup_ready",
            }.get(_text(metrics.get("integrity"), limit=40), "backup_missing")
        available_actions: list[dict[str, Any]] = []
        if code == "backup" and state != "正常":
            available_actions.append(
                _available_action(
                    "health.recover",
                    "health.backup.create",
                    "创建并校验新备份",
                    target_kind="health",
                    expected_revision=revision,
                    description="创建完整备份并验证清单、数据库哈希、存储结构与关联完整性。",
                )
            )
        elif (
            code in {"delivery_outbox", "storage_outbox", "event_outbox"}
            and _integer(metrics.get("permanently_failed"), 0) > 0
        ):
            available_actions.append(
                _available_action(
                    "health.recover",
                    "health.outbox.retry",
                    "重试最紧急失败项目",
                    target_kind="health",
                    expected_revision=revision,
                    description="仅把一项永久失败记录重新放回安全队列，不重放领域结算。",
                )
            )
        elif (
            code in {"author_jobs", "operations"}
            and _integer(metrics.get("expired_leases"), 0) > 0
        ):
            available_actions.append(
                _available_action(
                    "health.recover",
                    "health.lease.release_expired",
                    "释放过期租约",
                    target_kind="health",
                    expected_revision=revision,
                    description="只释放已经过期的工作租约，保留已提交结果和可恢复状态。",
                )
            )
        projected_key = context.key("health", code or f"component:{index}")
        if code:
            component_keys[code] = projected_key
        items.append(
            {
                "key": projected_key,
                "object_kind": "health",
                "label": label,
                "summary": _public_text(
                    item.get("summary"),
                    limit=180,
                    default=health_summary(label, state),
                ),
                "reason": _public_text(
                    item.get("reason"),
                    limit=180,
                    default=(
                        health_summary(label, state)
                        if state != "正常"
                        else ""
                    ),
                ),
                "affected_scope": _public_text(
                    item.get("affected_scope"),
                    limit=120,
                ),
                "state": state,
                "condition": condition,
                "automatic_action": (
                    _public_text(item.get("automatic_action"), limit=180)
                    or (
                        "系统会继续重试并保留已经成功的数据。"
                        if state in {"正在恢复", "不可用", "维护中"}
                        else "系统持续监测该服务。"
                    )
                ),
                "next_step": (
                    _public_text(next_action.get("label"), limit=120)
                    or (
                        "查看恢复方案"
                        if state in {"正在恢复", "不可用", "维护中"}
                        else "无需操作"
                    )
                ),
                "revision": revision,
                "available_actions": available_actions,
                "updated_at": _text(item.get("checked_at"), limit=80),
                "last_checked_at": _text(item.get("checked_at"), limit=80),
            }
        )
    priority = {"不可用": 0, "维护中": 1, "正在恢复": 2, "尚未确认": 3, "正常": 4}
    items.sort(key=lambda row: (priority.get(row["state"], 3), row["label"]))
    overall = health_state(summary.get("overall"))
    most_urgent = next(
        (item for item in items if item["state"] == "不可用"),
        next(
            (item for item in items if item["state"] == "正在恢复"),
            items[0] if items else None,
        ),
    )
    dependencies: list[dict[str, str]] = []
    for item in raw_components:
        source_code = _text(item.get("code"), limit=100)
        for raw_target in _sequence(item.get("dependencies")):
            target_code = _text(raw_target, limit=100)
            if source_code in component_keys and target_code in component_keys:
                dependencies.append(
                    {
                        "source": component_keys[source_code],
                        "target": component_keys[target_code],
                    }
                )
    topology = (
        {"services": items, "dependencies": dependencies}
        if dependencies
        else {}
    )
    latency: list[dict[str, Any]] = []
    for index, raw in enumerate(_sequence(summary.get("latency_samples"))[:20]):
        sample = _mapping(raw)
        raw_value = (
            sample.get("milliseconds")
            if sample.get("milliseconds") is not None
            else sample.get("duration_ms")
        )
        value = number_or_none(raw_value)
        if value is None:
            continue
        latency.append(
            {
                "label": _public_text(
                    sample.get("stage"), limit=80, default=f"样本 {index + 1}"
                ),
                "value": value,
                "state": "毫秒",
                "updated_at": _text(sample.get("created_at"), limit=80),
            }
        )
    incidents: list[dict[str, Any]] = []
    for raw in _sequence(summary.get("incidents"))[:20]:
        incident = _mapping(raw)
        label = _public_text(incident.get("label"), limit=100)
        summary_text = _public_text(incident.get("summary"), limit=180)
        if label and summary_text:
            incidents.append(
                {
                    "label": label,
                    "summary": summary_text,
                    "state": health_state(incident.get("state")),
                    "created_at": _text(incident.get("created_at"), limit=80),
                }
            )
    actionable = [item for item in items if item.get("available_actions")]
    attention_count = sum(1 for item in items if item["state"] != "正常")
    recovering_count = sum(1 for item in items if item["state"] == "正在恢复")
    metrics = [
        {
            "key": "overall",
            "label": "总体状态",
            "value": overall,
            "detail": "按最需关注服务汇总",
            "tone": "jade" if overall == "正常" else "danger",
        },
        {
            "key": "visible_services",
            "label": "已检查服务",
            "value": len(items),
            "detail": "本次健康检查",
            "tone": "blue",
        },
        {
            "key": "attention_services",
            "label": "需要关注",
            "value": attention_count,
            "detail": "异常、维护或待确认",
            "tone": "danger" if attention_count else "jade",
        },
        {
            "key": "recovery_actions",
            "label": "可安全恢复",
            "value": len(actionable),
            "detail": f"{recovering_count} 项正在恢复",
            "tone": "amber" if actionable or recovering_count else "jade",
        },
    ]
    recovery: Any = actionable or {
        "label": "当前无需人工恢复",
        "summary": "系统会继续监测；出现可安全执行的恢复动作时才会显示按钮。",
        "state": overall,
    }
    return SurfaceProjection(
        data={
            "items": items[:6],
            "additional_services": items[6:],
            "metrics": metrics,
            "overall": overall,
            "impact": most_urgent,
            "recovery": recovery,
            "topology": topology,
            "latency": latency,
            "incidents": incidents,
            "diagnostics": {
                "label": "脱敏健康摘要",
                "summary": f"本次检查覆盖 {len(items)} 个可见服务。",
                "state": overall,
                "updated_at": _text(summary.get("generated_at"), limit=80),
            },
        },
        summary={
            "label": most_urgent["label"] if most_urgent else "健康信息暂不可用",
            "summary": (
                most_urgent["summary"]
                if most_urgent
                else "没有可展示的服务状态。"
            ),
            "state": overall,
            "count": len(items),
        },
        updated_at=_text(summary.get("generated_at"), limit=80),
        permissions={
            "can_view": True,
            "can_manage": True,
            "can_view_diagnostics": True,
        },
        empty=not items,
    )


__all__ = [name for name in globals() if not name.startswith('__')]
