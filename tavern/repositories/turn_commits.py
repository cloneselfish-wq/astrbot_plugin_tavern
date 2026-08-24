"""evidence, author-job, outbox and health repositories.

The tables live in Schema 21.  This mixin keeps the new product capabilities
behind one persistence boundary so BOT and Web use the same data lifecycle.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..constants import DATABASE_SCHEMA_VERSION, PLUGIN_VERSION
from ..health_policy import (
    AUTHOR_LEASE_BLOCKED_SECONDS,
    AUTHOR_JOB_THRESHOLD,
    BACKUP_BLOCKED_SECONDS,
    BACKUP_DEGRADED_SECONDS,
    COMPONENT_COPY,
    OPERATION_RECOVERY_FAILURE_LIMIT,
    OUTBOX_THRESHOLD,
    projection_state,
    timed_state,
)
from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
    insert_session_event,
    json_dump,
    json_load,
    new_id,
    retry_backoff_after,
    utc_now,
)
from ..runtime.turn_commit import (
    AuditIntent,
    CausalEvent,
    EventHookIntent,
    KnowledgeEvidenceIntent,
    StateChange,
    StorageSyncIntent,
    TendencyEvidenceIntent,
    TurnCommitPlan,
)


TENDENCY_DIMENSIONS = (
    "risk",
    "cooperation",
    "mercy",
    "curiosity",
    "authority",
    "planning",
)
TENDENCY_LABELS = {
    "risk": ("更常稳妥评估", "更常接受风险"),
    "cooperation": ("更常独立承担", "更常协作分担"),
    "mercy": ("更常采取强硬处置", "更常保留宽恕空间"),
    "curiosity": ("更常优先完成目标", "更常探索未知线索"),
    "authority": ("更常质疑既有安排", "更常遵从公开约定"),
    "planning": ("更常即兴应对", "更常先做规划"),
}
TENDENCY_SOURCE_KINDS = frozenset(
    {"action", "vote", "quest", "host_correction"}
)
AUTHOR_JOB_TYPES = frozenset(
    {"playtest", "semantic_diff", "full_preflight"}
)
AUTHOR_ACTIVE_STATUSES = frozenset(
    {"queued", "leased", "running", "retry_wait"}
)


def _utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str, now: datetime) -> int:
    parsed = _utc(value)
    if parsed is None:
        return 0
    return max(0, int((now - parsed).total_seconds()))


def _safe_summary(value: object, maximum: int = 120) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text[:maximum]


def _health_component(
    code: str,
    state: str,
    summary: str,
    *,
    reason: str = "",
    metrics: Mapping[str, Any] | None = None,
    checked_at: str,
) -> dict[str, Any]:
    copy = COMPONENT_COPY[code]
    return {
        "code": code,
        "label": copy["label"],
        "state": state,
        "summary": summary,
        "reason": reason,
        "automatic_action": copy["automatic_action"],
        "next_action": {
            "label": copy["next_label"],
            "action": f"health.{code}.inspect",
            "enabled": state != "ready",
        },
        "checked_at": checked_at,
        "metrics": dict(metrics or {}),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configured_provider_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    model = payload.get("model")
    model = model if isinstance(model, Mapping) else {}
    candidates: list[object] = [
        model.get("provider_id"),
        *(model.get(f"fallback_provider_{index}_id") for index in range(1, 5)),
        model.get("image_caption_provider_id"),
    ]
    aggregate = model.get("fallback_provider_ids")
    if isinstance(aggregate, Sequence) and not isinstance(
        aggregate,
        (str, bytes),
    ):
        candidates.extend(aggregate)
    result: list[str] = []
    for value in candidates:
        provider_id = str(value or "").strip()
        if provider_id and provider_id not in result:
            result.append(provider_id)
    return tuple(result)


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _tendency_profile(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_last_seq: int,
) -> dict[str, Any]:
    dimensions: dict[str, dict[str, Any]] = {}
    eligible: list[str] = []
    for dimension in TENDENCY_DIMENSIONS:
        evidence = [
            row
            for row in rows
            if str(row.get("dimension") or "") == dimension
            and not str(row.get("revoked_at") or "")
        ]
        positive = 0.0
        negative = 0.0
        event_ids: set[str] = set()
        for row in evidence:
            effective = (
                int(row.get("direction") or 0)
                * int(row.get("weight") or 0)
                * float(row.get("confidence") or 0)
            )
            if effective >= 0:
                positive += effective
            else:
                negative += abs(effective)
            event_ids.add(str(row.get("event_id") or ""))
        total = positive + negative
        score = (positive - negative) / max(total, 1.0)
        coverage = min(len(event_ids) / 6.0, 1.0)
        strength = min(total / 12.0, 1.0)
        consistency = abs(positive - negative) / max(total, 1.0)
        confidence = round(
            coverage * strength * (0.5 + 0.5 * consistency),
            2,
        )
        visible = (
            len(event_ids) >= 3
            and total >= 4
            and confidence >= 0.35
        )
        negative_label, positive_label = TENDENCY_LABELS[dimension]
        if not visible:
            label = "证据仍不足"
        elif score >= 0.35:
            label = positive_label
        elif score <= -0.35:
            label = negative_label
        else:
            label = "近期选择方向不一致"
        if visible:
            eligible.append(dimension)
        dimensions[dimension] = {
            "score": round(score, 4),
            "confidence": confidence,
            "label": label,
            "visible": visible,
            "distinct_events": len(event_ids),
            "evidence_count": len(evidence),
            "total_weight": round(total, 3),
        }
    return {
        "schema": "tavern-player-tendency-profile/1.0.0-rc10",
        "dimensions": dimensions,
        "ai_eligible_dimensions": eligible,
        "last_source_seq": int(source_last_seq),
    }


class TurnCommitRepositoryMixin:
    _TURN_COMMIT_STATE_HANDLERS = {
        "tendency_evidence.visibility": "_turn_commit_tendency_visibility_locked",
        "author_job.create": "_turn_commit_author_job_create_locked",
        "author_job.cancel": "_turn_commit_author_job_action_locked",
        "author_job.retry": "_turn_commit_author_job_action_locked",
    }

    async def execute_turn_commit_plan(
        self,
        plan: TurnCommitPlan,
    ) -> dict[str, Any]:
        """Commit one validated plan through a single SQLite transaction."""

        return await self._run(self._execute_turn_commit_plan, plan)

    def _execute_turn_commit_plan(
        self,
        plan: TurnCommitPlan,
    ) -> dict[str, Any]:
        plan.validate()
        if plan.preview_only:
            return {
                **dict(plan.receipt.result),
                "preview": True,
                "replayed": False,
            }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT session_id, input_hash, status, result_json
                    FROM operation_commits WHERE operation_id=?
                    """,
                    (plan.operation_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["session_id"] or "") != plan.session_id
                        or str(existing["input_hash"] or "") != plan.input_hash
                    ):
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项操作"
                        )
                    if str(existing["status"] or "") != "completed":
                        raise DatabaseConflictError(
                            "该操作仍在恢复处理中，请稍后重试"
                        )
                    result = json_load(existing["result_json"], {})
                    if not isinstance(result, Mapping):
                        raise DatabaseConflictError(
                            "原操作结果损坏，已停止重复执行"
                        )
                    connection.execute("COMMIT")
                    return {**dict(result), "replayed": True}
                self._turn_commit_fault("after_receipt_check", plan)
                self._assert_turn_commit_revision_locked(connection, plan)

                state_results: list[dict[str, Any]] = []
                for change in plan.state_changes:
                    handler_name = self._TURN_COMMIT_STATE_HANDLERS.get(
                        change.kind
                    )
                    if not handler_name:
                        raise ValueError(
                            f"TurnCommitPlan 不支持状态类型：{change.kind}"
                        )
                    handler = getattr(self, handler_name)
                    result = handler(connection, plan, change)
                    state_results.append(
                        dict(result) if isinstance(result, Mapping) else {}
                    )
                self._turn_commit_fault("after_state", plan)

                event_rows: list[dict[str, Any]] = []
                for event in plan.events:
                    actor_ref = event.actor_ref
                    if (
                        plan.operation_type
                        == "tendency.evidence.visibility"
                        and state_results
                    ):
                        actor_ref = str(
                            state_results[-1].get("participant_id")
                            or actor_ref
                        )
                    event_rows.append(
                        insert_session_event(
                            connection,
                            session_id=event.session_id,
                            event_id=event.event_id,
                            type_=event.type,
                            actor_ref=actor_ref,
                            command_id=plan.operation_id,
                            causation_id=event.causation_id,
                            correlation_id=event.correlation_id,
                            payload=event.payload,
                            visibility=event.visibility,
                        )
                    )
                self._turn_commit_fault("after_event", plan)

                for evidence in plan.tendency_evidence:
                    self._insert_tendency_evidence_intent_locked(
                        connection,
                        plan,
                        evidence,
                    )
                for evidence in plan.knowledge_evidence:
                    self._insert_knowledge_evidence_intent_locked(
                        connection,
                        plan,
                        evidence,
                    )
                self._after_turn_commit_events_locked(
                    connection,
                    plan,
                    state_results,
                    event_rows,
                )
                self._turn_commit_fault("after_evidence", plan)

                for intent in plan.storage_outbox:
                    self._enqueue_storage_sync(
                        connection,
                        [intent.session_id],
                        kind=intent.kind,
                        payload=intent.payload,
                    )
                self._turn_commit_fault("after_storage_outbox", plan)

                for intent in plan.delivery_outbox:
                    record = dict(intent.record or intent.payload)
                    record.setdefault("session_id", plan.session_id)
                    record.setdefault("message_type", intent.kind)
                    record.setdefault("audience", intent.audience or "player")
                    record.setdefault("dedupe_key", intent.dedupe_key)
                    record.setdefault("text", intent.text)
                    if intent.projection:
                        record.setdefault(
                            "projection_snapshot",
                            dict(intent.projection),
                        )
                    if isinstance(intent.target, Mapping):
                        record.setdefault(
                            "target_snapshot",
                            dict(intent.target),
                        )
                    self._create_delivery_locked(connection, record)
                self._turn_commit_fault("after_delivery_outbox", plan)

                for intent in plan.event_outbox:
                    self._insert_event_outbox_intent_locked(
                        connection,
                        plan,
                        intent,
                    )
                self._turn_commit_fault("after_event_outbox", plan)

                for audit in plan.audit_entries:
                    self._insert_audit(
                        connection,
                        plan.session_id,
                        plan.actor_ref,
                        audit.action,
                        audit.target,
                        {
                            **dict(audit.detail),
                            "operation_type": plan.operation_type,
                        },
                    )
                self._turn_commit_fault("after_audit", plan)

                result = self._turn_commit_public_result_locked(
                    connection,
                    plan,
                    state_results,
                    event_rows,
                )
                self._turn_commit_fault("before_receipt_complete", plan)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        plan.operation_id,
                        plan.session_id,
                        plan.input_hash,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._turn_commit_fault("before_commit", plan)
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _turn_commit_fault(
        self,
        stage: str,
        plan: TurnCommitPlan,
    ) -> None:
        injector = getattr(self, "turn_commit_fault_injector", None)
        if callable(injector):
            injector(str(stage), plan)

    def _assert_turn_commit_revision_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
    ) -> None:
        if plan.operation_type == "author.job.create":
            # A caller that omits the revision is using the legacy list-scoped
            # contract.  Semantic world-scoped creation instead supplies the
            # selected world's revision, which must be checked inside this same
            # transaction before the job snapshot is inserted.
            if plan.expected_revision is None:
                return
            payload = (
                dict(plan.state_changes[0].payload)
                if plan.state_changes
                else {}
            )
            world_ref = str(payload.get("world_ref") or "")
            row = connection.execute(
                """
                SELECT revision FROM worlds
                WHERE id=? OR slug=?
                """,
                (world_ref, world_ref),
            ).fetchone()
            if row is None:
                raise DatabaseNotFoundError("作者任务对应世界不存在")
            current = int(row["revision"] or 0)
            if current != int(plan.expected_revision):
                raise DatabaseConflictError(
                    "世界草稿已更新，请刷新后重新提交"
                )
            return
        if plan.expected_revision is None:
            return
        if plan.session_id:
            row = connection.execute(
                "SELECT revision FROM sessions WHERE id=?",
                (plan.session_id,),
            ).fetchone()
            if row is None:
                raise DatabaseNotFoundError("会话不存在")
            current = int(row["revision"] or 0)
            label = "副本"
        elif plan.operation_type in {"author.job.cancel", "author.job.retry"}:
            payload = (
                dict(plan.state_changes[0].payload)
                if plan.state_changes
                else {}
            )
            row = self._author_job_by_public_ref_locked(
                connection,
                str(payload.get("job_ref") or ""),
            )
            if row is None:
                raise DatabaseNotFoundError("作者任务不存在")
            current = int(row["revision"] or 0)
            label = "目标作者任务"
        else:
            raise ValueError("当前写计划没有可校验的状态版本")
        if current != int(plan.expected_revision):
            raise DatabaseConflictError(
                f"{label}已更新，请刷新后重新提交"
            )

    def _after_turn_commit_events_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        state_results: Sequence[Mapping[str, Any]],
        event_rows: Sequence[Mapping[str, Any]],
    ) -> None:
        event_by_id = {
            str(row.get("event_id") or ""): dict(row)
            for row in event_rows
            if str(row.get("event_id") or "")
        }
        tendency_profiles: dict[str, dict[str, Any]] = {}
        for evidence in plan.tendency_evidence:
            tendency_profiles[evidence.participant_id] = (
                self._rebuild_tendency_profile_locked(
                    connection,
                    plan.session_id,
                    evidence.participant_id,
                )
            )
        knowledge_profiles: dict[str, dict[str, Any]] = {}
        for evidence in plan.knowledge_evidence:
            knowledge_profiles[evidence.character_id] = (
                self._rebuild_npc_knowledge_locked(
                    connection,
                    evidence.character_id,
                )
            )
        tendency = next(
            (
                item
                for item in state_results
                if item.get("kind") == "tendency_evidence.visibility"
            ),
            None,
        )
        tendency_event_seqs = [
            int(event_by_id[evidence.event_id].get("seq") or 0)
            for evidence in plan.tendency_evidence
            if evidence.event_id in event_by_id
        ]
        if tendency is not None and event_rows:
            tendency_event_seqs.append(int(event_rows[-1].get("seq") or 0))
            participant_id = str(tendency.get("participant_id") or "")
            if participant_id in tendency_profiles and isinstance(
                tendency,
                dict,
            ):
                tendency["profile"] = tendency_profiles[participant_id]
        if tendency_event_seqs:
            profile = (
                next(iter(tendency_profiles.values()))
                if tendency_profiles
                else (
                    tendency.get("profile")
                    if isinstance(tendency, Mapping)
                    else {}
                )
            )
            profile = profile if isinstance(profile, Mapping) else {}
            self._upsert_turn_commit_checkpoint_locked(
                connection,
                plan.session_id,
                "player_tendency",
                max(tendency_event_seqs),
                {
                    "mode": "turn_commit",
                    "profile_last_source_seq": int(
                        profile.get("last_source_seq") or 0
                    ),
                },
            )
        knowledge_event_seqs = [
            int(event_by_id[evidence.event_id].get("seq") or 0)
            for evidence in plan.knowledge_evidence
            if evidence.event_id in event_by_id
        ]
        if knowledge_event_seqs:
            self._upsert_turn_commit_checkpoint_locked(
                connection,
                plan.session_id,
                "npc_knowledge",
                max(knowledge_event_seqs),
                {
                    "mode": "turn_commit",
                    "characters_rebuilt": len(knowledge_profiles),
                },
            )

    @staticmethod
    def _upsert_turn_commit_checkpoint_locked(
        connection: Any,
        session_id: str,
        projection_name: str,
        last_seq: int,
        payload: Mapping[str, Any],
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO projection_checkpoints(
                session_id, projection_name, last_seq,
                payload_json, revision, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(session_id, projection_name) DO UPDATE SET
                last_seq=MAX(
                    projection_checkpoints.last_seq,
                    excluded.last_seq
                ),
                payload_json=excluded.payload_json,
                revision=projection_checkpoints.revision + 1,
                updated_at=excluded.updated_at
            """,
            (
                session_id,
                projection_name,
                int(last_seq),
                json_dump(dict(payload)),
                now,
            ),
        )

    def _turn_commit_public_result_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        state_results: Sequence[Mapping[str, Any]],
        event_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        result = dict(plan.receipt.result)
        result.update(dict(plan.public_projection_seed))
        state = dict(state_results[-1]) if state_results else {}
        if plan.operation_type.startswith("author.job."):
            result = dict(state.get("job") or result)
            result["replayed"] = bool(state.get("state_replayed"))
            result["list_revision"] = self._author_jobs_revision_locked(
                connection
            )
            return result
        if plan.operation_type == "tendency.evidence.visibility":
            event = dict(event_rows[-1]) if event_rows else {}
            row = connection.execute(
                "SELECT revision FROM sessions WHERE id=?",
                (plan.session_id,),
            ).fetchone()
            return {
                **result,
                "restored": bool(state.get("restored")),
                "summary": str(state.get("summary") or ""),
                "profile": dict(state.get("profile") or {}),
                "event_seq": int(event.get("seq") or 0),
                "committed_revision": int(row["revision"] if row else 0),
                "replayed": False,
            }
        result["replayed"] = False
        return result


__all__ = ["TurnCommitRepositoryMixin"]
