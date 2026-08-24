"""Pure RC8 story-generation reminder scheduling contracts.

This module deliberately has no database, worker, transport, or WebUI imports.
It defines the immutable configuration snapshot and the clock arithmetic that
those integration layers must share.  A live process schedules against a
monotonic clock; persisted UTC timestamps are used only to reconstruct that
monotonic schedule after restart.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_REMINDER_ENABLED = True
DEFAULT_REMINDER_INTERVAL_SECONDS = 60
MIN_REMINDER_INTERVAL_SECONDS = 30
MAX_REMINDER_INTERVAL_SECONDS = 600
REMINDER_INTERVAL_STEP_SECONDS = 15

REMINDER_CONFIG_SOURCES = frozenset(
    {"global_default", "session_override", "implicit_default"}
)

ACTIVE_REMINDER_OPERATION_STATUSES = frozenset(
    {
        "pending",
        "reserved",
        "generating",
        "dice_locked",
        "ready_to_commit",
        "cancel_requested",
    }
)
TERMINAL_REMINDER_OPERATION_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "failed_retryable",
        "needs_recovery",
        "compensated",
        "cancelled",
    }
)
REMINDER_OPERATION_STATUSES = (
    ACTIVE_REMINDER_OPERATION_STATUSES
    | TERMINAL_REMINDER_OPERATION_STATUSES
)

IMMEDIATE_ACK_MESSAGE = (
    "【请求已接受】\n"
    "正在准备本轮故事。\n\n"
    "自动处理：结果安全提交前不会改变世界，已锁定检定不会重复。\n\n"
    "下一步：请等待故事结果；如需停止，请发送：\n"
    "/团 取消"
)
CANCELLATION_MESSAGE = (
    "【正在安全取消】\n"
    "取消请求已收到，系统正在停止尚未提交的故事生成。\n\n"
    "自动处理：已锁定检定不会重复，已经提交的故事不会回滚。\n\n"
    "下一步：请等待取消结果，无需重复发送命令。"
)


class GenerationReminderConfigError(ValueError):
    """A reminder setting cannot be accepted as an authoritative snapshot."""


class GenerationReminderStateError(ValueError):
    """A persisted schedule or operation status violates the RC8 contract."""


def _strict_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise GenerationReminderConfigError(f"{field} must be a boolean")
    return value


def _strict_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise GenerationReminderConfigError(
            "reminder config revision must be a non-negative integer"
        )
    return value


def validate_reminder_interval(value: object) -> int:
    """Return one valid interval; never coerce, clamp, or accept bools."""

    if type(value) is not int:
        raise GenerationReminderConfigError(
            "story generation reminder interval must be an integer"
        )
    if not MIN_REMINDER_INTERVAL_SECONDS <= value <= MAX_REMINDER_INTERVAL_SECONDS:
        raise GenerationReminderConfigError(
            "story generation reminder interval must be between 30 and 600 seconds"
        )
    if value % REMINDER_INTERVAL_STEP_SECONDS != 0:
        raise GenerationReminderConfigError(
            "story generation reminder interval must use 15-second steps"
        )
    return value


def _monotonic(value: object) -> float:
    if isinstance(value, bool):
        raise GenerationReminderStateError("monotonic time must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GenerationReminderStateError("monotonic time must be finite") from exc
    if not math.isfinite(result):
        raise GenerationReminderStateError("monotonic time must be finite")
    return result


def parse_utc(value: datetime | str) -> datetime:
    """Normalize a timezone-aware datetime or ISO string to UTC."""

    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise GenerationReminderStateError("UTC timestamp is required")
        try:
            parsed = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError as exc:
            raise GenerationReminderStateError("UTC timestamp is invalid") from exc
    else:
        raise GenerationReminderStateError("UTC timestamp is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GenerationReminderStateError("UTC timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime | str) -> str:
    return parse_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class GenerationReminderConfig:
    """Immutable per-operation reminder settings.

    ``revision`` is the session-local compare-and-swap revision.  The separate
    ``source_revision`` identifies the global/session source snapshot that was
    frozen when the generation operation started.
    """

    enabled: bool = DEFAULT_REMINDER_ENABLED
    interval_seconds: int = DEFAULT_REMINDER_INTERVAL_SECONDS
    source: str = "global_default"
    revision: int = 0
    source_revision: int = 0

    def __post_init__(self) -> None:
        _strict_bool(self.enabled, field="story generation reminder enabled")
        validate_reminder_interval(self.interval_seconds)
        if self.source not in REMINDER_CONFIG_SOURCES:
            raise GenerationReminderConfigError(
                "story generation reminder source is invalid"
            )
        _strict_revision(self.revision)
        _strict_revision(self.source_revision)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        source: str | None = None,
        revision: object | None = None,
        fail_safe: bool = False,
        source_revision: object | None = None,
    ) -> "GenerationReminderConfig":
        """Freeze session-style or global-style settings.

        ``fail_safe=True`` is the runtime-read boundary: any malformed value
        becomes the documented true/60 default instead of the predecessor 8/30 timing.
        Interactive save boundaries should leave it false and report the error.
        """

        raw = dict(value) if isinstance(value, Mapping) else {}
        enabled = raw.get(
            "enabled",
            raw.get(
                "story_generation_reminder_enabled",
                DEFAULT_REMINDER_ENABLED,
            ),
        )
        interval = raw.get(
            "interval_seconds",
            raw.get(
                "story_generation_reminder_interval_seconds",
                DEFAULT_REMINDER_INTERVAL_SECONDS,
            ),
        )
        resolved_source = str(
            source if source is not None else raw.get("source") or "global_default"
        )
        resolved_revision = (
            revision
            if revision is not None
            else raw.get("revision", raw.get("config_revision", 0))
        )
        resolved_source_revision = (
            source_revision
            if source_revision is not None
            else raw.get("source_revision", resolved_revision)
        )
        try:
            return cls(
                enabled=_strict_bool(
                    enabled,
                    field="story generation reminder enabled",
                ),
                interval_seconds=validate_reminder_interval(interval),
                source=resolved_source,
                revision=_strict_revision(resolved_revision),
                source_revision=_strict_revision(resolved_source_revision),
            )
        except GenerationReminderConfigError:
            if not fail_safe:
                raise
            fallback_source = (
                resolved_source
                if resolved_source in REMINDER_CONFIG_SOURCES
                else "implicit_default"
            )
            fallback_revision = (
                resolved_revision
                if type(resolved_revision) is int and resolved_revision >= 0
                else 0
            )
            return cls(
                enabled=DEFAULT_REMINDER_ENABLED,
                interval_seconds=DEFAULT_REMINDER_INTERVAL_SECONDS,
                source=fallback_source,
                revision=fallback_revision,
                source_revision=(
                    resolved_source_revision
                    if type(resolved_source_revision) is int
                    and resolved_source_revision >= 0
                    else 0
                ),
            )

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "source": self.source,
            "revision": self.revision,
            "source_revision": self.source_revision,
        }


def freeze_generation_reminder_config(
    value: GenerationReminderConfig | Mapping[str, Any] | None,
    *,
    source: str | None = None,
    revision: object | None = None,
    fail_safe: bool = False,
) -> GenerationReminderConfig:
    if isinstance(value, GenerationReminderConfig):
        return GenerationReminderConfig(**value.to_snapshot())
    return GenerationReminderConfig.from_mapping(
        value,
        source=source,
        revision=revision,
        fail_safe=fail_safe,
    )


def _operation_id(value: object) -> str:
    result = str(value or "").strip()
    if not result:
        raise GenerationReminderStateError("operation id is required")
    return result


def _sequence(value: object, *, allow_zero: bool) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise GenerationReminderStateError(
            f"reminder sequence must be an integer >= {minimum}"
        )
    return value


def reminder_identity_digest(operation_id: object, sequence: object) -> str:
    """Hash the complete operation-id/sequence pair without truncation."""

    operation = _operation_id(operation_id)
    number = _sequence(sequence, allow_zero=False)
    material = f"generation-reminder\0{operation}\0{number}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def reminder_dedupe_key(operation_id: object, sequence: object) -> str:
    return "generation-reminder:" + reminder_identity_digest(
        operation_id, sequence
    )


def reminder_event_id(operation_id: object, sequence: object) -> str:
    return "generation-reminder-event:" + reminder_identity_digest(
        operation_id, sequence
    )


def reminder_safe_stage(value: object) -> str:
    raw = str(value or "").strip().lower()
    if any(token in raw for token in ("commit", "ready_to_commit", "save")):
        return "安全提交"
    if any(token in raw for token in ("dice", "check", "resolv", "vote")):
        return "结算行动"
    if any(
        token in raw
        for token in ("validat", "quality", "repair", "narrative", "generat", "model")
    ):
        return "整理正文"
    if any(token in raw for token in ("deliver", "send")):
        return "投递故事"
    return "准备故事"


def progress_reminder_message(
    elapsed_minutes: int,
    progress_stage: object = "",
) -> str:
    waited = (
        f"{max(0, int(elapsed_minutes))} 分钟"
        if elapsed_minutes > 0
        else "未满 1 分钟"
    )
    return (
        "【故事生成中】\n"
        f"当前进度：{reminder_safe_stage(progress_stage)}。\n"
        "正在安全处理本轮故事。\n\n"
        f"已等待：{waited}。\n\n"
        "自动处理：结果安全提交前不会改变世界，已锁定检定不会重复。\n\n"
        "下一步：可以继续等待，或发送：\n"
        "/团 取消"
    )


@dataclass(frozen=True, slots=True)
class ReminderEmission:
    sequence: int
    kind: str
    due_at_utc: datetime
    emitted_at_utc: datetime
    elapsed_minutes: int
    dedupe_key: str
    event_id: str
    message: str

    def __post_init__(self) -> None:
        _sequence(self.sequence, allow_zero=False)
        if self.kind not in {"progress", "cancelling"}:
            raise GenerationReminderStateError("reminder emission kind is invalid")
        object.__setattr__(self, "due_at_utc", parse_utc(self.due_at_utc))
        object.__setattr__(self, "emitted_at_utc", parse_utc(self.emitted_at_utc))


@dataclass(frozen=True, slots=True)
class GenerationReminderSchedule:
    """Immutable state advanced only by claim functions.

    ``anchor_monotonic`` and ``anchor_utc`` describe the same t=0 acceptance
    instant.  Sequence N is always due at ``anchor + N * interval``.
    """

    operation_id: str
    config: GenerationReminderConfig
    anchor_monotonic: float
    anchor_utc: datetime
    reminder_sequence: int = 0
    ack_sent: bool = False
    last_reminder_at_utc: datetime | None = None
    cancel_notice_sent: bool = False
    stopped: bool = False
    stop_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation_id", _operation_id(self.operation_id))
        if not isinstance(self.config, GenerationReminderConfig):
            raise GenerationReminderStateError(
                "schedule requires a frozen reminder config"
            )
        object.__setattr__(
            self,
            "anchor_monotonic",
            _monotonic(self.anchor_monotonic),
        )
        object.__setattr__(self, "anchor_utc", parse_utc(self.anchor_utc))
        _sequence(self.reminder_sequence, allow_zero=True)
        if type(self.ack_sent) is not bool:
            raise GenerationReminderStateError("ack_sent must be boolean")
        if type(self.cancel_notice_sent) is not bool:
            raise GenerationReminderStateError(
                "cancel_notice_sent must be boolean"
            )
        if type(self.stopped) is not bool:
            raise GenerationReminderStateError("stopped must be boolean")
        if self.last_reminder_at_utc is not None:
            object.__setattr__(
                self,
                "last_reminder_at_utc",
                parse_utc(self.last_reminder_at_utc),
            )
        if self.cancel_notice_sent and not self.stopped:
            raise GenerationReminderStateError(
                "a cancellation notice must stop further reminders"
            )

    @classmethod
    def begin(
        cls,
        operation_id: object,
        config: GenerationReminderConfig | Mapping[str, Any] | None,
        *,
        now_monotonic: object,
        now_utc: datetime | str,
        source: str | None = None,
        revision: object | None = None,
        fail_safe_config: bool = False,
    ) -> "GenerationReminderSchedule":
        return cls(
            operation_id=_operation_id(operation_id),
            config=freeze_generation_reminder_config(
                config,
                source=source,
                revision=revision,
                fail_safe=fail_safe_config,
            ),
            anchor_monotonic=_monotonic(now_monotonic),
            anchor_utc=parse_utc(now_utc),
        )

    @classmethod
    def recover(
        cls,
        operation_id: object,
        config: GenerationReminderConfig | Mapping[str, Any] | None,
        *,
        reminder_sequence: object,
        next_reminder_at_utc: datetime | str,
        now_monotonic: object,
        now_utc: datetime | str,
        ack_sent: bool = True,
        last_reminder_at_utc: datetime | str | None = None,
        source: str | None = None,
        revision: object | None = None,
        fail_safe_config: bool = False,
    ) -> "GenerationReminderSchedule":
        """Rebuild a monotonic anchor from the persisted next UTC deadline."""

        frozen = freeze_generation_reminder_config(
            config,
            source=source,
            revision=revision,
            fail_safe=fail_safe_config,
        )
        sequence = _sequence(reminder_sequence, allow_zero=True)
        next_due = parse_utc(next_reminder_at_utc)
        current_utc = parse_utc(now_utc)
        current_monotonic = _monotonic(now_monotonic)
        next_number = sequence + 1
        seconds_until_due = (next_due - current_utc).total_seconds()
        anchor_monotonic = (
            current_monotonic
            + seconds_until_due
            - next_number * frozen.interval_seconds
        )
        anchor_utc = next_due - timedelta(
            seconds=next_number * frozen.interval_seconds
        )
        return cls(
            operation_id=_operation_id(operation_id),
            config=frozen,
            anchor_monotonic=anchor_monotonic,
            anchor_utc=anchor_utc,
            reminder_sequence=sequence,
            ack_sent=ack_sent,
            last_reminder_at_utc=(
                parse_utc(last_reminder_at_utc)
                if last_reminder_at_utc is not None
                else None
            ),
        )

    def due_monotonic(self, sequence: int | None = None) -> float | None:
        if self.stopped or not self.config.enabled:
            return None
        number = (
            self.reminder_sequence + 1
            if sequence is None
            else _sequence(sequence, allow_zero=False)
        )
        return self.anchor_monotonic + number * self.config.interval_seconds

    def due_utc(self, sequence: int | None = None) -> datetime | None:
        if self.stopped or not self.config.enabled:
            return None
        number = (
            self.reminder_sequence + 1
            if sequence is None
            else _sequence(sequence, allow_zero=False)
        )
        return self.anchor_utc + timedelta(
            seconds=number * self.config.interval_seconds
        )

    def utc_at_monotonic(self, now_monotonic: object) -> datetime:
        current = _monotonic(now_monotonic)
        return self.anchor_utc + timedelta(
            seconds=current - self.anchor_monotonic
        )

    def progress_record(self) -> dict[str, Any]:
        next_due = self.due_utc()
        return {
            "reminder_enabled": self.config.enabled,
            "reminder_interval_seconds": self.config.interval_seconds,
            "reminder_sequence": self.reminder_sequence,
            "last_reminder_at": (
                format_utc(self.last_reminder_at_utc)
                if self.last_reminder_at_utc is not None
                else ""
            ),
            "next_reminder_at": format_utc(next_due) if next_due else "",
            "reminder_config_revision": self.config.revision,
            "reminder_source_revision": self.config.source_revision,
        }


@dataclass(frozen=True, slots=True)
class ImmediateAckDecision:
    schedule: GenerationReminderSchedule
    emit: bool
    message: str = IMMEDIATE_ACK_MESSAGE


@dataclass(frozen=True, slots=True)
class ReminderDecision:
    schedule: GenerationReminderSchedule
    emission: ReminderEmission | None = None


def claim_immediate_ack(
    schedule: GenerationReminderSchedule,
) -> ImmediateAckDecision:
    """Claim t=0 acknowledgement once without consuming reminder sequence 1."""

    if schedule.ack_sent:
        return ImmediateAckDecision(schedule=schedule, emit=False)
    return ImmediateAckDecision(
        schedule=replace(schedule, ack_sent=True),
        emit=True,
    )


def _emission(
    schedule: GenerationReminderSchedule,
    *,
    sequence: int,
    kind: str,
    due_at_utc: datetime,
    now_monotonic: float,
    progress_stage: object = "",
) -> ReminderEmission:
    emitted_at = schedule.utc_at_monotonic(now_monotonic)
    elapsed_seconds = max(0.0, now_monotonic - schedule.anchor_monotonic)
    elapsed_minutes = int(elapsed_seconds // 60)
    return ReminderEmission(
        sequence=sequence,
        kind=kind,
        due_at_utc=due_at_utc,
        emitted_at_utc=emitted_at,
        elapsed_minutes=elapsed_minutes,
        dedupe_key=reminder_dedupe_key(schedule.operation_id, sequence),
        event_id=reminder_event_id(schedule.operation_id, sequence),
        message=(
            CANCELLATION_MESSAGE
            if kind == "cancelling"
            else progress_reminder_message(elapsed_minutes, progress_stage)
        ),
    )


def claim_due_reminder(
    schedule: GenerationReminderSchedule,
    *,
    operation_status: object,
    now_monotonic: object,
    progress_stage: object = "",
) -> ReminderDecision:
    """Advance at most one delivery claim.

    Database integration must run the persisted compare/update and outbox/event
    inserts in one transaction.  This pure function supplies the exact next
    state and deterministic identities for that transaction.
    """

    status = str(operation_status or "").strip().lower()
    if status not in REMINDER_OPERATION_STATUSES:
        raise GenerationReminderStateError("operation status is not registered")
    current = _monotonic(now_monotonic)
    if schedule.stopped:
        return ReminderDecision(schedule=schedule)
    if status in TERMINAL_REMINDER_OPERATION_STATUSES:
        return ReminderDecision(
            schedule=replace(
                schedule,
                stopped=True,
                stop_reason=status,
            )
        )
    if status == "cancel_requested":
        if schedule.cancel_notice_sent:
            return ReminderDecision(schedule=schedule)
        sequence = schedule.reminder_sequence + 1
        emitted_at = schedule.utc_at_monotonic(current)
        emission = _emission(
            schedule,
            sequence=sequence,
            kind="cancelling",
            due_at_utc=emitted_at,
            now_monotonic=current,
        )
        return ReminderDecision(
            schedule=replace(
                schedule,
                reminder_sequence=sequence,
                last_reminder_at_utc=emission.emitted_at_utc,
                cancel_notice_sent=True,
                stopped=True,
                stop_reason="cancel_requested",
            ),
            emission=emission,
        )
    if not schedule.config.enabled:
        return ReminderDecision(
            schedule=replace(
                schedule,
                stopped=True,
                stop_reason="disabled",
            )
        )
    sequence = schedule.reminder_sequence + 1
    due_monotonic = schedule.due_monotonic(sequence)
    due_at_utc = schedule.due_utc(sequence)
    assert due_monotonic is not None and due_at_utc is not None
    if current < due_monotonic:
        return ReminderDecision(schedule=schedule)
    emission = _emission(
        schedule,
        sequence=sequence,
        kind="progress",
        due_at_utc=due_at_utc,
        now_monotonic=current,
        progress_stage=progress_stage,
    )
    return ReminderDecision(
        schedule=replace(
            schedule,
            reminder_sequence=sequence,
            last_reminder_at_utc=emission.emitted_at_utc,
        ),
        emission=emission,
    )


__all__ = [
    "ACTIVE_REMINDER_OPERATION_STATUSES",
    "CANCELLATION_MESSAGE",
    "DEFAULT_REMINDER_ENABLED",
    "DEFAULT_REMINDER_INTERVAL_SECONDS",
    "GenerationReminderConfig",
    "GenerationReminderConfigError",
    "GenerationReminderSchedule",
    "GenerationReminderStateError",
    "IMMEDIATE_ACK_MESSAGE",
    "ImmediateAckDecision",
    "MAX_REMINDER_INTERVAL_SECONDS",
    "MIN_REMINDER_INTERVAL_SECONDS",
    "REMINDER_CONFIG_SOURCES",
    "REMINDER_INTERVAL_STEP_SECONDS",
    "REMINDER_OPERATION_STATUSES",
    "ReminderDecision",
    "ReminderEmission",
    "TERMINAL_REMINDER_OPERATION_STATUSES",
    "claim_due_reminder",
    "claim_immediate_ack",
    "format_utc",
    "freeze_generation_reminder_config",
    "parse_utc",
    "progress_reminder_message",
    "reminder_safe_stage",
    "reminder_dedupe_key",
    "reminder_event_id",
    "reminder_identity_digest",
    "validate_reminder_interval",
]
