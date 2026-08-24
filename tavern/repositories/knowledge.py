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


class KnowledgeRepositoryMixin:
    def _insert_knowledge_evidence_intent_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        evidence: KnowledgeEvidenceIntent,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO npc_knowledge_evidence(
                id, session_id, character_id, fact_ref, fact_text,
                belief_kind, source_event_id, source_kind, confidence,
                visibility, known_since, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("knowledge_evidence"),
                plan.session_id,
                evidence.character_id,
                evidence.fact_ref,
                evidence.fact_text,
                evidence.belief_kind,
                evidence.event_id,
                evidence.source_kind,
                evidence.confidence,
                evidence.visibility,
                utc_now(),
                evidence.expires_at,
            ),
        )

    async def add_npc_knowledge_evidence(
        self,
        *,
        session_id: str,
        character_id: str,
        fact_ref: str,
        fact_text: str,
        belief_kind: str,
        source_event_id: str,
        source_kind: str,
        confidence: float = 1.0,
        visibility: str = "host",
    ) -> dict[str, Any]:
        return await self._run(
            self._add_npc_knowledge_evidence,
            str(session_id),
            str(character_id),
            str(fact_ref),
            _safe_summary(fact_text, 500),
            str(belief_kind),
            str(source_event_id),
            str(source_kind),
            float(confidence),
            str(visibility),
        )

    def _add_npc_knowledge_evidence(
        self,
        session_id: str,
        character_id: str,
        fact_ref: str,
        fact_text: str,
        belief_kind: str,
        source_event_id: str,
        source_kind: str,
        confidence: float,
        visibility: str,
    ) -> dict[str, Any]:
        if belief_kind not in {"known", "misconception"}:
            raise ValueError("NPC 信念类型必须是 known 或 misconception")
        if source_kind not in {
            "witnessed",
            "told",
            "document",
            "inference",
            "world_preset",
        }:
            raise ValueError("NPC 知识来源类型无效")
        if visibility not in {"public", "party", "host", "secret"}:
            raise ValueError("NPC 知识可见性无效")
        if not 0 <= confidence <= 1:
            raise ValueError("NPC 知识 confidence 必须在 0 到 1")
        if not fact_ref or not fact_text:
            raise ValueError("NPC 知识必须包含事实引用和语义文本")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                character = connection.execute(
                    """
                    SELECT id FROM session_characters
                    WHERE id=? AND session_id=?
                    """,
                    (character_id, session_id),
                ).fetchone()
                event = connection.execute(
                    """
                    SELECT event_id, seq FROM session_events
                    WHERE event_id=? AND session_id=?
                    """,
                    (source_event_id, session_id),
                ).fetchone()
                if character is None:
                    raise DatabaseNotFoundError("NPC 不存在")
                if event is None:
                    raise DatabaseNotFoundError("NPC 知识来源事件不存在")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO npc_knowledge_evidence(
                        id, session_id, character_id, fact_ref, fact_text,
                        belief_kind, source_event_id, source_kind,
                        confidence, visibility, known_since
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("knowledge"),
                        session_id,
                        character_id,
                        fact_ref[:240],
                        fact_text,
                        belief_kind,
                        source_event_id,
                        source_kind,
                        confidence,
                        visibility,
                        utc_now(),
                    ),
                )
                projection = self._rebuild_npc_knowledge_locked(
                    connection,
                    character_id,
                )
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        session_id, projection_name, last_seq,
                        payload_json, revision, updated_at
                    ) VALUES (?, 'npc_knowledge', ?, ?, 1, ?)
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
                                "mode": "evidence_update",
                                "characters_rebuilt": 1,
                            }
                        ),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return projection

    async def rebuild_npc_knowledge(
        self,
        character_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._rebuild_npc_knowledge,
            str(character_id),
        )

    def _rebuild_npc_knowledge(self, character_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                projection = self._rebuild_npc_knowledge_locked(
                    connection,
                    character_id,
                )
                character = connection.execute(
                    """
                    SELECT session_id FROM session_characters WHERE id=?
                    """,
                    (character_id,),
                ).fetchone()
                latest = connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) AS seq
                    FROM session_events WHERE session_id=?
                    """,
                    (character["session_id"],),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        session_id, projection_name, last_seq,
                        payload_json, revision, updated_at
                    ) VALUES (?, 'npc_knowledge', ?, ?, 1, ?)
                    ON CONFLICT(session_id, projection_name) DO UPDATE SET
                        last_seq=excluded.last_seq,
                        payload_json=excluded.payload_json,
                        revision=projection_checkpoints.revision + 1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        character["session_id"],
                        int(latest["seq"] if latest else 0),
                        json_dump(
                            {
                                "mode": "character_full_rebuild",
                                "characters_rebuilt": 1,
                            }
                        ),
                        utc_now(),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return projection

    def _rebuild_npc_knowledge_locked(
        self,
        connection: Any,
        character_id: str,
    ) -> dict[str, Any]:
        character = connection.execute(
            "SELECT * FROM session_characters WHERE id=?",
            (character_id,),
        ).fetchone()
        if character is None:
            raise DatabaseNotFoundError("NPC 不存在")
        rows = connection.execute(
            """
            SELECT * FROM npc_knowledge_evidence
            WHERE character_id=? AND revoked_at=''
              AND (expires_at='' OR expires_at>?)
            ORDER BY known_since, id
            """,
            (character_id, utc_now()),
        ).fetchall()
        known: list[str] = []
        misconceptions: list[str] = []
        for row in rows:
            text = str(row["fact_text"] or row["fact_ref"] or "").strip()
            if not text:
                continue
            target = (
                known
                if str(row["belief_kind"]) == "known"
                else misconceptions
            )
            if text not in target:
                target.append(text)
        connection.execute(
            """
            UPDATE session_characters SET
                known_facts_json=?,
                misconceptions_json=?,
                revision=revision + 1,
                updated_at=?
            WHERE id=?
            """,
            (
                json_dump(known),
                json_dump(misconceptions),
                utc_now(),
                character_id,
            ),
        )
        return {
            "character_name": str(character["name"] or ""),
            "known_facts": known,
            "misconceptions": misconceptions,
            "evidence_count": len(rows),
        }


__all__ = ["KnowledgeRepositoryMixin"]
