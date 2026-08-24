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


class AuthorJobWorkerRepositoryMixin:
    async def claim_author_jobs(
        self,
        worker_id: str,
        *,
        limit: int = 1,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._claim_author_jobs,
            str(worker_id),
            int(limit),
            int(lease_seconds),
        )

    def _claim_author_jobs(
        self,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        if not worker_id:
            raise ValueError("author worker_id 不能为空")
        now = utc_now()
        lease_until = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(10, lease_seconds))
        ).isoformat(timespec="seconds")
        claimed: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                maintenance = connection.execute(
                    "SELECT value FROM tavern_meta WHERE key='maintenance_mode'"
                ).fetchone()
                if (
                    maintenance is not None
                    and str(maintenance["value"] or "") == "1"
                ):
                    connection.execute("COMMIT")
                    return []
                expired = connection.execute(
                    """
                    SELECT id, revision FROM author_jobs
                    WHERE status IN ('leased', 'running')
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (now,),
                ).fetchall()
                for expired_row in expired:
                    connection.execute(
                        """
                        UPDATE author_jobs SET
                            status='retry_wait',
                            lease_owner='',
                            leased_at='',
                            lease_expires_at='',
                            next_retry_at=?,
                            last_error_code='author.lease_expired',
                            last_error='任务租约过期，系统已安排安全重试',
                            revision=revision+1,
                            updated_at=?
                        WHERE id=? AND revision=?
                          AND status IN ('leased', 'running')
                          AND lease_expires_at<>'' AND lease_expires_at<=?
                        """,
                        (
                            now,
                            now,
                            expired_row["id"],
                            expired_row["revision"],
                            now,
                        ),
                    )
                rows = connection.execute(
                    """
                    SELECT * FROM author_jobs
                    WHERE status IN ('queued', 'retry_wait')
                      AND (next_retry_at='' OR next_retry_at<=?)
                    ORDER BY created_at, id LIMIT ?
                    """,
                    (now, max(1, min(20, limit))),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE author_jobs SET
                            status='leased',
                            attempts=attempts + 1,
                            lease_owner=?,
                            leased_at=?,
                            lease_expires_at=?,
                            revision=revision+1,
                            updated_at=?
                        WHERE id=? AND revision=?
                          AND status IN ('queued', 'retry_wait')
                        """,
                        (
                            worker_id,
                            now,
                            lease_until,
                            now,
                            row["id"],
                            row["revision"],
                        ),
                    )
                    current = connection.execute(
                        "SELECT * FROM author_jobs WHERE id=?",
                        (row["id"],),
                    ).fetchone()
                    if current is not None and current["lease_owner"] == worker_id:
                        claimed.append(dict(current))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    async def start_author_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        progress_total: int = 1,
    ) -> dict[str, Any]:
        return await self._run(
            self._start_author_job,
            str(job_id),
            str(worker_id),
            int(progress_total),
        )

    def _start_author_job(
        self,
        job_id: str,
        worker_id: str,
        progress_total: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("作者任务不存在")
                if row["status"] == "cancel_requested":
                    cursor = connection.execute(
                        """
                        UPDATE author_jobs SET status='cancelled',
                            finished_at=?, revision=revision+1, updated_at=?
                        WHERE id=? AND revision=?
                        """,
                        (utc_now(), utc_now(), job_id, row["revision"]),
                    )
                elif (
                    row["status"] != "leased"
                    or str(row["lease_owner"] or "") != worker_id
                ):
                    raise InvalidTransitionError("作者任务租约不属于当前 worker")
                else:
                    cursor = connection.execute(
                        """
                        UPDATE author_jobs SET
                            status='running',
                            progress_total=?,
                            progress_current=0,
                            revision=revision+1,
                            updated_at=?
                        WHERE id=? AND revision=?
                        """,
                        (
                            max(1, progress_total),
                            utc_now(),
                            job_id,
                            row["revision"],
                        ),
                    )
                if cursor.rowcount != 1:
                    raise DatabaseConflictError(
                        "目标作者任务已更新，worker 必须重新获取租约状态"
                    )
                current = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(current)

    async def checkpoint_author_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        progress_current: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._checkpoint_author_job,
            str(job_id),
            str(worker_id),
            int(progress_current),
        )

    def _checkpoint_author_job(
        self,
        job_id: str,
        worker_id: str,
        progress_current: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("作者任务不存在")
                if (
                    str(row["lease_owner"] or "") != worker_id
                    or str(row["status"] or "")
                    not in {"running", "cancel_requested"}
                ):
                    raise InvalidTransitionError(
                        "作者任务检查点不属于当前 worker"
                    )
                total = max(1, int(row["progress_total"] or 1))
                current = max(
                    int(row["progress_current"] or 0),
                    min(total, max(0, progress_current)),
                )
                cursor = connection.execute(
                    """
                    UPDATE author_jobs SET
                        progress_current=?, revision=revision+1, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (current, utc_now(), job_id, row["revision"]),
                )
                if cursor.rowcount != 1:
                    raise DatabaseConflictError(
                        "目标作者任务已更新，检查点未写入"
                    )
                updated = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(updated)

    async def author_job_worker_view(
        self,
        job_id: str,
        worker_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._author_job_worker_view,
            str(job_id),
            str(worker_id),
        )

    def _author_job_worker_view(
        self,
        job_id: str,
        worker_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM author_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise DatabaseNotFoundError("作者任务不存在")
        if str(row["lease_owner"] or "") != worker_id:
            raise InvalidTransitionError("作者任务租约不属于当前 worker")
        return {
            "status": str(row["status"] or ""),
            "progress_current": int(row["progress_current"] or 0),
            "progress_total": int(row["progress_total"] or 0),
            "attempts": int(row["attempts"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
        }

    async def finish_author_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        summary: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return await self._run(
            self._finish_author_job,
            str(job_id),
            str(worker_id),
            dict(summary),
            [dict(item) for item in artifacts],
        )

    def _finish_author_job(
        self,
        job_id: str,
        worker_id: str,
        summary: Mapping[str, Any],
        artifacts: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("作者任务不存在")
                if (
                    row["status"] not in {"running", "cancel_requested"}
                    or str(row["lease_owner"] or "") != worker_id
                ):
                    raise InvalidTransitionError("作者任务不能由当前 worker 完成")
                if row["status"] == "cancel_requested":
                    cursor = connection.execute(
                        """
                        UPDATE author_jobs SET
                            status='cancelled', lease_owner='',
                            leased_at='', lease_expires_at='',
                            finished_at=?, revision=revision+1, updated_at=?
                        WHERE id=? AND revision=?
                        """,
                        (now, now, job_id, row["revision"]),
                    )
                else:
                    for artifact in artifacts:
                        artifact_type = str(
                            artifact.get("artifact_type") or ""
                        )
                        content = artifact.get("content")
                        if artifact_type not in {
                            "playtest_report",
                            "semantic_diff",
                            "preflight_report",
                            "coverage_matrix",
                        }:
                            raise ValueError(
                                f"未知分析产物类型：{artifact_type}"
                            )
                        content_json = json_dump(content)
                        content_hash = hashlib.sha256(
                            content_json.encode("utf-8")
                        ).hexdigest()
                        connection.execute(
                            """
                            INSERT INTO world_analysis_artifacts(
                                id, job_id, artifact_type, schema_id,
                                content_json, content_hash, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(job_id, artifact_type) DO UPDATE SET
                                schema_id=excluded.schema_id,
                                content_json=excluded.content_json,
                                content_hash=excluded.content_hash,
                                created_at=excluded.created_at
                            """,
                            (
                                new_id("artifact"),
                                job_id,
                                artifact_type,
                                str(artifact.get("schema_id") or ""),
                                content_json,
                                content_hash,
                                now,
                            ),
                        )
                    cursor = connection.execute(
                        """
                        UPDATE author_jobs SET
                            status='succeeded',
                            progress_current=progress_total,
                            result_summary_json=?,
                            lease_owner='', leased_at='',
                            lease_expires_at='', next_retry_at='',
                            last_error_code='', last_error='',
                            finished_at=?, revision=revision+1, updated_at=?
                        WHERE id=? AND revision=?
                        """,
                        (
                            json_dump(summary),
                            now,
                            now,
                            job_id,
                            row["revision"],
                        ),
                    )
                if cursor.rowcount != 1:
                    raise DatabaseConflictError(
                        "目标作者任务已更新，完成结果未提交"
                    )
                current = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(current)

    async def fail_author_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._fail_author_job,
            str(job_id),
            str(worker_id),
            str(error_code),
            _safe_summary(error_message, 1000),
        )

    def _fail_author_job(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("作者任务不存在")
                if str(row["lease_owner"] or "") != worker_id:
                    raise InvalidTransitionError("作者任务租约不属于当前 worker")
                permanent = int(row["attempts"] or 0) >= int(
                    row["max_attempts"] or 3
                )
                status = "permanently_failed" if permanent else "retry_wait"
                next_retry = (
                    ""
                    if permanent
                    else retry_backoff_after(int(row["attempts"] or 1))
                )
                cursor = connection.execute(
                    """
                    UPDATE author_jobs SET
                        status=?, lease_owner='', leased_at='',
                        lease_expires_at='', next_retry_at=?,
                        last_error_code=?, last_error=?,
                        finished_at=CASE WHEN ? THEN ? ELSE '' END,
                        revision=revision+1, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (
                        status,
                        next_retry,
                        error_code[:120],
                        error_message,
                        1 if permanent else 0,
                        utc_now(),
                        utc_now(),
                        job_id,
                        row["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise DatabaseConflictError(
                        "目标作者任务已更新，失败状态未提交"
                    )
                current = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(current)

__all__=["AuthorJobWorkerRepositoryMixin"]
