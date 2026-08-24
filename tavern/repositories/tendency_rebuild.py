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


class TendencyRebuildRepositoryMixin:
    async def project_tendency_event(self, event_id: str) -> dict[str, Any]:
        return await self._run(self._project_tendency_event, str(event_id))

    def _project_tendency_event(self, event_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._project_tendency_event_locked(
                    connection,
                    event_id,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result

    def _project_tendency_event_locked(
        self,
        connection: Any,
        event_id: str,
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM session_events WHERE event_id=?",
            (str(event_id),),
        ).fetchone()
        if row is None:
            raise DatabaseNotFoundError("倾向来源事件不存在")
        payload = json_load(row["payload_json"], {})
        payload = payload if isinstance(payload, Mapping) else {}
        signals = payload.get("tendency_signals")
        if not isinstance(signals, Sequence) or isinstance(
            signals,
            (str, bytes),
        ):
            signals = []
        participant_id = str(
            payload.get("participant_id")
            or payload.get("actor_participant_id")
            or ""
        ).strip()
        if not participant_id and row["actor_ref"]:
            participant = connection.execute(
                """
                SELECT id FROM participants
                WHERE session_id=? AND (
                    id=? OR group_user_id=? OR character_code=?
                )
                """,
                (
                    row["session_id"],
                    row["actor_ref"],
                    row["actor_ref"],
                    row["actor_ref"],
                ),
            ).fetchone()
            participant_id = str(participant["id"]) if participant else ""
        if signals and not participant_id:
            raise DatabaseNotFoundError("倾向事件无法解析到副本玩家")
        now = utc_now()
        inserted = 0
        for signal in signals:
            if not isinstance(signal, Mapping):
                continue
            dimension = str(signal.get("dimension") or "").strip()
            direction = int(signal.get("direction") or 0)
            weight = int(signal.get("weight") or 0)
            confidence = float(signal.get("confidence") or 1.0)
            source_kind = str(
                signal.get("source_kind")
                or payload.get("source_kind")
                or "action"
            )
            if dimension not in TENDENCY_DIMENSIONS:
                raise ValueError(f"未知倾向维度：{dimension}")
            if direction not in {-1, 1}:
                raise ValueError("倾向 direction 只能是 -1 或 1")
            if not 1 <= weight <= 5:
                raise ValueError("倾向 weight 必须在 1 到 5")
            if source_kind not in TENDENCY_SOURCE_KINDS:
                raise ValueError(f"倾向来源类型无效：{source_kind}")
            if source_kind != "host_correction" and weight > 3:
                raise ValueError("普通世界倾向信号 weight 不能超过 3")
            if not 0 <= confidence <= 1:
                raise ValueError("倾向 confidence 必须在 0 到 1")
            action_summary = _safe_summary(
                signal.get("action_summary")
                or payload.get("summary")
                or payload.get("title")
                or "一次已提交的结构化选择"
            )
            rationale = _safe_summary(
                signal.get("rationale")
                or signal.get("rationale_text")
                or action_summary,
                300,
            )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO player_tendency_evidence(
                    id, session_id, participant_id, event_id, dimension,
                    direction, weight, confidence, rationale,
                    action_summary, source_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("tendency"),
                    row["session_id"],
                    participant_id,
                    row["event_id"],
                    dimension,
                    direction,
                    weight,
                    confidence,
                    rationale,
                    action_summary,
                    source_kind,
                    now,
                ),
            )
            inserted += max(0, int(cursor.rowcount or 0))
        if participant_id:
            profile = self._rebuild_tendency_profile_locked(
                connection,
                str(row["session_id"]),
                participant_id,
            )
        else:
            profile = {}
        connection.execute(
            """
            INSERT INTO projection_checkpoints(
                session_id, projection_name, last_seq,
                payload_json, revision, updated_at
            ) VALUES (?, 'player_tendency', ?, ?, 1, ?)
            ON CONFLICT(session_id, projection_name) DO UPDATE SET
                last_seq=MAX(projection_checkpoints.last_seq, excluded.last_seq),
                payload_json=excluded.payload_json,
                revision=projection_checkpoints.revision + 1,
                updated_at=excluded.updated_at
            """,
            (
                row["session_id"],
                int(row["seq"] or 0),
                json_dump(
                    {
                        "last_event_type": str(row["type"] or ""),
                        "inserted": inserted,
                    }
                ),
                now,
            ),
        )
        return {
            "event_id": str(row["event_id"]),
            "inserted": inserted,
            "participant_id": participant_id,
            "profile": profile,
        }

    async def rebuild_tendency_profiles(
        self,
        session_id: str,
        participant_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._rebuild_tendency_profiles,
            str(session_id),
            str(participant_id),
        )

    def _rebuild_tendency_profiles(
        self,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if participant_id:
                    ids = [participant_id]
                else:
                    ids = [
                        str(row["id"])
                        for row in connection.execute(
                            "SELECT id FROM participants WHERE session_id=?",
                            (session_id,),
                        ).fetchall()
                    ]
                profiles = [
                    self._rebuild_tendency_profile_locked(
                        connection,
                        session_id,
                        item,
                    )
                    for item in ids
                ]
                latest = connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) AS seq
                    FROM session_events WHERE session_id=?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        session_id, projection_name, last_seq,
                        payload_json, revision, updated_at
                    ) VALUES (?, 'player_tendency', ?, ?, 1, ?)
                    ON CONFLICT(session_id, projection_name) DO UPDATE SET
                        last_seq=excluded.last_seq,
                        payload_json=excluded.payload_json,
                        revision=projection_checkpoints.revision + 1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        int(latest["seq"] if latest else 0),
                        json_dump({"profiles": len(profiles), "mode": "full"}),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "session_id": session_id,
            "profiles": profiles,
        }

    def _rebuild_tendency_profile_locked(
        self,
        connection: Any,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        participant = connection.execute(
            """
            SELECT id FROM participants WHERE id=? AND session_id=?
            """,
            (participant_id, session_id),
        ).fetchone()
        if participant is None:
            raise DatabaseNotFoundError("倾向画像对应玩家不存在")
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT e.*, se.seq
                FROM player_tendency_evidence e
                JOIN session_events se ON se.event_id=e.event_id
                WHERE e.participant_id=? AND e.session_id=?
                ORDER BY se.seq, e.created_at, e.id
                """,
                (participant_id, session_id),
            ).fetchall()
        ]
        last_seq = max(
            (int(row.get("seq") or 0) for row in rows),
            default=0,
        )
        profile = _tendency_profile(rows, source_last_seq=last_seq)
        now = utc_now()
        connection.execute(
            """
            INSERT INTO player_tendency_profiles(
                participant_id, session_id, source_last_seq, revision,
                summary_json, evidence_count, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(participant_id) DO UPDATE SET
                session_id=excluded.session_id,
                source_last_seq=excluded.source_last_seq,
                revision=player_tendency_profiles.revision + 1,
                summary_json=excluded.summary_json,
                evidence_count=excluded.evidence_count,
                updated_at=excluded.updated_at
            """,
            (
                participant_id,
                session_id,
                last_seq,
                json_dump(profile),
                len(rows),
                now,
            ),
        )
        stored = connection.execute(
            """
            SELECT revision FROM player_tendency_profiles
            WHERE participant_id=?
            """,
            (participant_id,),
        ).fetchone()
        return {
            **profile,
            "revision": int(stored["revision"] if stored else 1),
        }

__all__ = ["TendencyRebuildRepositoryMixin"]
