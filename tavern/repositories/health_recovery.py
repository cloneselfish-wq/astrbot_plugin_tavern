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


class HealthRecoveryRepositoryMixin:
    async def run_health_action(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._run_health_action,
            str(action),
            dict(payload),
            str(operation_id),
            str(actor_id),
        )

    async def health_action_receipt(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._health_action_receipt,
            str(action),
            dict(payload),
            str(operation_id),
        )

    def _health_action_receipt(
        self,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any] | None:
        if not operation_id:
            raise ValueError("健康恢复动作缺少防重复凭据")
        canonical = dict(payload)
        canonical.pop("verified_result", None)
        input_hash = hashlib.sha256(
            json_dump({"action": action, "payload": canonical}).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            receipt = connection.execute(
                """
                SELECT input_hash, result_json FROM operation_commits
                WHERE operation_id=?
                """,
                (operation_id,),
            ).fetchone()
        if receipt is None:
            return None
        if str(receipt["input_hash"] or "") != input_hash:
            raise DatabaseConflictError(
                "该防重复凭据已用于另一项健康恢复"
            )
        result = json_load(receipt["result_json"], {})
        return {
            **(dict(result) if isinstance(result, Mapping) else {}),
            "replayed": True,
        }

    @staticmethod
    def _health_outbox_table(component: str) -> tuple[str, str]:
        value = str(component or "")
        tables = {
            "delivery_outbox": ("delivery_outbox", "id"),
            "storage_outbox": ("storage_sync_outbox", "rowid"),
            "event_outbox": ("event_outbox", "id"),
        }
        if value not in tables:
            raise ValueError("健康动作的队列类型无效")
        return tables[value]

    def _run_health_action(
        self,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        allowed = {
            "health.outbox.retry",
            "health.lease.release_expired",
            "health.projection.rebuild",
            "health.world.verify",
            "health.author_job.retry",
            "health.backup.create",
            "health.diagnostics.export",
        }
        if action not in allowed:
            raise ValueError("不支持的健康恢复动作")
        if not operation_id:
            raise ValueError("健康恢复动作缺少防重复凭据")
        canonical = dict(payload)
        canonical.pop("verified_result", None)
        request_payload = {"action": action, "payload": canonical}
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
                            "该防重复凭据已用于另一项健康恢复"
                        )
                    result = json_load(receipt["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(result), "replayed": True}
                now = utc_now()
                result: dict[str, Any]
                if action == "health.outbox.retry":
                    component = str(payload.get("component") or "")
                    number = int(payload.get("number") or 0)
                    if number < 1:
                        raise ValueError("请选择要重试的失败项目")
                    table, identity = self._health_outbox_table(component)
                    rows = connection.execute(
                        f"""
                        SELECT {identity} AS target FROM {table}
                        WHERE status='permanently_failed'
                        ORDER BY updated_at DESC, created_at DESC
                        """
                    ).fetchall()
                    if number > len(rows):
                        raise DatabaseNotFoundError("失败项目序号已失效")
                    target = rows[number - 1]["target"]
                    if component == "delivery_outbox":
                        connection.execute(
                            """
                            UPDATE delivery_outbox SET
                                status=CASE
                                    WHEN next_part_index>0
                                    THEN 'partially_sent' ELSE 'retry_wait'
                                END,
                                attempts=0, next_retry_at=?,
                                lease_owner='', leased_at='',
                                last_error_code='', last_error='',
                                updated_at=?
                            WHERE id=? AND status='permanently_failed'
                            """,
                            (now, now, target),
                        )
                    elif component == "storage_outbox":
                        connection.execute(
                            """
                            UPDATE storage_sync_outbox SET
                                status='retry_wait', attempts=0,
                                next_retry_at=?, lease_owner='',
                                leased_at='', lease_expires_at='',
                                last_error_code='', last_error='',
                                updated_at=?
                            WHERE rowid=? AND status='permanently_failed'
                            """,
                            (now, now, target),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE event_outbox SET
                                status='retry_wait', attempts=0,
                                next_retry_at=?, lease_owner='',
                                leased_at='', lease_expires_at='',
                                last_error_code='', last_error='',
                                updated_at=?
                            WHERE id=? AND status='permanently_failed'
                            """,
                            (now, now, target),
                        )
                    result = {
                        "summary": "失败项目已重新进入安全重试队列。",
                        "component": component,
                        "number": number,
                    }
                elif action == "health.lease.release_expired":
                    component = str(payload.get("component") or "")
                    if component == "delivery_outbox":
                        cursor = connection.execute(
                            """
                            UPDATE delivery_outbox SET
                                status=CASE
                                    WHEN next_part_index>0
                                    THEN 'partially_sent' ELSE 'retry_wait'
                                END,
                                lease_owner='', leased_at='',
                                next_retry_at=?, updated_at=?
                            WHERE status='leased' AND leased_at<>''
                              AND leased_at<=?
                            """,
                            (now, now, now),
                        )
                    elif component in {"storage_outbox", "event_outbox"}:
                        table, _ = self._health_outbox_table(component)
                        cursor = connection.execute(
                            f"""
                            UPDATE {table} SET
                                status='retry_wait', lease_owner='',
                                leased_at='', lease_expires_at='',
                                next_retry_at=?, updated_at=?
                            WHERE status='leased'
                              AND lease_expires_at<>''
                              AND lease_expires_at<=?
                            """,
                            (now, now, now),
                        )
                    elif component == "author_jobs":
                        cursor = connection.execute(
                            """
                            UPDATE author_jobs SET
                                status='retry_wait', lease_owner='',
                                leased_at='', lease_expires_at='',
                                next_retry_at=?,
                                last_error_code='author.lease_expired',
                                last_error='任务租约过期，系统已安排安全重试',
                                updated_at=?
                            WHERE status IN ('leased', 'running')
                              AND lease_expires_at<>''
                              AND lease_expires_at<=?
                            """,
                            (now, now, now),
                        )
                    elif component == "operations":
                        placeholders = ",".join(
                            "?" for _ in (
                                "reserved",
                                "generating",
                                "dice_locked",
                                "ready_to_commit",
                            )
                        )
                        cursor = connection.execute(
                            f"""
                            UPDATE operation_receipts SET
                                status='failed_retryable',
                                phase='lease_expired',
                                last_error_code='lease_expired',
                                updated_at=?
                            WHERE status IN ({placeholders})
                              AND lease_expires_at<>''
                              AND lease_expires_at<=?
                            """,
                            (
                                now,
                                "reserved",
                                "generating",
                                "dice_locked",
                                "ready_to_commit",
                                now,
                            ),
                        )
                    else:
                        raise ValueError("健康动作的租约类型无效")
                    result = {
                        "summary": f"已释放 {max(0, cursor.rowcount)} 个过期租约。",
                        "component": component,
                        "released": max(0, cursor.rowcount),
                    }
                elif action == "health.projection.rebuild":
                    projection = str(payload.get("projection") or "")
                    session_id = str(payload.get("session_id") or "")
                    if not session_id:
                        raise ValueError("请选择要重建投影的副本")
                    session = connection.execute(
                        "SELECT id FROM sessions WHERE id=?",
                        (session_id,),
                    ).fetchone()
                    if session is None:
                        raise DatabaseNotFoundError("副本不存在")
                    if projection == "player_tendency":
                        participant_ids = [
                            str(row["id"])
                            for row in connection.execute(
                                "SELECT id FROM participants WHERE session_id=?",
                                (session_id,),
                            ).fetchall()
                        ]
                        for participant_id in participant_ids:
                            self._rebuild_tendency_profile_locked(
                                connection,
                                session_id,
                                participant_id,
                            )
                        rebuilt = len(participant_ids)
                    elif projection == "npc_knowledge":
                        character_ids = [
                            str(row["id"])
                            for row in connection.execute(
                                """
                                SELECT id FROM session_characters
                                WHERE session_id=?
                                """,
                                (session_id,),
                            ).fetchall()
                        ]
                        for character_id in character_ids:
                            self._rebuild_npc_knowledge_locked(
                                connection,
                                character_id,
                            )
                        rebuilt = len(character_ids)
                    else:
                        raise ValueError("不支持的投影类型")
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
                        ) VALUES (?, ?, ?, ?, 1, ?)
                        ON CONFLICT(session_id, projection_name) DO UPDATE SET
                            last_seq=excluded.last_seq,
                            payload_json=excluded.payload_json,
                            revision=projection_checkpoints.revision + 1,
                            updated_at=excluded.updated_at
                        """,
                        (
                            session_id,
                            projection,
                            int(latest["seq"] if latest else 0),
                            json_dump({"mode": "full", "rebuilt": rebuilt}),
                            now,
                        ),
                    )
                    result = {
                        "summary": f"已从权威证据重建 {rebuilt} 项投影。",
                        "projection": projection,
                        "rebuilt": rebuilt,
                    }
                elif action == "health.world.verify":
                    number = int(payload.get("world_no") or 0)
                    row = connection.execute(
                        "SELECT * FROM worlds WHERE display_no=?",
                        (number,),
                    ).fetchone()
                    if row is None:
                        raise DatabaseNotFoundError("世界序号不存在")
                    world = self._world(row)
                    character_rows = connection.execute(
                        """
                        SELECT * FROM characters
                        WHERE world_id=? AND enabled=1
                        ORDER BY sort_order, name COLLATE NOCASE
                        """,
                        (row["id"],),
                    ).fetchall()
                    world["characters"] = [
                        self._character(item) for item in character_rows
                    ]
                    from ..twp.validation.privacy import check_template

                    report = check_template(world)
                    result = {
                        "summary": (
                            "世界检查通过。"
                            if report.get("compatible")
                            else "世界检查发现阻断问题。"
                        ),
                        "world_no": number,
                        "compatible": bool(report.get("compatible")),
                        "errors": len(report.get("errors") or []),
                        "warnings": len(report.get("warnings") or []),
                    }
                elif action == "health.author_job.retry":
                    number = int(payload.get("job_no") or 0)
                    if number < 1:
                        raise ValueError("请选择要重试的作者任务")
                    rows = connection.execute(
                        """
                        SELECT * FROM author_jobs
                        ORDER BY created_at DESC, id DESC
                        """
                    ).fetchall()
                    if number > len(rows):
                        raise DatabaseNotFoundError("作者任务序号已失效")
                    row = rows[number - 1]
                    if str(row["status"] or "") != "permanently_failed":
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
                    result = {
                        "summary": "已创建新的作者任务尝试，旧报告保持不变。",
                        "job": self._public_author_job(
                            connection.execute(
                                "SELECT * FROM author_jobs WHERE id=?",
                                (retry_id,),
                            ).fetchone(),
                            1,
                        ),
                    }
                elif action == "health.backup.create":
                    result = dict(payload.get("verified_result") or {})
                    if not result.get("archive_sha256"):
                        raise ValueError("备份尚未通过完整性校验")
                else:
                    result = {
                        "summary": "脱敏诊断已生成。",
                        "download_token": str(
                            payload.get("download_token") or ""
                        ),
                    }
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
                    action,
                    "",
                    {
                        "operation_id": operation_id,
                        "component": str(payload.get("component") or ""),
                    },
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

__all__ = ["HealthRecoveryRepositoryMixin"]
