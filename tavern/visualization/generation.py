"""GenerationWaterfall projection without provider, prompt, trace, or raw result."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from .common import integer, number_or_none, text
from .keys import OpaqueKeyFactory


_OPERATION_LABELS = {
    "generate_choices": "准备下一步行动",
    "generate_vote": "准备投票",
    "generate_narrative": "生成故事正文",
    "turn_commit": "结算并保存本轮",
    "choice_resolution": "结算玩家行动",
    "vote_resolution": "结算投票",
    "story_pacing": "检查剧情节奏",
}

_ACTIVE_STATUSES = frozenset(
    {"pending", "reserved", "generating", "dice_locked", "ready_to_commit", "cancel_requested", "running", "validated"}
)
_CANCELLABLE_STATUSES = _ACTIVE_STATUSES - {"cancel_requested"}
_FAILED_STATUSES = frozenset(
    {"failed", "failed_retryable", "needs_recovery", "compensated", "rejected", "rolled_back"}
)


_STAGE_PRESENTATIONS: Mapping[str, tuple[str, str, str]] = {
    "pending": ("等待开始生成", "neutral", "○"),
    "reserved": ("已排队等待生成", "neutral", "○"),
    "prepare": ("准备故事生成", "neutral", "○"),
    "prepare_context": ("整理本轮线索与角色状态", "neutral", "○"),
    "context_ready": ("本轮线索与角色状态已整理", "beneficial", "✓"),
    "planning": ("规划故事推进", "neutral", "○"),
    "story_plan_generated": ("故事推进方案已准备", "beneficial", "✓"),
    "generate": ("生成故事内容", "warning", "◷"),
    "generating": ("生成故事内容", "warning", "◷"),
    "story_generation": ("生成故事进展", "warning", "◷"),
    "generate_resolution": ("结算本轮行动", "warning", "◷"),
    "generate_narrative": ("生成故事正文", "warning", "◷"),
    "generate_choices": ("准备下一步行动", "warning", "◷"),
    "check": ("检查生成结果", "warning", "◇"),
    "validating": ("检查故事一致性", "warning", "◇"),
    "repair_or_validate": ("检查故事一致性", "warning", "◇"),
    "targeted_quality_repair": ("修正故事内容", "warning", "↻"),
    "check_resolved": ("检定结果已确认", "beneficial", "✓"),
    "save": ("保存本轮结果", "warning", "◷"),
    "dice_locked": ("检定结果已保存", "beneficial", "✓"),
    "ready_to_commit": ("等待保存本轮结果", "warning", "◷"),
    "deliver": ("发送本轮结果", "warning", "↑"),
    "commit_and_deliver": ("保存并发送本轮结果", "warning", "↑"),
    "committed": ("本轮结果已保存并发送", "beneficial", "✓"),
    "completed": ("故事生成已完成", "beneficial", "✓"),
    "cancel_requested": ("正在安全取消", "warning", "◷"),
    "cancelled": ("故事生成已取消", "neutral", "×"),
    "not_required": ("本次无需生成故事", "neutral", "•"),
    "story_plan_failed": ("故事生成未完成", "harmful", "!"),
    "dm_beat_generation_failed": ("故事生成未完成", "harmful", "!"),
    "reroll_generation_failed": ("故事生成未完成", "harmful", "!"),
    "quality_rejected": ("故事一致性检查未通过", "harmful", "!"),
    "revision_conflict": ("本轮结果保存失败", "harmful", "!"),
    "turn_commit_failed": ("本轮结果保存失败", "harmful", "!"),
    "reroll_commit_failed": ("本轮结果保存失败", "harmful", "!"),
    "lease_expired": ("故事生成需要恢复", "warning", "↻"),
    "failed": ("故事生成失败", "harmful", "!"),
    "failed_retryable": ("故事生成未完成，可以重试", "warning", "↻"),
    "needs_recovery": ("故事生成需要恢复", "warning", "↻"),
    "compensated": ("未完成的更改已回退", "neutral", "↻"),
    "rejected": ("故事生成未通过检查", "harmful", "!"),
    "rolled_back": ("未完成的更改已回退", "neutral", "↻"),
}
_REPAIR_STAGES = frozenset({"repair_or_validate", "targeted_quality_repair"})


def _stage_projection(value: Any) -> dict[str, Any]:
    raw = text(value, limit=80).strip().casefold()
    presentation = _STAGE_PRESENTATIONS.get(raw)
    if presentation is None:
        return {
            "stage_label": "生成阶段无法识别",
            "stage_tone": "unknown",
            "stage_symbol": "?",
            "stage_problem": {
                "code": "visual.generation.stage_unknown",
                "message": "生成阶段无法识别",
                "recovery": "请刷新生成进度；系统不会显示无法识别的内部阶段。",
                "retryable": True,
            },
        }
    label, tone, symbol = presentation
    return {
        "stage_label": label,
        "stage_tone": tone,
        "stage_symbol": symbol,
    }


def _terminal_stage_result(status: str) -> str:
    if status in _FAILED_STATUSES:
        return "failed"
    if status == "cancelled":
        return "cancelled"
    if status in _ACTIVE_STATUSES:
        return "running"
    return "completed"


def _safe_result(value: Any) -> str:
    raw = text(value, limit=40).lower()
    if raw in {"ok", "success", "completed", "accepted"}:
        return "completed"
    if "fallback" in raw:
        return "fallback"
    if "repair" in raw:
        return "repaired"
    if raw in {"running", "pending", "started", "ready"}:
        return "running"
    if raw in {"failed", "error", "timeout", "rejected"}:
        return "failed"
    return "completed" if raw else "unknown"


def _failure_class(value: Any) -> str:
    raw = text(value, limit=100).lower()
    if "timeout" in raw or "deadline" in raw:
        return "timeout"
    if "quality" in raw or "repair" in raw:
        return "quality"
    if "delivery" in raw or "send" in raw:
        return "delivery"
    if raw:
        return "processing"
    return ""


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = text(value, limit=80)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reminder_projection(
    raw: Mapping[str, Any], *, current: datetime, scheduled: bool
) -> dict[str, Any]:
    request = raw.get("request") if isinstance(raw.get("request"), Mapping) else {}
    frozen = (
        request.get("reminder_config")
        if isinstance(request.get("reminder_config"), Mapping)
        else {}
    )
    created = _utc_datetime(raw.get("created_at"))
    next_at = _utc_datetime(raw.get("next_reminder_at"))
    enabled = bool(raw.get("reminder_enabled"))
    source = text(frozen.get("source"), limit=40).lower()
    return {
        "enabled": enabled,
        "interval_seconds": max(30, min(600, integer(raw.get("reminder_interval_seconds"), 60))),
        "source_label": {
            "global_default": "全局默认",
            "session_override": "副本覆盖",
            "implicit_default": "安全默认",
        }.get(source, "安全默认"),
        "elapsed_minutes": max(0, int((current - created).total_seconds() // 60)) if created else 0,
        "sequence": max(0, integer(raw.get("reminder_sequence"), 0)),
        "last_reminder_at": text(raw.get("last_reminder_at"), limit=80) or None,
        "next_reminder_at": (text(raw.get("next_reminder_at"), limit=80) or None) if scheduled else None,
        "next_reminder_in_seconds": (
            max(0, int((next_at - current).total_seconds()))
            if enabled and scheduled and next_at
            else None
        ),
    }


def project_generation(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    privileged: bool,
    keys: OpaqueKeyFactory,
    diagnostics: bool = False,
    cursor: str = "",
    page_size: int = 10,
    active_operation: Mapping[str, Any] | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    current = _utc_datetime(now) or datetime.now(timezone.utc)
    active_row = active_operation if isinstance(active_operation, Mapping) else {}
    operations: list[dict[str, Any]] = []
    for operation_index, raw in enumerate(rows or ()):
        if not isinstance(raw, Mapping):
            continue
        result = raw.get("result") if isinstance(raw.get("result"), Mapping) else {}
        stage_rows = [
            item
            for item in result.get("generation_stages") or ()
            if isinstance(item, Mapping)
        ]
        status = text(raw.get("status"), limit=40, default="pending")
        phase = text(raw.get("last_progress_stage") or raw.get("phase"), limit=80)
        if not stage_rows and not phase:
            continue
        providers: list[str] = []
        stages: list[dict[str, Any]] = []
        for stage_index, stage in enumerate(stage_rows):
            raw_stage = text(stage.get("stage"), limit=80)
            provider = text(stage.get("provider_id"), limit=160)
            if provider and provider not in providers:
                providers.append(provider)
            duration = number_or_none(
                stage.get("duration_seconds", stage.get("elapsed"))
            )
            stage_result = _safe_result(stage.get("result"))
            if (
                stage_index == len(stage_rows) - 1
                and stage_result in {"running", "unknown"}
                and status not in _ACTIVE_STATUSES
            ):
                stage_result = _terminal_stage_result(status)
            projected = {
                "key": keys.key(
                    "generationstage", f"{operation_index}:{stage_index}:{raw_stage}"
                ),
                **_stage_projection(raw_stage),
                "started_at": text(stage.get("started_at"), limit=80) or None,
                "ended_at": text(stage.get("ended_at"), limit=80) or None,
                "duration_seconds": duration,
                "result": stage_result,
                "repair_used": raw_stage.casefold() in _REPAIR_STAGES
                or stage_result == "repaired",
                "fallback_used": stage_result == "fallback",
            }
            if diagnostics:
                projected["provider_category"] = (
                    "primary" if not provider or providers.index(provider) == 0 else "fallback"
                )
                failure = _failure_class(stage.get("result"))
                if failure:
                    projected["failure_class"] = failure
            stages.append(projected)
        if not stages:
            stages.append(
                {
                    "key": keys.key("generationstage", f"{operation_index}:{phase}"),
                    **_stage_projection(phase),
                    "started_at": None,
                    "ended_at": None,
                    "duration_seconds": None,
                    "result": _terminal_stage_result(status),
                    "repair_used": False,
                    "fallback_used": False,
                }
            )
        operation_type = text(raw.get("operation_type"), limit=80)
        active = status in _ACTIVE_STATUSES
        cancelling = status == "cancel_requested"
        item: dict[str, Any] = {
            "_cursor_identity": text(
                raw.get("operation_id")
                or f"{raw.get('created_at') or ''}:{operation_type}:{operation_index}",
                limit=300,
            ),
            "key": keys.key(
                "generation", f"{operation_index}:{raw.get('created_at') or ''}"
            ),
            "label": _OPERATION_LABELS.get(operation_type, "处理故事进展"),
            "state": (
                "cancelling"
                if cancelling
                else "running"
                if active
                else "failed"
                if status in _FAILED_STATUSES
                else "cancelled"
                if status == "cancelled"
                else "completed"
            ),
            "stages": stages,
            "repair_used": any(stage["repair_used"] for stage in stages),
            "fallback_used": any(stage["fallback_used"] for stage in stages),
            "can_cancel": False,
            "reminder": _reminder_projection(
                raw, current=current, scheduled=status in _CANCELLABLE_STATUSES
            ),
            "updated_at": text(raw.get("last_progress_at") or raw.get("updated_at"), limit=80),
        }
        same_active = bool(
            privileged
            and status in _CANCELLABLE_STATUSES
            and text(active_row.get("operation_id"), limit=300)
            == text(raw.get("operation_id"), limit=300)
        )
        item["can_cancel"] = same_active
        if same_active:
            item["available_actions"] = [
                {
                    "action_id": "generation-cancel",
                    "intent": "operation.cancel.request",
                    "label": "请求停止当前生成",
                    "target_kind": "session",
                    "expected_revision": integer(active_row.get("revision"), 0),
                    "description": "只请求在安全边界停止；已经结算的事实不会回滚。",
                    "transportReady": True,
                    "focus_return": "opener",
                    "fields": [
                        {"name": "reason", "type": "textarea", "labelKey": "action.field.reason", "required": True}
                    ],
                }
            ]
        if diagnostics and item["state"] == "failed":
            item["failure_class"] = _failure_class(raw.get("last_error_code")) or "processing"
        operations.append(item)
    operations.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("_cursor_identity") or ""),
        ),
        reverse=True,
    )
    identities = [item["_cursor_identity"] for item in operations]
    offset = keys.after_anchor("generation", cursor, identities) if cursor else 0
    page_size = max(1, min(50, int(page_size)))
    selected = operations[offset : offset + page_size]
    items = [
        {key: value for key, value in item.items() if key != "_cursor_identity"}
        for item in selected
    ]
    next_offset = offset + len(items)
    has_more = next_offset < len(operations)
    return {
        "items": items,
        "next_cursor": (
            keys.anchor_cursor("generation", selected[-1]["_cursor_identity"])
            if has_more and selected
            else ""
        ),
        "has_more": has_more,
        "page_size": page_size,
        "total_items": len(operations),
        "problems": [],
    }


__all__ = ["project_generation"]
