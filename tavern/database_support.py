from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from .constants import (
    DATABASE_SCHEMA_VERSION,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
    SESSION_STATES,
)
from .card_lifecycle import validate_card_revision
from .card_wizard import choose_options, store_preset_snapshots
from .lifecycle import (
    CARD_APPROVED,
    CARD_DRAFT,
    CARD_PENDING,
    CARD_REJECTED,
    CARD_UNCREATED,
    CHOICE_KEYS,
    PARTICIPANT_ACTIVE,
    PARTICIPANT_ARCHIVED,
    PARTICIPANT_AWAY,
    PARTICIPANT_RESERVED,
    PARTICIPANT_RETIRED,
    PARTICIPANT_STANDBY,
    SEAT_HOLDING_STATUSES,
    card_stat_allocation,
    card_template,
    deadline_after,
    next_fillable_card_step,
    repair_profession_preset_draft,
    resolve_profession_stats,
    uses_profession_preset_stats,
    fallback_choices,
    normalize_choices,
    normalize_progress,
    normalize_time_rules,
    opening_choices,
    player_limits,
    safe_exit_narrative,
    initial_character_runtime_state,
    validate_card_template_config,
    utc_now as lifecycle_utc_now,
    vote_result,
    world_session_modules,
    world_time_rules,
)
from .stat_generation import (
    STAT_GENERATION_SNAPSHOT_KEY,
    calculate_preset_stack_stats,
    clear_generated_stats,
    stat_generation_config,
    sync_preset_stack_fields,
    uses_preset_stack_stats,
)
from .resolution import memory_fingerprint
from .presets import resolve_character_presets, validate_preset_selection
from .world_contract import validate_world_contract
from .security import clean_text, truncate_text, validate_platform_id, validate_slug
from .recovery_ranges import parse_recovery_json
from .storage import (
    InstanceStorage,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)
from .turns import (
    advance_turn,
    embed_turn_state,
    join_turn,
    leave_turn,
    normalize_turn_state,
    public_world_state,
    replace_turn_order,
    turn_state_from_world,
)

T = TypeVar("T")
TIMER_REMINDER_INTERVAL_SECONDS = 30
CARD_COMPLETION_REMINDER_INTERVAL_SECONDS = 2 * 60
COUNTDOWN_TYPES = (
    "card_code",
    "card_completion",
    "preparation",
    "ready",
    "turn",
    "vote",
    "standby",
    "all_idle",
)
# 同一副本内只允许存在一个在跑的实例；换人/换回合时旧计时器必须作废。
# 否则 继续/读档/回合推进 会不断叠加 turn 计时器，
# 每一轮轮询都按行数重复推送提醒，形成刷屏。
SESSION_SINGLETON_TIMER_TYPES = frozenset(
    {
        "turn",
        "vote",
        "preparation",
        "all_idle",
    }
)


