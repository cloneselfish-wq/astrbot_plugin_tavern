"""Durable generation-reminder claims and delivery-outbox handoff."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    insert_session_event,
    json_dump,
    json_load,
    utc_now,
)
from ..lifecycle import normalize_time_rules
from ..resolution_receipts import content_hash
from ..delivery.target import DeliveryTarget
from ..generation_reminders import (
    ACTIVE_REMINDER_OPERATION_STATUSES,
    GenerationReminderConfig,
    GenerationReminderConfigError,
    GenerationReminderSchedule,
    GenerationReminderStateError,
    claim_due_reminder,
    format_utc,
    parse_utc,
    reminder_identity_digest,
)


STORY_GENERATION_OPERATION_TYPES = frozenset(
    {"turn", "vote_resolution", "dm_beat"}
)


def _stored_integer(
    row: Any,
    field: str,
    *,
    minimum: int = 0,
    allowed: frozenset[int] | None = None,
) -> int:
    value = row[field]
    if (
        type(value) is not int
        or value < minimum
        or (allowed is not None and value not in allowed)
    ):
        raise GenerationReminderStateError(
            f"persisted {field} is not a valid integer"
        )
    return value


def _stored_source(row: Any) -> str:
    request = json_load(row["request_json"], {})
    snapshot = (
        request.get("reminder_config")
        if isinstance(request, Mapping)
        else None
    )
    if isinstance(snapshot, Mapping):
        source = str(snapshot.get("source") or "")
        if source:
            return source
    return "implicit_default"


def _config_from_row(row: Any) -> GenerationReminderConfig:
    return GenerationReminderConfig(
        enabled=bool(
            _stored_integer(
                row,
                "reminder_enabled",
                allowed=frozenset({0, 1}),
            )
        ),
        interval_seconds=_stored_integer(
            row,
            "reminder_interval_seconds",
        ),
        source=_stored_source(row),
        revision=_stored_integer(row, "reminder_config_revision"),
        source_revision=_stored_integer(row, "reminder_source_revision"),
    )


def _reminder_view(row: Any) -> dict[str, Any]:
    return {
        "enabled": bool(row["reminder_enabled"]),
        "interval_seconds": int(row["reminder_interval_seconds"] or 60),
        "sequence": int(row["reminder_sequence"] or 0),
        "acknowledged": bool(row["reminder_acknowledged"]),
        "last_reminder_at": str(row["reminder_last_at"] or ""),
        "next_reminder_at": str(row["reminder_next_at"] or ""),
        "config_revision": int(row["reminder_config_revision"] or 0),
        "source_revision": int(row["reminder_source_revision"] or 0),
        "source": _stored_source(row),
    }


def _schedule_cache(repository: Any) -> dict[str, GenerationReminderSchedule]:
    cache = getattr(repository, "_generation_reminder_schedule_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(repository, "_generation_reminder_schedule_cache", cache)
    return cache


def _schedule_from_row(
    repository: Any,
    row: Any,
    config: GenerationReminderConfig,
    *,
    current_utc: datetime,
    current_monotonic: float,
) -> GenerationReminderSchedule:
    operation_id = str(row["operation_id"] or "")
    sequence = _stored_integer(row, "reminder_sequence")
    acknowledged = _stored_integer(
        row,
        "reminder_acknowledged",
        allowed=frozenset({0, 1}),
    )
    next_text = str(row["reminder_next_at"] or "")
    status = str(row["status"] or "")
    cache = _schedule_cache(repository)
    cached = cache.get(operation_id)
    if (
        cached is not None
        and cached.config == config
        and cached.reminder_sequence == sequence
        and not cached.stopped
    ):
        if status == "cancel_requested":
            return cached
        cached_due = cached.due_utc()
        if next_text and cached_due is not None and format_utc(cached_due) == next_text:
            return cached

    last_text = str(row["reminder_last_at"] or "")
    if next_text:
        schedule = GenerationReminderSchedule.recover(
            operation_id,
            config,
            reminder_sequence=sequence,
            next_reminder_at_utc=next_text,
            now_monotonic=current_monotonic,
            now_utc=current_utc,
            ack_sent=bool(acknowledged),
            last_reminder_at_utc=last_text or None,
        )
    else:
        schedule = GenerationReminderSchedule(
            operation_id=operation_id,
            config=config,
            anchor_monotonic=current_monotonic,
            anchor_utc=current_utc,
            reminder_sequence=sequence,
            ack_sent=bool(acknowledged),
            last_reminder_at_utc=(parse_utc(last_text) if last_text else None),
        )
    cache[operation_id] = schedule
    return schedule


def _claim_reminder_row_locked(
    repository: Any,
    connection: Any,
    row: Any,
    *,
    current_utc: datetime,
    current_monotonic: float,
) -> dict[str, Any] | None:
    try:
        config = _config_from_row(row)
    except GenerationReminderConfigError:
        config = GenerationReminderConfig(
            enabled=True,
            interval_seconds=60,
            source="implicit_default",
            revision=0,
            source_revision=0,
        )
    operation_id = str(row["operation_id"] or "")
    cancelling = str(row["status"] or "") == "cancel_requested"
    schedule = _schedule_from_row(
        repository,
        row,
        config,
        current_utc=current_utc,
        current_monotonic=current_monotonic,
    )
    status_text = str(row["status"] or "")
    progress_stage = (
        status_text
        if status_text in {"dice_locked", "ready_to_commit"}
        else row["last_progress_stage"] or row["phase"] or status_text
    )
    decision = claim_due_reminder(
        schedule,
        operation_status=row["status"],
        now_monotonic=current_monotonic,
        progress_stage=progress_stage,
    )
    emission = decision.emission
    if emission is None:
        return None
    sequence = emission.sequence
    claim_time = format_utc(emission.emitted_at_utc)
    following_due = decision.schedule.due_utc()
    next_due = (
        format_utc(following_due)
        if following_due is not None and not decision.schedule.stopped
        else ""
    )
    updated = connection.execute(
        """
        UPDATE operation_receipts SET
            reminder_sequence=?, reminder_last_at=?,
            reminder_next_at=?, last_progress_at=?, updated_at=?
            , status=CASE WHEN ? THEN 'cancelled' ELSE status END
            , phase=CASE WHEN ? THEN 'cancelled' ELSE phase END
            , lease_expires_at=CASE
                WHEN ? THEN '' ELSE lease_expires_at END
        WHERE operation_id=? AND reminder_sequence=?
          AND status=? AND reminder_next_at=?
        """,
        (
            sequence,
            claim_time,
            next_due,
            claim_time,
            claim_time,
            int(cancelling),
            int(cancelling),
            int(cancelling),
            row["operation_id"],
            schedule.reminder_sequence,
            row["status"],
            row["reminder_next_at"],
        ),
    )
    if updated.rowcount != 1:
        _schedule_cache(repository).pop(operation_id, None)
        return None
    _schedule_cache(repository)[operation_id] = decision.schedule

    origin = str(row["unified_origin"] or "")
    target = DeliveryTarget.from_origin(
        origin,
        verified_binding=True,
        source="generation_reminder",
    )
    if target is None:
        target = DeliveryTarget.webui_only(source="generation_reminder")
    digest = reminder_identity_digest(operation_id, sequence)
    delivery_id = "generation-reminder-delivery:" + digest
    delivery_kind = (
        "webui_only"
        if target.message_type == "webui_only"
        else "generation_reminder"
    )
    repository._create_delivery_locked(
        connection,
        {
            "delivery_id": delivery_id,
            "session_id": row["session_id"],
            "origin": origin or "webui:generation",
            "audience": "group",
            "target_snapshot": target.to_snapshot(),
            "message_type": delivery_kind,
            "rendered_parts": [emission.message],
            "text": emission.message,
            "status": (
                "webui_only"
                if delivery_kind == "webui_only"
                else "pending"
            ),
            "priority": 40,
            "next_retry_at": claim_time,
            "max_attempts": 8,
            "dedupe_key": emission.dedupe_key,
            "created_at": claim_time,
            "updated_at": claim_time,
            "meta": {
                "source": "story_generation_reminder",
                "sequence": sequence,
                "content_format": "plain",
            },
        },
    )
    insert_session_event(
        connection,
        session_id=str(row["session_id"] or ""),
        event_id=emission.event_id,
        type_="event:generation.changed",
        actor_ref="system",
        payload={
            "status": "cancelling" if cancelling else "generating",
            "reminder_sequence": sequence,
            "affected_modules": ["generation"],
        },
        created_at=claim_time,
    )
    return {
        "sequence": sequence,
        "kind": "cancelling" if cancelling else "progress",
        "delivery_id": delivery_id,
    }


class GenerationReminderRepositoryMixin:
    async def get_session_generation_reminder(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._get_session_generation_reminder,
            str(session_id),
        )

    def _get_session_generation_reminder(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT time_rules_json, updated_at FROM instance_configs "
                "WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise DatabaseNotFoundError("副本提醒设置不存在")
            rules = normalize_time_rules(
                json_load(row["time_rules_json"], {})
            )
            snapshot = dict(rules["story_generation_reminder"])
            return {
                **snapshot,
                "updated_at": str(row["updated_at"] or ""),
            }

    async def save_session_generation_reminder(
        self,
        session_id: str,
        *,
        enabled: bool,
        interval_seconds: int,
        inherit_global: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        global_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_generation_reminder,
            str(session_id),
            enabled,
            interval_seconds,
            inherit_global,
            expected_revision,
            str(actor_id),
            str(idempotency_key),
            dict(global_config or {}),
        )

    def _save_session_generation_reminder(
        self,
        session_id: str,
        enabled: bool,
        interval_seconds: int,
        inherit_global: bool,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        global_config: dict[str, Any],
    ) -> dict[str, Any]:
        request_key = str(idempotency_key or "").strip()
        if not request_key:
            raise ValueError("保存故事生成提醒需要幂等键")
        if type(inherit_global) is not bool:
            raise ValueError("继承全局设置必须为布尔值")
        request_payload = {
            "inherit_global": inherit_global,
            "expected_revision": int(expected_revision),
        }
        if not inherit_global:
            request_payload.update(
                {
                    "enabled": enabled,
                    "interval_seconds": interval_seconds,
                }
            )
        input_hash = content_hash(request_payload)
        operation_id = (
            "generation-reminder-setting:"
            + reminder_identity_digest(
                f"{session_id}\0{request_key}",
                1,
            )[:32]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同幂等键已用于另一份提醒设置"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError(
                        "提醒设置仍在处理中，请稍后重试"
                    )
                self._assert_session_writable(connection, session_id)
                config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if config is None:
                    raise DatabaseNotFoundError("副本提醒设置不存在")
                rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                current = dict(rules["story_generation_reminder"])
                current_revision = int(current.get("revision") or 0)
                if current_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "提醒设置已经变化；已保留你的草稿，请重新读取后比较"
                    )
                next_revision = current_revision + 1
                if inherit_global:
                    frozen = GenerationReminderConfig.from_mapping(
                        global_config,
                        source="global_default",
                        revision=next_revision,
                        source_revision=global_config.get("revision", 0),
                    )
                else:
                    frozen = GenerationReminderConfig(
                        enabled=enabled,
                        interval_seconds=interval_seconds,
                        source="session_override",
                        revision=next_revision,
                        source_revision=next_revision,
                    )
                snapshot = frozen.to_snapshot()
                rules["story_generation_reminder"] = snapshot
                rules = normalize_time_rules(rules)
                now = utc_now()
                result = {
                    **dict(rules["story_generation_reminder"]),
                    "updated_at": now,
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'generation.reminder_update', ?, ?,
                              'completed', 'committed', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        json_dump(result),
                        input_hash,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE instance_configs SET time_rules_json=?, "
                    "updated_at=? WHERE session_id=?",
                    (json_dump(rules), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "generation.reminder_update",
                    session_id,
                    {
                        "source": snapshot["source"],
                        "revision_before": current_revision,
                        "revision_after": next_revision,
                        "applies_to": "next_generation",
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{operation_id}:event",
                    type_="event:generation.changed",
                    actor_ref=actor_id,
                    command_id=operation_id,
                    payload={
                        "status": "reminder_configured",
                        "affected_modules": ["generation"],
                    },
                    visibility="public",
                    created_at=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def arm_generation_reminder(
        self,
        operation_id: str,
        config: GenerationReminderConfig | Mapping[str, Any],
    ) -> dict[str, Any]:
        frozen = (
            config
            if isinstance(config, GenerationReminderConfig)
            else GenerationReminderConfig.from_mapping(config)
        )
        return await self._run(
            self._arm_generation_reminder,
            str(operation_id),
            frozen,
        )

    def _arm_generation_reminder(
        self,
        operation_id: str,
        frozen: GenerationReminderConfig,
    ) -> dict[str, Any]:
        now = parse_utc(utc_now())
        now_text = format_utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("故事生成操作不存在")
                if str(row["operation_type"] or "") not in (
                    STORY_GENERATION_OPERATION_TYPES
                ):
                    raise ValueError("该操作不是故事生成流程")

                request = json_load(row["request_json"], {})
                request = dict(request) if isinstance(request, Mapping) else {}
                previous = request.get("reminder_config")
                if isinstance(previous, Mapping):
                    stored = _config_from_row(row)
                    next_due = str(row["reminder_next_at"] or "")
                    if (
                        not next_due
                        and stored.enabled
                        and str(row["status"] or "")
                        in ACTIVE_REMINDER_OPERATION_STATUSES
                    ):
                        next_due = format_utc(
                            now + timedelta(seconds=stored.interval_seconds)
                        )
                        connection.execute(
                            "UPDATE operation_receipts SET reminder_next_at=?, "
                            "updated_at=? WHERE operation_id=?",
                            (next_due, now_text, operation_id),
                        )
                else:
                    request["reminder_config"] = frozen.to_snapshot()
                    next_due = (
                        format_utc(
                            now
                            + timedelta(seconds=frozen.interval_seconds)
                        )
                        if frozen.enabled
                        else ""
                    )
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            request_json=?, reminder_enabled=?,
                            reminder_interval_seconds=?,
                            reminder_config_revision=?,
                            reminder_source_revision=?, reminder_next_at=?,
                            updated_at=?
                        WHERE operation_id=?
                        """,
                        (
                            json_dump(request),
                            int(frozen.enabled),
                            frozen.interval_seconds,
                            frozen.revision,
                            frozen.source_revision,
                            next_due,
                            now_text,
                            operation_id,
                        ),
                    )
                refreshed = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                connection.execute("COMMIT")
                _schedule_cache(self).pop(operation_id, None)
                return _reminder_view(refreshed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def claim_generation_ack(self, operation_id: str) -> dict[str, Any]:
        return await self._run(self._claim_generation_ack, str(operation_id))

    def _claim_generation_ack(self, operation_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("故事生成操作不存在")
                emit = (
                    not bool(row["reminder_acknowledged"])
                    and str(row["status"] or "")
                    in ACTIVE_REMINDER_OPERATION_STATUSES
                )
                if emit:
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            reminder_acknowledged=1,
                            last_progress_at=?, updated_at=?
                        WHERE operation_id=? AND reminder_acknowledged=0
                        """,
                        (now, now, operation_id),
                    )
                    insert_session_event(
                        connection,
                        session_id=str(row["session_id"] or ""),
                        event_id=(
                            "generation-ack:"
                            + reminder_identity_digest(operation_id, 1)
                        ),
                        type_="event:generation.changed",
                        actor_ref="system",
                        payload={
                            "status": "accepted",
                            "affected_modules": ["generation"],
                        },
                    )
                connection.execute("COMMIT")
                return {"emit": emit, "at": now}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def claim_due_generation_reminders(
        self,
        *,
        now: str | None = None,
        now_monotonic: float | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        current_utc = now or utc_now()
        monotonic_value = (
            float(now_monotonic)
            if now_monotonic is not None
            else parse_utc(current_utc).timestamp()
        )
        return await self._run(
            self._claim_due_generation_reminders,
            current_utc,
            monotonic_value,
            max(1, min(100, int(limit))),
        )

    def _claim_due_generation_reminders(
        self,
        now: str,
        now_monotonic: float,
        limit: int,
    ) -> dict[str, Any]:
        current = parse_utc(now)
        active = sorted(ACTIVE_REMINDER_OPERATION_STATUSES)
        statuses = ",".join("?" for _ in active)
        kinds = sorted(STORY_GENERATION_OPERATION_TYPES)
        kind_placeholders = ",".join("?" for _ in kinds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    f"""
                    SELECT o.*, s.unified_origin
                    FROM operation_receipts o
                    JOIN sessions s ON s.id=o.session_id
                    WHERE o.operation_type IN ({kind_placeholders})
                      AND o.status IN ({statuses})
                      AND (
                        o.status='cancel_requested'
                        OR (
                          o.reminder_enabled=1
                          AND o.reminder_next_at<>''
                        )
                      )
                    ORDER BY
                      CASE WHEN o.status='cancel_requested' THEN 0 ELSE 1 END,
                      o.reminder_next_at, o.created_at
                    LIMIT ?
                    """,
                    (*kinds, *active, limit),
                ).fetchall()
                visible_operations = {
                    str(row["operation_id"] or "") for row in rows
                }
                cache = _schedule_cache(self)
                for operation_id in tuple(cache):
                    if operation_id not in visible_operations:
                        cache.pop(operation_id, None)
                claimed: list[dict[str, Any]] = []
                problems: list[dict[str, Any]] = []
                for row in rows:
                    connection.execute("SAVEPOINT generation_reminder_row")
                    try:
                        item = _claim_reminder_row_locked(
                            self,
                            connection,
                            row,
                            current_utc=current,
                            current_monotonic=now_monotonic,
                        )
                    except Exception as exc:
                        connection.execute(
                            "ROLLBACK TO SAVEPOINT generation_reminder_row"
                        )
                        connection.execute(
                            "RELEASE SAVEPOINT generation_reminder_row"
                        )
                        operation_id = str(row["operation_id"] or "")
                        _schedule_cache(self).pop(operation_id, None)
                        problem_code = "generation.reminder_state_invalid"
                        quarantined = False
                        connection.execute(
                            "SAVEPOINT generation_reminder_quarantine"
                        )
                        try:
                            stopped = connection.execute(
                                """
                                UPDATE operation_receipts SET
                                    reminder_enabled=0, reminder_next_at='',
                                    reminder_sequence=CASE
                                        WHEN typeof(reminder_sequence)='integer'
                                         AND reminder_sequence >= 0
                                        THEN reminder_sequence ELSE 0 END,
                                    last_error_code=?, updated_at=?
                                WHERE operation_id=? AND status=?
                                """,
                                (
                                    problem_code,
                                    format_utc(current),
                                    row["operation_id"],
                                    row["status"],
                                ),
                            )
                            quarantined = stopped.rowcount == 1
                        except Exception:
                            connection.execute(
                                "ROLLBACK TO SAVEPOINT "
                                "generation_reminder_quarantine"
                            )
                        finally:
                            connection.execute(
                                "RELEASE SAVEPOINT "
                                "generation_reminder_quarantine"
                            )
                        problems.append(
                            {
                                "code": (
                                    problem_code
                                    if quarantined
                                    else "generation.reminder_quarantine_failed"
                                ),
                                "operation_id": operation_id,
                                "error_type": type(exc).__name__,
                            }
                        )
                        continue
                    connection.execute(
                        "RELEASE SAVEPOINT generation_reminder_row"
                    )
                    if item is not None:
                        claimed.append(item)
                connection.execute("COMMIT")
                return {
                    "claimed": len(claimed),
                    "items": claimed,
                    "problems": problems,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise


__all__ = [
    "GenerationReminderRepositoryMixin",
    "STORY_GENERATION_OPERATION_TYPES",
]
