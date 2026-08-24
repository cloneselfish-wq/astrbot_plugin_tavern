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


class TendencyRepositoryMixin:
    def _turn_commit_tendency_visibility_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        change: StateChange,
    ) -> dict[str, Any]:
        payload = dict(change.payload)
        user_id = str(payload.get("user_id") or "")
        number = int(payload.get("number") or 0)
        restore = bool(payload.get("restore"))
        if not plan.session_id or not user_id or number < 1:
            raise ValueError("倾向依据操作缺少副本、用户或有效序号")
        self._assert_session_writable(connection, plan.session_id)
        participant = connection.execute(
            """
            SELECT id FROM participants
            WHERE session_id=? AND group_user_id=?
            """,
            (plan.session_id, user_id),
        ).fetchone()
        if participant is None:
            raise DatabaseNotFoundError("你尚未加入当前副本")
        revoked_clause = "e.revoked_at<>''" if restore else "e.revoked_at=''"
        rows = connection.execute(
            f"""
            SELECT e.* FROM player_tendency_evidence e
            JOIN session_events se ON se.event_id=e.event_id
            WHERE e.participant_id=? AND {revoked_clause}
            ORDER BY se.seq DESC, e.created_at DESC, e.id
            """,
            (participant["id"],),
        ).fetchall()
        if number > len(rows):
            raise DatabaseNotFoundError(
                "当前页没有这条倾向依据，请重新查看列表"
            )
        row = rows[number - 1]
        now = utc_now()
        if restore:
            if str(row["revoke_reason"] or "") != "player_ignored":
                raise InvalidTransitionError(
                    "该依据不是由本人忽略，不能手动恢复"
                )
            connection.execute(
                """
                UPDATE player_tendency_evidence SET
                    revoked_at='', revoked_by='', revoke_reason=''
                WHERE id=?
                """,
                (row["id"],),
            )
        else:
            connection.execute(
                """
                UPDATE player_tendency_evidence SET
                    revoked_at=?, revoked_by=?, revoke_reason='player_ignored'
                WHERE id=?
                """,
                (now, user_id, row["id"]),
            )
        profile = self._rebuild_tendency_profile_locked(
            connection,
            plan.session_id,
            str(participant["id"]),
        )
        connection.execute(
            """
            UPDATE sessions SET revision=revision + 1, updated_at=?
            WHERE id=?
            """,
            (now, plan.session_id),
        )
        return {
            "kind": change.kind,
            "restored": restore,
            "summary": str(row["action_summary"] or ""),
            "profile": profile,
            "participant_id": str(participant["id"]),
        }

    def _insert_tendency_evidence_intent_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        evidence: TendencyEvidenceIntent,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO player_tendency_evidence(
                id, session_id, participant_id, event_id, dimension,
                direction, weight, confidence, rationale, action_summary,
                source_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("tendency_evidence"),
                plan.session_id,
                evidence.participant_id,
                evidence.event_id,
                evidence.dimension,
                evidence.direction,
                evidence.weight,
                evidence.confidence,
                evidence.rationale,
                evidence.action_summary,
                evidence.source_kind,
                utc_now(),
            ),
        )

    async def player_tendency_view(
        self,
        session_id: str,
        user_id: str,
        *,
        include_revoked: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._player_tendency_view,
            str(session_id),
            str(user_id),
            bool(include_revoked),
        )

    def _player_tendency_view(
        self,
        session_id: str,
        user_id: str,
        include_revoked: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            participant = connection.execute(
                """
                SELECT p.id, s.revision AS session_revision
                FROM participants p
                JOIN sessions s ON s.id=p.session_id
                WHERE p.session_id=? AND p.group_user_id=?
                """,
                (session_id, user_id),
            ).fetchone()
            if participant is None:
                raise DatabaseNotFoundError("你尚未加入当前副本")
            participant_id = str(participant["id"])
            profile_row = connection.execute(
                """
                SELECT * FROM player_tendency_profiles
                WHERE participant_id=?
                """,
                (participant_id,),
            ).fetchone()
            if profile_row is None:
                profile = self._rebuild_tendency_profile_locked(
                    connection,
                    session_id,
                    participant_id,
                )
            else:
                profile = json_load(profile_row["summary_json"], {})
                profile["revision"] = int(profile_row["revision"] or 1)
            clauses = ["e.participant_id=?"]
            if not include_revoked:
                clauses.append("e.revoked_at=''")
            rows = connection.execute(
                f"""
                SELECT e.*, se.seq
                FROM player_tendency_evidence e
                JOIN session_events se ON se.event_id=e.event_id
                WHERE {' AND '.join(clauses)}
                ORDER BY se.seq DESC, e.created_at DESC, e.id
                """,
                (participant_id,),
            ).fetchall()
        active: list[dict[str, Any]] = []
        revoked: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "summary": str(row["action_summary"] or ""),
                "rationale": str(row["rationale"] or ""),
                "source_kind": str(row["source_kind"] or ""),
                "created_at": str(row["created_at"] or ""),
                "restorable": (
                    str(row["revoke_reason"] or "") == "player_ignored"
                ),
            }
            target = revoked if str(row["revoked_at"] or "") else active
            item["number"] = len(target) + 1
            target.append(item)
        dimensions = [
            {
                "label": str(value.get("label") or ""),
                "visible": bool(value.get("visible")),
                "confidence_label": (
                    "较稳定"
                    if float(value.get("confidence") or 0) >= 0.6
                    else "初步"
                ),
            }
            for value in dict(profile.get("dimensions") or {}).values()
            if bool(value.get("visible"))
        ]
        return {
            "schema": "tavern-player-tendency-view/1.0.0-rc10",
            "observations": dimensions,
            "insufficient": not dimensions,
            "active_evidence": active,
            "revoked_evidence": revoked,
            "privacy_notice": (
                "这些只是当前副本中已提交选择形成的行为迹象，"
                "不会替你决定角色，也不会用于权限、惩罚或奖励倍率。"
            ),
            "source_last_seq": int(profile.get("last_source_seq") or 0),
            "revision": int(profile.get("revision") or 1),
            "session_revision": int(participant["session_revision"] or 0),
        }

    async def set_tendency_evidence_visibility(
        self,
        session_id: str,
        user_id: str,
        number: int,
        *,
        restore: bool,
        operation_id: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        from ..runtime.turn_commit import (
            build_tendency_visibility_plan,
        )

        operation_id = str(operation_id).strip() or new_id(
            "tendency_action"
        )
        plan = build_tendency_visibility_plan(
            operation_id=operation_id,
            session_id=str(session_id),
            user_id=str(user_id),
            number=int(number),
            restore=bool(restore),
            actor_ref=str(user_id),
            correlation_id=operation_id,
            expected_revision=expected_revision,
        )
        return await self.execute_turn_commit_plan(plan)

    def _set_tendency_evidence_visibility(
        self,
        session_id: str,
        user_id: str,
        number: int,
        restore: bool,
        operation_id: str,
    ) -> dict[str, Any]:
        if number < 1:
            raise ValueError("依据序号必须大于 0")
        operation_id = operation_id.strip() or new_id("tendency_action")
        request_payload = {
            "session_id": session_id,
            "user_id": user_id,
            "number": number,
            "restore": restore,
        }
        input_hash = hashlib.sha256(
            json_dump(request_payload).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT session_id, input_hash, result_json
                    FROM operation_commits WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["session_id"] or "") != session_id
                        or str(existing["input_hash"] or "") != input_hash
                    ):
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项倾向操作"
                        )
                    result = json_load(existing["result_json"], {})
                    if not isinstance(result, Mapping):
                        result = {}
                    replay = {**dict(result), "replayed": True}
                    connection.execute("COMMIT")
                    return replay
                self._assert_session_writable(connection, session_id)
                participant = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id=? AND group_user_id=?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if participant is None:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                revoked_clause = "e.revoked_at<>''" if restore else "e.revoked_at=''"
                rows = connection.execute(
                    f"""
                    SELECT e.* FROM player_tendency_evidence e
                    JOIN session_events se ON se.event_id=e.event_id
                    WHERE e.participant_id=? AND {revoked_clause}
                    ORDER BY se.seq DESC, e.created_at DESC, e.id
                    """,
                    (participant["id"],),
                ).fetchall()
                if number > len(rows):
                    raise DatabaseNotFoundError(
                        "当前页没有这条倾向依据，请重新查看列表"
                    )
                row = rows[number - 1]
                if restore:
                    if str(row["revoke_reason"] or "") != "player_ignored":
                        raise InvalidTransitionError(
                            "该依据不是由本人忽略，不能手动恢复"
                        )
                    connection.execute(
                        """
                        UPDATE player_tendency_evidence SET
                            revoked_at='', revoked_by='', revoke_reason=''
                        WHERE id=?
                        """,
                        (row["id"],),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE player_tendency_evidence SET
                            revoked_at=?, revoked_by=?, revoke_reason='player_ignored'
                        WHERE id=?
                        """,
                        (utc_now(), user_id, row["id"]),
                    )
                profile = self._rebuild_tendency_profile_locked(
                    connection,
                    session_id,
                    str(participant["id"]),
                )
                now = utc_now()
                event_type = (
                    "event:tendency_evidence_restored"
                    if restore
                    else "event:tendency_evidence_ignored"
                )
                event = insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=new_id("event"),
                    type_=event_type,
                    actor_ref=str(participant["id"]),
                    command_id=operation_id,
                    causation_id=operation_id,
                    correlation_id=operation_id,
                    payload={
                        "schema": "tavern-causal-event/1.0.0-rc10",
                        "operation_id": operation_id,
                        "subject_refs": ["player_tendency"],
                        "participants": [
                            {
                                "role": "actor",
                                "ref": str(participant["id"]),
                            }
                        ],
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
                    visibility="host",
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        session_id, projection_name, last_seq,
                        payload_json, revision, updated_at
                    ) VALUES (?, 'player_tendency', ?, ?, 1, ?)
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
                        int(event["seq"] or 0),
                        json_dump(
                            {
                                "mode": "owner_visibility_change",
                                "profile_last_source_seq": int(
                                    profile.get("last_source_seq") or 0
                                ),
                            }
                        ),
                        now,
                    ),
                )
                result = {
                    "restored": restore,
                    "summary": str(row["action_summary"] or ""),
                    "profile": profile,
                    "event_seq": int(event["seq"] or 0),
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO event_outbox(
                        id, session_id, event_id, topic, payload_json,
                        audience, dedupe_key, status, max_attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'tendency.evidence_changed', ?, 'internal',
                              ?, 'pending', 8, ?, ?)
                    """,
                    (
                        new_id("event_outbox"),
                        session_id,
                        str(event["event_id"]),
                        json_dump(
                            {
                                "type": "tendency",
                                "action": (
                                    "restore" if restore else "ignore"
                                ),
                                "session_id": session_id,
                                "latest_seq": int(event["seq"] or 0),
                            }
                        ),
                        f"tendency:{operation_id}",
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        input_hash,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    (
                        "tendency.evidence.restore"
                        if restore
                        else "tendency.evidence.ignore"
                    ),
                    "",
                    {
                        "operation_id": operation_id,
                        "event_seq": int(event["seq"] or 0),
                    },
                )
                self._enqueue_storage_sync(
                    connection,
                    [session_id],
                    "sync",
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result

__all__ = ["TendencyRepositoryMixin"]
