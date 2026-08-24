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


class OutboxRepositoryMixin:
    def _insert_event_outbox_intent_locked(
        self,
        connection: Any,
        plan: TurnCommitPlan,
        intent: EventHookIntent,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM event_outbox WHERE dedupe_key=?",
            (intent.dedupe_key,),
        ).fetchone()
        payload = dict(intent.payload)
        if intent.event_id:
            event = connection.execute(
                "SELECT seq FROM session_events WHERE event_id=?",
                (intent.event_id,),
            ).fetchone()
            if event is not None:
                payload.setdefault("latest_seq", int(event["seq"] or 0))
        payload_json = json_dump(payload)
        if existing is not None:
            if (
                str(existing["session_id"] or "") != plan.session_id
                or str(existing["event_id"] or "") != intent.event_id
                or str(existing["topic"] or "") != intent.topic
                or str(existing["payload_json"] or "") != payload_json
                or str(existing["audience"] or "") != intent.audience
            ):
                raise DatabaseConflictError(
                    "event outbox 防重复凭据已用于不同内容"
                )
            return
        now = utc_now()
        connection.execute(
            """
            INSERT INTO event_outbox(
                id, session_id, event_id, topic, payload_json,
                audience, dedupe_key, status, max_attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 8, ?, ?)
            """,
            (
                new_id("event_outbox"),
                plan.session_id,
                intent.event_id,
                intent.topic,
                payload_json,
                intent.audience,
                intent.dedupe_key,
                now,
                now,
            ),
        )

    async def enqueue_event_outbox(
        self,
        *,
        topic: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        session_id: str = "",
        event_id: str = "",
        audience: str = "internal",
        max_attempts: int = 8,
    ) -> dict[str, Any]:
        return await self._run(
            self._enqueue_event_outbox,
            str(topic),
            dict(payload),
            str(dedupe_key),
            str(session_id),
            str(event_id),
            str(audience),
            int(max_attempts),
        )

    def _enqueue_event_outbox(
        self,
        topic: str,
        payload: Mapping[str, Any],
        dedupe_key: str,
        session_id: str,
        event_id: str,
        audience: str,
        max_attempts: int,
    ) -> dict[str, Any]:
        if not topic or not dedupe_key:
            raise ValueError("event outbox 必须包含 topic 和 dedupe_key")
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO event_outbox(
                    id, session_id, event_id, topic, payload_json,
                    audience, dedupe_key, status, max_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    new_id("event_outbox"),
                    session_id,
                    event_id,
                    topic[:160],
                    json_dump(payload),
                    audience[:80],
                    dedupe_key[:240],
                    max(1, min(20, max_attempts)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM event_outbox WHERE dedupe_key=?",
                (dedupe_key[:240],),
            ).fetchone()
        return dict(row)

    async def claim_event_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._claim_event_outbox,
            str(worker_id),
            int(limit),
            int(lease_seconds),
        )

    def _claim_event_outbox(
        self,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        if not worker_id:
            raise ValueError("event worker_id 不能为空")
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
                connection.execute(
                    """
                    UPDATE event_outbox SET
                        status='retry_wait', lease_owner='',
                        leased_at='', lease_expires_at='',
                        next_retry_at=?, updated_at=?
                    WHERE status='leased'
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (now, now, now),
                )
                rows = connection.execute(
                    """
                    SELECT * FROM event_outbox
                    WHERE status IN ('pending', 'retry_wait')
                      AND (next_retry_at='' OR next_retry_at<=?)
                    ORDER BY created_at, id LIMIT ?
                    """,
                    (now, max(1, min(100, limit))),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE event_outbox SET
                            status='leased', attempts=attempts + 1,
                            lease_owner=?, leased_at=?,
                            lease_expires_at=?, updated_at=?
                        WHERE id=? AND status IN ('pending', 'retry_wait')
                        """,
                        (
                            worker_id,
                            now,
                            lease_until,
                            now,
                            row["id"],
                        ),
                    )
                    current = connection.execute(
                        "SELECT * FROM event_outbox WHERE id=?",
                        (row["id"],),
                    ).fetchone()
                    if current is not None and current["lease_owner"] == worker_id:
                        item = dict(current)
                        item["payload"] = json_load(
                            item.pop("payload_json"),
                            {},
                        )
                        claimed.append(item)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    async def finish_event_outbox(
        self,
        record_id: str,
        worker_id: str,
        *,
        delivered: bool,
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._finish_event_outbox,
            str(record_id),
            str(worker_id),
            bool(delivered),
            str(error_code),
            _safe_summary(error_message, 1000),
        )

    def _finish_event_outbox(
        self,
        record_id: str,
        worker_id: str,
        delivered: bool,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM event_outbox WHERE id=?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("event outbox 记录不存在")
                if (
                    row["status"] != "leased"
                    or str(row["lease_owner"] or "") != worker_id
                ):
                    raise InvalidTransitionError("event outbox 租约不属于当前 worker")
                now = utc_now()
                if delivered:
                    connection.execute(
                        """
                        UPDATE event_outbox SET
                            status='delivered', lease_owner='',
                            leased_at='', lease_expires_at='',
                            next_retry_at='', last_error_code='',
                            last_error='', delivered_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, now, record_id),
                    )
                else:
                    permanent = int(row["attempts"] or 0) >= int(
                        row["max_attempts"] or 8
                    )
                    connection.execute(
                        """
                        UPDATE event_outbox SET
                            status=?, lease_owner='', leased_at='',
                            lease_expires_at='', next_retry_at=?,
                            last_error_code=?, last_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            (
                                "permanently_failed"
                                if permanent
                                else "retry_wait"
                            ),
                            (
                                ""
                                if permanent
                                else retry_backoff_after(
                                    int(row["attempts"] or 1)
                                )
                            ),
                            error_code[:120] or "event.dispatch_failed",
                            error_message,
                            now,
                            record_id,
                        ),
                    )
                current = connection.execute(
                    "SELECT * FROM event_outbox WHERE id=?",
                    (record_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return dict(current)

    async def claim_storage_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 20,
        lease_seconds: int = 120,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._claim_storage_outbox,
            str(worker_id),
            int(limit),
            int(lease_seconds),
        )

    def _claim_storage_outbox(
        self,
        worker_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        if not worker_id:
            raise ValueError("storage worker_id 不能为空")
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
                connection.execute(
                    """
                    UPDATE storage_sync_outbox SET
                        status='retry_wait', lease_owner='',
                        leased_at='', lease_expires_at='',
                        next_retry_at=?, updated_at=?
                    WHERE status='leased'
                      AND lease_expires_at<>'' AND lease_expires_at<=?
                    """,
                    (now, now, now),
                )
                rows = connection.execute(
                    """
                    SELECT * FROM storage_sync_outbox
                    WHERE status IN ('pending', 'retry_wait')
                      AND (next_retry_at='' OR next_retry_at<=?)
                    ORDER BY created_at, session_id LIMIT ?
                    """,
                    (now, max(1, min(100, limit))),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        """
                        UPDATE storage_sync_outbox SET
                            status='leased', attempts=attempts + 1,
                            leased_generation=desired_generation,
                            lease_owner=?, leased_at=?,
                            lease_expires_at=?, updated_at=?
                        WHERE session_id=? AND kind=?
                          AND status IN ('pending', 'retry_wait')
                        """,
                        (
                            worker_id,
                            now,
                            lease_until,
                            now,
                            row["session_id"],
                            row["kind"],
                        ),
                    )
                    current = connection.execute(
                        """
                        SELECT * FROM storage_sync_outbox
                        WHERE session_id=? AND kind=?
                        """,
                        (row["session_id"], row["kind"]),
                    ).fetchone()
                    if current is not None and current["lease_owner"] == worker_id:
                        item = dict(current)
                        item["payload"] = json_load(
                            item.pop("payload_json"),
                            {},
                        )
                        claimed.append(item)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return claimed

    async def finish_storage_outbox(
        self,
        session_id: str,
        kind: str,
        worker_id: str,
        *,
        delivered: bool,
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._finish_storage_outbox,
            str(session_id),
            str(kind),
            str(worker_id),
            bool(delivered),
            str(error_code),
            _safe_summary(error_message, 1000),
        )

    def _finish_storage_outbox(
        self,
        session_id: str,
        kind: str,
        worker_id: str,
        delivered: bool,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM storage_sync_outbox
                    WHERE session_id=? AND kind=?
                    """,
                    (session_id, kind),
                ).fetchone()
                if row is None:
                    return {
                        "session_id": session_id,
                        "kind": kind,
                        "status": "delivered",
                    }
                if (
                    row["status"] != "leased"
                    or str(row["lease_owner"] or "") != worker_id
                ):
                    raise InvalidTransitionError(
                        "storage outbox 租约不属于当前 worker"
                    )
                if delivered:
                    desired = int(row["desired_generation"] or 1)
                    leased = int(row["leased_generation"] or 0)
                    if desired > leased:
                        # 同步期间被再次入队：完成旧代际后立即保留为 pending，
                        # 等待下一次同步；不得删除新代际。
                        connection.execute(
                            """
                            UPDATE storage_sync_outbox SET
                                status='pending',
                                completed_generation=?,
                                lease_owner='', leased_at='',
                                lease_expires_at='', next_retry_at='',
                                last_error_code='', last_error='',
                                updated_at=?
                            WHERE session_id=? AND kind=?
                            """,
                            (leased, utc_now(), session_id, kind),
                        )
                        result = {
                            "session_id": session_id,
                            "kind": kind,
                            "status": "pending",
                            "completed_generation": leased,
                            "desired_generation": desired,
                        }
                    else:
                        connection.execute(
                            """
                            DELETE FROM storage_sync_outbox
                            WHERE session_id=? AND kind=?
                            """,
                            (session_id, kind),
                        )
                        result = {
                            "session_id": session_id,
                            "kind": kind,
                            "status": "delivered",
                        }
                else:
                    permanent = int(row["attempts"] or 0) >= int(
                        row["max_attempts"] or 8
                    )
                    connection.execute(
                        """
                        UPDATE storage_sync_outbox SET
                            status=?, lease_owner='', leased_at='',
                            lease_expires_at='', next_retry_at=?,
                            last_error_code=?, last_error=?, updated_at=?
                        WHERE session_id=? AND kind=?
                        """,
                        (
                            (
                                "permanently_failed"
                                if permanent
                                else "retry_wait"
                            ),
                            (
                                ""
                                if permanent
                                else retry_backoff_after(
                                    int(row["attempts"] or 1)
                                )
                            ),
                            error_code[:120] or "storage.sync_failed",
                            error_message,
                            utc_now(),
                            session_id,
                            kind,
                        ),
                    )
                    current = connection.execute(
                        """
                        SELECT * FROM storage_sync_outbox
                        WHERE session_id=? AND kind=?
                        """,
                        (session_id, kind),
                    ).fetchone()
                    result = dict(current)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return result


__all__ = ["OutboxRepositoryMixin"]
