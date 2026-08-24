"""D1 自动终局计划纯服务（幂等、确定性、可恢复）。

职责：
- 生成终局计划（幂等键 terminal:{session}:{condition}:{trigger_revision}）；
- 并发重复输入判定（apply / replayed / superseded）；
- 多条件并发命中的确定性合并（两个后台扫描必须选同一个赢家）；
- 玩家可见终局投影（不暴露条件 id、幂等键与内部状态）。

宿主在事务内调用：先 evaluate + arbitrate + build plan，再按
classify_plan 的决策执行唯一一次归档；快照可延迟到事务外完成，
重试复用同一幂等键（D1_PLAN 18 §10-11）。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts.actor_fate import (
    ARCHIVE_POLICIES,
    TERMINATION_TYPES,
    collect_condition_paths,
    condition_has_member_guard,
    parse_terminal_conditions,
)
from .models import FinalizationPlan
from .terminal_service import arbitrate_terminal_conditions

_ARCHIVE_STATE_LABELS = {
    "manual": "等待管理员完结",
    "automatic": "自动归档",
    "automatic_readonly": "永久归档",
    "automatic_failed_readonly": "永久归档",
}
_RESULT_LABELS = {
    "completed": "正常完成",
    "failed": "失败",
    "aborted": "强制终止",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def canonical_json(value: Any) -> str:
    """确定性 JSON 序列化（与发布链同一约定）。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def finalization_idempotency_key(
    session_id: str,
    condition_id: str,
    trigger_revision: int,
) -> str:
    """终局幂等键：terminal:{session_id}:{condition_id}:{trigger_revision}。"""

    return (
        f"terminal:{session_id}:{condition_id}:{trigger_revision}"
    )


def archive_state_label(
    archive_policy: str,
    termination_type: str,
) -> str:
    """归档策略的玩家可见说明。"""

    policy = str(archive_policy or "").lower()
    if policy not in ARCHIVE_POLICIES:
        return "归档状态未知"
    return str(_ARCHIVE_STATE_LABELS.get(policy) or "归档状态未知")


def result_label(termination_type: str) -> str:
    termination_type = str(termination_type or "").lower()
    if termination_type not in TERMINATION_TYPES:
        return "结果未知"
    return str(_RESULT_LABELS.get(termination_type) or "结果未知")


def _plan_steps(
    *,
    termination_type: str,
    deferred_snapshot: bool,
) -> list[dict[str, Any]]:
    steps = [
        {"step": "lock_player_input", "status": "required"},
        {"step": "cancel_pending_operations", "status": "required"},
        {"step": "persist_fate_records", "status": "required"},
        {"step": "record_terminal_receipt", "status": "required"},
        {"step": "write_ending_event", "status": "required"},
        {"step": "create_final_snapshot", "status": "required"},
        {"step": "create_session_archive", "status": "required"},
        {"step": "queue_outbox", "status": "required"},
    ]
    if deferred_snapshot:
        steps.insert(
            5,
            {
                "step": "mark_finalization_pending",
                "status": "required",
            },
        )
        for step in steps:
            if step["step"] == "create_final_snapshot":
                step["status"] = "deferred"
            if step["step"] == "create_session_archive":
                step["status"] = "deferred_until_snapshot"
    return steps


