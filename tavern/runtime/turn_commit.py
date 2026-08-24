"""Validated atomic commit-plan contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from .contracts import DeliveryIntent


def _mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def canonical_input_hash(
    operation_type: str,
    payload: Mapping[str, Any],
) -> str:
    """Return the stable digest used by ``operation_commits``.

    The operation type is part of the digest so one transport idempotency key
    cannot be replayed across two different write authorities.
    """

    canonical = json.dumps(
        {
            "operation_type": str(operation_type or "").strip(),
            "payload": dict(payload or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def stable_event_id(operation_id: str, suffix: str = "event") -> str:
    """Build a deterministic event id without exposing the request key."""

    digest = hashlib.sha256(
        f"{str(operation_id)}:{str(suffix)}".encode("utf-8")
    ).hexdigest()
    return f"event:turn_commit:{digest[:32]}"


@dataclass(frozen=True, slots=True)
class StateChange:
    kind: str
    target_ref: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _mapping(self.payload))


@dataclass(frozen=True, slots=True)
class CausalEvent:
    event_id: str
    session_id: str
    type: str
    actor_ref: str
    causation_id: str
    correlation_id: str
    visibility: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _mapping(self.payload))


@dataclass(frozen=True, slots=True)
class ReceiptWrite:
    operation_id: str
    operation_type: str
    input_hash: str
    result: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "result", _mapping(self.result))


@dataclass(frozen=True, slots=True)
class StorageSyncIntent:
    session_id: str
    kind: str = "sync"
    payload: Mapping[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _mapping(self.payload))


@dataclass(frozen=True, slots=True)
class EventHookIntent:
    topic: str
    payload: Mapping[str, Any]
    dedupe_key: str
    audience: str = "internal"
    event_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _mapping(self.payload))


@dataclass(frozen=True, slots=True)
class TendencyEvidenceIntent:
    participant_id: str
    event_id: str
    dimension: str
    direction: int
    weight: int
    confidence: float
    rationale: str
    action_summary: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceIntent:
    character_id: str
    event_id: str
    fact_ref: str
    belief_kind: str
    source_kind: str
    confidence: float
    visibility: str
    fact_text: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True)
class AuditIntent:
    action: str
    target: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", _mapping(self.detail))


@dataclass(frozen=True, slots=True)
class TurnCommitPlan:
    operation_id: str
    idempotency_key: str
    input_hash: str
    session_id: str
    operation_type: str
    actor_ref: str
    expected_revision: int | None
    state_changes: tuple[StateChange, ...]
    events: tuple[CausalEvent, ...]
    receipt: ReceiptWrite
    storage_outbox: tuple[StorageSyncIntent, ...] = ()
    delivery_outbox: tuple[DeliveryIntent, ...] = ()
    event_outbox: tuple[EventHookIntent, ...] = ()
    tendency_evidence: tuple[TendencyEvidenceIntent, ...] = ()
    knowledge_evidence: tuple[KnowledgeEvidenceIntent, ...] = ()
    audit_entries: tuple[AuditIntent, ...] = ()
    public_projection_seed: Mapping[str, Any] = field(default_factory=dict)
    preview_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "public_projection_seed",
            _mapping(self.public_projection_seed),
        )

    def validate(self) -> None:
        if not self.operation_id or not self.idempotency_key or not self.input_hash:
            raise ValueError("写计划缺少 operation_id、idempotency_key 或 input_hash")
        if not self.operation_type:
            raise ValueError("写计划缺少 operation_type")
        if self.expected_revision is not None and self.expected_revision < 0:
            raise ValueError("expected_revision 不能小于 0")
        if self.receipt.operation_id != self.operation_id:
            raise ValueError("receipt.operation_id 与计划不一致")
        if self.receipt.input_hash != self.input_hash:
            raise ValueError("receipt.input_hash 与计划不一致")
        if self.receipt.operation_type != self.operation_type:
            raise ValueError("receipt.operation_type 与计划不一致")
        if self.preview_only and (
            self.state_changes
            or self.events
            or self.storage_outbox
            or self.delivery_outbox
            or self.event_outbox
            or self.tendency_evidence
            or self.knowledge_evidence
            or self.audit_entries
        ):
            raise ValueError("preview_only 计划不得包含持久化副作用")

        for change in self.state_changes:
            if not change.kind or not change.target_ref:
                raise ValueError("state change 缺少 kind 或 target_ref")

        event_ids: set[str] = set()
        for event in self.events:
            if event.session_id != self.session_id:
                raise ValueError("事件 session_id 与计划不一致")
            if not all(
                (
                    event.event_id,
                    event.type,
                    event.causation_id,
                    event.correlation_id,
                    event.visibility,
                )
            ):
                raise ValueError("事件缺少因果信封字段")
            if event.event_id in event_ids:
                raise ValueError(f"计划内事件 ID 重复：{event.event_id}")
            event_ids.add(event.event_id)

        keys: set[str] = set()
        for intent in (
            *self.storage_outbox,
            *self.delivery_outbox,
            *self.event_outbox,
        ):
            key = str(getattr(intent, "dedupe_key", "") or "")
            if not key:
                raise ValueError("outbox intent 缺少 dedupe_key")
            if key in keys:
                raise ValueError(f"计划内 dedupe_key 重复：{key}")
            keys.add(key)

        for intent in self.storage_outbox:
            if intent.session_id != self.session_id:
                raise ValueError("storage intent session_id 与计划不一致")
        for evidence in self.tendency_evidence:
            if evidence.event_id not in event_ids:
                raise ValueError(
                    "计划内 evidence 必须引用同一计划中的事件"
                )
            if (
                not evidence.participant_id
                or not evidence.dimension
                or evidence.direction not in {-1, 1}
                or not 1 <= evidence.weight <= 5
                or not 0 <= evidence.confidence <= 1
            ):
                raise ValueError("倾向 evidence 字段无效")
        for evidence in self.knowledge_evidence:
            if evidence.event_id not in event_ids:
                raise ValueError(
                    "计划内 evidence 必须引用同一计划中的事件"
                )
            if (
                not evidence.character_id
                or not evidence.fact_ref
                or evidence.belief_kind not in {"known", "misconception"}
                or not 0 <= evidence.confidence <= 1
            ):
                raise ValueError("知识 evidence 字段无效")
        for intent in self.event_outbox:
            if not intent.topic:
                raise ValueError("event outbox intent 缺少 topic")
        for audit in self.audit_entries:
            if not audit.action:
                raise ValueError("audit intent 缺少 action")


class TurnCommitRepository(Protocol):
    async def execute_turn_commit_plan(
        self,
        plan: TurnCommitPlan,
    ) -> dict[str, Any]: ...


async def commit_plan(
    repository: TurnCommitRepository,
    plan: TurnCommitPlan,
) -> dict[str, Any]:
    """Validate and submit one plan through the repository transaction adapter."""

    plan.validate()
    executor = getattr(repository, "execute_turn_commit_plan", None)
    if callable(executor):
        result = await executor(plan)
    else:
        # Test doubles and third-party adapters may still expose only the
        # A repository operation may not expose the transaction hook in tests.
        # Production TavernDatabase always uses
        # the atomic executor above.
        result = await _legacy_commit_adapter(repository, plan)
    if not isinstance(result, Mapping):
        raise TypeError("TurnCommitPlan 执行器必须返回公开结果映射")
    return dict(result)


async def _legacy_commit_adapter(
    repository: Any,
    plan: TurnCommitPlan,
) -> Mapping[str, Any]:
    if plan.operation_type == "tendency.evidence.visibility":
        change = plan.state_changes[0]
        payload = dict(change.payload)
        return await repository.set_tendency_evidence_visibility(
            plan.session_id,
            str(payload.get("user_id") or ""),
            int(payload.get("number") or 0),
            restore=bool(payload.get("restore")),
            operation_id=plan.operation_id,
        )
    raise TypeError("仓储未实现 TurnCommitPlan 执行器")


def build_tendency_visibility_plan(
    *,
    operation_id: str,
    session_id: str,
    user_id: str,
    number: int,
    restore: bool,
    actor_ref: str,
    correlation_id: str,
    expected_revision: int | None,
) -> TurnCommitPlan:
    if not str(operation_id).strip():
        raise ValueError("倾向依据操作缺少防重复凭据")
    if not str(session_id).strip() or not str(user_id).strip():
        raise ValueError("倾向依据操作缺少副本或用户")
    if int(number) < 1:
        raise ValueError("依据序号必须大于 0")
    operation_type = "tendency.evidence.visibility"
    request = {
        "session_id": str(session_id),
        "user_id": str(user_id),
        "number": int(number),
        "restore": bool(restore),
    }
    input_hash = canonical_input_hash(operation_type, request)
    event_id = stable_event_id(operation_id, "visibility")
    action = "restore" if restore else "ignore"
    event = CausalEvent(
        event_id=event_id,
        session_id=str(session_id),
        type=(
            "event:tendency_evidence_restored"
            if restore
            else "event:tendency_evidence_ignored"
        ),
        actor_ref=str(actor_ref),
        causation_id=str(operation_id),
        correlation_id=str(correlation_id or operation_id),
        visibility="host",
        payload={
            "schema": "tavern-causal-event/1.0.0-rc10",
            "subject_refs": ["player_tendency"],
            "changes": [
                {
                    "domain": "tendency",
                    "kind": (
                        "evidence_restored"
                        if restore
                        else "evidence_ignored"
                    ),
                    "visibility": "host",
                }
            ],
            "summary": {
                "text": (
                    "玩家恢复了一条本人倾向依据。"
                    if restore
                    else "玩家忽略了一条本人倾向依据。"
                )
            },
        },
    )
    return TurnCommitPlan(
        operation_id=str(operation_id),
        idempotency_key=str(operation_id),
        input_hash=input_hash,
        session_id=str(session_id),
        operation_type=operation_type,
        actor_ref=str(actor_ref),
        expected_revision=expected_revision,
        state_changes=(
            StateChange(
                "tendency_evidence.visibility",
                f"owner:{user_id}:evidence:{int(number)}",
                {
                    "user_id": str(user_id),
                    "number": int(number),
                    "restore": bool(restore),
                },
            ),
        ),
        events=(event,),
        receipt=ReceiptWrite(
            str(operation_id),
            operation_type,
            input_hash,
        ),
        storage_outbox=(
            StorageSyncIntent(
                session_id=str(session_id),
                kind="sync",
                dedupe_key=f"storage:{operation_id}",
            ),
        ),
        event_outbox=(
            EventHookIntent(
                topic="tendency.evidence_changed",
                payload={
                    "type": "tendency",
                    "action": action,
                    "session_id": str(session_id),
                },
                dedupe_key=f"tendency:{operation_id}",
                event_id=event_id,
            ),
        ),
        audit_entries=(
            AuditIntent(
                action=f"tendency.evidence.{action}",
                detail={"evidence_number": int(number)},
            ),
        ),
    )


def build_author_job_create_plan(
    *,
    operation_id: str,
    job_type: str,
    world_ref: str,
    request_payload: Mapping[str, Any],
    actor_ref: str,
    max_attempts: int,
    expected_revision: int | None,
) -> TurnCommitPlan:
    if not str(operation_id).strip():
        raise ValueError("作者任务创建缺少防重复凭据")
    if not str(actor_ref).strip():
        raise PermissionError("作者任务缺少创建者")
    operation_type = "author.job.create"
    request = {
        "job_type": str(job_type),
        "world_ref": str(world_ref),
        "request": dict(request_payload),
        "max_attempts": int(max_attempts),
    }
    input_hash = canonical_input_hash(operation_type, request)
    return TurnCommitPlan(
        operation_id=str(operation_id),
        idempotency_key=str(operation_id),
        input_hash=input_hash,
        session_id="",
        operation_type=operation_type,
        actor_ref=str(actor_ref),
        expected_revision=expected_revision,
        state_changes=(
            StateChange(
                "author_job.create",
                "author_jobs",
                {
                    **request,
                    "created_by": str(actor_ref),
                },
            ),
        ),
        events=(),
        receipt=ReceiptWrite(
            str(operation_id),
            operation_type,
            input_hash,
        ),
        audit_entries=(
            AuditIntent(
                action=operation_type,
                detail={
                    "job_type": str(job_type),
                    "world_selected": bool(str(world_ref)),
                },
            ),
        ),
    )


def build_author_job_action_plan(
    *,
    operation_id: str,
    job_ref: str,
    action: str,
    actor_ref: str,
    expected_revision: int | None,
) -> TurnCommitPlan:
    if not str(operation_id).strip():
        raise ValueError("作者任务操作缺少防重复凭据")
    job_ref = str(job_ref or "").strip()
    if not job_ref.startswith("public:author-job:"):
        raise ValueError("作者任务引用无效")
    action = str(action)
    if action not in {"cancel", "retry"}:
        raise ValueError("作者任务动作必须是 cancel 或 retry")
    if expected_revision is None or int(expected_revision) < 1:
        raise ValueError("作者任务动作缺少有效的任务 revision")
    operation_type = f"author.job.{action}"
    request = {
        "job_ref": job_ref,
        "action": action,
        "expected_revision": int(expected_revision),
    }
    input_hash = canonical_input_hash(operation_type, request)
    return TurnCommitPlan(
        operation_id=str(operation_id),
        idempotency_key=str(operation_id),
        input_hash=input_hash,
        session_id="",
        operation_type=operation_type,
        actor_ref=str(actor_ref),
        expected_revision=expected_revision,
        state_changes=(
            StateChange(
                f"author_job.{action}",
                job_ref,
                {"job_ref": job_ref},
            ),
        ),
        events=(),
        receipt=ReceiptWrite(
            str(operation_id),
            operation_type,
            input_hash,
        ),
        audit_entries=(
            AuditIntent(
                action=operation_type,
                detail={"job_ref": job_ref},
            ),
        ),
    )


__all__ = [
    "AuditIntent",
    "CausalEvent",
    "EventHookIntent",
    "KnowledgeEvidenceIntent",
    "ReceiptWrite",
    "StateChange",
    "StorageSyncIntent",
    "TendencyEvidenceIntent",
    "TurnCommitPlan",
    "TurnCommitRepository",
    "build_author_job_action_plan",
    "build_author_job_create_plan",
    "build_tendency_visibility_plan",
    "canonical_input_hash",
    "commit_plan",
    "stable_event_id",
]