def timer_reminder_interval(
    timer_type: object,
    action: Mapping[str, Any] | None = None,
) -> int:
    if isinstance(action, Mapping):
        try:
            configured = int(action.get("reminder_interval_seconds") or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured > 0:
            return configured
    if str(timer_type or "") == "card_completion":
        return CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
    return TIMER_REMINDER_INTERVAL_SECONDS


def timer_reminder_enabled(
    timer_type: object,
    action: Mapping[str, Any],
) -> bool:
    if str(timer_type or "") == "card_completion":
        return bool(action.get("reminder_enabled", False))
    try:
        return int(action.get("reminder_interval_seconds") or 0) > 0
    except (TypeError, ValueError):
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dump(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_load(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed


def _active_event_window(
    connection: sqlite3.Connection,
    session_id: str,
) -> tuple[int, list[tuple[int, int]]]:
    """Return the authoritative visible event window for a session.

    Story projections must follow the same history floor and rollback exclusion
    rules as ``StoryRepositoryMixin.recent_events``.  Keeping the calculation
    here lets AI turns, vote resolutions and DM narration share one definition
    when they link a new story revision to the previous visible revision.
    """
    row = connection.execute(
        """
        SELECT s.history_floor_seq, sr.recovery_json
        FROM sessions s
        LEFT JOIN session_rule_states sr ON sr.session_id = s.id
        WHERE s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if not row:
        raise DatabaseNotFoundError("副本不存在")
    recovery = parse_recovery_json(
        row["recovery_json"]
        if row["recovery_json"] is not None
        else "{}"
    )
    return (
        int(row["history_floor_seq"] or 0),
        list(recovery.excluded_event_ranges),
    )


def latest_public_story_row(
    connection: sqlite3.Connection,
    session_id: str,
) -> sqlite3.Row | None:
    """Find the latest explicit public story event in the active timeline."""
    history_floor, excluded = _active_event_window(connection, session_id)
    rows = connection.execute(
        """
        SELECT * FROM events
        WHERE session_id = ? AND seq >= ? AND role = 'narrator'
          AND content <> ''
        ORDER BY seq DESC
        """,
        (session_id, history_floor),
    ).fetchall()
    for row in rows:
        seq = int(row["seq"] or 0)
        if any(start <= seq <= end for start, end in excluded):
            continue
        meta = json_load(row["meta_json"], {})
        if not isinstance(meta, Mapping):
            continue
        if meta.get("event_type") != "story_progress":
            continue
        if meta.get("visibility") != "public":
            continue
        return row
    return None


def story_progress_meta(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    source: str,
    session_revision: int,
    visibility: str = "public",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build metadata for a new immutable story revision."""
    previous = latest_public_story_row(connection, session_id)
    previous_meta = (
        json_load(previous["meta_json"], {})
        if previous is not None
        else {}
    )
    try:
        previous_revision = int(
            previous_meta.get("story_revision", 0)
            if isinstance(previous_meta, Mapping)
            else 0
        )
    except (TypeError, ValueError, OverflowError):
        previous_revision = 0
    result: dict[str, Any] = {
        "event_type": "story_progress",
        "source": str(source or "system"),
        "visibility": str(visibility or "public"),
        "session_revision": int(session_revision),
        "story_revision": previous_revision + 1,
        "supersedes_event_id": str(previous["id"] if previous is not None else ""),
    }
    if extra:
        result.update(dict(extra))
    return result


def story_turn_history(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """读取最近 N 条已提交的叙事进度事件，供停滞检测消费。

    与 latest_public_story_row 共用同一可见时间窗；每条输出包含
    turn_no、scene_ref、六类进展指示物与 DM/表决活动标记，结构对齐
    story_pacing.detect_story_stall 的输入契约。
    """
    limit = max(1, min(50, int(limit)))
    history_floor, excluded = _active_event_window(connection, session_id)
    exclusions = "".join(
        " AND NOT (seq BETWEEN ? AND ?)" for _ in excluded
    )
    parameters: list[Any] = [session_id, history_floor]
    for start, end in excluded:
        parameters.extend((start, end))
    parameters.append(limit)
    rows = connection.execute(
        f"""
        SELECT * FROM events
        WHERE session_id = ? AND seq >= ? AND role = 'narrator'
          AND content <> ''
          {exclusions}
        ORDER BY seq DESC LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    history: list[dict[str, Any]] = []
    for row in reversed(rows):
        meta = json_load(row["meta_json"], {})
        if not isinstance(meta, Mapping):
            continue
        if meta.get("event_type") != "story_progress":
            continue
        progress = meta.get("progress")
        progress = progress if isinstance(progress, Mapping) else {}
        history.append(
            {
                "turn_no": int(row["turn_no"] or 0),
                "scene_ref": str(meta.get("scene_ref") or ""),
                "new_facts": bool(progress.get("new_facts", False)),
                "scene_changes": bool(progress.get("scene_changes", False)),
                "quest_changes": bool(progress.get("quest_changes", False)),
                "resource_changes": bool(
                    progress.get("resource_changes", False)
                ),
                "npc_changes": bool(progress.get("npc_changes", False)),
                "irreversible_choices": bool(
                    progress.get("irreversible_choices", False)
                ),
                "roleplay_active": bool(
                    meta.get("roleplay_active")
                    or meta.get("edited_by_dm")
                    or meta.get("dm_beat")
                    or meta.get("vote_resolution")
                ),
                "action_fingerprint": str(
                    meta.get("action_fingerprint") or ""
                ),
                "narrative_fingerprint": str(
                    meta.get("narrative_fingerprint") or ""
                ),
            }
        )
    return history


def story_stall_after_write(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] = (),
    limit: int = 8,
) -> dict[str, Any]:
    """在叙事事件写入后调用：消费六类进展指示物并检测连续停滞。

    命中停滞时返回 build_stall_intervention 的可执行引导/转场计划，
    由引擎渲染给玩家与主持人；未命中时 stalled=False。历史读取与
    当前事件在同一事务内完成，保证检测基于已提交的权威事件序列。
    """
    from .story_pacing import (
        build_stall_intervention,
        detect_story_stall,
        stall_policy_for_scene,
    )

    scene_ref = str(
        runtime.get("current_scene") or runtime.get("scene_ref") or ""
    )
    policy = stall_policy_for_scene(world, scene_ref)
    history = story_turn_history(connection, session_id, limit=limit)
    detection = detect_story_stall(history, policy)
    if not detection.get("stalled"):
        return {
            "stalled": False,
            "detection": detection,
            "intervention": None,
        }
    intervention = build_stall_intervention(
        world=world,
        runtime=runtime,
        session=session,
        squad=squad,
        history=history,
        policy=policy,
    )
    return {
        "stalled": True,
        "detection": detection,
        "intervention": intervention,
    }


def clean_card_field(
    value: object,
    *,
    label: str,
    max_chars: int,
) -> str:
    raw = str(value or "")
    if any(character.isspace() for character in raw):
        raise ValueError(
            f"{label}不能包含空格、全角空格、换行或制表符"
        )
    return clean_text(raw, max_chars=max_chars)


def bounded_int(
    value: Any,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse editable JSON integers without letting one bad rule stop play."""
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return min(maximum, max(minimum, parsed))


class DatabaseConflictError(RuntimeError):
    pass


class DatabaseNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    pass


# ── D1 Schema 20：主动投递退避与副本事件/归档集中写入辅助 ────────────
# 这些纯函数只接收已开事务的 connection，由各仓储在 BEGIN IMMEDIATE
# 事务内调用；主线程的 DeliveryService / TerminalService / Finalization
# Service 可以直接复用，避免散点裸写。

# D1-DEL-006：建议退避（按已失败次数）。
# 第 1 次立即；第 2 次 15 秒；第 3 次 1 分钟；第 4 次 5 分钟；
# 第 5 次 15 分钟；后续 30 分钟，上限按消息类型配置（max_attempts）。
DELIVERY_RETRY_BACKOFF_SECONDS = (0, 15, 60, 300, 900, 1800)


def retry_backoff_after(attempts: int, now: str | None = None) -> str:
    """按已失败次数计算下一次重试的绝对时间（ISO 字符串）。"""
    try:
        count = max(1, int(attempts))
    except (TypeError, ValueError, OverflowError):
        count = 1
    index = min(count, len(DELIVERY_RETRY_BACKOFF_SECONDS)) - 1
    delay = DELIVERY_RETRY_BACKOFF_SECONDS[index]
    base = datetime.now(timezone.utc)
    if now:
        try:
            base = datetime.fromisoformat(str(now))
        except (TypeError, ValueError):
            pass
    return (base + timedelta(seconds=delay)).isoformat(timespec="seconds")


def next_session_event_seq(
    connection: sqlite3.Connection,
    session_id: str,
) -> int:
    """返回该副本下一条事件序号（D1-RUN-006：按副本单调递增）。"""
    row = connection.execute(
        """
        SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
        FROM session_events
        WHERE session_id = ?
        """,
        (str(session_id),),
    ).fetchone()
    return int(row["next_seq"] if row is not None else 1)


def insert_session_event(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    event_id: str,
    type_: str,
    actor_ref: str = "",
    command_id: str = "",
    causation_id: str = "",
    correlation_id: str = "",
    payload: Mapping[str, Any] | None = None,
    visibility: str = "public",
    created_at: str | None = None,
) -> dict[str, Any]:
    """集中写入 session event；按 event_id 幂等（重复提交返回原事件）。

    调用方负责外层事务；复合主键 (session_id, seq) 与全局唯一 event_id
    共同防止重复应用（WP-11 增量投影的幂等基础）。
    """
    session_id = str(session_id or "").strip()
    event_id = str(event_id or "").strip()
    event_type = str(type_ or "").strip()
    if not session_id or not event_id or not event_type:
        raise ValueError("副本事件必须包含 session_id、event_id 与类型")
    existing = connection.execute(
        "SELECT * FROM session_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    seq = next_session_event_seq(connection, session_id)
    now = created_at or utc_now()
    connection.execute(
        """
        INSERT INTO session_events(
            session_id, seq, event_id, type, actor_ref, command_id,
            causation_id, correlation_id, payload_json, visibility, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            seq,
            event_id,
            event_type[:80],
            str(actor_ref or "")[:160],
            str(command_id or "")[:160],
            str(causation_id or "")[:160],
            str(correlation_id or "")[:160],
            json_dump(dict(payload) if isinstance(payload, Mapping) else {}),
            str(visibility or "public")[:40],
            now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM session_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return dict(row)


SESSION_ARCHIVE_TYPES = ("completed", "failed", "aborted")


def insert_session_archive(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    termination_type: str,
    reason: str,
    final_snapshot_id: str,
    ended_by: str,
    ended_at: str | None = None,
    readonly: int = 1,
    ending_ref: str = "",
    ending_label: str = "",
) -> dict[str, Any]:
    """写入永久归档记录（D1 统一 completed|failed|aborted）。

    一个副本只允许一条最终归档；已存在时抛 InvalidTransitionError，
    保证终局幂等（D1-RUN-013/18 §11）。
    """
    session_id = str(session_id or "").strip()
    termination_type = str(termination_type or "").strip().lower()
    if termination_type not in SESSION_ARCHIVE_TYPES:
        raise ValueError("结束类型必须为 completed、failed 或 aborted")
    if not session_id or not final_snapshot_id or not ended_by:
        raise ValueError("归档记录必须包含副本、最终快照与执行人")
    existing = connection.execute(
        "SELECT * FROM session_archives WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if existing is not None:
        raise InvalidTransitionError("该副本已经永久归档")
    now = ended_at or utc_now()
    connection.execute(
        """
        INSERT INTO session_archives(
            session_id, termination_type, reason, final_snapshot_id,
            ended_by, ended_at, readonly, ending_ref, ending_label
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            termination_type,
            str(reason or "")[:1000],
            final_snapshot_id,
            ended_by,
            now,
            1 if readonly else 0,
            str(ending_ref or "")[:160],
            str(ending_label or "")[:160],
        ),
    )
    row = connection.execute(
        "SELECT * FROM session_archives WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return dict(row)
