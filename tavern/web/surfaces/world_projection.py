from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .registry import (
    SurfaceContext,
    _integer,
    _job_state,
    _mapping,
    _public_text,
    _safe_label,
    _sequence,
    _text,
    number_or_none,
)
from .runtime import _WORLD_CAPABILITY_LABELS
from .world_public_details import (
    project_public_world_summary,
    world_declared_capabilities,
    world_display_tags,
    world_gameplay_profile,
    world_public_details,
    world_resolution_details,
)
from ...visualization.ui_profile import public_ui_profile


def _world_author(raw: Mapping[str, Any]) -> str:
    item = _mapping(raw)
    metadata = _mapping(item.get("metadata") or item.get("manifest"))
    value = (
        item.get("author")
        or item.get("author_name")
        or item.get("source_author")
        or metadata.get("author")
        or metadata.get("creator")
    )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        value = "、".join(
            _public_text(part, limit=60)
            for part in value
            if _public_text(part, limit=60)
        )
    return _public_text(value, limit=100)


def _world_capability_entries(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    item = _mapping(raw)
    rules = _mapping(item.get("rules"))
    source = _mapping(item.get("capabilities") or rules.get("capabilities"))
    result: list[dict[str, Any]] = []
    for index, (raw_key, raw_value) in enumerate(source.items()):
        key = _text(raw_key, limit=120)
        if not key:
            continue
        value = _mapping(raw_value)
        label = _public_text(
            value.get("label") or value.get("name") or value.get("title"),
            limit=100,
            default=_WORLD_CAPABILITY_LABELS.get(
                key.lower(), f"声明能力 {index + 1}"
            ),
        )
        enabled = bool(value.get("enabled", True)) if value else bool(raw_value)
        result.append({"key": key, "label": label, "enabled": enabled})
    if result:
        return result[:32]
    return [
        {
            "key": entry["key"],
            "label": entry["label"],
            "enabled": bool(entry.get("enabled", True)),
            "summary": entry.get("summary", ""),
        }
        for entry in world_declared_capabilities(item)
    ][:32]


def _project_world(
    context: SurfaceContext,
    raw: Mapping[str, Any],
    package: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw = _mapping(raw)
    package = _mapping(package)
    view = project_public_world_summary(raw, package)
    name = _safe_label(view.get("name") or raw.get("name"), "世界名称缺失")
    description = _public_text(
        view.get("description") or raw.get("description"),
        limit=180,
        default="该世界尚未提供玩法摘要。",
    )
    internal = _text(raw.get("id") or raw.get("slug"), limit=300)
    module = _mapping(view.get("module_summary"))
    declared = number_or_none(module.get("declared"))
    enabled = number_or_none(module.get("enabled"))
    readiness = (
        "已归档"
        if bool(raw.get("archived"))
        else "需要修复"
        if _text(raw.get("install_state"), limit=50).lower()
        in {"failed", "blocked", "error"}
        else "可以开团"
    )
    profile = public_ui_profile(raw.get("ui_profile"))
    density = _text(profile.get("density"), limit=20).lower()
    if density not in {"minimal", "standard", "rich"}:
        density = "minimal"
    density_label = {
        "minimal": "简洁展示",
        "standard": "标准展示",
        "rich": "丰富展示",
    }[density]
    lens_labels = [
        _public_text(_mapping(item).get("label"), limit=60)
        for item in _sequence(profile.get("live_lenses"))
        if _public_text(_mapping(item).get("label"), limit=60)
    ]
    detail_sections = [
        _text(item, limit=40)
        for item in _sequence(_mapping(profile.get("actor_detail")).get("sections"))
        if _text(item, limit=40)
    ]
    return {
        "key": context.key("world", internal or name),
        "object_kind": "world",
        "label": name,
        "summary": description,
        "state": readiness,
        "character_count": number_or_none(raw.get("character_count")),
        **world_public_details(view),
        "gameplay_profile": world_gameplay_profile(raw),
        "display_tags": world_display_tags(raw),
        "resolution_details": world_resolution_details(raw),
        "revision": _integer(raw.get("revision"), 0),
        "module_summary": {
            "declared": declared,
            "enabled": enabled,
            "state": "可用" if module.get("state") == "ready" else "统计不可用",
        },
        "adaptive_ui": {
            **profile,
            "density": density,
            "density_label": density_label,
            "lens_labels": lens_labels[:12],
            "actor_detail_section_count": len(detail_sections),
            "attribute_visualization_count": len(
                _sequence(profile.get("visualizations"))
            ),
            "empty_policy": "不显示世界未声明的空字段",
        },
        "ui_profile": profile,
        "updated_at": _text(raw.get("updated_at"), limit=80),
    }


_AUTHOR_JOB_TYPE_LABELS = {
    "playtest": "试玩报告",
    "semantic_diff": "语义差异",
    "full_preflight": "发布前完整检查",
    "world_health": "世界体检",
    "world_validate": "世界验证",
    "world_build": "构建世界",
    "world_export": "导出世界",
    "package_build": "生成安装产物",
    "publish": "发布世界",
}


def project_world_author_job(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Project a world-scoped author job without public or database handles."""

    raw = _mapping(raw)
    raw_status = _text(raw.get("status"), limit=50).lower()
    state = (
        "永久失败"
        if raw_status == "permanently_failed"
        else _job_state(raw_status)
    )
    current = _integer(raw.get("progress_current"), 0)
    total = _integer(raw.get("progress_total"), 0)
    summary = (
        "任务未完成，输入与已有产物保持不变。"
        if state in {"可重试失败", "已停止重试", "永久失败"}
        else f"进度 {current}/{total}"
        if total > 0
        else "尚未提供阶段总数"
    )
    failure_reason = _public_text(raw.get("last_error"), limit=240)
    if not failure_reason and raw_status in {
        "failed", "retry_wait", "permanently_failed"
    }:
        failure_reason = "未提供可公开的失败原因。"
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
        "permanently_failed": "查看失败原因后，前往作者任务页重新执行。",
        "succeeded": "按需查看任务报告或已生成产物。",
        "completed": "按需查看任务报告或已生成产物。",
        "cancelled": "如仍需执行，请从原作者流程重新触发任务。",
    }.get(raw_status, "刷新任务状态后再决定下一步。")
    artifacts = []
    for artifact in _sequence(raw.get("artifacts")):
        value = _mapping(artifact)
        artifacts.append(
            {
                "label": _safe_label(value.get("label"), "任务报告"),
                "summary": _public_text(
                    value.get("summary"),
                    limit=180,
                    default="任务报告已生成。",
                ),
                "state": _safe_label(value.get("state"), "已生成"),
                "updated_at": _text(value.get("updated_at"), limit=80),
            }
        )
    return {
        "type_label": _AUTHOR_JOB_TYPE_LABELS.get(
            _text(raw.get("job_type"), limit=80).lower(), "作者任务"
        ),
        "state": state,
        "summary": summary,
        "progress_current": current,
        "progress_total": total,
        "attempts": _integer(raw.get("attempts"), 0),
        "max_attempts": _integer(raw.get("max_attempts"), 0),
        "failure_reason": failure_reason,
        "automatic_action": automatic_action,
        "next_step": next_step,
        "artifacts": artifacts,
        "updated_at": _text(raw.get("updated_at"), limit=80),
    }


__all__ = [
    "_project_world",
    "_world_author",
    "_world_capability_entries",
    "project_world_author_job",
]
