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


class HealthSummaryRepositoryMixin:
    async def health_summary(self) -> dict[str, Any]:
        return await self._run(self._health_summary)

__all__=["HealthSummaryRepositoryMixin"]