def build_finalization_plan(
    *,
    session_id: str,
    match: Mapping[str, Any],
    trigger_revision: int,
    created_at: str = "",
    deferred_snapshot: bool = False,
    endings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """为仲裁赢家生成确定性终局计划。"""

    session_id = str(session_id or "").strip()
    condition_id = str(match.get("condition_id") or "").strip()
    if not session_id or not condition_id:
        raise ValueError("终局计划必须指定副本与终局条件")
    trigger_revision = int(trigger_revision or 0)
    termination_type = str(match.get("termination_type") or "completed").lower()
    if termination_type not in TERMINATION_TYPES:
        raise ValueError(f"未注册的终止类型：{termination_type}")
    archive_policy = str(match.get("archive_policy") or "automatic_readonly").lower()
    if archive_policy not in ARCHIVE_POLICIES:
        raise ValueError(f"未注册的归档策略：{archive_policy}")
    ending_ref = str(match.get("ending_ref") or "")
    label = str(match.get("label") or condition_id)
    reason = str(match.get("reason") or "").strip()
    if not reason:
        reason = f"副本触发了终局条件：{label}"
    idempotency_key = finalization_idempotency_key(
        session_id, condition_id, trigger_revision
    )
    ending = _mapping((endings or {}).get(ending_ref))
    ending_label = str(ending.get("label") or label)
    projection = {
        "ending": ending_label,
        "result": result_label(termination_type),
        "reason": reason,
        "archive_state": archive_state_label(archive_policy, termination_type),
        "readonly": archive_policy in {
            "automatic_readonly",
            "automatic_failed_readonly",
        },
    }
    body = {
        "session_id": session_id,
        "condition_id": condition_id,
        "trigger_revision": trigger_revision,
        "idempotency_key": idempotency_key,
        "priority": int(match.get("priority", 0) or 0),
        "elimination": bool(match.get("elimination")),
        "termination_type": termination_type,
        "ending_ref": ending_ref,
        "archive_policy": archive_policy,
        "reason": reason,
        "created_at": str(created_at or ""),
        "deferred_snapshot": bool(deferred_snapshot),
        "steps": _plan_steps(
            termination_type=termination_type,
            deferred_snapshot=bool(deferred_snapshot),
        ),
        "projection": projection,
    }
    plan = FinalizationPlan(
        session_id=session_id,
        condition_id=condition_id,
        trigger_revision=trigger_revision,
        idempotency_key=idempotency_key,
        plan_hash=hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest(),
        priority=int(match.get("priority", 0) or 0),
        elimination=bool(match.get("elimination")),
        termination_type=termination_type,
        ending_ref=ending_ref,
        archive_policy=archive_policy,
        reason=reason,
        created_at=str(created_at or ""),
        deferred_snapshot=bool(deferred_snapshot),
        steps=tuple(dict(item) for item in _sequence(body.get("steps"))),
        projection=dict(projection),
    )
    return plan.to_dict()


def classify_plan(
    plan: Mapping[str, Any],
    receipts: Sequence[Mapping[str, Any]],
) -> str:
    """并发/重复输入判定。

    - apply：本计划可执行（副本尚无终局回执）；
    - replayed：同一幂等键已存在回执（重试/重复输入，直接返回既有结果）；
    - superseded：副本已被其他终局条件归档（多条件同时命中只执行一次）。
    """

    session_id = str(plan.get("session_id") or "")
    idempotency_key = str(plan.get("idempotency_key") or "")
    for receipt in receipts:
        if str(receipt.get("session_id") or "") != session_id:
            continue
        if str(receipt.get("idempotency_key") or "") == idempotency_key:
            return "replayed"
        return "superseded"
    return "apply"


def merge_concurrent_plans(
    plans: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """合并同一事务/同一扫描窗口内的多个终局计划。

    两个后台任务独立仲裁时结果必须一致：赢家唯一，其余全部抑制。
    返回供宿主事务使用的合并结果（只执行 winner）。
    """

    by_session: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        by_session.setdefault(str(plan.get("session_id") or ""), []).append(dict(plan))
    merged: dict[str, Any] = {"sessions": {}, "winners": []}
    for session_id, session_plans in by_session.items():
        winner = arbitrate_terminal_conditions(
            [
                {
                    "condition_id": plan.get("condition_id"),
                    "label": plan.get("projection", {}).get("ending", ""),
                    "matched": True,
                    "priority": plan.get("priority", 0),
                    "termination_type": plan.get("termination_type"),
                    "ending_ref": plan.get("ending_ref"),
                    "archive_policy": plan.get("archive_policy"),
                    "reason": plan.get("reason"),
                    "elimination": bool(plan.get("elimination")),
                }
                for plan in session_plans
            ]
        )
        decisions: list[dict[str, Any]] = []
        winner_claimed = False
        for plan in session_plans:
            if (
                winner is not None
                and str(plan.get("condition_id") or "")
                == str(winner.get("condition_id") or "")
                and not winner_claimed
            ):
                winner_claimed = True
                decisions.append(
                    {
                        "condition_id": plan.get("condition_id"),
                        "idempotency_key": plan.get("idempotency_key"),
                        "decision": "apply",
                    }
                )
            else:
                decisions.append(
                    {
                        "condition_id": plan.get("condition_id"),
                        "idempotency_key": plan.get("idempotency_key"),
                        "decision": "suppressed",
                    }
                )
        merged["sessions"][session_id] = {
            "winner": winner,
            "decisions": decisions,
        }
        if winner is not None:
            merged["winners"].append(
                {
                    "session_id": session_id,
                    "condition_id": winner.get("condition_id"),
                }
            )
    return merged


def empty_party_blocked(
    condition: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    """终局执行前的空队伍保护兜底（D1_PLAN 18 §6）。"""

    party = _mapping(context.get("party"))
    if int(party.get("member_count", 0) or 0) > 0:
        return False
    paths = collect_condition_paths(condition.get("when"))
    if not any(path in {
        "party.living_count",
        "party.dead_count",
        "party.incapacitated_count",
        "party.members",
    } for path in paths):
        return False
    return not condition_has_member_guard(condition.get("when"))


def project_finalization(
    plan: Mapping[str, Any],
    endings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """玩家可见终局投影（D1_PLAN 18 §13.2）：不暴露条件 id/幂等键。"""

    projection = _mapping(plan.get("projection"))
    ending_ref = str(plan.get("ending_ref") or "")
    ending_label = str(projection.get("ending") or "")
    if endings and ending_ref:
        ending = _mapping(endings.get(ending_ref))
        ending_label = str(ending.get("label") or ending_label)
    return {
        "ending": ending_label,
        "result": str(projection.get("result") or "结果未知"),
        "reason": str(projection.get("reason") or ""),
        "archive_state": str(projection.get("archive_state") or "归档状态未知"),
        "readonly": bool(projection.get("readonly")),
    }


def plan_receipt(
    plan: Mapping[str, Any],
    *,
    status: str = "applied",
) -> dict[str, Any]:
    """生成持久化回执的规范结构（宿主写入唯一约束表）。"""

    return {
        "session_id": str(plan.get("session_id") or ""),
        "condition_id": str(plan.get("condition_id") or ""),
        "trigger_revision": int(plan.get("trigger_revision", 0) or 0),
        "idempotency_key": str(plan.get("idempotency_key") or ""),
        "plan_hash": str(plan.get("plan_hash") or ""),
        "status": str(status or "applied"),
    }


def world_terminal_conditions(
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """读取并归一化世界包终局条件（宿主接线入口）。"""

    return parse_terminal_conditions(world)


__all__ = [
    "archive_state_label",
    "build_finalization_plan",
    "canonical_json",
    "classify_plan",
    "empty_party_blocked",
    "finalization_idempotency_key",
    "merge_concurrent_plans",
    "plan_receipt",
    "project_finalization",
    "result_label",
    "world_terminal_conditions",
]
