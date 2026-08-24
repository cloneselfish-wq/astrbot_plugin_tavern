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


class AuthorJobRepositoryMixin:
    @staticmethod
    def _author_jobs_revision_locked(connection: Any) -> int:
        rows = connection.execute(
            """
            SELECT rowid, id, status, revision, updated_at
            FROM author_jobs ORDER BY rowid
            """
        ).fetchall()
        if not rows:
            return 0
        canonical = json_dump(
            [
                {
                    "rowid": int(row["rowid"] or 0),
                    "id": str(row["id"] or ""),
                    "status": str(row["status"] or ""),
                    "revision": int(row["revision"] or 0),
                    "updated_at": str(row["updated_at"] or ""),
                }
                for row in rows
            ]
        )
        # 13 个十六进制位不超过 JavaScript 的安全整数范围。
        return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:13], 16)

    def _public_author_job_locked(
        self,
        connection: Any,
        job_id: str,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT * FROM author_jobs
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        for number, row in enumerate(rows, start=1):
            if str(row["id"] or "") == str(job_id):
                return self._public_author_job(row, number)
        raise DatabaseNotFoundError("作者任务不存在")

    @staticmethod
    def _author_job_ref(job_id: object) -> str:
        digest = hashlib.sha256(
            str(job_id or "").encode("utf-8")
        ).hexdigest()[:16].upper()
        return f"public:author-job:{digest}"

    def _author_job_by_public_ref_locked(
        self,
        connection: Any,
        job_ref: str,
    ) -> Any | None:
        if not str(job_ref).startswith("public:author-job:"):
            return None
        rows = connection.execute(
            "SELECT * FROM author_jobs ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return next(
            (
                row
                for row in rows
                if self._author_job_ref(row["id"]) == str(job_ref)
            ),
            None,
        )

    def _turn_commit_author_job_create_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        change: StateChange,
    ) -> dict[str, Any]:
        payload = dict(change.payload)
        job_type = str(payload.get("job_type") or "")
        world_ref = str(payload.get("world_ref") or "")
        request_payload = payload.get("request")
        request_payload = (
            dict(request_payload)
            if isinstance(request_payload, Mapping)
            else {}
        )
        created_by = str(payload.get("created_by") or plan.actor_ref)
        max_attempts = int(payload.get("max_attempts") or 3)
        if job_type not in AUTHOR_JOB_TYPES:
            raise ValueError(f"不支持的作者任务：{job_type}")
        if not created_by:
            raise PermissionError("作者任务缺少创建者")
        canonical = json_dump(request_payload)
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        world_id = ""
        world_revision = 0
        if world_ref:
            world = connection.execute(
                """
                SELECT id, revision FROM worlds
                WHERE id=? OR slug=?
                """,
                (world_ref, world_ref),
            ).fetchone()
            if world is None:
                raise DatabaseNotFoundError("作者任务对应世界不存在")
            world_id = str(world["id"])
            world_revision = int(world["revision"] or 0)
        row = connection.execute(
            """
            SELECT * FROM author_jobs
            WHERE job_type=? AND world_id=? AND world_revision=?
              AND input_hash=?
              AND status IN (
                'queued', 'leased', 'running',
                'retry_wait', 'succeeded'
              )
            ORDER BY created_at DESC LIMIT 1
            """,
            (job_type, world_id, world_revision, input_hash),
        ).fetchone()
        state_replayed = row is not None
        if row is None:
            now = utc_now()
            job_id = new_id("author_job")
            connection.execute(
                """
                INSERT INTO author_jobs(
                    id, job_type, world_id, world_revision,
                    input_hash, request_json, status,
                    max_attempts, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    job_type,
                    world_id,
                    world_revision,
                    input_hash,
                    canonical,
                    max(1, min(10, max_attempts)),
                    created_by,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM author_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        public = self._public_author_job_locked(
            connection,
            str(row["id"]),
        )
        return {
            "kind": change.kind,
            "job": public,
            "state_replayed": state_replayed,
        }

    def _turn_commit_author_job_action_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        change: StateChange,
    ) -> dict[str, Any]:
        payload = dict(change.payload)
        job_ref = str(payload.get("job_ref") or "")
        action = change.kind.rsplit(".", 1)[-1]
        if not job_ref or action not in {"cancel", "retry"}:
            raise ValueError("作者任务动作或任务引用无效")
        row = self._author_job_by_public_ref_locked(connection, job_ref)
        if row is None:
            raise DatabaseNotFoundError("作者任务不存在")
        expected_revision = int(plan.expected_revision or 0)
        status = str(row["status"] or "")
        now = utc_now()
        state_replayed = False
        if action == "cancel":
            if status == "queued":
                next_status = "cancelled"
            elif status in {"leased", "running"}:
                next_status = "cancel_requested"
            elif status in {"cancel_requested", "cancelled"}:
                next_status = status
                state_replayed = True
            else:
                raise InvalidTransitionError("当前任务状态不能取消")
            connection.execute(
                """
                UPDATE author_jobs SET status=?,
                    finished_at=CASE
                        WHEN ?='cancelled' THEN ? ELSE finished_at
                    END,
                    revision=revision+1, updated_at=?
                WHERE id=? AND revision=?
                """,
                (
                    next_status,
                    next_status,
                    now,
                    now,
                    row["id"],
                    expected_revision,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise DatabaseConflictError(
                    "目标作者任务已更新，请使用返回的最新状态重试"
                )
            job_id = str(row["id"])
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
            job_id = new_id("author_job")
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
                    job_id,
                    row["job_type"],
                    row["world_id"],
                    row["world_revision"],
                    row["input_hash"],
                    row["request_json"],
                    row["max_attempts"],
                    plan.actor_ref,
                    now,
                    now,
                ),
            )
        return {
            "kind": change.kind,
            "job": self._public_author_job_locked(connection, job_id),
            "state_replayed": state_replayed,
        }

    async def create_author_job(
        self,
        *,
        job_type: str,
        world_ref: str,
        request_payload: Mapping[str, Any],
        created_by: str,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        return await self._run(
            self._create_author_job,
            str(job_type),
            str(world_ref),
            dict(request_payload),
            str(created_by),
            int(max_attempts),
        )

    def _create_author_job(
        self,
        job_type: str,
        world_ref: str,
        request_payload: Mapping[str, Any],
        created_by: str,
        max_attempts: int,
    ) -> dict[str, Any]:
        if job_type not in AUTHOR_JOB_TYPES:
            raise ValueError(f"不支持的作者任务：{job_type}")
        if not created_by:
            raise PermissionError("作者任务缺少创建者")
        canonical = json_dump(dict(request_payload))
        input_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                world_id = ""
                world_revision = 0
                if world_ref:
                    world = connection.execute(
                        """
                        SELECT id, revision FROM worlds
                        WHERE id=? OR slug=?
                        """,
                        (world_ref, world_ref),
                    ).fetchone()
                    if world is None:
                        raise DatabaseNotFoundError("作者任务对应世界不存在")
                    world_id = str(world["id"])
                    world_revision = int(world["revision"] or 0)
                existing = connection.execute(
                    """
                    SELECT * FROM author_jobs
                    WHERE job_type=? AND world_id=? AND world_revision=?
                      AND input_hash=?
                      AND status IN (
                        'queued', 'leased', 'running',
                        'retry_wait', 'succeeded'
                      )
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (job_type, world_id, world_revision, input_hash),
                ).fetchone()
                if existing is not None:
                    connection.execute("ROLLBACK")
                    return {
                        **dict(existing),
                        "replayed": True,
                    }
                job_id = new_id("author_job")
                connection.execute(
                    """
                    INSERT INTO author_jobs(
                        id, job_type, world_id, world_revision,
                        input_hash, request_json, status,
                        max_attempts, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        job_type,
                        world_id,
                        world_revision,
                        input_hash,
                        canonical,
                        max(1, min(10, max_attempts)),
                        created_by,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (job_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {**dict(row), "replayed": False}

    async def list_author_jobs(
        self,
        *,
        world_ref: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_author_jobs,
            str(world_ref),
            int(limit),
        )

    async def author_jobs_revision(self) -> int:
        return await self._run(self._author_jobs_revision)

    def _author_jobs_revision(self) -> int:
        with self._connect() as connection:
            return self._author_jobs_revision_locked(connection)

    def _list_author_jobs(
        self,
        world_ref: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            values: list[Any] = []
            where = ""
            if world_ref:
                world = connection.execute(
                    "SELECT id FROM worlds WHERE id=? OR slug=?",
                    (world_ref, world_ref),
                ).fetchone()
                if world is None:
                    raise DatabaseNotFoundError("世界不存在")
                where = "WHERE world_id=?"
                values.append(world["id"])
            values.append(max(1, min(500, limit)))
            rows = connection.execute(
                f"""
                SELECT * FROM author_jobs {where}
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                tuple(values),
            ).fetchall()
            labels = {
                "playtest_report": "试玩报告",
                "semantic_diff": "语义差异报告",
                "preflight_report": "预检报告",
                "coverage_matrix": "内容覆盖矩阵",
            }
            result: list[dict[str, Any]] = []
            for index, row in enumerate(rows, start=1):
                item = self._public_author_job(row, index)
                artifact_rows = connection.execute(
                    """
                    SELECT artifact_type, created_at
                    FROM world_analysis_artifacts
                    WHERE job_id=?
                    ORDER BY created_at DESC, artifact_type
                    """,
                    (row["id"],),
                ).fetchall()
                item["artifacts"] = [
                    {
                        "label": labels.get(
                            str(artifact["artifact_type"] or ""),
                            "任务报告",
                        ),
                        "summary": "任务已生成该报告，可在详情中查看安全摘要。",
                        "state": "已生成",
                        "updated_at": str(artifact["created_at"] or ""),
                    }
                    for artifact in artifact_rows
                ]
                result.append(item)
        return result

    async def author_job_public_view(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        """Resolve one internal worker job to its current public list number."""

        return await self._run(
            self._author_job_public_view,
            str(job_id),
        )

    def _author_job_public_view(
        self,
        job_id: str,
    ) -> dict[str, Any]:
        if not job_id:
            raise DatabaseNotFoundError("作者任务不存在")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM author_jobs
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        for index, row in enumerate(rows, start=1):
            if str(row["id"] or "") == job_id:
                return self._public_author_job(row, index)
        raise DatabaseNotFoundError("作者任务不存在")

    async def author_job_public_view_by_ref(
        self,
        job_ref: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._author_job_public_view_by_ref,
            str(job_ref),
        )

    def _author_job_public_view_by_ref(
        self,
        job_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._author_job_by_public_ref_locked(connection, job_ref)
            if row is None:
                raise DatabaseNotFoundError("作者任务不存在")
            rows = connection.execute(
                """
                SELECT id FROM author_jobs
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        number = next(
            (
                index
                for index, item in enumerate(rows, start=1)
                if str(item["id"]) == str(row["id"])
            ),
            0,
        )
        return self._public_author_job(row, number)

    @staticmethod
    def _public_author_job(row: Mapping[str, Any], number: int) -> dict[str, Any]:
        return {
            "number": number,
            "job_ref": AuthorJobRepositoryMixin._author_job_ref(row["id"]),
            "revision": int(row["revision"] or 0),
            "job_type": str(row["job_type"] or ""),
            "world_revision": int(row["world_revision"] or 0),
            "status": str(row["status"] or ""),
            "progress_current": int(row["progress_current"] or 0),
            "progress_total": int(row["progress_total"] or 0),
            "attempts": int(row["attempts"] or 0),
            "max_attempts": int(row["max_attempts"] or 0),
            "result_summary": json_load(row["result_summary_json"], {}),
            "last_error_code": str(row["last_error_code"] or ""),
            "last_error": _safe_summary(row["last_error"], 300),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }

    async def act_on_author_job(
        self,
        number: int,
        *,
        action: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._act_on_author_job,
            int(number),
            str(action),
        )

    def _act_on_author_job(
        self,
        number: int,
        action: str,
    ) -> dict[str, Any]:
        if number < 1:
            raise ValueError("任务序号必须大于 0")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                            finished_at=CASE WHEN ?='cancelled' THEN ? ELSE finished_at END,
                            updated_at=? WHERE id=?
                        """,
                        (next_status, next_status, now, now, row["id"]),
                    )
                elif action == "retry":
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
                            row["created_by"],
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM author_jobs WHERE id=?",
                        (retry_id,),
                    ).fetchone()
                else:
                    raise ValueError("作者任务动作必须是 cancel 或 retry")
                current = connection.execute(
                    "SELECT * FROM author_jobs WHERE id=?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._public_author_job(current, number)

    async def author_job_artifact(
        self,
        job_ref: str,
        artifact_type: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._author_job_artifact,
            str(job_ref),
            str(artifact_type),
        )

    def _author_job_artifact(
        self,
        job_ref: str,
        artifact_type: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            job = self._author_job_by_public_ref_locked(connection, job_ref)
            if job is None:
                raise DatabaseNotFoundError("作者任务不存在")
            row = connection.execute(
                """
                SELECT * FROM world_analysis_artifacts
                WHERE job_id=? AND artifact_type=?
                """,
                (job["id"], artifact_type),
            ).fetchone()
        if row is None:
            raise DatabaseNotFoundError("任务尚未生成该分析报告")
        return {
            "artifact_type": str(row["artifact_type"]),
            "schema": str(row["schema_id"]),
            "content": json_load(row["content_json"], {}),
            "content_hash": str(row["content_hash"]),
            "created_at": str(row["created_at"]),
        }

__all__=["AuthorJobRepositoryMixin"]
