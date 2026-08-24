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


class AuthorJobReceiptRepositoryMixin:
    async def author_job_creation_receipt(
        self,
        *,
        operation_id: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return await self._run(
            self._author_job_creation_receipt,
            str(operation_id),
            dict(request_payload),
        )

    def _author_job_creation_receipt(
        self,
        operation_id: str,
        request_payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if not operation_id:
            raise ValueError("作者任务创建缺少防重复凭据")
        input_hash = hashlib.sha256(
            json_dump(dict(request_payload)).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT input_hash, result_json FROM operation_commits
                WHERE operation_id=?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        if str(row["input_hash"] or "") != input_hash:
            raise DatabaseConflictError(
                "该防重复凭据已用于另一项作者任务创建"
            )
        result = json_load(row["result_json"], {})
        return dict(result.get("job") or {}) if isinstance(result, Mapping) else {}

    async def complete_author_job_creation_receipt(
        self,
        *,
        operation_id: str,
        request_payload: Mapping[str, Any],
        public_job: Mapping[str, Any],
        actor_id: str,
    ) -> None:
        await self._run(
            self._complete_author_job_creation_receipt,
            str(operation_id),
            dict(request_payload),
            dict(public_job),
            str(actor_id),
        )

    def _complete_author_job_creation_receipt(
        self,
        operation_id: str,
        request_payload: Mapping[str, Any],
        public_job: Mapping[str, Any],
        actor_id: str,
    ) -> None:
        input_hash = hashlib.sha256(
            json_dump(dict(request_payload)).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT input_hash FROM operation_commits
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项作者任务创建"
                        )
                    connection.execute("COMMIT")
                    return
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, '', ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        operation_id,
                        input_hash,
                        json_dump({"job": dict(public_job)}),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "author.job.create",
                    "",
                    {
                        "operation_id": operation_id,
                        "job_number": int(public_job.get("number") or 0),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def act_on_author_job_idempotent(
        self,
        number: int,
        *,
        action: str,
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._act_on_author_job_idempotent,
            int(number),
            str(action),
            str(operation_id),
            str(actor_id),
        )

    def _act_on_author_job_idempotent(
        self,
        number: int,
        action: str,
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if number < 1 or action not in {"cancel", "retry"}:
            raise ValueError("作者任务动作或序号无效")
        if not operation_id:
            raise ValueError("作者任务动作缺少防重复凭据")
        request_payload = {"number": number, "action": action}
        input_hash = hashlib.sha256(
            json_dump(request_payload).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    """
                    SELECT input_hash, result_json FROM operation_commits
                    WHERE operation_id=?
                    """,
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项作者任务操作"
                        )
                    result = json_load(receipt["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(result), "replayed": True}
                rows = connection.execute(
                    """
                    SELECT * FROM author_jobs
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
                if number > len(rows):
                    raise DatabaseNotFoundError("作者任务序号已失效")
                row = rows[number - 1]
                status = str(row["status"] or "")
                now = utc_now()
                if action == "cancel":
                    if status == "queued":
                        next_status = "cancelled"
                    elif status in {"leased", "running"}:
                        next_status = "cancel_requested"
                    elif status in {"cancel_requested", "cancelled"}:
                        next_status = status
                    else:
                        raise InvalidTransitionError("当前任务状态不能取消")
                    connection.execute(
                        """
                        UPDATE author_jobs SET status=?,
                            finished_at=CASE
                                WHEN ?='cancelled' THEN ? ELSE finished_at
                            END,
                            updated_at=? WHERE id=?
                        """,
                        (next_status, next_status, now, now, row["id"]),
                    )
                    current = connection.execute(
                        "SELECT * FROM author_jobs WHERE id=?",
                        (row["id"],),
                    ).fetchone()
                else:
                    if status != "permanently_failed":
                        raise InvalidTransitionError("只有永久失败任务可以重试")
                    current_world = connection.execute(
                        "SELECT revision FROM worlds WHERE id=?",
                        (row["world_id"],),
                    ).fetchone()
                    if row["world_id"] and (
                        current_world is None
                        or int(current_world["revision"] or 0)
                        != int(row["world_revision"] or 0)
                    ):
                        raise InvalidTransitionError(
                            "世界修订已变化，请创建新的作者任务"
                        )
                    retry_id = new_id("author_job")
                    connection.execute(
                        """
                        INSERT INTO author_jobs(
                            id, job_type, world_id, world_revision,
                            input_hash, request_json, status,
                            progress_current, progress_total,
                            attempts, max_attempts, lease_owner,
                            leased_at, lease_expires_at, next_retry_at,
                            result_summary_json, last_error_code, last_error,
                            created_by, created_at, updated_at, finished_at
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, 'queued',
                            0, 0, 0, ?, '', '', '', '',
                            '{}', '', '', ?, ?, ?, ''
                        )
                        """,
                        (
                            retry_id,
                            row["job_type"],
                            row["world_id"],
                            row["world_revision"],
                            row["input_hash"],
                            row["request_json"],
                            row["max_attempts"],
                            actor_id,
                            now,
                            now,
                        ),
                    )
                    current = connection.execute(
                        "SELECT * FROM author_jobs WHERE id=?",
                        (retry_id,),
                    ).fetchone()
                result = self._public_author_job(current, 1 if action == "retry" else number)
                result["replayed"] = False
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, '', ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        operation_id,
                        input_hash,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    f"author.job.{action}",
                    "",
                    {"number": number, "operation_id": operation_id},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

__all__ = ["AuthorJobReceiptRepositoryMixin"]
