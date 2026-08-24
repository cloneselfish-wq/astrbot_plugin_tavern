"""D1 角色命运/终局服务的稳定数据结构（纯数据，无 I/O）。

服务函数以普通 dict 作为输入/输出（便于持久化与跨模块接线），
本模块提供对应的类型化视图与 to_dict 转换。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FateRecord:
    """一次角色命运状态转换记录（D1_PLAN 18 §3.2）。"""

    actor_ref: str
    from_state: str
    to_state: str
    reason: str
    source: str
    reversible: bool = False
    opens_rescue_window: bool = False
    rescue_window_kind: str = ""
    consumed_protection_resource: str = ""
    sequence: int = 0
    created_at: str = ""
    event_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_ref": self.actor_ref,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "source": self.source,
            "reversible": self.reversible,
            "opens_rescue_window": self.opens_rescue_window,
            "rescue_window_kind": self.rescue_window_kind,
            "consumed_protection_resource": self.consumed_protection_resource,
            "sequence": self.sequence,
            "created_at": self.created_at,
            "event_ref": self.event_ref,
        }


@dataclass(frozen=True)
class RescueWindow:
    """救援窗口（创建/完成/过期均幂等，D1_PLAN 18 §5）。"""

    actor_ref: str
    kind: str
    status: str  # open / succeeded / failed
    opened_at: str
    expires_on: str
    allowed_rescue_commands: tuple[str, ...]
    success_transition: tuple[str, str]
    failure_transition: tuple[str, str]
    command_labels: dict[str, str]
    completed_at: str = ""
    outcome: str = ""  # succeeded / failed / expired
    command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor_ref": self.actor_ref,
            "kind": self.kind,
            "status": self.status,
            "opened_at": self.opened_at,
            "expires_on": self.expires_on,
            "allowed_rescue_commands": list(self.allowed_rescue_commands),
            "success_transition": list(self.success_transition),
            "failure_transition": list(self.failure_transition),
            "command_labels": dict(self.command_labels),
            "completed_at": self.completed_at,
            "outcome": self.outcome,
            "command": self.command,
        }


@dataclass(frozen=True)
class PartySummary:
    """队伍聚合结果（D1_PLAN 18 §6）。"""

    member_count: int
    living_count: int
    dead_count: int
    incapacitated_count: int
    members: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_count": self.member_count,
            "living_count": self.living_count,
            "dead_count": self.dead_count,
            "incapacitated_count": self.incapacitated_count,
            "members": [dict(item) for item in self.members],
            "excluded": [dict(item) for item in self.excluded],
        }


@dataclass(frozen=True)
class TerminalMatch:
    """一次终局条件求值结果（含仲裁所需的全部稳定字段）。"""

    condition_id: str
    label: str
    matched: bool
    priority: int
    termination_type: str
    ending_ref: str
    archive_policy: str
    reason: str
    elimination: bool = False
    blocked_reason: str = ""
    reads: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "label": self.label,
            "matched": self.matched,
            "priority": self.priority,
            "termination_type": self.termination_type,
            "ending_ref": self.ending_ref,
            "archive_policy": self.archive_policy,
            "reason": self.reason,
            "elimination": self.elimination,
            "blocked_reason": self.blocked_reason,
            "reads": [dict(item) for item in self.reads],
        }


@dataclass(frozen=True)
class FinalizationPlan:
    """自动终局计划（幂等键 + 确定哈希 + 玩家投影，D1_PLAN 18 §10-11）。"""

    session_id: str
    condition_id: str
    trigger_revision: int
    idempotency_key: str
    plan_hash: str
    priority: int
    elimination: bool
    termination_type: str
    ending_ref: str
    archive_policy: str
    reason: str
    created_at: str
    deferred_snapshot: bool
    steps: tuple[dict[str, Any], ...]
    projection: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "condition_id": self.condition_id,
            "trigger_revision": self.trigger_revision,
            "idempotency_key": self.idempotency_key,
            "plan_hash": self.plan_hash,
            "priority": self.priority,
            "elimination": self.elimination,
            "termination_type": self.termination_type,
            "ending_ref": self.ending_ref,
            "archive_policy": self.archive_policy,
            "reason": self.reason,
            "created_at": self.created_at,
            "deferred_snapshot": self.deferred_snapshot,
            "steps": [dict(item) for item in self.steps],
            "projection": dict(self.projection),
        }


__all__ = [
    "FateRecord",
    "FinalizationPlan",
    "PartySummary",
    "RescueWindow",
    "TerminalMatch",
]
