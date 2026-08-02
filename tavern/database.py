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
    DEFAULT_CHARACTERS,
    DEFAULT_WORLD,
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
    SESSION_STATES,
)
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
from .resolution import memory_fingerprint
from .world_contract import validate_world_contract
from .security import clean_text, validate_platform_id, validate_slug
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


def timer_reminder_interval(timer_type: object) -> int:
    if str(timer_type or "") == "card_completion":
        return CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
    return TIMER_REMINDER_INTERVAL_SECONDS


def timer_reminder_enabled(
    timer_type: object,
    action: Mapping[str, Any],
) -> bool:
    if str(timer_type or "") != "card_completion":
        return True
    return bool(action.get("reminder_enabled", True))


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


class TavernDatabase:
    """SQLite persistence with short, explicit transactions.

    A fresh connection is used for each operation so methods can safely run in
    worker threads. Per-session async locks in the engine serialize story turns;
    optimistic revisions provide a second line of defense.
    """

    _SESSION_MUTATIONS = {
        "_ensure_session",
        "_clone_session",
        "_transition_session",
        "_finalize_session",
        "_save_manual_state",
        "_ensure_player",
        "_join_turn_order",
        "_leave_turn_order",
        "_skip_turn",
        "_set_turn_order",
        "_designate_turn",
        "_save_player",
        "_delete_player",
        "_append_ooc",
        "_save_memory",
        "_delete_memory",
        "_create_snapshot",
        "_restore_snapshot",
        "_restore_latest_auto",
        "_delete_snapshot",
        "_commit_turn_sync",
        "_save_instance_time_rules",
        "_save_session_rule_state",
        "_save_session_character",
        "_lock_check_result",
        "_reserve_participant",
        "_bind_card_code",
        "_fill_card_draft",
        "_reset_card_draft_stats",
        "_confirm_card_draft",
        "_cancel_card_draft",
        "_review_character_card",
        "_set_participant_ready",
        "_force_all_ready",
        "_activate_story",
        "_replace_active_choices",
        "_cast_vote",
        "_set_participant_away",
        "_return_to_queue",
        "_retire_participant",
        "_retire_and_ban",
        "_revoke_ban",
        "_request_return",
        "_control_timer",
        "_set_timer_policy",
        "_extend_active_timer",
        "_pause_session_timers",
        "_resume_session_timers",
        "_process_due_timers",
        "_grant_delegation",
        "_revoke_delegation",
        "_grant_permission",
        "_set_token_quota",
        "_set_group_token_quota",
        "_delete_session",
        "_write_audit",
        "_cleanup",
        "_import_bundle",
    }
    _ALL_SESSION_MUTATIONS = {
        "_process_due_timers",
        "_cleanup",
        "_import_bundle",
    }
    _ENTITY_SESSION_LOOKUPS = {
        "_delete_player": ("players", "id"),
        "_delete_memory": ("memories", "id"),
        "_delete_snapshot": ("snapshots", "id"),
        "_control_timer": ("timer_instances", "id"),
    }

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_path = self.data_dir / "tavern.sqlite3"
        self.path = self.data_dir / "catalog.sqlite3"
        self._schema_lock = threading.Lock()
        self._legacy_migration_backup = self._prepare_catalog_path()
        self.migration_backup_path = (
            self._legacy_migration_backup
            or self._backup_before_schema_upgrade()
        )
        try:
            self._initialize()
            self.storage = InstanceStorage(
                data_dir=self.data_dir,
                catalog_path=self.path,
                connect_catalog=self._connect,
                schema_version=DATABASE_SCHEMA_VERSION,
            )
            self.storage.bootstrap(
                migration=bool(self._legacy_migration_backup)
            )
            self.legacy_retained_path = self._retain_legacy_database()
        except Exception:
            if self._legacy_migration_backup and self.path.exists():
                failed_dir = self.data_dir / "migration_backups"
                failed_dir.mkdir(parents=True, exist_ok=True)
                failed_path = next_timestamped_path(
                    failed_dir,
                    "failed_catalog",
                    ".sqlite3",
                )
                replace_with_retry(self.path, failed_path)
            raise

    def _retain_legacy_database(self) -> Path | None:
        if not self._legacy_migration_backup or not self.legacy_path.exists():
            return None
        backup_dir = self.data_dir / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = next_timestamped_path(
            backup_dir,
            "backup_legacy_tavern",
            ".sqlite3",
        )
        replace_with_retry(self.legacy_path, target)
        return target

    def _prepare_catalog_path(self) -> Path | None:
        """Copy the legacy monolithic database into the v0.5.1 catalog.

        The original ``tavern.sqlite3`` is deliberately retained. The switch
        to ``catalog.sqlite3`` happens only after two SQLite-native consistent
        copies have completed.
        """

        if self.path.exists() or not self.legacy_path.exists():
            return None
        if self.legacy_path.stat().st_size == 0:
            return None
        backup_dir = self.data_dir / "migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = next_timestamped_path(
            backup_dir,
            "backup_catalog",
            ".sqlite3",
        )
        temporary = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with closing(sqlite3.connect(self.legacy_path)) as source:
                quick = str(source.execute("PRAGMA quick_check").fetchone()[0])
                if quick != "ok":
                    raise RuntimeError(
                        f"旧版数据库完整性校验失败：{quick}"
                    )
                with closing(
                    sqlite3.connect(backup_path)
                ) as destination:
                    source.backup(destination)
                with closing(
                    sqlite3.connect(temporary)
                ) as destination:
                    source.backup(destination)
            replace_with_retry(temporary, self.path)
            return backup_path
        except Exception as exc:
            unlink_with_retry(temporary, suppress_errors=True)
            unlink_with_retry(self.path, suppress_errors=True)
            raise RuntimeError(
                "检测到旧版酒馆数据库，但 v0.5.1 Alpha 迁移前备份"
                "或目录库创建失败；原 tavern.sqlite3 未被修改。"
            ) from exc

    def _backup_before_schema_upgrade(self) -> Path | None:
        """Create one consistent SQLite backup before a structural upgrade."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        try:
            with closing(sqlite3.connect(self.path)) as source:
                has_meta = source.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'tavern_meta'
                    """
                ).fetchone()
                if has_meta:
                    row = source.execute(
                        """
                        SELECT value FROM tavern_meta
                        WHERE key = 'schema_version'
                        """
                    ).fetchone()
                    current = int(row[0]) if row else 1
                else:
                    current = 1
                if current >= DATABASE_SCHEMA_VERSION:
                    return None
                backup_dir = self.data_dir / "migration_backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                target = next_timestamped_path(
                    backup_dir,
                    "backup_catalog",
                    ".sqlite3",
                )
                with closing(sqlite3.connect(target)) as destination:
                    source.backup(destination)
                return target
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "检测到旧版酒馆数据库，但迁移前安全备份失败；"
                "已停止升级，请先检查插件数据目录权限与磁盘空间。"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock:
            with closing(self._connect()) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tavern_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS worlds (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        rules_json TEXT NOT NULL DEFAULT '{}',
                        opening_scene TEXT NOT NULL DEFAULT '',
                        initial_state_json TEXT NOT NULL DEFAULT '{}',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS characters (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id)
                            ON DELETE CASCADE,
                        slug TEXT NOT NULL,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'npc',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        prompt TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(world_id, slug)
                    );

                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        instance_slug TEXT NOT NULL,
                        instance_name TEXT NOT NULL,
                        selected INTEGER NOT NULL DEFAULT 0,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        state TEXT NOT NULL DEFAULT 'closed'
                            CHECK(state IN (
                                'closed', 'preparing', 'running', 'paused',
                                'finished', 'maintenance'
                            )),
                        turn_no INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        world_state_json TEXT NOT NULL DEFAULT '{}',
                        history_floor_seq INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id, instance_slug)
                    );

                    CREATE TABLE IF NOT EXISTS players (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        turn_no INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        actor_name TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_events_session_seq
                        ON events(session_id, seq DESC);

                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        scope TEXT NOT NULL DEFAULT 'world',
                        scope_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'fact',
                        content TEXT NOT NULL,
                        importance INTEGER NOT NULL DEFAULT 3,
                        salience REAL NOT NULL DEFAULT 1,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        fingerprint TEXT NOT NULL,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_accessed_at TEXT NOT NULL,
                        UNIQUE(session_id, fingerprint)
                    );

                    CREATE INDEX IF NOT EXISTS idx_memories_session
                        ON memories(session_id, importance DESC, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS snapshots (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'manual',
                        turn_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        world_id TEXT NOT NULL,
                        world_state_json TEXT NOT NULL,
                        created_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, name)
                    );

                    CREATE INDEX IF NOT EXISTS idx_snapshots_session
                        ON snapshots(session_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        target TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_audit_session
                        ON audit_logs(session_id, id DESC);
                    """
                )
                self._migrate_sessions_v2(connection)
                self._migrate_sessions_v3(connection)
                connection.executescript(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_selected_group
                    ON sessions(platform_id, group_id)
                    WHERE selected = 1;

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_running_group
                    ON sessions(platform_id, group_id)
                    WHERE state = 'running';

                    CREATE INDEX IF NOT EXISTS idx_sessions_group
                    ON sessions(platform_id, group_id, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS instance_configs (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL DEFAULT 1,
                        world_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        time_rules_json TEXT NOT NULL DEFAULT '{}',
                        phase_meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS character_cards (
                        id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        display_name TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        current_version INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_character_cards_owner
                    ON character_cards(owner_user_id, world_id, archived);

                    CREATE TABLE IF NOT EXISTS character_card_versions (
                        id TEXT PRIMARY KEY,
                        character_card_id TEXT NOT NULL
                            REFERENCES character_cards(id) ON DELETE CASCADE,
                        version_no INTEGER NOT NULL,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending_review'
                            CHECK(status IN (
                                'draft', 'pending_review', 'approved',
                                'rejected'
                            )),
                        review_note TEXT NOT NULL DEFAULT '',
                        reviewed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(character_card_id, version_no)
                    );

                    CREATE TABLE IF NOT EXISTS participants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        player_id TEXT REFERENCES players(id)
                            ON DELETE SET NULL,
                        group_user_id TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        display_name TEXT NOT NULL,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        character_version_id TEXT
                            REFERENCES character_card_versions(id)
                            ON DELETE SET NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        character_code TEXT NOT NULL DEFAULT '',
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        card_status TEXT NOT NULL DEFAULT 'uncreated'
                            CHECK(card_status IN (
                                'uncreated', 'draft', 'pending_review',
                                'approved', 'rejected'
                            )),
                        ready INTEGER NOT NULL DEFAULT 0,
                        participation_status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(participation_status IN (
                                'reserved', 'active', 'standby', 'away',
                                'retired', 'archived'
                            )),
                        seat_reserved_at TEXT NOT NULL,
                        joined_round INTEGER NOT NULL DEFAULT 1,
                        consecutive_timeouts INTEGER NOT NULL DEFAULT 0,
                        exit_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, group_user_id)
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_participant_code_session
                    ON participants(session_id, character_code)
                    WHERE character_code <> ''
                      AND participation_status NOT IN ('retired', 'archived');

                    CREATE TABLE IF NOT EXISTS character_runtime_states (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, participant_id)
                    );

                    CREATE TABLE IF NOT EXISTS character_card_drafts (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        fields_json TEXT NOT NULL DEFAULT '{}',
                        current_step INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'confirmed', 'cancelled', 'expired'
                            )),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(participant_id)
                    );

                    CREATE TABLE IF NOT EXISTS card_binding_codes (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        code TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'used', 'expired')),
                        expires_at TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        used_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS choice_sets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        round_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        choices_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'selected', 'superseded',
                                'cancelled'
                            )),
                        reroll_count INTEGER NOT NULL DEFAULT 0,
                        selected_key TEXT NOT NULL DEFAULT '',
                        flavor_text TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_choice_active_session
                    ON choice_sets(session_id)
                    WHERE status = 'active';

                    CREATE TABLE IF NOT EXISTS rolls (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        choice_set_id TEXT REFERENCES choice_sets(id)
                            ON DELETE SET NULL,
                        participant_id TEXT REFERENCES participants(id)
                            ON DELETE SET NULL,
                        roll_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS group_votes (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        question TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        eligible_user_ids_json TEXT NOT NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'open'
                            CHECK(status IN (
                                'open', 'passed', 'rejected', 'cancelled'
                            )),
                        winner_key TEXT NOT NULL DEFAULT '',
                        suspended_user_id TEXT NOT NULL DEFAULT '',
                        deadline_at TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_group_vote_open_session
                    ON group_votes(session_id)
                    WHERE status = 'open';

                    CREATE TABLE IF NOT EXISTS vote_ballots (
                        id TEXT PRIMARY KEY,
                        vote_id TEXT NOT NULL REFERENCES group_votes(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        option_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(vote_id, user_id)
                    );

                    CREATE TABLE IF NOT EXISTS selected_world_events (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        round_no INTEGER NOT NULL,
                        pool_item_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'selected'
                            CHECK(status IN (
                                'selected', 'narrated', 'cancelled'
                            )),
                        narrative TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        resolved_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(session_id, round_no)
                    );

                    CREATE TABLE IF NOT EXISTS timer_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        participant_id TEXT NOT NULL DEFAULT '',
                        timer_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'paused', 'expired', 'cancelled',
                                'completed'
                            )),
                        deadline_at TEXT NOT NULL DEFAULT '',
                        remaining_seconds INTEGER,
                        reminder_at TEXT NOT NULL DEFAULT '',
                        reminder_sent INTEGER NOT NULL DEFAULT 0,
                        action_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_timers_due
                    ON timer_instances(status, deadline_at);

                    CREATE TABLE IF NOT EXISTS delegation_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        owner_user_id TEXT NOT NULL,
                        delegate_user_id TEXT NOT NULL,
                        permissions_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'expired', 'revoked'
                            )),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS permission_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL
                            CHECK(role IN ('host', 'moderator')),
                        granted_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id, role)
                    );

                    CREATE TABLE IF NOT EXISTS ban_records (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        platform_id TEXT NOT NULL DEFAULT '',
                        group_id TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL
                            CHECK(scope IN ('instance', 'group', 'global')),
                        reason TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'expired', 'revoked')),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_bans_target
                    ON ban_records(user_id, status, scope);

                    CREATE TABLE IF NOT EXISTS return_requests (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        requested_by TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'requested'
                            CHECK(status IN (
                                'requested', 'voting', 'quest_active',
                                'completed', 'rejected', 'cancelled'
                            )),
                        exit_type TEXT NOT NULL DEFAULT 'departure',
                        objective TEXT NOT NULL DEFAULT '',
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        vote_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS snapshot_workflows (
                        snapshot_id TEXT PRIMARY KEY REFERENCES snapshots(id)
                            ON DELETE CASCADE,
                        workflow_json TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE TABLE IF NOT EXISTS session_archives (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        termination_type TEXT NOT NULL
                            CHECK(termination_type IN ('completed', 'aborted')),
                        reason TEXT NOT NULL DEFAULT '',
                        final_snapshot_id TEXT NOT NULL
                            REFERENCES snapshots(id),
                        ended_by TEXT NOT NULL,
                        ended_at TEXT NOT NULL,
                        readonly INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS session_rule_states (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        content_boundaries_json TEXT NOT NULL DEFAULT '{}',
                        npc_policy_json TEXT NOT NULL DEFAULT '{}',
                        context_budget_json TEXT NOT NULL DEFAULT '{}',
                        dice_rules_json TEXT NOT NULL DEFAULT '{}',
                        recovery_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS session_characters (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        name TEXT NOT NULL,
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        role_type TEXT NOT NULL DEFAULT 'npc',
                        public_profile_json TEXT NOT NULL DEFAULT '{}',
                        known_facts_json TEXT NOT NULL DEFAULT '[]',
                        misconceptions_json TEXT NOT NULL DEFAULT '[]',
                        source TEXT NOT NULL DEFAULT 'model_generated'
                            CHECK(source IN (
                                'world_preset', 'model_generated', 'admin'
                            )),
                        review_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(review_status IN (
                                'pending', 'approved', 'rejected',
                                'duplicate'
                            )),
                        lifecycle_status TEXT NOT NULL DEFAULT 'active'
                            CHECK(lifecycle_status IN (
                                'active', 'departed', 'dead', 'archived'
                            )),
                        persistent INTEGER NOT NULL DEFAULT 1,
                        first_event_id TEXT NOT NULL DEFAULT '',
                        last_event_id TEXT NOT NULL DEFAULT '',
                        first_turn INTEGER NOT NULL DEFAULT 0,
                        last_turn INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE INDEX IF NOT EXISTS idx_session_characters_context
                    ON session_characters(
                        session_id, lifecycle_status, last_turn DESC
                    );

                    CREATE TABLE IF NOT EXISTS session_character_states (
                        character_id TEXT PRIMARY KEY
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS story_ledger (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'objective'
                            CHECK(kind IN (
                                'main', 'side', 'objective', 'clue',
                                'milestone', 'failed'
                            )),
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'failed', 'archived'
                            )),
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host')),
                        source_event_id TEXT NOT NULL DEFAULT '',
                        completed_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE TABLE IF NOT EXISTS scene_clocks (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        segments INTEGER NOT NULL
                            CHECK(segments IN (4, 6, 8)),
                        current_value INTEGER NOT NULL DEFAULT 0,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'vague', 'hidden')),
                        trigger_text TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'archived'
                            )),
                        triggered_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );

                    CREATE TABLE IF NOT EXISTS memory_governance (
                        memory_id TEXT PRIMARY KEY REFERENCES memories(id)
                            ON DELETE CASCADE,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host', 'private')),
                        locked INTEGER NOT NULL DEFAULT 0,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        invalidated INTEGER NOT NULL DEFAULT 0,
                        supersedes_id TEXT NOT NULL DEFAULT '',
                        conflict_status TEXT NOT NULL DEFAULT 'clear'
                            CHECK(conflict_status IN (
                                'clear', 'conflict', 'resolved'
                            )),
                        note TEXT NOT NULL DEFAULT '',
                        updated_by TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS assist_tokens (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        target_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        stat TEXT NOT NULL DEFAULT '',
                        method TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'consumed', 'expired')),
                        expires_round INTEGER NOT NULL DEFAULT 0,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        consumed_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE TABLE IF NOT EXISTS roll_revisions (
                        id TEXT PRIMARY KEY,
                        roll_id TEXT NOT NULL REFERENCES rolls(id)
                            ON DELETE CASCADE,
                        revision_no INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        previous_json TEXT NOT NULL,
                        revised_json TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(roll_id, revision_no)
                    );

                    CREATE TABLE IF NOT EXISTS inspiration_transactions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        delta INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        operation_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'healthy'
                            CHECK(status IN ('healthy', 'open', 'half_open')),
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_failure_reason TEXT NOT NULL DEFAULT '',
                        last_failure_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        circuit_until TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS configuration_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        saved_by TEXT NOT NULL,
                        saved_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        operation_type TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'completed'
                            CHECK(status IN (
                                'pending', 'completed', 'failed'
                            )),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS group_registry (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        remark TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_group_registry_remark
                    ON group_registry(remark, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS story_storage (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_registry_id TEXT NOT NULL
                            REFERENCES group_registry(id) ON DELETE CASCADE,
                        relative_path TEXT NOT NULL UNIQUE,
                        playthrough_no INTEGER NOT NULL DEFAULT 1,
                        created_stamp TEXT NOT NULL,
                        last_synced_revision INTEGER NOT NULL DEFAULT 0,
                        last_checksum TEXT NOT NULL DEFAULT '',
                        sync_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(sync_status IN (
                                'pending', 'ready', 'error'
                            )),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_story_storage_group
                    ON story_storage(group_registry_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS timer_policies (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        global_enabled INTEGER NOT NULL DEFAULT 1,
                        switches_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL DEFAULT 'system',
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS token_usage (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_id TEXT NOT NULL,
                        request_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL DEFAULT '',
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        reserved_tokens INTEGER NOT NULL DEFAULT 0,
                        usage_source TEXT NOT NULL DEFAULT 'estimated',
                        status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(status IN (
                                'reserved', 'completed', 'failed'
                            )),
                        created_at TEXT NOT NULL,
                        settled_at TEXT NOT NULL DEFAULT ''
                    );

                    CREATE INDEX IF NOT EXISTS idx_token_usage_session_time
                    ON token_usage(session_id, created_at DESC);

                    CREATE INDEX IF NOT EXISTS idx_token_usage_group_time
                    ON token_usage(group_id, created_at DESC);

                    CREATE TABLE IF NOT EXISTS token_quota_policies (
                        id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL
                            CHECK(scope_type IN ('group', 'session')),
                        scope_id TEXT NOT NULL,
                        window_seconds INTEGER NOT NULL,
                        token_limit INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(scope_type, scope_id)
                    );
                    """
                )
                self._initialize_vnext_rows(connection)
                self._initialize_v05_rows(connection)
                self._initialize_v051_rows(connection)
                self._initialize_v053_rows(connection)
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('schema_version', ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (str(DATABASE_SCHEMA_VERSION),),
                )
                self._seed_default_world(connection)

    @staticmethod
    def _stable_key(value: Any, fallback: str = "") -> str:
        text = " ".join(str(value or "").strip().casefold().split())
        if text:
            return text[:160]
        return str(fallback or uuid.uuid4().hex)[:160]

    def _initialize_v05_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Populate additive v0.5 state without rewriting v0.4 records."""

        now = utc_now()
        sessions = connection.execute(
            """
            SELECT s.id, s.turn_no, s.state, s.world_id,
                   ic.world_snapshot_json
            FROM sessions s
            LEFT JOIN instance_configs ic ON ic.session_id = s.id
            """
        ).fetchall()
        for session in sessions:
            world = json_load(session["world_snapshot_json"], {})
            if not isinstance(world, Mapping) or not world:
                world_row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (session["world_id"],),
                ).fetchone()
                if world_row:
                    world = {
                        "id": world_row["id"],
                        "rules": json_load(world_row["rules_json"], {}),
                        "initial_state": json_load(
                            world_row["initial_state_json"],
                            {},
                        ),
                    }
                else:
                    world = {}
            modules = world_session_modules(world)
            connection.execute(
                """
                INSERT INTO session_rule_states(
                    session_id, progress_json, content_boundaries_json,
                    npc_policy_json, context_budget_json, dice_rules_json,
                    recovery_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    session["id"],
                    json_dump(modules["progress"]),
                    json_dump(modules["content_boundaries"]),
                    json_dump(modules["npc_policy"]),
                    json_dump(modules["context_budget"]),
                    json_dump(modules["dice_rules"]),
                    json_dump(modules["recovery"]),
                    now,
                    now,
                ),
            )

            preset_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order, created_at
                """,
                (session["world_id"],),
            ).fetchall()
            for preset in preset_rows:
                stable_key = f"world:{preset['id']}"
                character_id = new_id("snpc")
                connection.execute(
                    """
                    INSERT INTO session_characters(
                        id, session_id, stable_key, name, aliases_json,
                        role_type, public_profile_json, known_facts_json,
                        misconceptions_json, source, review_status,
                        lifecycle_status, persistent, first_turn, last_turn,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', ?, ?, '[]', '[]',
                              'world_preset', 'approved', 'active', 1,
                              0, ?, 1, ?, ?)
                    ON CONFLICT(session_id, stable_key) DO NOTHING
                    """,
                    (
                        character_id,
                        session["id"],
                        stable_key,
                        preset["name"],
                        preset["role"],
                        preset["profile_json"],
                        session["turn_no"],
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id FROM session_characters
                    WHERE session_id = ? AND stable_key = ?
                    """,
                    (session["id"], stable_key),
                ).fetchone()
                if row:
                    profile = json_load(preset["profile_json"], {})
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO NOTHING
                        """,
                        (
                            row["id"],
                            json_dump(
                                {
                                    "location": profile.get("location", ""),
                                    "faction": profile.get("faction", ""),
                                    "status": "active",
                                }
                            ),
                            now,
                        ),
                    )

            if session["state"] == SESSION_FINISHED:
                archived = connection.execute(
                    "SELECT 1 FROM session_archives WHERE session_id = ?",
                    (session["id"],),
                ).fetchone()
                if not archived:
                    full_session = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (session["id"],),
                    ).fetchone()
                    latest_snapshot = connection.execute(
                        """
                        SELECT id FROM snapshots
                        WHERE session_id = ?
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        (session["id"],),
                    ).fetchone()
                    final_snapshot_id = (
                        str(latest_snapshot["id"])
                        if latest_snapshot
                        else self._insert_snapshot(
                            connection,
                            full_session,
                            f"final-migrated-{str(session['id'])[-8:]}",
                            "final",
                            "migration",
                            replace=False,
                        )
                    )
                    connection.execute(
                        """
                        INSERT INTO session_archives(
                            session_id, termination_type, reason,
                            final_snapshot_id, ended_by, ended_at, readonly
                        ) VALUES (?, 'completed', ?, ?, 'migration', ?, 1)
                        """,
                        (
                            session["id"],
                            "由 v0.4 finished 状态迁移为永久归档",
                            final_snapshot_id,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE sessions SET selected = 0
                        WHERE id = ?
                        """,
                        (session["id"],),
                    )

        runtime_rows = connection.execute(
            "SELECT * FROM character_runtime_states"
        ).fetchall()
        for runtime in runtime_rows:
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            defaults = initial_character_runtime_state()
            changed = False
            for key, value in defaults.items():
                if key not in state:
                    state[key] = value
                    changed = True
            if changed:
                connection.execute(
                    """
                    UPDATE character_runtime_states
                    SET state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )

        connection.execute(
            """
            INSERT INTO memory_governance(
                memory_id, visibility, locked, pinned, invalidated,
                supersedes_id, conflict_status, note, updated_by, updated_at
            )
            SELECT id,
                   CASE WHEN scope = 'player' THEN 'private' ELSE 'public' END,
                   0, 0, 0, '', 'clear', '', 'migration', ?
            FROM memories
            WHERE id NOT IN (SELECT memory_id FROM memory_governance)
            """,
            (now,),
        )

    def _initialize_v051_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Register every known group before filesystem materialization."""

        now = utc_now()
        rows = connection.execute(
            """
            SELECT DISTINCT platform_id, group_id
            FROM sessions
            ORDER BY platform_id, group_id
            """
        ).fetchall()
        for row in rows:
            platform_id = str(row["platform_id"])
            group_id = str(row["group_id"])
            registry_id = (
                "group_"
                + hashlib.sha256(
                    f"{platform_id}\0{group_id}".encode("utf-8")
                ).hexdigest()[:24]
            )
            connection.execute(
                """
                INSERT INTO group_registry(
                    id, platform_id, group_id, remark, revision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, '', 1, ?, ?)
                ON CONFLICT(platform_id, group_id) DO NOTHING
                """,
                (
                    registry_id,
                    platform_id,
                    group_id,
                    now,
                    now,
                ),
            )

    def _initialize_v053_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        now = utc_now()
        connection.execute(
            """
            INSERT INTO timer_policies(
                session_id, global_enabled, switches_json,
                revision, updated_by, updated_at
            )
            SELECT id, 1, '{}', 1, 'migration', ?
            FROM sessions
            WHERE id NOT IN (SELECT session_id FROM timer_policies)
            """,
            (now,),
        )

    @staticmethod
    def _migrate_sessions_v2(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(sessions)"
            ).fetchall()
        }
        if {"instance_slug", "instance_name", "selected"}.issubset(columns):
            return

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP TABLE IF EXISTS sessions_v2")
            connection.execute(
                """
                CREATE TABLE sessions_v2 (
                    id TEXT PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    unified_origin TEXT NOT NULL DEFAULT '',
                    instance_slug TEXT NOT NULL,
                    instance_name TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    world_id TEXT NOT NULL REFERENCES worlds(id),
                    state TEXT NOT NULL DEFAULT 'closed'
                        CHECK(state IN (
                            'closed', 'preparing', 'running', 'paused',
                            'finished', 'maintenance'
                        )),
                    turn_no INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    world_state_json TEXT NOT NULL DEFAULT '{}',
                    history_floor_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform_id, group_id, instance_slug)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sessions_v2(
                    id, platform_id, group_id, unified_origin,
                    instance_slug, instance_name, selected, world_id,
                    state, turn_no, revision, world_state_json,
                    history_floor_seq, created_at, updated_at
                )
                SELECT
                    s.id, s.platform_id, s.group_id, s.unified_origin,
                    w.slug, w.name, 1, s.world_id,
                    CASE
                        WHEN s.state IN ('running', 'maintenance')
                        THEN 'paused'
                        ELSE s.state
                    END,
                    s.turn_no, s.revision, s.world_state_json,
                    s.history_floor_seq, s.created_at, s.updated_at
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                """
            )
            connection.execute("DROP TABLE sessions")
            connection.execute("ALTER TABLE sessions_v2 RENAME TO sessions")
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise RuntimeError(
                    "数据库副本迁移后的外键校验失败"
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _migrate_sessions_v3(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'sessions'
            """
        ).fetchone()
        sql = str(row["sql"] if row else "")
        if "'preparing'" in sql and "'finished'" in sql:
            return

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP TABLE IF EXISTS sessions_v3")
            connection.execute(
                """
                CREATE TABLE sessions_v3 (
                    id TEXT PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    unified_origin TEXT NOT NULL DEFAULT '',
                    instance_slug TEXT NOT NULL,
                    instance_name TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    world_id TEXT NOT NULL REFERENCES worlds(id),
                    state TEXT NOT NULL DEFAULT 'closed'
                        CHECK(state IN (
                            'closed', 'preparing', 'running', 'paused',
                            'finished', 'maintenance'
                        )),
                    turn_no INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    world_state_json TEXT NOT NULL DEFAULT '{}',
                    history_floor_seq INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform_id, group_id, instance_slug)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO sessions_v3(
                    id, platform_id, group_id, unified_origin,
                    instance_slug, instance_name, selected, world_id,
                    state, turn_no, revision, world_state_json,
                    history_floor_seq, created_at, updated_at
                )
                SELECT
                    id, platform_id, group_id, unified_origin,
                    instance_slug, instance_name, selected, world_id,
                    CASE
                        WHEN state IN ('running', 'maintenance')
                        THEN 'paused'
                        ELSE state
                    END,
                    turn_no, revision, world_state_json,
                    history_floor_seq, created_at, updated_at
                FROM sessions
                """
            )
            connection.execute("DROP TABLE sessions")
            connection.execute("ALTER TABLE sessions_v3 RENAME TO sessions")
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            if violations:
                raise RuntimeError("数据库 vNext 状态机迁移后的外键校验失败")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _initialize_vnext_rows(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """Snapshot old worlds and bridge legacy players into vNext entities."""

        now = utc_now()
        sessions = connection.execute(
            """
            SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                   w.description, w.system_prompt, w.rules_json,
                   w.opening_scene, w.initial_state_json,
                   w.revision AS world_revision
            FROM sessions s
            JOIN worlds w ON w.id = s.world_id
            """
        ).fetchall()
        for session in sessions:
            world = {
                "id": session["world_id"],
                "slug": session["world_slug"],
                "name": session["world_name"],
                "description": session["description"],
                "system_prompt": session["system_prompt"],
                "rules": json_load(session["rules_json"], {}),
                "opening_scene": session["opening_scene"],
                "initial_state": json_load(
                    session["initial_state_json"],
                    {},
                ),
                "revision": session["world_revision"],
            }
            phase_meta = {
                "resume_mode": bool(session["turn_no"]),
                "migrated_to_vnext": True,
            }
            connection.execute(
                """
                INSERT INTO instance_configs(
                    session_id, world_revision, world_snapshot_json,
                    time_rules_json, phase_meta_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO NOTHING
                """,
                (
                    session["id"],
                    session["world_revision"],
                    json_dump(world),
                    json_dump(world_time_rules(world)),
                    json_dump(phase_meta),
                    now,
                    now,
                ),
            )

        legacy_players = connection.execute(
            """
            SELECT p.*, s.world_id
            FROM players p
            JOIN sessions s ON s.id = p.session_id
            LEFT JOIN participants pt
              ON pt.session_id = p.session_id
             AND pt.group_user_id = p.user_id
            WHERE pt.id IS NULL
            ORDER BY p.created_at, p.id
            """
        ).fetchall()
        used_codes: dict[str, set[str]] = {}
        for player in legacy_players:
            session_id = str(player["session_id"])
            used = used_codes.setdefault(
                session_id,
                {
                    str(item["character_code"])
                    for item in connection.execute(
                        """
                        SELECT character_code FROM participants
                        WHERE session_id = ? AND character_code <> ''
                        """,
                        (session_id,),
                    ).fetchall()
                },
            )
            character_name = clean_text(
                player["character_name"]
                or player["display_name"]
                or f"玩家{str(player['user_id'])[-4:]}",
                max_chars=80,
            )
            base_code = clean_text(character_name, max_chars=20) or (
                f"P{str(player['user_id'])[-6:]}"
            )
            code = base_code
            suffix = 2
            while code in used:
                code = f"{base_code[:16]}-{suffix}"
                suffix += 1
            used.add(code)

            card_id = new_id("pcard")
            version_id = new_id("pcardv")
            participant_id = new_id("participant")
            connection.execute(
                """
                INSERT INTO character_cards(
                    id, owner_user_id, world_id, display_name,
                    archived, deleted, current_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?)
                """,
                (
                    card_id,
                    player["user_id"],
                    player["world_id"],
                    character_name,
                    now,
                    now,
                ),
            )
            profile = json_load(player["profile_json"], {})
            if not isinstance(profile, dict):
                profile = {}
            profile.setdefault("name", character_name)
            profile.setdefault("code", code)
            connection.execute(
                """
                INSERT INTO character_card_versions(
                    id, character_card_id, version_no, template_version,
                    profile_json, stats_json, status, review_note,
                    reviewed_by, created_at
                ) VALUES (?, ?, 1, 1, ?, '{}', 'approved',
                          '由旧版玩家资料自动迁移', 'system', ?)
                """,
                (version_id, card_id, json_dump(profile), now),
            )
            participation_status = (
                PARTICIPANT_ACTIVE
                if bool(player["enabled"])
                else PARTICIPANT_ARCHIVED
            )
            connection.execute(
                """
                INSERT INTO participants(
                    id, session_id, player_id, group_user_id, display_name,
                    character_card_id, character_version_id, character_name,
                    character_code, aliases_json, card_status, ready,
                    participation_status, seat_reserved_at, joined_round,
                    consecutive_timeouts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 'approved', 0,
                          ?, ?, 1, 0, ?, ?)
                """,
                (
                    participant_id,
                    session_id,
                    player["id"],
                    player["user_id"],
                    player["display_name"],
                    card_id,
                    version_id,
                    character_name,
                    code,
                    participation_status,
                    player["created_at"] or now,
                    player["created_at"] or now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO character_runtime_states(
                    id, session_id, participant_id, character_card_id,
                    state_json, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '{}', 1, ?, ?)
                """,
                (
                    new_id("runtime"),
                    session_id,
                    participant_id,
                    card_id,
                    now,
                    now,
                ),
            )

    def _seed_default_world(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT id FROM worlds WHERE slug = ?",
            (DEFAULT_WORLD["slug"],),
        ).fetchone()
        if existing:
            return
        now = utc_now()
        world_id = new_id("world")
        connection.execute(
            """
            INSERT INTO worlds(
                id, slug, name, description, system_prompt, rules_json,
                opening_scene, initial_state_json, archived, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                world_id,
                DEFAULT_WORLD["slug"],
                DEFAULT_WORLD["name"],
                DEFAULT_WORLD["description"],
                DEFAULT_WORLD["system_prompt"],
                json_dump(DEFAULT_WORLD["rules"]),
                DEFAULT_WORLD["opening_scene"],
                json_dump(DEFAULT_WORLD["initial_state"]),
                now,
                now,
            ),
        )
        for character in DEFAULT_CHARACTERS:
            connection.execute(
                """
                INSERT INTO characters(
                    id, world_id, slug, name, role, profile_json, prompt,
                    enabled, sort_order, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?)
                """,
                (
                    new_id("character"),
                    world_id,
                    character["slug"],
                    character["name"],
                    character["role"],
                    json_dump(character["profile"]),
                    character["prompt"],
                    character["sort_order"],
                    now,
                    now,
                ),
            )

    @staticmethod
    def _candidate_session_values(value: Any) -> set[str]:
        result: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) == "session_id" and item:
                    result.add(str(item))
                result.update(TavernDatabase._candidate_session_values(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                result.update(TavernDatabase._candidate_session_values(item))
        elif isinstance(value, str) and value.startswith("session_"):
            result.add(value)
        return result

    def _all_session_ids(self) -> set[str]:
        with self._connect() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM sessions"
                ).fetchall()
            }

    def _affected_session_ids(
        self,
        method_name: str,
        args: Sequence[Any],
        result: Any = None,
    ) -> set[str]:
        if method_name in self._ALL_SESSION_MUTATIONS:
            return self._all_session_ids()
        candidates: set[str] = set()
        if method_name == "_set_group_token_quota" and len(args) >= 2:
            with self._connect() as connection:
                candidates.update(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT id FROM sessions
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (str(args[0]), str(args[1])),
                    ).fetchall()
                )
        for value in (*args, result):
            candidates.update(self._candidate_session_values(value))
        if args and isinstance(args[0], str):
            candidates.add(str(args[0]))
        with self._connect() as connection:
            if candidates:
                placeholders = ",".join("?" for _ in candidates)
                candidates = {
                    str(row[0])
                    for row in connection.execute(
                        f"""
                        SELECT id FROM sessions
                        WHERE id IN ({placeholders})
                        """,
                        tuple(candidates),
                    ).fetchall()
                }
            lookup = self._ENTITY_SESSION_LOOKUPS.get(method_name)
            if not candidates and lookup and args:
                table, column = lookup
                row = connection.execute(
                    f"""
                    SELECT session_id FROM {table}
                    WHERE {column} = ?
                    """,
                    (str(args[0]),),
                ).fetchone()
                if row:
                    candidates.add(str(row[0]))
            if not candidates and method_name == "_cancel_card_draft" and args:
                row = connection.execute(
                    """
                    SELECT pt.session_id
                    FROM character_card_drafts draft
                    JOIN participants pt ON pt.id = draft.participant_id
                    WHERE pt.private_origin = ?
                    ORDER BY draft.created_at DESC LIMIT 1
                    """,
                    (str(args[0]),),
                ).fetchone()
                if row:
                    candidates.add(str(row[0]))
        return candidates

    def _sync_after_mutation(
        self,
        method_name: str,
        args: Sequence[Any],
        result: Any,
        before: set[str],
    ) -> None:
        session_ids = before | self._affected_session_ids(
            method_name,
            args,
            result,
        )
        for session_id in sorted(session_ids):
            try:
                self.storage.sync_session(session_id)
            except Exception:
                # The catalog transaction has already committed. The storage
                # row records the error and startup bootstrap retries it.
                continue

        if method_name == "_create_snapshot":
            for session_id in sorted(session_ids):
                try:
                    self.storage.create_archive(
                        session_id,
                        kind="save",
                        reason="手动命名存档",
                        refresh=False,
                    )
                except Exception:
                    continue
        elif method_name == "_finalize_session":
            for session_id in sorted(session_ids):
                try:
                    self.storage.create_archive(
                        session_id,
                        kind="save",
                        reason="副本最终存档",
                        refresh=False,
                    )
                except Exception:
                    continue
        elif method_name == "_commit_turn_sync" and result:
            interval = int(args[12] or 0) if len(args) > 12 else 0
            turn_no = int(
                result.get("turn_no", 0)
                if isinstance(result, Mapping)
                else 0
            )
            if interval > 0 and turn_no > 0 and turn_no % interval == 0:
                for session_id in sorted(session_ids):
                    try:
                        self.storage.create_archive(
                            session_id,
                            kind="backup",
                            reason=f"第 {turn_no} 回合自动安全备份",
                            refresh=False,
                        )
                    except Exception:
                        continue

    def _mark_storage_pending(self, session_ids: Sequence[str]) -> None:
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE story_storage SET
                    sync_status = 'pending', last_error = '',
                    updated_at = ?
                WHERE session_id IN ({placeholders})
                """,
                (utc_now(), *session_ids),
            )

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        def invoke() -> T:
            method_name = fn.__name__
            before: set[str] = set()
            if (
                method_name in self._SESSION_MUTATIONS
                and hasattr(self, "storage")
            ):
                before = self._affected_session_ids(
                    method_name,
                    args,
                )
                self._mark_storage_pending(sorted(before))
            result = fn(*args)
            if (
                method_name in self._SESSION_MUTATIONS
                and hasattr(self, "storage")
            ):
                self._sync_after_mutation(
                    method_name,
                    args,
                    result,
                    before,
                )
            return result

        return await asyncio.to_thread(invoke)

    @staticmethod
    def _world(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "rules": json_load(row["rules_json"], {}),
            "opening_scene": row["opening_scene"],
            "initial_state": json_load(row["initial_state_json"], {}),
            "archived": bool(row["archived"]),
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        result["world_schema_version"] = int(result["rules"].get("world_schema_version", 1))
        result["capabilities"] = dict(result["rules"].get("capabilities") or {})
        result["player_limits"] = player_limits(result)
        result["card_template"] = card_template(result)
        result["time_rules"] = world_time_rules(result)
        rules = result["rules"]
        result["choice_mode"] = (
            "strict_abcd"
            if bool(rules.get("strict_choices", True))
            else "free_text"
        )
        result["check_density"] = str(
            rules.get("check_density", "standard")
        )
        return result

    @staticmethod
    def _character(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "world_id": row["world_id"],
            "slug": row["slug"],
            "name": row["name"],
            "role": row["role"],
            "profile": json_load(row["profile_json"], {}),
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "sort_order": row["sort_order"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        stored_world_state = json_load(row["world_state_json"], {})
        result = {
            "id": row["id"],
            "platform_id": row["platform_id"],
            "group_id": row["group_id"],
            "unified_origin": row["unified_origin"],
            "instance_slug": (
                row["instance_slug"]
                if "instance_slug" in keys
                else row["world_slug"]
            ),
            "instance_name": (
                row["instance_name"]
                if "instance_name" in keys
                else row["world_name"]
            ),
            "selected": bool(row["selected"]) if "selected" in keys else True,
            "world_id": row["world_id"],
            "state": row["state"],
            "turn_no": row["turn_no"],
            "revision": row["revision"],
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "history_floor_seq": row["history_floor_seq"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if "world_name" in keys:
            result["world_name"] = row["world_name"]
        if "world_slug" in keys:
            result["world_slug"] = row["world_slug"]
        if "world_description" in keys:
            result["world_description"] = str(
                row["world_description"] or ""
            )
        if "group_remark" in keys:
            result["group_remark"] = str(row["group_remark"] or "")
        if "group_revision" in keys:
            result["group_revision"] = int(row["group_revision"] or 1)
        if "storage_relative_path" in keys:
            result["storage_relative_path"] = str(
                row["storage_relative_path"] or ""
            )
        if "storage_sync_status" in keys:
            result["storage_sync_status"] = str(
                row["storage_sync_status"] or "pending"
            )
        if "storage_last_error" in keys:
            result["storage_last_error"] = str(
                row["storage_last_error"] or ""
            )
        if "playthrough_no" in keys:
            result["playthrough_no"] = int(row["playthrough_no"] or 1)
        if "player_count" in keys:
            result["player_count"] = row["player_count"]
        for key in (
            "ready_count",
            "memory_count",
            "snapshot_count",
            "npc_count",
            "active_timer_count",
        ):
            if key in keys:
                result[key] = int(row[key] or 0)
        if "progress_json" in keys:
            result["progress"] = normalize_progress(
                json_load(row["progress_json"], {})
            )
        if "recovery_json" in keys:
            result["recovery"] = json_load(row["recovery_json"], {})
        if "termination_type" in keys:
            result["archive"] = (
                {
                    "termination_type": row["termination_type"],
                    "reason": row["archive_reason"],
                    "final_snapshot_id": row["final_snapshot_id"],
                    "ended_by": row["ended_by"],
                    "ended_at": row["ended_at"],
                    "readonly": bool(row["readonly"]),
                }
                if row["termination_type"]
                else None
            )
            result["readonly"] = bool(row["readonly"])
        if "waiting_for" in keys:
            result["waiting_for"] = str(row["waiting_for"] or "")
        if "active_deadline_at" in keys:
            result["active_deadline_at"] = str(
                row["active_deadline_at"] or ""
            )
        progress = result.get("progress")
        if isinstance(progress, Mapping):
            total = int(progress.get("total_milestones") or 0)
            completed = int(progress.get("completed_milestones") or 0)
            result["progress_percent"] = (
                round(completed * 100 / total) if total > 0 else None
            )
        return result

    @staticmethod
    def _player(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "character_name": row["character_name"],
            "profile": json_load(row["profile_json"], {}),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "id": row["id"],
            "session_id": row["session_id"],
            "turn_no": row["turn_no"],
            "role": row["role"],
            "actor_id": row["actor_id"],
            "actor_name": row["actor_name"],
            "content": row["content"],
            "meta": json_load(row["meta_json"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _memory(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "scope": row["scope"],
            "scope_id": row["scope_id"],
            "kind": row["kind"],
            "content": row["content"],
            "importance": row["importance"],
            "salience": row["salience"],
            "tags": json_load(row["tags_json"], []),
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "visibility": (
                row["governance_visibility"]
                if "governance_visibility" in keys
                and row["governance_visibility"]
                else "public"
            ),
            "locked": bool(
                row["governance_locked"]
                if "governance_locked" in keys else 0
            ),
            "pinned": bool(
                row["governance_pinned"]
                if "governance_pinned" in keys else 0
            ),
            "invalidated": bool(
                row["governance_invalidated"]
                if "governance_invalidated" in keys else 0
            ),
            "supersedes_id": (
                row["governance_supersedes_id"]
                if "governance_supersedes_id" in keys else ""
            ),
            "conflict_status": (
                row["governance_conflict_status"]
                if "governance_conflict_status" in keys
                and row["governance_conflict_status"]
                else "clear"
            ),
            "governance_note": (
                row["governance_note"]
                if "governance_note" in keys else ""
            ),
        }

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        stored_world_state = json_load(row["world_state_json"], {})
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "kind": row["kind"],
            "turn_no": row["turn_no"],
            "session_revision": row["session_revision"],
            "world_id": row["world_id"],
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _session_character(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "name": row["name"],
            "aliases": json_load(row["aliases_json"], []),
            "role_type": row["role_type"],
            "public_profile": json_load(row["public_profile_json"], {}),
            "known_facts": json_load(row["known_facts_json"], []),
            "misconceptions": json_load(row["misconceptions_json"], []),
            "source": row["source"],
            "review_status": row["review_status"],
            "lifecycle_status": row["lifecycle_status"],
            "persistent": bool(row["persistent"]),
            "first_event_id": row["first_event_id"],
            "last_event_id": row["last_event_id"],
            "first_turn": row["first_turn"],
            "last_turn": row["last_turn"],
            "revision": row["revision"],
            "state": (
                json_load(row["state_json"], {})
                if "state_json" in keys
                else {}
            ),
            "state_revision": (
                int(row["state_revision"] or 0)
                if "state_revision" in keys
                else 0
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ledger_entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "kind": row["kind"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "visibility": row["visibility"],
            "source_event_id": row["source_event_id"],
            "completed_event_id": row["completed_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _scene_clock(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "title": row["title"],
            "segments": row["segments"],
            "current_value": row["current_value"],
            "visibility": row["visibility"],
            "trigger_text": row["trigger_text"],
            "status": row["status"],
            "triggered_event_id": row["triggered_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _assert_session_writable(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT s.state, sa.readonly
            FROM sessions s
            LEFT JOIN session_archives sa ON sa.session_id = s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            raise DatabaseNotFoundError("会话不存在")
        if row["state"] == SESSION_FINISHED or bool(row["readonly"]):
            raise InvalidTransitionError(
                "该副本已永久归档并处于只读状态"
            )

    async def list_worlds(
        self,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_worlds, include_archived)

    def _list_worlds(self, include_archived: bool) -> list[dict[str, Any]]:
        with self._connect() as connection:
            condition = "" if include_archived else "WHERE archived = 0"
            rows = connection.execute(
                f"""
                SELECT * FROM worlds
                {condition}
                ORDER BY archived ASC, updated_at DESC, name COLLATE NOCASE
                """
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._world(row)
                count = connection.execute(
                    "SELECT COUNT(*) FROM characters WHERE world_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                item["character_count"] = count
                result.append(item)
            return result

    async def get_world(self, world_ref: str) -> dict[str, Any]:
        return await self._run(self._get_world, world_ref)

    def _get_world(self, world_ref: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worlds WHERE id = ? OR slug = ?",
                (world_ref, world_ref),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("世界包不存在")
            world = self._world(row)
            character_rows = connection.execute(
                """
                SELECT * FROM characters
                WHERE world_id = ? AND enabled = 1
                ORDER BY sort_order ASC, name COLLATE NOCASE
                """,
                (row["id"],),
            ).fetchall()
            world["characters"] = [
                self._character(character) for character in character_rows
            ]
            return world

    async def save_world(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_world, dict(payload), actor_id)

    def _save_world(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        world_id = str(payload.get("id") or "").strip()
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=400)
        if not name:
            raise ValueError("世界名称不能为空")
        description = clean_text(payload.get("description"), max_chars=20000)
        system_prompt = clean_text(
            payload.get("system_prompt"),
            max_chars=200000,
        )
        if not system_prompt:
            raise ValueError("世界设定不能为空")
        opening_scene = clean_text(
            payload.get("opening_scene"),
            max_chars=50000,
        )
        rules = payload.get("rules")
        initial_state = payload.get("initial_state")
        if not isinstance(rules, Mapping):
            raise ValueError("规则必须是 JSON 对象")
        if not isinstance(initial_state, Mapping):
            raise ValueError("初始状态必须是 JSON 对象")
        rules = dict(rules)
        if "world_schema_version" in payload:
            rules["world_schema_version"] = payload["world_schema_version"]
        if "capabilities" in payload:
            rules["capabilities"] = payload["capabilities"]
        validate_world_contract({**payload, "rules": rules})
        if "character_card" in rules:
            raw_card = rules["character_card"]
            if isinstance(raw_card, Mapping) and "fields" not in raw_card:
                rules["character_card"] = card_template({"rules": rules})
            validate_card_template_config(rules["character_card"])
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if world_id:
                    current = connection.execute(
                        "SELECT * FROM worlds WHERE id = ?",
                        (world_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("世界包不存在")
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "世界包已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            archived = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            slug,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            (
                                int(bool(payload["archived"]))
                                if "archived" in payload
                                else current["archived"]
                            ),
                            now,
                            world_id,
                        ),
                    )
                    action = "world.update"
                else:
                    world_id = new_id("world")
                    connection.execute(
                        """
                        INSERT INTO worlds(
                            id, slug, name, description, system_prompt,
                            rules_json, opening_scene, initial_state_json,
                            archived, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                        """,
                        (
                            world_id,
                            slug,
                            name,
                            description,
                            system_prompt,
                            json_dump(dict(rules)),
                            opening_scene,
                            json_dump(dict(initial_state)),
                            now,
                            now,
                        ),
                    )
                    action = "world.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    world_id,
                    {"slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _commit_vnext_workflow(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        next_turn_state: Mapping[str, Any],
        player_user_id: str,
        player_event_id: str,
        narrator_event_id: str,
        world_state: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        workflow: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Persist choices, votes, rolls, events and timers in the turn TX."""

        result: dict[str, Any] = {}
        if not workflow:
            return result

        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
            """,
            (session["id"], player_user_id),
        ).fetchone()
        if not participant:
            raise InvalidTransitionError("当前玩家没有有效的副本参与记录")

        choice_set_id = str(workflow.get("choice_set_id") or "")
        selected_key = str(workflow.get("selected_key") or "").upper()
        flavor_text = clean_text(
            workflow.get("flavor_text"),
            max_chars=160,
        )
        if not choice_set_id or selected_key not in CHOICE_KEYS:
            raise ValueError("缺少有效的选项提交信息")
        choice_row = connection.execute(
            """
            SELECT * FROM choice_sets
            WHERE id = ? AND session_id = ? AND status = 'active'
            """,
            (choice_set_id, session["id"]),
        ).fetchone()
        if not choice_row:
            raise DatabaseConflictError("当前选项已经失效，请重新查看回合")
        if choice_row["participant_id"] != participant["id"]:
            raise PermissionError("该选项不属于当前玩家")
        if int(choice_row["session_revision"]) != int(session["revision"]):
            raise DatabaseConflictError("场景已变化，旧选项不能继续使用")
        choices = normalize_choices(json_load(choice_row["choices_json"], []))
        selected = next(
            item for item in choices if item["key"] == selected_key
        )
        connection.execute(
            """
            UPDATE choice_sets SET
                status = 'selected', selected_key = ?,
                flavor_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (selected_key, flavor_text, now, choice_set_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'completed', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'turn' AND status = 'active'
            """,
            (now, session["id"], participant["id"]),
        )
        connection.execute(
            """
            UPDATE participants
            SET consecutive_timeouts = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, participant["id"]),
        )
        result["choice"] = {
            "choice_set_id": choice_set_id,
            "key": selected_key,
            "text": selected["text"],
        }

        if check_payload:
            roll_id = new_id("roll")
            connection.execute(
                """
                INSERT INTO rolls(
                    id, session_id, choice_set_id, participant_id,
                    roll_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    roll_id,
                    session["id"],
                    choice_set_id,
                    participant["id"],
                    json_dump(dict(check_payload)),
                    now,
                ),
            )
            result["roll_id"] = roll_id

        config = connection.execute(
            """
            SELECT * FROM instance_configs WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        time_rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        round_completed = int(next_turn_state["round_no"]) > acting_round
        if round_completed:
            selected_event = self._select_world_event(
                connection,
                session_id=session["id"],
                round_no=acting_round,
                world=world,
                turn_no=new_turn,
                now=now,
            )
            if selected_event:
                result["world_event"] = selected_event

        return_progress = workflow.get("return_progress")
        if isinstance(return_progress, Mapping):
            request_id = str(return_progress.get("request_id") or "")
            evidence = clean_text(
                return_progress.get("evidence"),
                max_chars=500,
            )
            if request_id and evidence:
                progress_result = self._record_return_progress(
                    connection,
                    session_id=session["id"],
                    request_id=request_id,
                    evidence=evidence,
                    completed=bool(return_progress.get("completed", False)),
                    round_no=int(next_turn_state["round_no"]),
                    turn_no=new_turn,
                    now=now,
                )
                if progress_result:
                    result["return_progress"] = progress_result

        next_user_id = str(next_turn_state["current_user_id"] or "")
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], next_user_id),
        ).fetchone()
        if not next_participant:
            result["next_choice_set_id"] = ""
            return result

        group_decision = workflow.get("group_decision")
        if isinstance(group_decision, Mapping):
            question = clean_text(
                group_decision.get("question"),
                max_chars=500,
            )
            options = self._normalize_vote_options(
                group_decision.get("options")
            )
            if question and len(options) >= 2:
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        ORDER BY MIN(created_at)
                        """,
                        (session["id"],),
                    ).fetchall()
                ]
                vote_id = new_id("vote")
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, source_event_id, question,
                        options_json, eligible_user_ids_json, stage,
                        status, suspended_user_id, deadline_at,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session["id"],
                        narrator_event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        next_user_id,
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session["id"],
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "stage": 1},
                )
                result["vote_id"] = vote_id
                return result

        next_choices_raw = workflow.get("next_choices")
        try:
            next_choices = normalize_choices(next_choices_raw)
        except ValueError:
            next_choices = fallback_choices(world_state)
            result["choice_fallback"] = True
        choice_id = new_id("choices")
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                next_participant["id"],
                next_turn_state["round_no"],
                int(session["revision"]) + 1,
                json_dump(next_choices),
                f"turn:{session['id']}:{new_turn + 1}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=time_rules["turn_timeout_seconds"],
            reminder_seconds=time_rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": next_user_id,
            },
        )
        result["next_choice_set_id"] = choice_id
        result["next_participant_id"] = next_participant["id"]
        return result

    def _apply_v05_turn_ops(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        participant: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        source_event_id: str,
        workflow: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Apply validated v0.5 state operations inside the turn transaction."""

        result: dict[str, Any] = {
            "npc": [],
            "clocks": [],
            "ledger": [],
            "statuses": [],
            "assists": [],
        }
        session_id = str(session["id"])

        inspiration_mode = str(
            workflow.get("inspiration_mode") or ""
        ).lower()
        if inspiration_mode in {"advantage", "reroll"} and check_payload:
            runtime = connection.execute(
                """
                SELECT * FROM character_runtime_states
                WHERE session_id = ? AND participant_id = ?
                """,
                (session_id, participant["id"]),
            ).fetchone()
            if not runtime:
                raise InvalidTransitionError("角色缺少副本运行状态")
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            balance = bounded_int(state.get("inspiration"), 1, 0, 3)
            if balance < 1:
                raise InvalidTransitionError("灵感点不足，本轮没有提交")
            operation_id = (
                f"inspiration:{workflow.get('choice_set_id')}:{inspiration_mode}"
            )
            existing = connection.execute(
                """
                SELECT balance_after FROM inspiration_transactions
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if not existing:
                balance -= 1
                state["inspiration"] = balance
                state["inspiration_max"] = bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                )
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO inspiration_transactions(
                        id, session_id, participant_id, delta,
                        balance_after, reason, operation_id, created_at
                    ) VALUES (?, ?, ?, -1, ?, ?, ?, ?)
                    """,
                    (
                        new_id("inspire"),
                        session_id,
                        participant["id"],
                        balance,
                        (
                            "投骰前取得优势"
                            if inspiration_mode == "advantage"
                            else "预授权重投完整骰池"
                        ),
                        operation_id,
                        now,
                    ),
                )
            else:
                balance = int(existing["balance_after"])
            result["inspiration"] = {
                "mode": inspiration_mode,
                "balance": balance,
            }

        assist_token_id = str(
            workflow.get("assist_token_id") or ""
        ).strip()
        if assist_token_id and check_payload:
            consumed = connection.execute(
                """
                UPDATE assist_tokens SET status = 'consumed',
                    consumed_at = ?
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (now, assist_token_id, session_id),
            )
            if consumed.rowcount:
                result["consumed_assist_id"] = assist_token_id

        status_ops = workflow.get("status_ops")
        if isinstance(status_ops, Sequence) and not isinstance(
            status_ops,
            (str, bytes),
        ):
            for operation in status_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                name = clean_text(operation.get("name"), max_chars=100)
                if not target_ref or not name:
                    continue
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target:
                    continue
                runtime = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id = ? AND participant_id = ?
                    """,
                    (session_id, target["id"]),
                ).fetchone()
                if not runtime:
                    continue
                state = json_load(runtime["state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                statuses = [
                    dict(item)
                    for item in state.get("statuses", [])
                    if isinstance(item, Mapping)
                ]
                op = str(operation.get("op") or "add").lower()
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(statuses)
                        if str(item.get("name") or "").casefold()
                        == name.casefold()
                    ),
                    -1,
                )
                if op == "remove":
                    if existing_index >= 0:
                        statuses.pop(existing_index)
                else:
                    entry = {
                        "name": name,
                        "severity": str(
                            operation.get("severity") or "minor"
                        ),
                        "affects": [
                            clean_text(item, max_chars=80)
                            for item in (
                                operation.get("affects")
                                if isinstance(
                                    operation.get("affects"),
                                    list,
                                )
                                else []
                            )[:12]
                            if clean_text(item, max_chars=80)
                        ],
                        "effect": clean_text(
                            operation.get("effect"),
                            max_chars=300,
                        ),
                        "removal": clean_text(
                            operation.get("removal"),
                            max_chars=300,
                        ),
                        "source_event_id": source_event_id,
                        "created_turn": new_turn,
                    }
                    if existing_index >= 0:
                        statuses[existing_index] = entry
                    else:
                        statuses.append(entry)
                state["statuses"] = statuses[:40]
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                result["statuses"].append(
                    {
                        "target_id": target["id"],
                        "name": name,
                        "op": op,
                    }
                )

        config = connection.execute(
            """
            SELECT npc_policy_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        npc_policy = json_load(
            config["npc_policy_json"] if config else "",
            {},
        )
        max_new_npcs = bounded_int(
            npc_policy.get("max_new_per_turn"),
            3,
            0,
            3,
        )
        if not bool(npc_policy.get("enabled", True)):
            max_new_npcs = 0
        created_count = 0
        npc_ops = workflow.get("npc_ops")
        if isinstance(npc_ops, Sequence) and not isinstance(
            npc_ops,
            (str, bytes),
        ):
            for operation in npc_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "").lower()
                name = clean_text(operation.get("name"), max_chars=80)
                npc_id = clean_text(operation.get("npc_id"), max_chars=128)
                aliases = [
                    clean_text(item, max_chars=80)
                    for item in (
                        operation.get("aliases")
                        if isinstance(operation.get("aliases"), list)
                        else []
                    )[:12]
                    if clean_text(item, max_chars=80)
                ]
                npc = None
                matched_by_name = False
                if npc_id:
                    npc = connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE id = ? AND session_id = ?
                        """,
                        (npc_id, session_id),
                    ).fetchone()
                if not npc and name:
                    normalized_names = {
                        self._stable_key(name),
                        *(self._stable_key(item) for item in aliases),
                    }
                    for candidate in connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall():
                        candidate_names = {
                            self._stable_key(candidate["name"]),
                            *(
                                self._stable_key(item)
                                for item in json_load(
                                    candidate["aliases_json"],
                                    [],
                                )
                            ),
                        }
                        if normalized_names & candidate_names:
                            npc = candidate
                            matched_by_name = True
                            break
                if op == "create" and npc and matched_by_name:
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc["id"],),
                    ).fetchone()
                    raw_duplicate_state = json_load(
                        state_row["state_json"] if state_row else "",
                        {},
                    )
                    duplicate_state = (
                        dict(raw_duplicate_state)
                        if isinstance(raw_duplicate_state, Mapping)
                        else {}
                    )
                    proposals = list(
                        duplicate_state.get("duplicate_proposals") or []
                    )
                    proposals.append(
                        {
                            "name": name,
                            "aliases": aliases,
                            "public_profile": dict(
                                operation.get("public_profile") or {}
                            ),
                            "source_event_id": source_event_id,
                            "turn_no": new_turn,
                        }
                    )
                    duplicate_state["duplicate_proposals"] = proposals[-5:]
                    connection.execute(
                        """
                        UPDATE session_characters
                        SET review_status = 'duplicate',
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, npc["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc["id"], json_dump(duplicate_state), now),
                    )
                    result["npc"].append(
                        {
                            "id": npc["id"],
                            "op": "duplicate_suspected",
                            "name": name,
                        }
                    )
                    continue
                if op == "create" and not npc:
                    registration_reasons = {
                        str(item)
                        for item in (
                            operation.get("registration_reasons") or []
                        )
                        if str(item)
                        in {
                            "direct_interaction",
                            "important_clue",
                            "long_term_memory",
                        }
                    }
                    if (
                        created_count >= max_new_npcs
                        or not name
                        or not bool(operation.get("persistent", True))
                        or not registration_reasons
                    ):
                        continue
                    created_count += 1
                    npc_id = new_id("snpc")
                    review_status = (
                        "pending"
                        if bool(
                            npc_policy.get(
                                "generated_requires_review",
                                True,
                            )
                        )
                        else "approved"
                    )
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'model_generated', ?, 'active', 1,
                                  ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            npc_id,
                            session_id,
                            f"generated:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            clean_text(
                                operation.get("role_type") or "npc",
                                max_chars=40,
                            ),
                            json_dump(
                                dict(operation.get("public_profile") or {})
                            ),
                            json_dump(
                                list(operation.get("known_facts") or [])[:30]
                            ),
                            json_dump(
                                list(operation.get("misconceptions") or [])[:20]
                            ),
                            review_status,
                            source_event_id,
                            source_event_id,
                            new_turn,
                            new_turn,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            npc_id,
                            json_dump(
                                dict(operation.get("runtime_state") or {})
                            ),
                            now,
                        ),
                    )
                    result["npc"].append(
                        {"id": npc_id, "op": "create", "name": name}
                    )
                    continue
                if not npc:
                    continue
                npc_id = str(npc["id"])
                lifecycle_status = str(npc["lifecycle_status"])
                if op == "archive":
                    lifecycle_status = "archived"
                elif op == "depart":
                    lifecycle_status = "departed"
                elif op == "kill":
                    lifecycle_status = "dead"
                elif op in {"update", "create"}:
                    lifecycle_status = "active"
                profile = dict(
                    json_load(npc["public_profile_json"], {})
                )
                if isinstance(operation.get("public_profile"), Mapping):
                    profile.update(dict(operation["public_profile"]))
                known = list(json_load(npc["known_facts_json"], []))
                for fact in list(operation.get("known_facts") or [])[:30]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in known:
                        known.append(text)
                misconceptions = list(
                    json_load(npc["misconceptions_json"], [])
                )
                for fact in list(
                    operation.get("misconceptions") or []
                )[:20]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in misconceptions:
                        misconceptions.append(text)
                connection.execute(
                    """
                    UPDATE session_characters SET
                        public_profile_json = ?, known_facts_json = ?,
                        misconceptions_json = ?, lifecycle_status = ?,
                        last_event_id = ?, last_turn = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(profile),
                        json_dump(known[:60]),
                        json_dump(misconceptions[:40]),
                        lifecycle_status,
                        source_event_id,
                        new_turn,
                        now,
                        npc_id,
                    ),
                )
                if isinstance(operation.get("runtime_state"), Mapping):
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc_id,),
                    ).fetchone()
                    state = dict(
                        json_load(
                            state_row["state_json"] if state_row else "",
                            {},
                        )
                    )
                    state.update(dict(operation["runtime_state"]))
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc_id, json_dump(state), now),
                    )
                result["npc"].append(
                    {"id": npc_id, "op": op, "name": npc["name"]}
                )

        ledger_ops = workflow.get("ledger_ops")
        if isinstance(ledger_ops, Sequence) and not isinstance(
            ledger_ops,
            (str, bytes),
        ):
            for operation in ledger_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "update").lower()
                entry_id = clean_text(
                    operation.get("entry_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=160)
                row = None
                if entry_id:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE id = ? AND session_id = ?
                        """,
                        (entry_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                status = {
                    "complete": "completed",
                    "fail": "failed",
                    "archive": "archived",
                }.get(op, "active")
                kind = str(operation.get("kind") or "objective").lower()
                if kind not in {
                    "main",
                    "side",
                    "objective",
                    "clue",
                    "milestone",
                    "failed",
                }:
                    kind = "objective"
                if not row and op == "create" and title:
                    entry_id = new_id("ledger")
                    connection.execute(
                        """
                        INSERT INTO story_ledger(
                            id, session_id, stable_key, kind, title,
                            description, status, visibility,
                            source_event_id, completed_event_id,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, '',
                                  1, ?, ?)
                        """,
                        (
                            entry_id,
                            session_id,
                            self._stable_key(title),
                            kind,
                            title,
                            clean_text(
                                operation.get("description"),
                                max_chars=800,
                            ),
                            (
                                "host"
                                if str(
                                    operation.get("visibility") or ""
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            source_event_id,
                            now,
                            now,
                        ),
                    )
                elif row:
                    entry_id = str(row["id"])
                    connection.execute(
                        """
                        UPDATE story_ledger SET
                            kind = ?, title = ?, description = ?,
                            status = ?, visibility = ?,
                            completed_event_id = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            kind,
                            title or row["title"],
                            clean_text(
                                operation.get("description")
                                or row["description"],
                                max_chars=800,
                            ),
                            status,
                            (
                                "host"
                                if str(
                                    operation.get("visibility")
                                    or row["visibility"]
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            (
                                source_event_id
                                if status in {"completed", "failed"}
                                else row["completed_event_id"]
                            ),
                            now,
                            entry_id,
                        ),
                    )
                else:
                    continue
                result["ledger"].append(
                    {"id": entry_id, "op": op, "status": status}
                )

        clock_ops = workflow.get("clock_ops")
        if isinstance(clock_ops, Sequence) and not isinstance(
            clock_ops,
            (str, bytes),
        ):
            for operation in clock_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "advance").lower()
                clock_id = clean_text(
                    operation.get("clock_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=100)
                row = None
                if clock_id:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE id = ? AND session_id = ?
                        """,
                        (clock_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                if not row and op == "create" and title:
                    segments = bounded_int(
                        operation.get("segments"),
                        4,
                        4,
                        8,
                    )
                    if segments not in {4, 6, 8}:
                        segments = 4
                    clock_id = new_id("clock")
                    connection.execute(
                        """
                        INSERT INTO scene_clocks(
                            id, session_id, stable_key, title, segments,
                            current_value, visibility, trigger_text, status,
                            triggered_event_id, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'active', '',
                                  1, ?, ?)
                        """,
                        (
                            clock_id,
                            session_id,
                            self._stable_key(title),
                            title,
                            segments,
                            str(operation.get("visibility") or "public"),
                            clean_text(
                                operation.get("trigger"),
                                max_chars=500,
                            ),
                            now,
                            now,
                        ),
                    )
                    current_value = 0
                    status = "active"
                elif row:
                    clock_id = str(row["id"])
                    segments = int(row["segments"])
                    current_value = int(row["current_value"])
                    if op == "advance":
                        current_value += bounded_int(
                            operation.get("delta"),
                            1,
                            -8,
                            8,
                        )
                    elif op == "set":
                        current_value = bounded_int(
                            operation.get("value"),
                            current_value,
                            0,
                            segments,
                        )
                    elif op == "complete":
                        current_value = segments
                    current_value = max(0, min(segments, current_value))
                    status = (
                        "archived"
                        if op == "archive"
                        else "completed"
                        if current_value >= segments
                        else "active"
                    )
                    triggered_event_id = str(row["triggered_event_id"] or "")
                    trigger_text = clean_text(
                        operation.get("trigger") or row["trigger_text"],
                        max_chars=500,
                    )
                    if (
                        status == "completed"
                        and not triggered_event_id
                    ):
                        triggered_event_id = new_id("event")
                        connection.execute(
                            """
                            INSERT INTO events(
                                id, session_id, turn_no, role, actor_id,
                                actor_name, content, meta_json, created_at
                            ) VALUES (?, ?, ?, 'system', 'clock',
                                      '场景时钟', ?, ?, ?)
                            """,
                            (
                                triggered_event_id,
                                session_id,
                                new_turn,
                                trigger_text
                                or f"场景时钟「{row['title']}」已填满。",
                                json_dump(
                                    {
                                        "kind": "scene_clock_trigger",
                                        "clock_id": clock_id,
                                    }
                                ),
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE scene_clocks SET
                            current_value = ?, status = ?,
                            triggered_event_id = ?, trigger_text = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            current_value,
                            status,
                            triggered_event_id,
                            trigger_text,
                            now,
                            clock_id,
                        ),
                    )
                else:
                    continue
                result["clocks"].append(
                    {
                        "id": clock_id,
                        "op": op,
                        "current_value": current_value,
                        "segments": segments,
                        "status": status,
                    }
                )

        assist_ops = workflow.get("assist_ops")
        selected_text = str(
            (workflow.get("selected_choice") or {}).get("text")
            if isinstance(workflow.get("selected_choice"), Mapping)
            else ""
        )
        if (
            isinstance(assist_ops, Sequence)
            and not isinstance(assist_ops, (str, bytes))
            and any(word in selected_text for word in ("协助", "帮助", "支援"))
        ):
            for operation in assist_ops[:1]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                method = clean_text(
                    operation.get("method"),
                    max_chars=300,
                )
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    ) LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target or not method or target["id"] == participant["id"]:
                    continue
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND target_participant_id = ?
                      AND status = 'active'
                    """,
                    (session_id, target["id"]),
                )
                token_id = new_id("assist")
                expires_round = bounded_int(
                    operation.get("expires_round"),
                    acting_round + 1,
                    acting_round,
                    acting_round + 1,
                )
                connection.execute(
                    """
                    INSERT INTO assist_tokens(
                        id, session_id, source_participant_id,
                        target_participant_id, stat, method, status,
                        expires_round, source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        token_id,
                        session_id,
                        participant["id"],
                        target["id"],
                        clean_text(operation.get("stat"), max_chars=40),
                        method,
                        expires_round,
                        source_event_id,
                        now,
                    ),
                )
                result["assists"].append(
                    {"id": token_id, "target_id": target["id"]}
                )

        connection.execute(
            """
            UPDATE assist_tokens SET status = 'expired'
            WHERE session_id = ? AND status = 'active'
              AND expires_round > 0 AND expires_round < ?
            """,
            (session_id, acting_round),
        )

        milestone = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed
            FROM story_ledger
            WHERE session_id = ? AND kind = 'milestone'
              AND status <> 'archived'
            """,
            (session_id,),
        ).fetchone()
        objective = connection.execute(
            """
            SELECT title FROM story_ledger
            WHERE session_id = ? AND status = 'active'
              AND kind IN ('main', 'objective')
            ORDER BY CASE kind WHEN 'main' THEN 0 ELSE 1 END,
                     updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        rule_row = connection.execute(
            """
            SELECT progress_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if rule_row:
            progress = normalize_progress(
                json_load(rule_row["progress_json"], {})
            )
            if int(milestone["total"] or 0) > 0:
                progress["total_milestones"] = int(milestone["total"])
                progress["completed_milestones"] = int(
                    milestone["completed"] or 0
                )
            if objective:
                progress["current_objective"] = str(objective["title"])
            connection.execute(
                """
                UPDATE session_rule_states SET progress_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (json_dump(progress), now, session_id),
            )
            result["progress"] = progress
        return result

    @staticmethod
    def _normalize_vote_options(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes),
        ):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(value[:4]):
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or CHOICE_KEYS[index]).upper()
            text = clean_text(item.get("text"), max_chars=240)
            if key not in CHOICE_KEYS or key in seen or not text:
                continue
            seen.add(key)
            result.append({"key": key, "text": text})
        return result

    def _select_world_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        round_no: int,
        world: Mapping[str, Any],
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        if connection.execute(
            """
            SELECT 1 FROM selected_world_events
            WHERE session_id = ? AND round_no = ?
            """,
            (session_id, round_no),
        ).fetchone():
            return None
        rules = world.get("rules")
        rules = rules if isinstance(rules, Mapping) else {}
        pool = rules.get("event_pool")
        if not isinstance(pool, Sequence) or isinstance(pool, (str, bytes)):
            return None
        session_row = connection.execute(
            """
            SELECT world_state_json FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        state = json_load(
            session_row["world_state_json"] if session_row else "",
            {},
        )
        location = str(state.get("location") or "").casefold()
        facts = {
            str(item).casefold()
            for item in (
                state.get("facts")
                if isinstance(state.get("facts"), list)
                else []
            )
        }
        active_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM participants
                WHERE session_id = ? AND participation_status = 'active'
                  AND card_status = 'approved'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        candidates: list[tuple[dict[str, Any], int]] = []
        for raw in pool[:200]:
            if not isinstance(raw, Mapping):
                continue
            item_id = clean_text(raw.get("id"), max_chars=80)
            description = clean_text(
                raw.get("description"),
                max_chars=1000,
            )
            if not item_id or not description:
                continue
            minimum_round = bounded_int(
                raw.get("minimum_round"),
                1,
                1,
                1_000_000,
            )
            if round_no < minimum_round:
                continue
            conditions = raw.get("conditions")
            conditions = (
                conditions if isinstance(conditions, Mapping) else {}
            )
            allowed_locations = conditions.get("locations")
            if isinstance(allowed_locations, Sequence) and not isinstance(
                allowed_locations,
                (str, bytes),
            ):
                normalized_locations = {
                    str(item).casefold()
                    for item in allowed_locations
                    if str(item).strip()
                }
                if normalized_locations and location not in normalized_locations:
                    continue
            required_facts = conditions.get("required_facts")
            if isinstance(required_facts, Sequence) and not isinstance(
                required_facts,
                (str, bytes),
            ):
                required = {
                    str(item).casefold()
                    for item in required_facts
                    if str(item).strip()
                }
                if not required.issubset(facts):
                    continue
            excluded_facts = conditions.get("excluded_facts")
            if isinstance(excluded_facts, Sequence) and not isinstance(
                excluded_facts,
                (str, bytes),
            ):
                excluded = {
                    str(item).casefold()
                    for item in excluded_facts
                    if str(item).strip()
                }
                if excluded.intersection(facts):
                    continue
            minimum_players = bounded_int(
                conditions.get("minimum_players"),
                0,
                0,
                32,
            )
            if active_count < minimum_players:
                continue
            maximum_players = conditions.get("maximum_players")
            if (
                maximum_players not in {None, ""}
                and active_count
                > bounded_int(maximum_players, 32, 0, 32)
            ):
                continue
            previous = connection.execute(
                """
                SELECT round_no FROM selected_world_events
                WHERE session_id = ? AND pool_item_id = ?
                ORDER BY round_no DESC LIMIT 1
                """,
                (session_id, item_id),
            ).fetchone()
            if previous and bool(raw.get("once", False)):
                continue
            cooldown = bounded_int(
                raw.get("cooldown_rounds"),
                0,
                0,
                1_000_000,
            )
            if previous and round_no - int(previous["round_no"]) <= cooldown:
                continue
            weight = bounded_int(raw.get("weight"), 1, 1, 1000)
            candidates.append((dict(raw), weight))
        if not candidates:
            return None
        total = sum(weight for _, weight in candidates)
        pick = secrets.randbelow(total)
        selected = candidates[-1][0]
        for item, weight in candidates:
            if pick < weight:
                selected = item
                break
            pick -= weight
        event_id = new_id("worldevent")
        item_id = clean_text(selected.get("id"), max_chars=80)
        description = clean_text(
            selected.get("description"),
            max_chars=1000,
        )
        payload = {
            "id": item_id,
            "title": clean_text(selected.get("title"), max_chars=120),
            "description": description,
            "severity": clean_text(
                selected.get("severity") or "standard",
                max_chars=30,
            ),
        }
        connection.execute(
            """
            INSERT INTO selected_world_events(
                id, session_id, round_no, pool_item_id, payload_json,
                status, narrative, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'narrated', ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                round_no,
                item_id,
                json_dump(payload),
                description,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'world', '世界脉冲', ?, ?, ?)
            """,
            (
                new_id("event"),
                session_id,
                turn_no,
                description,
                json_dump(
                    {
                        "kind": "world_pulse",
                        "selected_world_event_id": event_id,
                        "round_no": round_no,
                    }
                ),
                now,
            ),
        )
        return {"id": event_id, **payload}

    async def archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._archive_world, world_id, actor_id)

    def _archive_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                active_sessions = connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions
                    WHERE world_id = ? AND state != 'closed'
                    """,
                    (world_id,),
                ).fetchone()[0]
                if active_sessions:
                    raise ValueError("仍有运行中的会话使用该世界，不能归档")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 1, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.archive",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._restore_world, world_id, actor_id)

    def _restore_world(
        self,
        world_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("世界包不存在")
                connection.execute(
                    """
                    UPDATE worlds
                    SET archived = 0, revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (utc_now(), world_id),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "world.restore",
                    world_id,
                    {"slug": row["slug"]},
                )
                updated = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._world(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_characters(
        self,
        world_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_characters, world_id)

    def _list_characters(self, world_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if world_id:
                rows = connection.execute(
                    """
                    SELECT * FROM characters WHERE world_id = ?
                    ORDER BY sort_order ASC, name COLLATE NOCASE
                    """,
                    (world_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM characters
                    ORDER BY world_id, sort_order ASC, name COLLATE NOCASE
                    """
                ).fetchall()
            return [self._character(row) for row in rows]

    async def save_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_character,
            dict(payload),
            actor_id,
        )

    def _save_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        character_id = str(payload.get("id") or "").strip()
        world_id = validate_platform_id(
            payload.get("world_id"),
            label="世界 ID",
        )
        slug = validate_slug(payload.get("slug"))
        name = clean_text(payload.get("name"), max_chars=100)
        if not name:
            raise ValueError("角色名称不能为空")
        role = clean_text(payload.get("role") or "npc", max_chars=40)
        prompt = clean_text(payload.get("prompt"), max_chars=20000)
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("角色资料必须是 JSON 对象")
        try:
            sort_order = max(-10000, min(10000, int(payload.get("sort_order", 0))))
        except (TypeError, ValueError):
            sort_order = 0
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM worlds WHERE id = ?",
                    (world_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("世界包不存在")
                if character_id:
                    current = connection.execute(
                        "SELECT * FROM characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if not current:
                        raise DatabaseNotFoundError("角色不存在")
                    expected_revision = payload.get("revision")
                    if (
                        expected_revision is not None
                        and int(expected_revision) != current["revision"]
                    ):
                        raise DatabaseConflictError(
                            "角色已被其他操作更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE characters SET
                            world_id = ?, slug = ?, name = ?, role = ?,
                            profile_json = ?, prompt = ?, enabled = ?,
                            sort_order = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            character_id,
                        ),
                    )
                    action = "character.update"
                else:
                    character_id = new_id("char")
                    connection.execute(
                        """
                        INSERT INTO characters(
                            id, world_id, slug, name, role, profile_json,
                            prompt, enabled, sort_order, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            character_id,
                            world_id,
                            slug,
                            name,
                            role,
                            json_dump(dict(profile)),
                            prompt,
                            enabled,
                            sort_order,
                            now,
                            now,
                        ),
                    )
                    action = "character.create"
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    action,
                    character_id,
                    {"world_id": world_id, "slug": slug, "name": name},
                )
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_character, character_id, actor_id)

    def _delete_character(
        self,
        character_id: str,
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM characters WHERE id = ?",
                    (character_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("角色不存在")
                connection.execute(
                    "DELETE FROM characters WHERE id = ?",
                    (character_id,),
                )
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "character.delete",
                    character_id,
                    {"name": row["name"], "world_id": row["world_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group,
            platform_id,
            group_id,
        )

    def _get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY
                    s.selected DESC,
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
            return self._session(row) if row else None

    async def get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_session_by_group_ref,
            platform_id,
            group_id,
            instance_ref,
        )

    def _get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> dict[str, Any] | None:
        reference = str(instance_ref or "").strip()
        if not reference:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug
                FROM sessions s JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                  AND (s.id = ? OR s.instance_slug = ?)
                LIMIT 1
                """,
                (platform_id, group_id, reference, reference.lower()),
            ).fetchone()
            return self._session(row) if row else None

    async def list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_group_sessions,
            platform_id,
            group_id,
        )

    def _list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    w.description AS world_description,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                WHERE s.platform_id = ? AND s.group_id = ?
                ORDER BY s.selected DESC, s.updated_at DESC
                """,
                (platform_id, group_id),
            ).fetchall()
            return [self._session(row) for row in rows]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_session, session_id)

    def _get_session(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                       COALESCE(gr.remark, '') AS group_remark,
                       COALESCE(gr.revision, 1) AS group_revision,
                       COALESCE(ss.relative_path, '')
                           AS storage_relative_path,
                       COALESCE(ss.sync_status, 'pending')
                           AS storage_sync_status,
                       COALESCE(ss.last_error, '') AS storage_last_error,
                       COALESCE(ss.playthrough_no, 1) AS playthrough_no
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("会话不存在")
            return self._session(row)

    async def list_sessions(self) -> list[dict[str, Any]]:
        return await self._run(self._list_sessions)

    def _list_sessions(
        self,
        session_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        ids = (
            [str(item) for item in session_ids if str(item)]
            if session_ids is not None
            else None
        )
        if ids == []:
            return []
        where_clause = ""
        parameters: tuple[Any, ...] = ()
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            where_clause = f"WHERE s.id IN ({placeholders})"
            parameters = tuple(ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.*, w.name AS world_name, w.slug AS world_slug,
                    COALESCE(gr.remark, '') AS group_remark,
                    COALESCE(gr.revision, 1) AS group_revision,
                    COALESCE(ss.relative_path, '')
                        AS storage_relative_path,
                    COALESCE(ss.sync_status, 'pending')
                        AS storage_sync_status,
                    COALESCE(ss.last_error, '') AS storage_last_error,
                    COALESCE(ss.playthrough_no, 1) AS playthrough_no,
                    srs.progress_json, srs.recovery_json,
                    sa.termination_type, sa.reason AS archive_reason,
                    sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                    COALESCE(sa.readonly, 0) AS readonly,
                    (
                        SELECT CASE
                            WHEN EXISTS (
                                SELECT 1 FROM participants pt0
                                WHERE pt0.session_id = s.id
                            )
                            THEN (
                                SELECT COUNT(*) FROM participants pt
                                WHERE pt.session_id = s.id
                                  AND pt.participation_status IN (
                                      'reserved', 'active', 'standby', 'away'
                                  )
                            )
                            ELSE (
                                SELECT COUNT(*) FROM players p
                                WHERE p.session_id = s.id
                            )
                        END
                    ) AS player_count,
                    (
                        SELECT COUNT(*) FROM participants ready_pt
                        WHERE ready_pt.session_id = s.id
                          AND ready_pt.ready = 1
                          AND ready_pt.participation_status = 'active'
                    ) AS ready_count,
                    (
                        SELECT COUNT(*) FROM memories m
                        WHERE m.session_id = s.id
                    ) AS memory_count,
                    (
                        SELECT COUNT(*) FROM snapshots sn
                        WHERE sn.session_id = s.id
                    ) AS snapshot_count,
                    (
                        SELECT COUNT(*) FROM session_characters sc
                        WHERE sc.session_id = s.id
                          AND sc.lifecycle_status = 'active'
                    ) AS npc_count,
                    (
                        SELECT COUNT(*) FROM timer_instances ti
                        WHERE ti.session_id = s.id
                          AND ti.status IN ('active', 'paused')
                    ) AS active_timer_count,
                    CASE
                        WHEN EXISTS (
                            SELECT 1 FROM group_votes gv
                            WHERE gv.session_id = s.id AND gv.status = 'open'
                        ) THEN 'vote'
                        WHEN EXISTS (
                            SELECT 1 FROM choice_sets cs
                            WHERE cs.session_id = s.id AND cs.status = 'active'
                        ) THEN 'choice'
                        WHEN s.state = 'preparing' THEN 'preparation'
                        WHEN s.state = 'paused' THEN 'admin'
                        ELSE ''
                    END AS waiting_for,
                    (
                        SELECT deadline_at FROM timer_instances due
                        WHERE due.session_id = s.id
                          AND due.status = 'active'
                        ORDER BY CASE due.timer_type
                            WHEN 'vote' THEN 0
                            WHEN 'turn' THEN 1
                            ELSE 2
                        END, due.created_at DESC
                        LIMIT 1
                    ) AS active_deadline_at
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                LEFT JOIN story_storage ss ON ss.session_id = s.id
                LEFT JOIN session_rule_states srs ON srs.session_id = s.id
                LEFT JOIN session_archives sa ON sa.session_id = s.id
                {where_clause}
                ORDER BY
                    CASE s.state
                        WHEN 'running' THEN 0
                        WHEN 'preparing' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'maintenance' THEN 3
                        WHEN 'finished' THEN 5
                        ELSE 4
                    END,
                    s.updated_at DESC
                """,
                parameters,
            ).fetchall()
            return [self._session(row) for row in rows]

    async def search_sessions(
        self,
        query: str = "",
        scope: str = "all",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self._run(
            self._search_sessions,
            query,
            scope,
            page,
            page_size,
        )

    def _search_sessions(
        self,
        query: str,
        scope: str,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        normalized_scope = str(scope or "all").strip().lower()
        if normalized_scope not in {"all", "group", "story"}:
            raise ValueError("检索范围必须为 all、group 或 story")
        normalized_query = clean_text(query, max_chars=200).casefold()
        normalized_page = max(1, int(page or 1))
        normalized_page_size = min(100, max(5, int(page_size or 20)))
        group_match = """
            (
                instr(lower(COALESCE(gr.remark, '')), ?) > 0
                OR instr(lower(s.group_id), ?) > 0
                OR instr(lower(s.platform_id), ?) > 0
            )
        """
        story_match = """
            (
                instr(lower(s.instance_name), ?) > 0
                OR instr(lower(s.instance_slug), ?) > 0
                OR instr(lower(s.id), ?) > 0
                OR instr(lower(w.name), ?) > 0
                OR instr(lower(w.slug), ?) > 0
            )
        """
        where = ""
        parameters: list[Any] = []
        if normalized_query:
            if normalized_scope == "group":
                where = f"WHERE {group_match}"
                parameters.extend([normalized_query] * 3)
            elif normalized_scope == "story":
                where = f"WHERE {story_match}"
                parameters.extend([normalized_query] * 5)
            else:
                where = f"WHERE ({group_match} OR {story_match})"
                parameters.extend([normalized_query] * 8)
        with self._connect() as connection:
            base = f"""
                FROM sessions s
                JOIN worlds w ON w.id = s.world_id
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                {where}
            """
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) {base}",
                    tuple(parameters),
                ).fetchone()[0]
            )
            pages = max(
                1,
                (total + normalized_page_size - 1)
                // normalized_page_size,
            )
            effective_page = min(normalized_page, pages)
            offset = (effective_page - 1) * normalized_page_size
            ids = [
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT s.id
                    {base}
                    ORDER BY
                        CASE s.state
                            WHEN 'running' THEN 0
                            WHEN 'preparing' THEN 1
                            WHEN 'paused' THEN 2
                            WHEN 'maintenance' THEN 3
                            WHEN 'finished' THEN 5
                            ELSE 4
                        END,
                        s.updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        *parameters,
                        normalized_page_size,
                        offset,
                    ),
                ).fetchall()
            ]
            group_rows = connection.execute(
                """
                SELECT s.platform_id, s.group_id,
                       COALESCE(gr.remark, '') AS remark,
                       COALESCE(gr.revision, 1) AS revision,
                       COUNT(*) AS story_count,
                       SUM(CASE WHEN s.state = 'running' THEN 1 ELSE 0 END)
                           AS running_count
                FROM sessions s
                LEFT JOIN group_registry gr
                  ON gr.platform_id = s.platform_id
                 AND gr.group_id = s.group_id
                GROUP BY s.platform_id, s.group_id
                ORDER BY COALESCE(NULLIF(gr.remark, ''), s.group_id)
                """
            ).fetchall()
        items = self._list_sessions(ids)
        page_keys = {
            (item["platform_id"], item["group_id"]) for item in items
        }
        groups = [
            dict(row)
            for row in group_rows
            if (str(row["platform_id"]), str(row["group_id"])) in page_keys
        ]
        return {
            "items": items,
            "groups": groups,
            "query": normalized_query,
            "scope": normalized_scope,
            "page": effective_page,
            "page_size": normalized_page_size,
            "total": total,
            "pages": pages,
        }

    async def list_session_options(self) -> list[dict[str, Any]]:
        return await self._run(self._list_session_options)

    def _list_session_options(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                {
                    "id": str(row["id"]),
                    "platform_id": str(row["platform_id"]),
                    "group_id": str(row["group_id"]),
                    "group_remark": str(row["group_remark"] or ""),
                    "instance_name": str(row["instance_name"]),
                    "instance_slug": str(row["instance_slug"]),
                    "world_name": str(row["world_name"]),
                    "state": str(row["state"]),
                }
                for row in connection.execute(
                    """
                    SELECT s.id, s.platform_id, s.group_id,
                           s.instance_name, s.instance_slug, s.state,
                           w.name AS world_name,
                           COALESCE(gr.remark, '') AS group_remark
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN group_registry gr
                      ON gr.platform_id = s.platform_id
                     AND gr.group_id = s.group_id
                    ORDER BY
                        COALESCE(NULLIF(gr.remark, ''), s.group_id),
                        s.updated_at DESC
                    """
                ).fetchall()
            ]

    async def save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        result = await self._run(
            self._save_group_remark,
            platform_id,
            group_id,
            remark,
            actor_id,
            expected_revision,
        )
        await asyncio.to_thread(
            self.storage.sync_group,
            result["platform_id"],
            result["group_id"],
        )
        return result

    def _save_group_remark(
        self,
        platform_id: str,
        group_id: str,
        remark: str,
        actor_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台 ID",
        )
        normalized_group = validate_platform_id(group_id, label="群 ID")
        normalized_remark = clean_text(remark, max_chars=120)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    LIMIT 1
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not exists:
                    raise DatabaseNotFoundError("群会话不存在")
                row = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                if not row:
                    registry_id = (
                        "group_"
                        + hashlib.sha256(
                            (
                                f"{normalized_platform}\0"
                                f"{normalized_group}"
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                    )
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO group_registry(
                            id, platform_id, group_id, remark, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            registry_id,
                            normalized_platform,
                            normalized_group,
                            normalized_remark,
                            now,
                            now,
                        ),
                    )
                else:
                    if (
                        int(expected_revision or 0) > 0
                        and int(row["revision"]) != int(expected_revision)
                    ):
                        raise DatabaseConflictError(
                            "群备注已被其他管理员更新，请刷新后重试"
                        )
                    connection.execute(
                        """
                        UPDATE group_registry SET
                            remark = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (normalized_remark, utc_now(), row["id"]),
                    )
                updated = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (normalized_platform, normalized_group),
                ).fetchone()
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "group.remark",
                    f"{normalized_platform}:{normalized_group}",
                    {
                        "remark": normalized_remark,
                        "revision": int(updated["revision"]),
                    },
                )
                connection.execute("COMMIT")
                return dict(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_storage_info(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.storage_info,
            session_id,
        )

    async def verify_storage(self, session_id: str) -> dict[str, Any]:
        await self.get_session(session_id)
        return await asyncio.to_thread(
            self.storage.verify_instance,
            session_id,
        )

    async def ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str = "",
        instance_name: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_session,
            platform_id,
            group_id,
            unified_origin,
            world_ref,
            actor_id,
            instance_slug,
            instance_name,
        )

    async def clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        *,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._clone_session,
            source_session_id,
            actor_id,
            instance_slug,
            instance_name,
            snapshot_ref,
        )

    def _clone_session(
        self,
        source_session_id: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
        snapshot_ref: str,
    ) -> dict[str, Any]:
        slug = validate_slug(instance_slug)
        name = clean_text(instance_name, max_chars=100)
        if not name:
            raise ValueError("新副本名称不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                source = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (source_session_id,),
                ).fetchone()
                if not source:
                    raise DatabaseNotFoundError("源副本不存在")
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND instance_slug = ?
                    """,
                    (
                        source["platform_id"],
                        source["group_id"],
                        slug,
                    ),
                ).fetchone()
                if duplicate:
                    raise DatabaseConflictError("当前群已存在同标识副本")
                snapshot = None
                if snapshot_ref:
                    snapshot = connection.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE session_id = ? AND (id = ? OR name = ?)
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (
                            source_session_id,
                            snapshot_ref,
                            snapshot_ref,
                        ),
                    ).fetchone()
                    if not snapshot:
                        raise DatabaseNotFoundError("指定的源存档不存在")
                if not snapshot:
                    archive = connection.execute(
                        """
                        SELECT final_snapshot_id FROM session_archives
                        WHERE session_id = ?
                        """,
                        (source_session_id,),
                    ).fetchone()
                    if archive:
                        snapshot = connection.execute(
                            "SELECT * FROM snapshots WHERE id = ?",
                            (archive["final_snapshot_id"],),
                        ).fetchone()
                state_json = (
                    snapshot["world_state_json"]
                    if snapshot
                    else source["world_state_json"]
                )
                turn_no = (
                    int(snapshot["turn_no"])
                    if snapshot
                    else int(source["turn_no"])
                )
                now = utc_now()
                target_id = new_id("session")
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, 'closed', ?, 1, ?,
                              0, ?, ?)
                    """,
                    (
                        target_id,
                        source["platform_id"],
                        source["group_id"],
                        source["unified_origin"],
                        slug,
                        name,
                        source["world_id"],
                        turn_no,
                        state_json,
                        now,
                        now,
                    ),
                )
                config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if config:
                    phase = json_load(config["phase_meta_json"], {})
                    phase = dict(phase) if isinstance(phase, Mapping) else {}
                    phase["branched_from_session_id"] = source_session_id
                    phase["branched_from_snapshot_id"] = (
                        snapshot["id"] if snapshot else ""
                    )
                    connection.execute(
                        """
                        INSERT INTO instance_configs(
                            session_id, world_revision, world_snapshot_json,
                            time_rules_json, phase_meta_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_id,
                            config["world_revision"],
                            config["world_snapshot_json"],
                            config["time_rules_json"],
                            json_dump(phase),
                            now,
                            now,
                        ),
                    )
                rules = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (source_session_id,),
                ).fetchone()
                if rules:
                    recovery = {
                        "state": "idle",
                        "message": "",
                        "operation_id": "",
                        "updated_at": now,
                    }
                    connection.execute(
                        """
                        INSERT INTO session_rule_states(
                            session_id, progress_json,
                            content_boundaries_json, npc_policy_json,
                            context_budget_json, dice_rules_json,
                            recovery_json, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            target_id,
                            rules["progress_json"],
                            rules["content_boundaries_json"],
                            rules["npc_policy_json"],
                            rules["context_budget_json"],
                            rules["dice_rules_json"],
                            json_dump(recovery),
                            now,
                            now,
                        ),
                    )
                character_ids: dict[str, str] = {}
                for item in connection.execute(
                    """
                    SELECT sc.*, st.state_json
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall():
                    target_character_id = new_id("snpc")
                    character_ids[item["id"]] = target_character_id
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                  '', ?, ?, 1, ?, ?)
                        """,
                        (
                            target_character_id,
                            target_id,
                            item["stable_key"],
                            item["name"],
                            item["aliases_json"],
                            item["role_type"],
                            item["public_profile_json"],
                            item["known_facts_json"],
                            item["misconceptions_json"],
                            item["source"],
                            item["review_status"],
                            item["lifecycle_status"],
                            item["persistent"],
                            item["first_turn"],
                            item["last_turn"],
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            target_character_id,
                            item["state_json"] or "{}",
                            now,
                        ),
                    )
                for table, prefix, columns in (
                    (
                        "story_ledger",
                        "ledger",
                        (
                            "stable_key", "kind", "title", "description",
                            "status", "visibility",
                        ),
                    ),
                    (
                        "scene_clocks",
                        "clock",
                        (
                            "stable_key", "title", "segments",
                            "current_value", "visibility", "trigger_text",
                            "status",
                        ),
                    ),
                ):
                    for item in connection.execute(
                        f"SELECT * FROM {table} WHERE session_id = ?",
                        (source_session_id,),
                    ).fetchall():
                        target_row_id = new_id(prefix)
                        if table == "story_ledger":
                            connection.execute(
                                """
                                INSERT INTO story_ledger(
                                    id, session_id, stable_key, kind, title,
                                    description, status, visibility,
                                    source_event_id, completed_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                        else:
                            connection.execute(
                                """
                                INSERT INTO scene_clocks(
                                    id, session_id, stable_key, title,
                                    segments, current_value, visibility,
                                    trigger_text, status, triggered_event_id,
                                    revision, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                                          1, ?, ?)
                                """,
                                (
                                    target_row_id,
                                    target_id,
                                    *(item[column] for column in columns),
                                    now,
                                    now,
                                ),
                            )
                memory_ids: dict[str, str] = {}
                memory_rows = connection.execute(
                    """
                    SELECT m.*, mg.visibility, mg.locked, mg.pinned,
                           mg.invalidated, mg.supersedes_id,
                           mg.conflict_status, mg.note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.session_id = ?
                    """,
                    (source_session_id,),
                ).fetchall()
                for item in memory_rows:
                    target_memory_id = new_id("memory")
                    memory_ids[item["id"]] = target_memory_id
                    fingerprint = memory_fingerprint(
                        target_id,
                        item["scope"],
                        item["scope_id"],
                        item["kind"],
                        item["content"],
                    )
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            target_id,
                            item["scope"],
                            item["scope_id"],
                            item["kind"],
                            item["content"],
                            item["importance"],
                            item["salience"],
                            item["tags_json"],
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                for item in memory_rows:
                    target_memory_id = memory_ids[item["id"]]
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            target_memory_id,
                            item["visibility"] or "public",
                            int(item["locked"] or 0),
                            int(item["pinned"] or 0),
                            int(item["invalidated"] or 0),
                            memory_ids.get(item["supersedes_id"], ""),
                            item["conflict_status"] or "clear",
                            item["note"] or "",
                            actor_id,
                            now,
                        ),
                    )
                event_id = new_id("event")
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        event_id,
                        target_id,
                        turn_no,
                        actor_id,
                        f"已从副本「{source['instance_name']}」克隆分支。",
                        json_dump(
                            {
                                "kind": "session_branch",
                                "source_session_id": source_session_id,
                                "source_snapshot_id": (
                                    snapshot["id"] if snapshot else ""
                                ),
                            }
                        ),
                        now,
                    ),
                )
                target_row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (target_id,),
                ).fetchone()
                self._insert_snapshot(
                    connection,
                    target_row,
                    "branch-origin",
                    "manual",
                    actor_id,
                    replace=False,
                )
                self._insert_audit(
                    connection,
                    target_id,
                    actor_id,
                    "session.clone",
                    source_session_id,
                    {
                        "source_snapshot_id": (
                            snapshot["id"] if snapshot else ""
                        ),
                        "instance_slug": slug,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (target_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str,
        instance_name: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(platform_id, label="平台 ID")
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                world = connection.execute(
                    """
                    SELECT * FROM worlds
                    WHERE (id = ? OR slug = ?) AND archived = 0
                    """,
                    (world_ref, world_ref),
                ).fetchone()
                if not world:
                    raise DatabaseNotFoundError(
                        "指定世界包不存在或已归档"
                    )
                normalized_instance_slug = validate_slug(
                    instance_slug or world["slug"]
                )
                normalized_instance_name = clean_text(
                    instance_name or world["name"],
                    max_chars=100,
                )
                if not normalized_instance_name:
                    raise ValueError("副本名称不能为空")

                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.platform_id = ? AND s.group_id = ?
                      AND s.instance_slug = ?
                    """,
                    (
                        platform_id,
                        group_id,
                        normalized_instance_slug,
                    ),
                ).fetchone()
                if row:
                    if row["world_id"] != world["id"]:
                        raise DatabaseConflictError(
                            "该群中的副本标识已被其他世界使用"
                        )
                    if row["state"] == SESSION_FINISHED:
                        base = normalized_instance_slug[:45].rstrip("-_")
                        stamp = datetime.now().astimezone().strftime(
                            "%Y%m%d%H%M%S"
                        )
                        candidate = f"{base}-run-{stamp}"
                        serial = 2
                        while connection.execute(
                            """
                            SELECT 1 FROM sessions
                            WHERE platform_id = ? AND group_id = ?
                              AND instance_slug = ?
                            """,
                            (platform_id, group_id, candidate),
                        ).fetchone():
                            suffix = f"-{serial:02d}"
                            candidate = (
                                f"{base[:64 - len(suffix) - 19]}"
                                f"-run-{stamp}{suffix}"
                            )
                            serial += 1
                        normalized_instance_slug = validate_slug(candidate)
                        row = None
                if row:
                    if unified_origin and row["unified_origin"] != unified_origin:
                        connection.execute(
                            """
                            UPDATE sessions
                            SET unified_origin = ?, updated_at = ?
                            WHERE id = ?
                            """,
                            (unified_origin, utc_now(), row["id"]),
                        )
                        row = connection.execute(
                            """
                            SELECT s.*, w.name AS world_name,
                                   w.slug AS world_slug
                            FROM sessions s
                            JOIN worlds w ON w.id = s.world_id
                            WHERE s.id = ?
                            """,
                            (row["id"],),
                        ).fetchone()
                    connection.execute("COMMIT")
                    return self._session(row)

                has_selected = connection.execute(
                    """
                    SELECT 1 FROM sessions
                    WHERE platform_id = ? AND group_id = ? AND selected = 1
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                session_id = new_id("session")
                now = utc_now()
                initial_state = public_world_state(
                    json_load(world["initial_state_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, platform_id, group_id, unified_origin,
                        instance_slug, instance_name, selected, world_id,
                        state, turn_no, revision, world_state_json,
                        history_floor_seq, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'closed', 0, 1, ?, 0, ?, ?
                    )
                    """,
                    (
                        session_id,
                        platform_id,
                        group_id,
                        unified_origin,
                        normalized_instance_slug,
                        normalized_instance_name,
                        0 if has_selected else 1,
                        world["id"],
                        json_dump(initial_state),
                        now,
                        now,
                    ),
                )
                world_payload = {
                    "id": world["id"],
                    "slug": world["slug"],
                    "name": world["name"],
                    "description": world["description"],
                    "system_prompt": world["system_prompt"],
                    "rules": json_load(world["rules_json"], {}),
                    "opening_scene": world["opening_scene"],
                    "initial_state": json_load(
                        world["initial_state_json"],
                        {},
                    ),
                    "revision": world["revision"],
                }
                connection.execute(
                    """
                    INSERT INTO instance_configs(
                        session_id, world_revision, world_snapshot_json,
                        time_rules_json, phase_meta_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        world["revision"],
                        json_dump(world_payload),
                        json_dump(world_time_rules(world_payload)),
                        json_dump({"resume_mode": False}),
                        now,
                        now,
                    ),
                )
                self._initialize_v05_rows(connection)
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.create",
                    session_id,
                    {
                        "platform_id": platform_id,
                        "group_id": group_id,
                        "world_id": world["id"],
                        "instance_slug": normalized_instance_slug,
                        "instance_name": normalized_instance_name,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._transition_session,
            session_id,
            target_state,
            actor_id,
            world_ref,
        )

    def _transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str,
    ) -> dict[str, Any]:
        if target_state not in SESSION_STATES:
            raise ValueError("非法会话状态")
        if target_state == SESSION_FINISHED:
            raise InvalidTransitionError(
                "完结必须使用原子归档流程，不能直接切换状态"
            )
        allowed = {
            SESSION_CLOSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_MAINTENANCE,
            },
            SESSION_PREPARING: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
            SESSION_RUNNING: {
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_PAUSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_FINISHED: set(),
            SESSION_MAINTENANCE: {
                SESSION_PREPARING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                archived = connection.execute(
                    "SELECT 1 FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if archived or current["state"] == SESSION_FINISHED:
                    raise InvalidTransitionError(
                        "该副本已经永久归档；如需继续，请从最终存档克隆新副本"
                    )
                if target_state not in allowed[current["state"]]:
                    raise InvalidTransitionError(
                        f"不能从 {current['state']} 切换为 {target_state}"
                    )

                world_id = current["world_id"]
                world_state_json = current["world_state_json"]
                turn_no = current["turn_no"]
                history_floor_seq = current["history_floor_seq"]
                switched_world = False
                if world_ref:
                    world = connection.execute(
                        """
                        SELECT * FROM worlds
                        WHERE (id = ? OR slug = ?) AND archived = 0
                        """,
                        (world_ref, world_ref),
                    ).fetchone()
                    if not world:
                        raise DatabaseNotFoundError("世界包不存在或已归档")
                    if world["id"] != world_id:
                        if current["state"] != SESSION_CLOSED:
                            raise InvalidTransitionError(
                                "只有关闭状态才能更换世界包"
                            )
                        world_id = world["id"]
                        world_state_json = json_dump(
                            public_world_state(
                                json_load(world["initial_state_json"], {})
                            )
                        )
                        turn_no = 0
                        max_seq = connection.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0)
                            FROM events WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()[0]
                        history_floor_seq = max_seq + 1
                        switched_world = True
                        world_payload = {
                            "id": world["id"],
                            "slug": world["slug"],
                            "name": world["name"],
                            "description": world["description"],
                            "system_prompt": world["system_prompt"],
                            "rules": json_load(world["rules_json"], {}),
                            "opening_scene": world["opening_scene"],
                            "initial_state": json_load(
                                world["initial_state_json"],
                                {},
                            ),
                            "revision": world["revision"],
                        }
                        connection.execute(
                            """
                            UPDATE instance_configs SET
                                world_revision = ?,
                                world_snapshot_json = ?,
                                time_rules_json = ?,
                                phase_meta_json = ?,
                                updated_at = ?
                            WHERE session_id = ?
                            """,
                            (
                                world["revision"],
                                json_dump(world_payload),
                                json_dump(world_time_rules(world_payload)),
                                json_dump({"resume_mode": False}),
                                utc_now(),
                                session_id,
                            ),
                        )

                now = utc_now()
                selected = int(current["selected"])
                auto_paused: list[str] = []
                if target_state in {
                    SESSION_PREPARING,
                    SESSION_RUNNING,
                }:
                    running_rows = connection.execute(
                        """
                        SELECT id FROM sessions
                        WHERE platform_id = ? AND group_id = ?
                          AND state = 'running' AND id <> ?
                        """,
                        (
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    ).fetchall()
                    auto_paused = [str(row["id"]) for row in running_rows]
                    connection.execute(
                        """
                        UPDATE sessions SET
                            state = CASE
                                WHEN state = 'running' THEN 'paused'
                                ELSE state
                            END,
                            selected = 0,
                            revision = CASE
                                WHEN state = 'running' THEN revision + 1
                                ELSE revision
                            END,
                            updated_at = CASE
                                WHEN state = 'running' THEN ?
                                ELSE updated_at
                            END
                        WHERE platform_id = ? AND group_id = ? AND id <> ?
                        """,
                        (
                            now,
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    )
                    selected = 1
                if target_state == SESSION_PREPARING:
                    connection.execute(
                        """
                        UPDATE participants
                        SET ready = 0, updated_at = ?
                        WHERE session_id = ?
                          AND participation_status IN (
                              'reserved', 'active', 'standby', 'away'
                          )
                        """,
                        (now, session_id),
                    )
                    config_row = connection.execute(
                        """
                        SELECT phase_meta_json, time_rules_json
                        FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    phase_meta = json_load(
                        config_row["phase_meta_json"] if config_row else "",
                        {},
                    )
                    phase_meta["resume_mode"] = bool(turn_no)
                    phase_meta["entered_preparing_at"] = now
                    connection.execute(
                        """
                        UPDATE instance_configs
                        SET phase_meta_json = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (json_dump(phase_meta), now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'preparation'
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    time_rules = normalize_time_rules(
                        json_load(
                            config_row["time_rules_json"]
                            if config_row
                            else "",
                            {},
                        )
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id="",
                        timer_type="preparation",
                        timeout_seconds=time_rules[
                            "preparation_timeout_seconds"
                        ],
                        reminder_seconds=None,
                        action={"resume_mode": bool(turn_no)},
                    )
                if target_state in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE group_votes
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'open'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id = ?, state = ?, selected = ?, turn_no = ?,
                        world_state_json = ?, history_floor_seq = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        world_id,
                        target_state,
                        selected,
                        turn_no,
                        world_state_json,
                        history_floor_seq,
                        now,
                        session_id,
                    ),
                )
                detail = {
                    "from": current["state"],
                    "to": target_state,
                    "world_changed": switched_world,
                    "auto_paused_instances": auto_paused,
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.transition",
                    session_id,
                    detail,
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def finalize_session(
        self,
        session_id: str,
        actor_id: str,
        *,
        termination_type: str = "completed",
        reason: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._finalize_session,
            session_id,
            actor_id,
            termination_type,
            reason,
        )

    async def delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        result = await self._run(
            self._delete_session,
            session_id,
            actor_id,
            confirm_name,
        )
        try:
            trashed = await asyncio.to_thread(
                self.storage.trash_relative_path,
                str(result.get("relative_path") or ""),
                label=str(result.get("instance_slug") or "story"),
            )
            result["trash_path"] = str(trashed or "")
        except Exception as exc:
            result["trash_error"] = str(exc)[:500]
        return result

    def _delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT s.*, ss.relative_path
                    FROM sessions s
                    LEFT JOIN story_storage ss ON ss.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在")
                if row["state"] not in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    raise InvalidTransitionError(
                        "只能删除已关闭或已归档的副本"
                    )
                if str(confirm_name or "").strip() != str(
                    row["instance_name"]
                ):
                    raise ValueError("确认名称与副本名称不一致")
                detail = {
                    "instance_name": row["instance_name"],
                    "instance_slug": row["instance_slug"],
                    "relative_path": str(row["relative_path"] or ""),
                    "state": row["state"],
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.delete",
                    session_id,
                    detail,
                )
                connection.execute(
                    """
                    DELETE FROM token_quota_policies
                    WHERE scope_type = 'session' AND scope_id = ?
                    """,
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM sessions WHERE id = ?",
                    (session_id,),
                )
                replacement = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND state <> 'finished'
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (row["platform_id"], row["group_id"]),
                ).fetchone()
                if replacement:
                    connection.execute(
                        """
                        UPDATE sessions SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (
                            replacement["id"],
                            row["platform_id"],
                            row["group_id"],
                        ),
                    )
                connection.execute("COMMIT")
                return {
                    "deleted": True,
                    "session_id": session_id,
                    **detail,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _finalize_session(
        self,
        session_id: str,
        actor_id: str,
        termination_type: str,
        reason: str,
    ) -> dict[str, Any]:
        termination_type = str(termination_type or "").strip().lower()
        if termination_type not in {"completed", "aborted"}:
            raise ValueError("结束类型必须为 completed 或 aborted")
        reason = clean_text(reason, max_chars=1000)
        if termination_type == "aborted" and not reason:
            raise ValueError("强制终止必须填写原因")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                existing = connection.execute(
                    "SELECT * FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if existing or session["state"] == SESSION_FINISHED:
                    raise InvalidTransitionError("该副本已经永久归档")

                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    ) AND status = 'active'
                    """,
                    (now, session_id),
                )
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                final_snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    f"final-{termination_type}-{stamp}",
                    "final",
                    actor_id,
                    replace=False,
                )
                ending_text = (
                    "故事抵达了已经确认的结局，副本进入永久归档。"
                    if termination_type == "completed"
                    else f"副本由管理员强制终止并永久归档。原因：{reason}"
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id,
                        actor_name, content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        new_id("event"),
                        session_id,
                        session["turn_no"],
                        actor_id,
                        ending_text,
                        json_dump(
                            {
                                "kind": "session_finalized",
                                "termination_type": termination_type,
                                "reason": reason,
                                "final_snapshot_id": final_snapshot_id,
                            }
                        ),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE choice_sets SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE group_votes SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'open'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ? AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE delegation_grants SET status = 'revoked',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    "DELETE FROM permission_grants WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    """
                    UPDATE return_requests SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ?
                      AND status NOT IN ('completed', 'rejected', 'cancelled')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                )
                recovery_row = connection.execute(
                    """
                    SELECT recovery_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                recovery = json_load(
                    recovery_row["recovery_json"] if recovery_row else "",
                    {},
                )
                recovery = (
                    dict(recovery) if isinstance(recovery, Mapping) else {}
                )
                recovery.update(
                    {
                        "state": "archived",
                        "message": ending_text,
                        "operation_id": final_snapshot_id,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """
                    UPDATE session_rule_states
                    SET recovery_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(recovery), now, session_id),
                )
                connection.execute(
                    """
                    UPDATE sessions SET state = 'finished', selected = 0,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    INSERT INTO session_archives(
                        session_id, termination_type, reason,
                        final_snapshot_id, ended_by, ended_at, readonly
                    ) VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        session_id,
                        termination_type,
                        reason,
                        final_snapshot_id,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.finish"
                    if termination_type == "completed"
                    else "session.abort",
                    session_id,
                    {
                        "termination_type": termination_type,
                        "reason": reason,
                        "final_snapshot_id": final_snapshot_id,
                        "readonly": True,
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                           srs.progress_json, srs.recovery_json,
                           sa.termination_type,
                           sa.reason AS archive_reason,
                           sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                           sa.readonly
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN session_rule_states srs
                      ON srs.session_id = s.id
                    JOIN session_archives sa ON sa.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def save_manual_state(
        self,
        session_id: str,
        state: Mapping[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_manual_state,
            session_id,
            dict(state),
            expected_revision,
            actor_id,
        )

    def _save_manual_state(
        self,
        session_id: str,
        state: dict[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                if current["revision"] != expected_revision:
                    raise DatabaseConflictError("会话状态已改变，请刷新后重试")
                self._insert_snapshot(
                    connection,
                    current,
                    f"manual-before-edit-{current['revision']}",
                    "safety",
                    actor_id,
                    replace=True,
                )
                stored_state = json_load(current["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(
                    public_world_state(state),
                    turn_state,
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.state_edit",
                    session_id,
                    {"previous_revision": current["revision"]},
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._ensure_player,
            session_id,
            user_id,
            display_name,
        )

    def _ensure_player(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO players(
                    id, session_id, user_id, display_name, character_name,
                    profile_json, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                ON CONFLICT(session_id, user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    updated_at = excluded.updated_at
                """,
                (
                    new_id("player"),
                    session_id,
                    user_id,
                    display_name,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            return self._player(row)

    async def list_players(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_players, session_id)

    def _list_players(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM players
                WHERE session_id = ?
                ORDER BY enabled DESC, updated_at DESC
                """,
                (session_id,),
            ).fetchall()
            return [self._player(row) for row in rows]

    @staticmethod
    def _turn_status_for(
        connection: sqlite3.Connection,
        session_id: str,
        stored_world_state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if stored_world_state is None:
            session = connection.execute(
                "SELECT world_state_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            stored_world_state = json_load(session["world_state_json"], {})

        rows = connection.execute(
            """
            SELECT * FROM players
            WHERE session_id = ? AND enabled = 1
            """,
            (session_id,),
        ).fetchall()
        players = {str(row["user_id"]): row for row in rows}
        state = turn_state_from_world(
            stored_world_state,
            allowed_user_ids=players,
        )
        order = []
        for position, user_id in enumerate(state["order"], start=1):
            row = players[user_id]
            order.append(
                {
                    "position": position,
                    "player_id": row["id"],
                    "user_id": user_id,
                    "display_name": row["display_name"],
                    "character_name": row["character_name"],
                    "name": row["character_name"] or row["display_name"],
                }
            )
        current = next(
            (
                item
                for item in order
                if item["user_id"] == state["current_user_id"]
            ),
            None,
        )
        return {
            "round_no": state["round_no"],
            "current_user_id": state["current_user_id"],
            "current_name": current["name"] if current else "",
            "order": order,
        }

    async def get_turn_status(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_turn_status, session_id)

    def _get_turn_status(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._turn_status_for(connection, session_id)

    async def join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._join_turn_order,
            session_id,
            user_id,
            display_name,
            actor_id,
        )

    def _join_turn_order(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                connection.execute(
                    """
                    INSERT INTO players(
                        id, session_id, user_id, display_name, character_name,
                        profile_json, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                    ON CONFLICT(session_id, user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("player"),
                        session_id,
                        user_id,
                        display_name,
                        now,
                        now,
                    ),
                )
                player_row = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player_row["enabled"]:
                    raise InvalidTransitionError("你的玩家身份当前不可用")

                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state, joined = join_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), now, session_id),
                    )
                if joined:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.join",
                        user_id,
                        {"position": turn_state["order"].index(user_id) + 1},
                    )
                session_row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {
                    "joined": joined,
                    "player": self._player(player_row),
                    "session": self._session(session_row),
                    "turn": status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._leave_turn_order,
            session_id,
            user_id,
            actor_id,
        )

    def _leave_turn_order(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state, removed = leave_turn(turn_state, user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                if json_dump(updated_state) != json_dump(stored_state):
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(updated_state), utc_now(), session_id),
                    )
                if removed:
                    self._insert_audit(
                        connection,
                        session_id,
                        actor_id,
                        "turn_order.leave",
                        user_id,
                        {},
                    )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return {"removed": removed, "turn": status}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._skip_turn,
            session_id,
            requester_id,
            actor_id,
            force,
        )

    def _skip_turn(
        self,
        session_id: str,
        requester_id: str,
        actor_id: str,
        force: bool,
    ) -> dict[str, Any]:
        requester_id = validate_platform_id(requester_id, label="用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                current_user_id = turn_state["current_user_id"]
                if not current_user_id:
                    raise InvalidTransitionError("回合队列为空")
                if not force and requester_id != current_user_id:
                    current = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    raise InvalidTransitionError(
                        f"当前轮到 {current['current_name'] or current_user_id}"
                    )
                turn_state = advance_turn(turn_state, current_user_id)
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.force_skip" if force else "turn_order.skip",
                    current_user_id,
                    {"round_no": turn_state["round_no"]},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_turn_order(
        self,
        session_id: str,
        order: Sequence[str],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_turn_order,
            session_id,
            list(order),
            actor_id,
        )

    def _set_turn_order(
        self,
        session_id: str,
        order: list[str],
        actor_id: str,
    ) -> dict[str, Any]:
        if len(order) > 100:
            raise ValueError("回合队列最多 100 人")
        normalized_order = [
            validate_platform_id(item, label="用户 ID") for item in order
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                unknown = [
                    item for item in normalized_order if item not in enabled_ids
                ]
                if unknown:
                    raise ValueError("回合顺序包含不存在或已停用的玩家")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                turn_state = replace_turn_order(
                    turn_state,
                    normalized_order,
                )
                updated_state = embed_turn_state(stored_state, turn_state)
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(updated_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn_order.set",
                    session_id,
                    {"order": normalized_order},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    updated_state,
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def designate_turn(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._designate_turn,
            session_id,
            user_id,
            actor_id,
        )

    def _designate_turn(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                      AND participation_status = 'active'
                      AND card_status = 'approved'
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise ValueError("指定角色当前不在有效行动阵容中")
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                if user_id not in turn_state["order"]:
                    raise ValueError("指定角色当前不在回合队列中")
                turn_state["current_user_id"] = user_id
                now = utc_now()
                new_revision = int(session["revision"]) + 1
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(embed_turn_state(stored_state, turn_state)),
                        now,
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'turn'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                choice_id = new_id("choices")
                choices = fallback_choices(stored_state)
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status, reroll_count,
                        idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                    """,
                    (
                        choice_id,
                        session_id,
                        participant["id"],
                        turn_state["round_no"],
                        new_revision,
                        json_dump(choices),
                        f"designate:{session_id}:{new_revision}",
                        now,
                        now,
                    ),
                )
                config = connection.execute(
                    """
                    SELECT time_rules_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                rules = normalize_time_rules(
                    json_load(config["time_rules_json"] if config else "", {})
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant["id"],
                    timer_type="turn",
                    timeout_seconds=rules["turn_timeout_seconds"],
                    reminder_seconds=rules["turn_reminder_seconds"],
                    action={
                        "choice_set_id": choice_id,
                        "user_id": user_id,
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "turn.designate",
                    participant["id"],
                    {"user_id": user_id},
                )
                status = self._turn_status_for(
                    connection,
                    session_id,
                    embed_turn_state(stored_state, turn_state),
                )
                connection.execute("COMMIT")
                return status
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _remove_turn_member(
        connection: sqlite3.Connection,
        session_id: str,
        user_id: str,
        *,
        updated_at: str,
    ) -> bool:
        session = connection.execute(
            "SELECT world_state_json FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise DatabaseNotFoundError("会话不存在")
        stored_state = json_load(session["world_state_json"], {})
        turn_state, removed = leave_turn(
            turn_state_from_world(stored_state),
            user_id,
        )
        enabled_ids = {
            str(row["user_id"])
            for row in connection.execute(
                """
                SELECT user_id FROM players
                WHERE session_id = ? AND enabled = 1
                """,
                (session_id,),
            ).fetchall()
        }
        turn_state = normalize_turn_state(
            turn_state,
            allowed_user_ids=enabled_ids,
        )
        updated_state = embed_turn_state(stored_state, turn_state)
        if json_dump(updated_state) != json_dump(stored_state):
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (json_dump(updated_state), updated_at, session_id),
            )
        return removed

    async def save_player(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_player, dict(payload), actor_id)

    def _save_player(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        user_id = validate_platform_id(
            payload.get("user_id"),
            label="用户 ID",
        )
        display_name = clean_text(
            payload.get("display_name"),
            max_chars=100,
        )
        if not display_name:
            raise ValueError("显示名称不能为空")
        character_name = clean_text(
            payload.get("character_name"),
            max_chars=100,
        )
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("玩家资料必须是 JSON 对象")
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, character_name = ?,
                            profile_json = ?, enabled = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            existing["id"],
                        ),
                    )
                    player_id = existing["id"]
                    action = "player.update"
                else:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            now,
                        ),
                    )
                    action = "player.create"
                if not enabled:
                    self._remove_turn_member(
                        connection,
                        session_id,
                        user_id,
                        updated_at=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    player_id,
                    {"user_id": user_id, "display_name": display_name},
                )
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._player(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_player(
        self,
        player_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_player, player_id, actor_id)

    def _delete_player(self, player_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM players WHERE id = ?",
                    (player_id,),
                )
                self._remove_turn_member(
                    connection,
                    row["session_id"],
                    row["user_id"],
                    updated_at=utc_now(),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "player.delete",
                    player_id,
                    {"user_id": row["user_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        return await self._run(self._recent_events, session_id, limit)

    def _recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT s.history_floor_seq, sr.recovery_json
                FROM sessions s
                LEFT JOIN session_rule_states sr ON sr.session_id = s.id
                WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            recovery = json_load(session["recovery_json"], {})
            excluded_ranges: list[tuple[int, int]] = []
            if isinstance(recovery, Mapping):
                for item in recovery.get("excluded_event_ranges", []):
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    try:
                        start, end = int(item[0]), int(item[1])
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if start <= end:
                        excluded_ranges.append((start, end))
            exclusions = "".join(
                " AND NOT (seq BETWEEN ? AND ?)"
                for _ in excluded_ranges
            )
            parameters: list[Any] = [
                session_id,
                session["history_floor_seq"],
            ]
            for start, end in excluded_ranges:
                parameters.extend((start, end))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT * FROM events
                WHERE session_id = ? AND seq >= ?
                {exclusions}
                ORDER BY seq DESC LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            return [self._event(row) for row in reversed(rows)]

    async def append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._append_ooc,
            session_id,
            actor_id,
            actor_name,
            content,
        )

    def _append_ooc(
        self,
        session_id: str,
        actor_id: str,
        actor_name: str,
        content: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            event_id = new_id("event")
            now = utc_now()
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'ooc', ?, ?, ?, '{}', ?)
                """,
                (
                    event_id,
                    session_id,
                    session["turn_no"],
                    actor_id,
                    actor_name,
                    content,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()
            return self._event(row)

    async def list_memories(
        self,
        session_id: str,
        query: str = "",
        limit: int = 100,
        *,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_memories,
            session_id,
            query,
            limit,
            include_invalidated,
        )

    @staticmethod
    def _query_terms(query: str) -> set[str]:
        compact = "".join(str(query).lower().split())
        if not compact:
            return set()
        terms = {compact}
        terms.update(
            compact[index : index + 2]
            for index in range(max(0, len(compact) - 1))
        )
        return {term for term in terms if term}

    def _list_memories(
        self,
        session_id: str,
        query: str,
        limit: int,
        include_invalidated: bool,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       mg.visibility AS governance_visibility,
                       mg.locked AS governance_locked,
                       mg.pinned AS governance_pinned,
                       mg.invalidated AS governance_invalidated,
                       mg.supersedes_id AS governance_supersedes_id,
                       mg.conflict_status AS governance_conflict_status,
                       mg.note AS governance_note
                FROM memories m
                LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                WHERE m.session_id = ?
                  AND (? OR COALESCE(mg.invalidated, 0) = 0)
                ORDER BY COALESCE(mg.pinned, 0) DESC,
                         COALESCE(mg.locked, 0) DESC,
                         m.importance DESC, m.salience DESC, m.updated_at DESC
                LIMIT 500
                """,
                (session_id, int(include_invalidated)),
            ).fetchall()
            memories = [self._memory(row) for row in rows]
            terms = self._query_terms(query)
            if terms:
                for memory in memories:
                    haystack = "".join(
                        (
                            memory["content"]
                            + " "
                            + " ".join(memory["tags"])
                            + " "
                            + memory["kind"]
                        )
                        .lower()
                        .split()
                    )
                    matches = sum(term in haystack for term in terms)
                    memory["_score"] = (
                        matches * 5
                        + memory["importance"] * 2
                        + float(memory["salience"])
                    )
                memories = [
                    memory
                    for memory in memories
                    if (
                        memory["locked"]
                        or memory["pinned"]
                        or memory.get("_score", 0) > memory["importance"] * 2
                    )
                ]
                memories.sort(
                    key=lambda item: (
                        int(item["pinned"]),
                        int(item["locked"]),
                        item.get("_score", 0),
                    ),
                    reverse=True,
                )
            protected = [
                memory
                for memory in memories
                if memory["locked"] or memory["pinned"]
            ]
            selected = protected + [
                memory
                for memory in memories
                if memory not in protected
            ][: max(0, limit - len(protected))]
            archived = connection.execute(
                """
                SELECT readonly FROM session_archives
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if selected and not archived:
                now = utc_now()
                connection.executemany(
                    """
                    UPDATE memories
                    SET last_accessed_at = ?, salience = MIN(10, salience + 0.05)
                    WHERE id = ?
                    """,
                    [(now, item["id"]) for item in selected],
                )
            for item in selected:
                item.pop("_score", None)
            return selected

    async def save_memory(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._save_memory, dict(payload), actor_id)

    def _save_memory(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        memory_id = str(payload.get("id") or "").strip()
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        scope = str(payload.get("scope", "world")).lower()
        if scope not in {"world", "player", "npc"}:
            raise ValueError("非法记忆范围")
        scope_id = clean_text(payload.get("scope_id"), max_chars=128)
        kind = clean_text(payload.get("kind") or "fact", max_chars=32)
        content = clean_text(payload.get("content"), max_chars=1000)
        if not content:
            raise ValueError("记忆内容不能为空")
        try:
            importance = max(1, min(5, int(payload.get("importance", 3))))
        except (TypeError, ValueError):
            importance = 3
        tags_value = payload.get("tags")
        tags = []
        if isinstance(tags_value, list):
            tags = [
                clean_text(item, max_chars=32)
                for item in tags_value[:12]
                if clean_text(item, max_chars=32)
            ]
        fingerprint = memory_fingerprint(
            session_id,
            scope,
            scope_id,
            kind,
            content,
        )
        visibility = str(payload.get("visibility") or "public").lower()
        if visibility not in {"public", "host", "private"}:
            visibility = "public"
        conflict_status = str(
            payload.get("conflict_status") or "clear"
        ).lower()
        if conflict_status not in {"clear", "conflict", "resolved"}:
            conflict_status = "clear"
        supersedes_id = clean_text(
            payload.get("supersedes_id"),
            max_chars=128,
        )
        governance_note = clean_text(
            payload.get("governance_note"),
            max_chars=500,
        )
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current_governance = None
                if memory_id:
                    row = connection.execute(
                        "SELECT * FROM memories WHERE id = ?",
                        (memory_id,),
                    ).fetchone()
                    if not row:
                        raise DatabaseNotFoundError("记忆不存在")
                    if row["session_id"] != session_id:
                        raise ValueError("不能把记忆移动到其他副本")
                    current_governance = connection.execute(
                        """
                        SELECT * FROM memory_governance
                        WHERE memory_id = ?
                        """,
                        (memory_id,),
                    ).fetchone()
                    if current_governance:
                        if "visibility" not in payload:
                            visibility = current_governance["visibility"]
                        if "conflict_status" not in payload:
                            conflict_status = current_governance[
                                "conflict_status"
                            ]
                        if "supersedes_id" not in payload:
                            supersedes_id = current_governance[
                                "supersedes_id"
                            ]
                        if "governance_note" not in payload:
                            governance_note = current_governance["note"]
                    connection.execute(
                        """
                        UPDATE memories SET
                            scope = ?, scope_id = ?, kind = ?, content = ?,
                            importance = ?, tags_json = ?, fingerprint = ?,
                            updated_at = ?, last_accessed_at = ?
                        WHERE id = ?
                        """,
                        (
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            memory_id,
                        ),
                    )
                    action = "memory.update"
                else:
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '', ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            tags_json = excluded.tags_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            now,
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT * FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    memory_id = row["id"]
                    action = "memory.create"
                if supersedes_id:
                    replaced = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE id = ? AND session_id = ?
                        """,
                        (supersedes_id, session_id),
                    ).fetchone()
                    if not replaced or supersedes_id == memory_id:
                        raise ValueError("被替代记忆不存在或不能替代自身")
                    connection.execute(
                        """
                        INSERT INTO memory_governance(
                            memory_id, visibility, locked, pinned,
                            invalidated, supersedes_id, conflict_status,
                            note, updated_by, updated_at
                        ) VALUES (?, 'public', 0, 0, 1, '', 'resolved',
                                  '已被新事实替代', ?, ?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            invalidated = 1,
                            conflict_status = 'resolved',
                            note = CASE
                                WHEN note = '' THEN '已被新事实替代'
                                ELSE note
                            END,
                            updated_by = excluded.updated_by,
                            updated_at = excluded.updated_at
                        """,
                        (supersedes_id, actor_id, now),
                    )
                connection.execute(
                    """
                    INSERT INTO memory_governance(
                        memory_id, visibility, locked, pinned,
                        invalidated, supersedes_id, conflict_status,
                        note, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        visibility = excluded.visibility,
                        locked = excluded.locked,
                        pinned = excluded.pinned,
                        invalidated = excluded.invalidated,
                        supersedes_id = excluded.supersedes_id,
                        conflict_status = excluded.conflict_status,
                        note = excluded.note,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        memory_id,
                        visibility,
                        int(
                            bool(
                                payload.get(
                                    "locked",
                                    current_governance["locked"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "pinned",
                                    current_governance["pinned"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        int(
                            bool(
                                payload.get(
                                    "invalidated",
                                    current_governance["invalidated"]
                                    if memory_id and current_governance
                                    else False,
                                )
                            )
                        ),
                        supersedes_id,
                        conflict_status,
                        governance_note,
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    memory_id,
                    {"scope": scope, "kind": kind},
                )
                row = connection.execute(
                    """
                    SELECT m.*,
                           mg.visibility AS governance_visibility,
                           mg.locked AS governance_locked,
                           mg.pinned AS governance_pinned,
                           mg.invalidated AS governance_invalidated,
                           mg.supersedes_id AS governance_supersedes_id,
                           mg.conflict_status AS governance_conflict_status,
                           mg.note AS governance_note
                    FROM memories m
                    LEFT JOIN memory_governance mg ON mg.memory_id = m.id
                    WHERE m.id = ?
                    """,
                    (memory_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._memory(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_memory(
        self,
        memory_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_memory, memory_id, actor_id)

    def _delete_memory(self, memory_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?",
                    (memory_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("记忆不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM memories WHERE id = ?",
                    (memory_id,),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "memory.delete",
                    memory_id,
                    {"kind": row["kind"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        name: str,
        kind: str,
        created_by: str,
        *,
        replace: bool,
    ) -> str:
        name = clean_text(name, max_chars=100)
        if not name:
            raise ValueError("存档名不能为空")
        snapshot_id = new_id("save")
        if replace:
            connection.execute(
                """
                DELETE FROM snapshots
                WHERE session_id = ? AND name = ?
                """,
                (session["id"], name),
            )
        elif connection.execute(
            """
            SELECT 1 FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone():
            raise ValueError(
                "已存在同名存档；请确认后使用覆盖模式"
            )
        sql = """
            INSERT INTO snapshots(
                id, session_id, name, kind, turn_no, session_revision,
                world_id, world_state_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        connection.execute(
            sql,
            (
                snapshot_id,
                session["id"],
                name,
                kind,
                session["turn_no"],
                session["revision"],
                session["world_id"],
                session["world_state_json"],
                created_by,
                utc_now(),
            ),
        )
        row = connection.execute(
            """
            SELECT id FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone()
        snapshot_id = str(row["id"])
        workflow = self._collect_workflow_snapshot(
            connection,
            session["id"],
        )
        connection.execute(
            """
            INSERT INTO snapshot_workflows(snapshot_id, workflow_json)
            VALUES (?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                workflow_json = excluded.workflow_json
            """,
            (snapshot_id, json_dump(workflow)),
        )
        return snapshot_id

    @staticmethod
    def _collect_workflow_snapshot(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        session = connection.execute(
            """
            SELECT history_floor_seq FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        event_anchor_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) FROM events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        tables = {
            "participants": (
                "SELECT * FROM participants WHERE session_id = ?"
            ),
            "character_runtime_states": (
                "SELECT * FROM character_runtime_states WHERE session_id = ?"
            ),
            "choice_sets": (
                "SELECT * FROM choice_sets WHERE session_id = ?"
            ),
            "group_votes": (
                "SELECT * FROM group_votes WHERE session_id = ?"
            ),
            "selected_world_events": (
                "SELECT * FROM selected_world_events WHERE session_id = ?"
            ),
            "timer_instances": (
                "SELECT * FROM timer_instances WHERE session_id = ?"
            ),
            "delegation_grants": (
                "SELECT * FROM delegation_grants WHERE session_id = ?"
            ),
            "permission_grants": (
                "SELECT * FROM permission_grants WHERE session_id = ?"
            ),
            "return_requests": (
                "SELECT * FROM return_requests WHERE session_id = ?"
            ),
            "ban_records": (
                """
                SELECT * FROM ban_records
                WHERE session_id = ? AND scope = 'instance'
                """
            ),
            "session_rule_states": (
                "SELECT * FROM session_rule_states WHERE session_id = ?"
            ),
            "session_characters": (
                "SELECT * FROM session_characters WHERE session_id = ?"
            ),
            "story_ledger": (
                "SELECT * FROM story_ledger WHERE session_id = ?"
            ),
            "scene_clocks": (
                "SELECT * FROM scene_clocks WHERE session_id = ?"
            ),
            "assist_tokens": (
                "SELECT * FROM assist_tokens WHERE session_id = ?"
            ),
        }
        result: dict[str, Any] = {
            "format": "astrbot-tavern-workflow",
            "version": 3,
            "history_floor_seq": int(
                session["history_floor_seq"] if session else 0
            ),
            "event_anchor_seq": event_anchor_seq,
        }
        vote_ids: list[str] = []
        participant_ids: list[str] = []
        session_character_ids: list[str] = []
        for table, query in tables.items():
            rows = connection.execute(query, (session_id,)).fetchall()
            result[table] = [dict(row) for row in rows]
            if table == "group_votes":
                vote_ids = [str(row["id"]) for row in rows]
            if table == "participants":
                participant_ids = [str(row["id"]) for row in rows]
            if table == "session_characters":
                session_character_ids = [str(row["id"]) for row in rows]
        if vote_ids:
            placeholders = ",".join("?" for _ in vote_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM vote_ballots
                WHERE vote_id IN ({placeholders})
                """,
                tuple(vote_ids),
            ).fetchall()
            result["vote_ballots"] = [dict(row) for row in rows]
        else:
            result["vote_ballots"] = []
        if participant_ids:
            placeholders = ",".join("?" for _ in participant_ids)
            for table in (
                "character_card_drafts",
                "card_binding_codes",
            ):
                rows = connection.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE participant_id IN ({placeholders})
                    """,
                    tuple(participant_ids),
                ).fetchall()
                result[table] = [dict(row) for row in rows]
        else:
            result["character_card_drafts"] = []
            result["card_binding_codes"] = []
        if session_character_ids:
            placeholders = ",".join("?" for _ in session_character_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM session_character_states
                WHERE character_id IN ({placeholders})
                """,
                tuple(session_character_ids),
            ).fetchall()
            result["session_character_states"] = [
                dict(row) for row in rows
            ]
        else:
            result["session_character_states"] = []
        return result

    async def create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        replace: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._create_snapshot,
            session_id,
            name,
            actor_id,
            replace,
        )

    def _create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        replace: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    name,
                    "manual",
                    actor_id,
                    replace=replace,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.create",
                    snapshot_id,
                    {"name": name, "turn_no": session["turn_no"]},
                )
                row = connection.execute(
                    "SELECT * FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._snapshot(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_snapshots(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_snapshots, session_id)

    def _list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (session_id,),
            ).fetchall()
            return [self._snapshot(row) for row in rows]

    async def restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._restore_snapshot,
            session_id,
            snapshot_ref,
            actor_id,
        )

    def _restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot = connection.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE session_id = ? AND (id = ? OR name = ?)
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, snapshot_ref, snapshot_ref),
                ).fetchone()
                if not snapshot:
                    raise DatabaseNotFoundError("存档不存在")

                self._insert_snapshot(
                    connection,
                    session,
                    f"safety-before-restore-{session['revision']}",
                    "safety",
                    actor_id,
                    replace=True,
                )
                max_seq = connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0)
                    FROM events WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
                workflow_row = connection.execute(
                    """
                    SELECT workflow_json FROM snapshot_workflows
                    WHERE snapshot_id = ?
                    """,
                    (snapshot["id"],),
                ).fetchone()
                workflow = json_load(
                    workflow_row["workflow_json"] if workflow_row else "",
                    {},
                )
                if not isinstance(workflow, Mapping):
                    workflow = {}
                floor = bounded_int(
                    workflow.get("history_floor_seq"),
                    int(session["history_floor_seq"] or 0),
                    0,
                    int(max_seq) + 1,
                )
                anchor = bounded_int(
                    workflow.get("event_anchor_seq"),
                    int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0) FROM events
                            WHERE session_id = ? AND created_at <= ?
                            """,
                            (session_id, snapshot["created_at"]),
                        ).fetchone()[0]
                    ),
                    0,
                    int(max_seq),
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id = ?, turn_no = ?, world_state_json = ?,
                        history_floor_seq = ?, revision = revision + 1,
                        state = 'paused', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        snapshot["world_id"],
                        snapshot["turn_no"],
                        snapshot["world_state_json"],
                        floor,
                        now,
                        session_id,
                    ),
                )
                self._restore_workflow_snapshot(
                    connection,
                    snapshot["id"],
                    session_id,
                )
                rule_row = connection.execute(
                    """
                    SELECT recovery_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                recovery = json_load(
                    rule_row["recovery_json"] if rule_row else "",
                    {},
                )
                recovery = (
                    dict(recovery)
                    if isinstance(recovery, Mapping)
                    else {}
                )
                excluded: list[list[int]] = []
                for item in recovery.get("excluded_event_ranges", []):
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        continue
                    try:
                        start, end = int(item[0]), int(item[1])
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if start <= end:
                        excluded.append([start, end])
                if anchor + 1 <= int(max_seq):
                    excluded.append([anchor + 1, int(max_seq)])
                recovery.update(
                    {
                        "state": "restored",
                        "snapshot_id": str(snapshot["id"]),
                        "event_anchor_seq": anchor,
                        "excluded_event_ranges": excluded[-64:],
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """
                    UPDATE session_rule_states
                    SET recovery_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(recovery), now, session_id),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'system', ?, '酒馆系统', ?, ?, ?)
                    """,
                    (
                        new_id("event"),
                        session_id,
                        snapshot["turn_no"],
                        actor_id,
                        f"已恢复存档「{snapshot['name']}」，会话已暂停。",
                        json_dump(
                            {
                                "snapshot_id": snapshot["id"],
                                "restored": True,
                            }
                        ),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.restore",
                    snapshot["id"],
                    {
                        "name": snapshot["name"],
                        "turn_no": snapshot["turn_no"],
                    },
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _restore_workflow_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        session_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT workflow_json FROM snapshot_workflows
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if not row:
            # Old snapshots had no workflow section. Cancel incompatible
            # in-flight work rather than mixing it with the restored branch.
            now = utc_now()
            connection.execute(
                """
                UPDATE choice_sets SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE group_votes SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status = 'open'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE timer_instances SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status IN ('active', 'paused')
                """,
                (now, session_id),
            )
            return
        data = json_load(row["workflow_json"], {})
        if data.get("format") != "astrbot-tavern-workflow":
            raise ValueError("存档中的流程快照格式无效")
        for table in (
            "session_character_states",
            "assist_tokens",
            "vote_ballots",
            "group_votes",
            "choice_sets",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "participants",
            "scene_clocks",
            "story_ledger",
            "session_characters",
            "session_rule_states",
        ):
            if table == "vote_ballots":
                connection.execute(
                    """
                    DELETE FROM vote_ballots
                    WHERE vote_id IN (
                        SELECT id FROM group_votes WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table in {
                "character_card_drafts",
                "card_binding_codes",
            }:
                connection.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "session_character_states":
                connection.execute(
                    """
                    DELETE FROM session_character_states
                    WHERE character_id IN (
                        SELECT id FROM session_characters
                        WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "ban_records":
                connection.execute(
                    """
                    DELETE FROM ban_records
                    WHERE session_id = ? AND scope = 'instance'
                    """,
                    (session_id,),
                )
            else:
                connection.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
        columns: dict[str, tuple[str, ...]] = {
            "participants": (
                "id", "session_id", "player_id", "group_user_id",
                "private_user_id", "private_origin", "display_name",
                "character_card_id", "character_version_id",
                "character_name", "character_code", "aliases_json",
                "card_status", "ready", "participation_status",
                "seat_reserved_at", "joined_round",
                "consecutive_timeouts", "exit_reason",
                "created_at", "updated_at",
            ),
            "character_card_drafts": (
                "id", "participant_id", "template_version", "fields_json",
                "current_step", "status", "expires_at",
                "created_at", "updated_at",
            ),
            "card_binding_codes": (
                "id", "participant_id", "code", "status", "expires_at",
                "private_user_id", "private_origin", "created_at", "used_at",
            ),
            "character_runtime_states": (
                "id", "session_id", "participant_id", "character_card_id",
                "state_json", "revision", "created_at", "updated_at",
            ),
            "choice_sets": (
                "id", "session_id", "participant_id", "round_no",
                "session_revision", "choices_json", "status",
                "reroll_count", "selected_key", "flavor_text",
                "idempotency_key", "created_at", "updated_at",
            ),
            "group_votes": (
                "id", "session_id", "source_event_id", "question",
                "options_json", "eligible_user_ids_json", "stage", "status",
                "winner_key", "suspended_user_id", "deadline_at",
                "result_json", "created_at", "updated_at",
            ),
            "vote_ballots": (
                "id", "vote_id", "user_id", "option_key",
                "created_at", "updated_at",
            ),
            "selected_world_events": (
                "id", "session_id", "round_no", "pool_item_id",
                "payload_json", "status", "narrative",
                "created_at", "resolved_at",
            ),
            "timer_instances": (
                "id", "session_id", "participant_id", "timer_type",
                "status", "deadline_at", "remaining_seconds",
                "reminder_at", "reminder_sent", "action_json",
                "created_at", "updated_at",
            ),
            "delegation_grants": (
                "id", "session_id", "participant_id", "owner_user_id",
                "delegate_user_id", "permissions_json", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "permission_grants": (
                "id", "session_id", "user_id", "role",
                "granted_by", "created_at",
            ),
            "return_requests": (
                "id", "session_id", "participant_id", "requested_by",
                "status", "exit_type", "objective", "progress_json",
                "vote_id", "created_at", "updated_at",
            ),
            "ban_records": (
                "id", "session_id", "platform_id", "group_id", "user_id",
                "participant_id", "scope", "reason", "actor_id", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "session_rule_states": (
                "session_id", "progress_json",
                "content_boundaries_json", "npc_policy_json",
                "context_budget_json", "dice_rules_json", "recovery_json",
                "revision", "created_at", "updated_at",
            ),
            "session_characters": (
                "id", "session_id", "stable_key", "name", "aliases_json",
                "role_type", "public_profile_json", "known_facts_json",
                "misconceptions_json", "source", "review_status",
                "lifecycle_status", "persistent", "first_event_id",
                "last_event_id", "first_turn", "last_turn", "revision",
                "created_at", "updated_at",
            ),
            "session_character_states": (
                "character_id", "state_json", "revision", "updated_at",
            ),
            "story_ledger": (
                "id", "session_id", "stable_key", "kind", "title",
                "description", "status", "visibility", "source_event_id",
                "completed_event_id", "revision", "created_at", "updated_at",
            ),
            "scene_clocks": (
                "id", "session_id", "stable_key", "title", "segments",
                "current_value", "visibility", "trigger_text", "status",
                "triggered_event_id", "revision", "created_at", "updated_at",
            ),
            "assist_tokens": (
                "id", "session_id", "source_participant_id",
                "target_participant_id", "stat", "method", "status",
                "expires_round", "source_event_id", "created_at",
                "consumed_at",
            ),
        }
        insert_order = (
            "session_rule_states",
            "participants",
            "character_card_drafts",
            "card_binding_codes",
            "character_runtime_states",
            "choice_sets",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "session_characters",
            "session_character_states",
            "story_ledger",
            "scene_clocks",
            "assist_tokens",
        )
        for table in insert_order:
            rows = data.get(table, [])
            if table in {
                "session_rule_states",
                "session_characters",
                "session_character_states",
                "story_ledger",
                "scene_clocks",
                "assist_tokens",
            } and int(data.get("version", 1) or 1) < 2:
                rows = []
            if not isinstance(rows, list):
                raise ValueError(f"流程快照表 {table} 格式错误")
            self._import_rows(
                connection,
                table,
                rows,
                columns[table],
            )
        if not data.get("session_rule_states"):
            self._initialize_v05_rows(connection)
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'paused', reminder_at = '', reminder_sent = 0
            WHERE session_id = ? AND status = 'active'
            """,
            (session_id,),
        )

    async def restore_latest_auto(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._restore_latest_auto,
            session_id,
            actor_id,
        )

    def _restore_latest_auto(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id FROM snapshots
                WHERE session_id = ?
                  AND kind IN ('auto', 'safety', 'undo')
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有可回滚的保护点")
        return self._restore_snapshot(session_id, row["id"], actor_id)

    async def delete_snapshot(
        self,
        snapshot_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_snapshot, snapshot_id, actor_id)

    def _delete_snapshot(self, snapshot_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("存档不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                if row["kind"] in {"safety", "undo", "final"}:
                    raise ValueError(
                        "安全快照、回滚点与最终保护存档不能手动删除"
                    )
                connection.execute(
                    "DELETE FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "snapshot.delete",
                    snapshot_id,
                    {"name": row["name"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def commit_turn(
        self,
        *,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]],
        check_payload: Mapping[str, Any] | None,
        model_payload: Mapping[str, Any] | None,
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._commit_turn_sync,
            session_id,
            expected_revision,
            player_id,
            player_user_id,
            player_name,
            player_input,
            narrative,
            dict(world_state),
            [dict(item) for item in memories],
            dict(check_payload or {}),
            dict(model_payload or {}),
            clean_text(director_note, max_chars=500),
            auto_snapshot_interval,
            store_model_payload,
            dict(workflow or {}),
        )

    def _commit_turn_sync(
        self,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        check_payload: dict[str, Any],
        model_payload: dict[str, Any],
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["revision"] != expected_revision:
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("会话不在运行状态")

                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                if player_user_id not in turn_state["order"]:
                    if turn_state["order"]:
                        raise InvalidTransitionError(
                            "该玩家尚未加入当前回合队列"
                        )
                    turn_state, _ = join_turn(
                        turn_state,
                        player_user_id,
                    )
                if turn_state["current_user_id"] != player_user_id:
                    status = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    raise InvalidTransitionError(
                        f"当前轮到 {status['current_name'] or status['current_user_id']}"
                    )
                acting_round = turn_state["round_no"]
                group_decision = workflow.get("group_decision")
                preserves_action_right = isinstance(
                    group_decision,
                    Mapping,
                )
                next_turn_state = (
                    dict(turn_state)
                    if preserves_action_right
                    else advance_turn(
                        turn_state,
                        player_user_id,
                    )
                )
                if (
                    not preserves_action_right
                    and next_turn_state["round_no"] > acting_round
                ):
                    pending_rows = connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ? AND card_status = 'approved'
                          AND participation_status = 'active'
                          AND joined_round <= ?
                        ORDER BY created_at
                        """,
                        (session_id, next_turn_state["round_no"]),
                    ).fetchall()
                    for pending in pending_rows:
                        pending_user_id = str(pending["group_user_id"])
                        if pending_user_id not in next_turn_state["order"]:
                            next_turn_state, _ = join_turn(
                                next_turn_state,
                                pending_user_id,
                            )
                persisted_world_state = embed_turn_state(
                    public_world_state(world_state),
                    next_turn_state,
                )
                new_turn = session["turn_no"] + 1
                self._insert_snapshot(
                    connection,
                    session,
                    (
                        f"undo-before-turn-{new_turn}"
                        f"-revision-{session['revision']}"
                    ),
                    "undo",
                    "system",
                    replace=False,
                )
                if (
                    auto_snapshot_interval > 0
                    and session["turn_no"] > 0
                    and session["turn_no"] % auto_snapshot_interval == 0
                ):
                    self._insert_snapshot(
                        connection,
                        session,
                        f"auto-turn-{session['turn_no']}",
                        "auto",
                        "system",
                        replace=True,
                    )

                now = utc_now()
                player_event_id = new_id("event")
                narrator_event_id = new_id("event")
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'player', ?, ?, ?, ?, ?)
                    """,
                    (
                        player_event_id,
                        session_id,
                        new_turn,
                        player_user_id,
                        player_name,
                        player_input,
                        json_dump({"player_id": player_id}),
                        now,
                    ),
                )

                workflow_result = self._commit_vnext_workflow(
                    connection,
                    session=session,
                    new_turn=new_turn,
                    acting_round=acting_round,
                    next_turn_state=next_turn_state,
                    player_user_id=player_user_id,
                    player_event_id=player_event_id,
                    narrator_event_id=narrator_event_id,
                    world_state=persisted_world_state,
                    check_payload=check_payload,
                    workflow=workflow,
                    now=now,
                )
                narrator_meta: dict[str, Any] = {}
                if check_payload:
                    narrator_meta["check"] = check_payload
                if store_model_payload and model_payload:
                    narrator_meta["model_payload"] = model_payload
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, turn_no, role, actor_id, actor_name,
                        content, meta_json, created_at
                    ) VALUES (?, ?, ?, 'narrator', 'narrator',
                              '酒馆叙事者', ?, ?, ?)
                    """,
                    (
                        narrator_event_id,
                        session_id,
                        new_turn,
                        narrative,
                        json_dump(narrator_meta),
                        now,
                    ),
                )
                participant_row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, player_user_id),
                ).fetchone()
                if workflow and participant_row:
                    v05_result = self._apply_v05_turn_ops(
                        connection,
                        session=session,
                        participant=participant_row,
                        new_turn=new_turn,
                        acting_round=acting_round,
                        source_event_id=narrator_event_id,
                        workflow=workflow,
                        check_payload=check_payload,
                        now=now,
                    )
                    workflow_result["v05"] = v05_result
                connection.execute(
                    """
                    UPDATE sessions SET
                        turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_turn,
                        json_dump(persisted_world_state),
                        now,
                        session_id,
                    ),
                )

                for memory in memories[:12]:
                    scope = str(memory.get("scope", "world"))
                    scope_id = str(memory.get("scope_id", ""))
                    kind = str(memory.get("kind", "fact"))
                    content = str(memory.get("content", "")).strip()
                    if not content:
                        continue
                    importance = max(
                        1,
                        min(5, int(memory.get("importance", 3))),
                    )
                    tags = (
                        memory.get("tags")
                        if isinstance(memory.get("tags"), list)
                        else []
                    )
                    fingerprint = memory_fingerprint(
                        session_id,
                        scope,
                        scope_id,
                        kind,
                        content,
                    )
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            narrator_event_id,
                            now,
                            now,
                            now,
                        ),
                    )
                    stored_memory = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    if stored_memory:
                        memory_id = str(stored_memory["id"])
                        visibility = str(
                            memory.get("visibility") or "public"
                        ).lower()
                        if visibility not in {"public", "host", "private"}:
                            visibility = "public"
                        supersedes_id = clean_text(
                            memory.get("supersedes_id"),
                            max_chars=128,
                        )
                        connection.execute(
                            """
                            INSERT INTO memory_governance(
                                memory_id, visibility, locked, pinned,
                                invalidated, supersedes_id, conflict_status,
                                note, updated_by, updated_at
                            ) VALUES (?, ?, ?, ?, 0, ?, 'clear', '',
                                      'narrator', ?)
                            ON CONFLICT(memory_id) DO UPDATE SET
                                visibility = excluded.visibility,
                                locked = MAX(locked, excluded.locked),
                                pinned = MAX(pinned, excluded.pinned),
                                supersedes_id = CASE
                                    WHEN excluded.supersedes_id <> ''
                                    THEN excluded.supersedes_id
                                    ELSE supersedes_id
                                END,
                                updated_at = excluded.updated_at
                            """,
                            (
                                memory_id,
                                visibility,
                                int(bool(memory.get("locked", False))),
                                int(bool(memory.get("pinned", False))),
                                supersedes_id,
                                now,
                            ),
                        )
                        if supersedes_id:
                            connection.execute(
                                """
                                UPDATE memory_governance
                                SET invalidated = 1, updated_by = 'narrator',
                                    updated_at = ?
                                WHERE memory_id = ?
                                """,
                                (now, supersedes_id),
                            )

                self._insert_audit(
                    connection,
                    session_id,
                    player_user_id,
                    "turn.commit",
                    narrator_event_id,
                    {
                        "turn_no": new_turn,
                        "round_no": acting_round,
                        "next_player_user_id": (
                            next_turn_state["current_user_id"]
                        ),
                        "check": check_payload or None,
                        "memory_count": len(memories[:12]),
                        "director_note": director_note,
                        "workflow": workflow_result,
                    },
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'auto'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'undo'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._session(row)
                result["player_event_id"] = player_event_id
                result["narrator_event_id"] = narrator_event_id
                result["workflow"] = workflow_result
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _participant(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        result = {
            "id": row["id"],
            "session_id": row["session_id"],
            "player_id": row["player_id"],
            "group_user_id": row["group_user_id"],
            "private_user_id": row["private_user_id"],
            "private_origin": row["private_origin"],
            "display_name": row["display_name"],
            "character_card_id": row["character_card_id"],
            "character_version_id": row["character_version_id"],
            "character_name": row["character_name"],
            "character_code": row["character_code"],
            "aliases": json_load(row["aliases_json"], []),
            "card_status": row["card_status"],
            "ready": bool(row["ready"]),
            "participation_status": row["participation_status"],
            "seat_reserved_at": row["seat_reserved_at"],
            "joined_round": row["joined_round"],
            "consecutive_timeouts": row["consecutive_timeouts"],
            "exit_reason": row["exit_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for key in (
            "draft_status",
            "draft_step",
            "draft_expires_at",
            "binding_code",
            "binding_expires_at",
            "draft_profile_json",
            "draft_template_version",
            "runtime_state_json",
            "runtime_revision",
            "card_profile_json",
            "card_stats_json",
            "card_version_no",
            "card_template_version",
            "card_version_status",
            "card_review_note",
            "card_reviewed_by",
            "card_version_created_at",
        ):
            if key in keys:
                value = row[key]
                if key == "draft_profile_json":
                    result["draft_profile"] = json_load(value, {})
                elif key == "runtime_state_json":
                    result["runtime_state"] = json_load(value, {})
                elif key == "card_profile_json":
                    result["card_profile"] = json_load(value, {})
                elif key == "card_stats_json":
                    result["card_stats"] = json_load(value, {})
                elif key == "runtime_revision":
                    result["runtime_revision"] = value
                else:
                    result[key] = value
        return result

    @staticmethod
    def _choice_set(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "participant_id": row["participant_id"],
            "round_no": row["round_no"],
            "session_revision": row["session_revision"],
            "choices": json_load(row["choices_json"], []),
            "status": row["status"],
            "reroll_count": row["reroll_count"],
            "selected_key": row["selected_key"],
            "flavor_text": row["flavor_text"],
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _vote(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "source_event_id": row["source_event_id"],
            "question": row["question"],
            "options": json_load(row["options_json"], []),
            "eligible_user_ids": json_load(
                row["eligible_user_ids_json"],
                [],
            ),
            "stage": row["stage"],
            "status": row["status"],
            "winner_key": row["winner_key"],
            "suspended_user_id": row["suspended_user_id"],
            "deadline_at": row["deadline_at"],
            "result": json_load(row["result_json"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_instance_config(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_instance_config, session_id)

    def _get_instance_config(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本配置不存在")
            world_snapshot = json_load(
                row["world_snapshot_json"],
                {},
            )
            return {
                "session_id": row["session_id"],
                "world_revision": row["world_revision"],
                "world_snapshot": world_snapshot,
                "character_card_template": card_template(world_snapshot),
                "time_rules": normalize_time_rules(
                    json_load(row["time_rules_json"], {})
                ),
                "phase_meta": json_load(row["phase_meta_json"], {}),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def save_instance_time_rules(
        self,
        session_id: str,
        rules: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_time_rules(rules)
        await self._run(
            self._save_instance_time_rules,
            session_id,
            normalized,
            actor_id,
        )
        return await self.get_instance_config(session_id)

    def _save_instance_time_rules(
        self,
        session_id: str,
        rules: dict[str, Any],
        actor_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute(
                    "SELECT 1 FROM instance_configs WHERE session_id = ?",
                    (session_id,),
                ).fetchone():
                    raise DatabaseNotFoundError("副本配置不存在")
                self._assert_session_writable(connection, session_id)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET time_rules_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(rules), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timing.rules_update",
                    session_id,
                    {"rules": rules},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._get_session_archive, session_id)

    def _get_session_archive(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_archives WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "session_id": row["session_id"],
                "termination_type": row["termination_type"],
                "reason": row["reason"],
                "final_snapshot_id": row["final_snapshot_id"],
                "ended_by": row["ended_by"],
                "ended_at": row["ended_at"],
                "readonly": bool(row["readonly"]),
            }

    async def get_session_rule_state(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_session_rule_state, session_id)

    def _get_session_rule_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_rule_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                self._initialize_v05_rows(connection)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if not row:
                raise DatabaseNotFoundError("副本规则状态不存在")
            return {
                "session_id": row["session_id"],
                "progress": normalize_progress(
                    json_load(row["progress_json"], {})
                ),
                "content_boundaries": json_load(
                    row["content_boundaries_json"],
                    {},
                ),
                "npc_policy": json_load(row["npc_policy_json"], {}),
                "context_budget": json_load(
                    row["context_budget_json"],
                    {},
                ),
                "dice_rules": json_load(row["dice_rules_json"], {}),
                "recovery": json_load(row["recovery_json"], {}),
                "revision": row["revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def save_session_rule_state(
        self,
        session_id: str,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_rule_state,
            session_id,
            dict(payload),
            actor_id,
        )

    def _save_session_rule_state(
        self,
        session_id: str,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM session_rule_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    self._initialize_v05_rows(connection)
                    row = connection.execute(
                        "SELECT * FROM session_rule_states WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本规则状态不存在")
                expected = payload.get("revision")
                if expected not in {None, ""} and int(expected) != int(
                    row["revision"]
                ):
                    raise DatabaseConflictError("副本规则状态已被其他操作更新")
                progress = (
                    normalize_progress(payload["progress"])
                    if "progress" in payload
                    else normalize_progress(json_load(row["progress_json"], {}))
                )
                boundaries = (
                    dict(payload["content_boundaries"])
                    if isinstance(payload.get("content_boundaries"), Mapping)
                    else json_load(row["content_boundaries_json"], {})
                )
                npc_policy = (
                    dict(payload["npc_policy"])
                    if isinstance(payload.get("npc_policy"), Mapping)
                    else json_load(row["npc_policy_json"], {})
                )
                npc_policy["max_new_per_turn"] = bounded_int(
                    npc_policy.get("max_new_per_turn"),
                    3,
                    0,
                    3,
                )
                context_budget = (
                    dict(payload["context_budget"])
                    if isinstance(payload.get("context_budget"), Mapping)
                    else json_load(row["context_budget_json"], {})
                )
                dice_rules = (
                    dict(payload["dice_rules"])
                    if isinstance(payload.get("dice_rules"), Mapping)
                    else json_load(row["dice_rules_json"], {})
                )
                visibility = str(
                    dice_rules.get("visibility") or "public"
                ).lower()
                dice_rules["visibility"] = (
                    visibility
                    if visibility in {"public", "immersive", "hidden"}
                    else "public"
                )
                recovery = (
                    dict(payload["recovery"])
                    if isinstance(payload.get("recovery"), Mapping)
                    else json_load(row["recovery_json"], {})
                )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE session_rule_states SET
                        progress_json = ?, content_boundaries_json = ?,
                        npc_policy_json = ?, context_budget_json = ?,
                        dice_rules_json = ?, recovery_json = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json_dump(progress),
                        json_dump(boundaries),
                        json_dump(npc_policy),
                        json_dump(context_budget),
                        json_dump(dice_rules),
                        json_dump(recovery),
                        now,
                        session_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.rules.update",
                    session_id,
                    {
                        "progress": progress,
                        "npc_policy": npc_policy,
                        "dice_visibility": dice_rules["visibility"],
                    },
                )
                connection.execute("COMMIT")
                return self._get_session_rule_state(session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_session_characters(
        self,
        session_id: str,
        *,
        include_archived: bool = True,
        context_only: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_session_characters,
            session_id,
            include_archived,
            context_only,
        )

    def _list_session_characters(
        self,
        session_id: str,
        include_archived: bool,
        context_only: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            limit = 500
            if context_only:
                rules = connection.execute(
                    """
                    SELECT context_budget_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                budget = json_load(
                    rules["context_budget_json"] if rules else "",
                    {},
                )
                limit = bounded_int(budget.get("active_npcs"), 12, 0, 40)
            clauses = ["sc.session_id = ?"]
            params: list[Any] = [session_id]
            if not include_archived or context_only:
                clauses.append("sc.lifecycle_status = 'active'")
            if context_only:
                clauses.append("sc.review_status <> 'rejected'")
            rows = connection.execute(
                f"""
                SELECT sc.*, st.state_json,
                       st.revision AS state_revision
                FROM session_characters sc
                LEFT JOIN session_character_states st
                  ON st.character_id = sc.id
                WHERE {' AND '.join(clauses)}
                ORDER BY
                    CASE sc.source WHEN 'world_preset' THEN 0 ELSE 1 END,
                    sc.last_turn DESC, sc.updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return [self._session_character(row) for row in rows]

    async def save_session_character(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_session_character,
            dict(payload),
            actor_id,
        )

    def _save_session_character(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "").strip()
        character_id = str(payload.get("id") or "").strip()
        name = clean_text(payload.get("name"), max_chars=80)
        if not session_id or not name:
            raise ValueError("副本 ID 与 NPC 名称不能为空")
        aliases = [
            clean_text(item, max_chars=80)
            for item in (
                payload.get("aliases")
                if isinstance(payload.get("aliases"), list)
                else []
            )[:12]
            if clean_text(item, max_chars=80)
        ]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                current = (
                    connection.execute(
                        "SELECT * FROM session_characters WHERE id = ?",
                        (character_id,),
                    ).fetchone()
                    if character_id
                    else None
                )
                if character_id and not current:
                    raise DatabaseNotFoundError("副本 NPC 不存在")
                lowered_names = {
                    self._stable_key(name),
                    *(self._stable_key(item) for item in aliases),
                }
                candidates = connection.execute(
                    """
                    SELECT id, name, aliases_json FROM session_characters
                    WHERE session_id = ? AND id <> ?
                      AND lifecycle_status <> 'archived'
                    """,
                    (session_id, character_id),
                ).fetchall()
                for candidate in candidates:
                    candidate_names = {
                        self._stable_key(candidate["name"]),
                        *(
                            self._stable_key(item)
                            for item in json_load(
                                candidate["aliases_json"],
                                [],
                            )
                        ),
                    }
                    if lowered_names & candidate_names:
                        raise DatabaseConflictError(
                            f"NPC 名称或别名与「{candidate['name']}」重复"
                        )
                now = utc_now()
                profile = (
                    dict(payload.get("public_profile"))
                    if isinstance(payload.get("public_profile"), Mapping)
                    else {}
                )
                known_facts = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("known_facts")
                        if isinstance(payload.get("known_facts"), list)
                        else []
                    )[:30]
                    if clean_text(item, max_chars=400)
                ]
                misconceptions = [
                    clean_text(item, max_chars=400)
                    for item in (
                        payload.get("misconceptions")
                        if isinstance(payload.get("misconceptions"), list)
                        else []
                    )[:20]
                    if clean_text(item, max_chars=400)
                ]
                state = (
                    dict(payload.get("state"))
                    if isinstance(payload.get("state"), Mapping)
                    else {}
                )
                role_type = clean_text(
                    payload.get("role_type") or "npc",
                    max_chars=40,
                )
                review_status = str(
                    payload.get("review_status") or "approved"
                ).lower()
                if review_status not in {
                    "pending",
                    "approved",
                    "rejected",
                    "duplicate",
                }:
                    review_status = "approved"
                lifecycle_status = str(
                    payload.get("lifecycle_status") or "active"
                ).lower()
                if lifecycle_status not in {
                    "active",
                    "departed",
                    "dead",
                    "archived",
                }:
                    lifecycle_status = "active"
                if current:
                    expected = payload.get("revision")
                    if expected not in {None, ""} and int(expected) != int(
                        current["revision"]
                    ):
                        raise DatabaseConflictError("NPC 已被其他操作更新")
                    connection.execute(
                        """
                        UPDATE session_characters SET
                            name = ?, aliases_json = ?, role_type = ?,
                            public_profile_json = ?, known_facts_json = ?,
                            misconceptions_json = ?, review_status = ?,
                            lifecycle_status = ?, persistent = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            character_id,
                        ),
                    )
                    action = "session_npc.update"
                else:
                    character_id = new_id("snpc")
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_turn,
                            last_turn, revision, created_at, updated_at
                        ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admin', ?,
                                 ?, ?, turn_no, turn_no, 1, ?, ?
                          FROM sessions WHERE id = ?
                        """,
                        (
                            character_id,
                            session_id,
                            f"admin:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            role_type,
                            json_dump(profile),
                            json_dump(known_facts),
                            json_dump(misconceptions),
                            review_status,
                            lifecycle_status,
                            int(bool(payload.get("persistent", True))),
                            now,
                            now,
                            session_id,
                        ),
                    )
                    action = "session_npc.create"
                connection.execute(
                    """
                    INSERT INTO session_character_states(
                        character_id, state_json, revision, updated_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(character_id) DO UPDATE SET
                        state_json = excluded.state_json,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (character_id, json_dump(state), now),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    character_id,
                    {"name": name, "review_status": review_status},
                )
                row = connection.execute(
                    """
                    SELECT sc.*, st.state_json,
                           st.revision AS state_revision
                    FROM session_characters sc
                    LEFT JOIN session_character_states st
                      ON st.character_id = sc.id
                    WHERE sc.id = ?
                    """,
                    (character_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session_character(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_story_ledger(
        self,
        session_id: str,
        *,
        include_host: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_story_ledger,
            session_id,
            include_host,
        )

    def _list_story_ledger(
        self,
        session_id: str,
        include_host: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM story_ledger
                WHERE session_id = ?
                  AND (? = 1 OR visibility = 'public')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    CASE kind WHEN 'main' THEN 0 WHEN 'objective' THEN 1
                              WHEN 'side' THEN 2 ELSE 3 END,
                    updated_at DESC
                """,
                (session_id, int(include_host)),
            ).fetchall()
            return [self._ledger_entry(row) for row in rows]

    async def list_scene_clocks(
        self,
        session_id: str,
        *,
        include_hidden: bool = True,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_scene_clocks,
            session_id,
            include_hidden,
        )

    def _list_scene_clocks(
        self,
        session_id: str,
        include_hidden: bool,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM scene_clocks
                WHERE session_id = ?
                  AND (? = 1 OR visibility <> 'hidden')
                ORDER BY
                    CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    updated_at DESC
                """,
                (session_id, int(include_hidden)),
            ).fetchall()
            return [self._scene_clock(row) for row in rows]

    async def inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._inspiration_status,
            session_id,
            user_id,
        )

    def _inspiration_status(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, crs.state_json
                FROM participants pt
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise DatabaseNotFoundError("当前玩家没有副本角色")
            state = json_load(row["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            return {
                "participant_id": row["participant_id"],
                "character_name": (
                    row["character_name"] or row["display_name"]
                ),
                "balance": bounded_int(
                    state.get("inspiration"),
                    1,
                    0,
                    3,
                ),
                "maximum": bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                ),
            }

    async def check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        *,
        proposed_advantages: Sequence[str] = (),
        proposed_disadvantages: Sequence[str] = (),
        locked_advantages: Sequence[str] = (),
        locked_disadvantages: Sequence[str] = (),
    ) -> dict[str, Any]:
        return await self._run(
            self._check_context,
            session_id,
            user_id,
            stat,
            list(proposed_advantages),
            list(proposed_disadvantages),
            list(locked_advantages),
            list(locked_disadvantages),
        )

    def _check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        proposed_advantages: list[str],
        proposed_disadvantages: list[str],
        locked_advantages: list[str],
        locked_disadvantages: list[str],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, ccv.profile_json,
                       crs.state_json, s.world_state_json
                FROM participants pt
                JOIN sessions s ON s.id = pt.session_id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                # Schema 1/2 sessions may still have a legacy player row but
                # no participant/card binding. Preserve their old neutral
                # check behavior. Unverifiable model-proposed sources are
                # deliberately rejected instead of being trusted implicitly.
                return {
                    "participant_id": "",
                    "advantages": [],
                    "disadvantages": [],
                    "assist_token_id": "",
                    "rejected_advantages": list(
                        dict.fromkeys(
                            [*proposed_advantages, *locked_advantages]
                        )
                    ),
                    "rejected_disadvantages": list(
                        dict.fromkeys(
                            [*proposed_disadvantages, *locked_disadvantages]
                        )
                    ),
                }
            profile = json_load(row["profile_json"], {})
            runtime = json_load(row["state_json"], {})
            world_state = json_load(row["world_state_json"], {})
            profile = profile if isinstance(profile, Mapping) else {}
            runtime = runtime if isinstance(runtime, Mapping) else {}
            world_state = (
                world_state if isinstance(world_state, Mapping) else {}
            )
            allowed_advantages: set[str] = set()
            allowed_disadvantages: set[str] = set()
            raw_specialties = profile.get("specialties")
            specialties = (
                raw_specialties
                if isinstance(raw_specialties, list)
                else str(raw_specialties or "").replace("，", ",").split(",")
            )
            for specialty in specialties:
                text = clean_text(specialty, max_chars=80)
                if text:
                    allowed_advantages.add(f"专长：{text}")
            for source in runtime.get("advantage_sources", []):
                text = clean_text(source, max_chars=120)
                if text:
                    allowed_advantages.add(text)
            for status in runtime.get("statuses", []):
                if not isinstance(status, Mapping):
                    continue
                affects = status.get("affects")
                affects = affects if isinstance(affects, list) else []
                reference = str(stat or "").casefold()
                if affects and not any(
                    reference in str(item).casefold()
                    or str(item).casefold() in reference
                    for item in affects
                ):
                    continue
                name = clean_text(status.get("name"), max_chars=100)
                if name:
                    allowed_disadvantages.add(f"状态：{name}")
            modifiers = world_state.get("check_modifiers")
            if isinstance(modifiers, Mapping):
                for source in modifiers.get("advantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_advantages.add(text)
                for source in modifiers.get("disadvantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_disadvantages.add(text)

            # Option generation may disclose an environmental or prepared
            # source before the check. It is accepted only when the source can
            # be matched to an already persisted character/scene fact; the
            # model cannot manufacture a bonus merely by writing it twice.
            trusted_texts: list[str] = []

            def collect_trusted(value: Any) -> None:
                if isinstance(value, Mapping):
                    for nested in value.values():
                        collect_trusted(nested)
                elif isinstance(value, Sequence) and not isinstance(
                    value,
                    (str, bytes),
                ):
                    for nested in value:
                        collect_trusted(nested)
                else:
                    text = clean_text(value, max_chars=500)
                    if len(text) >= 2:
                        trusted_texts.append(text.casefold())

            collect_trusted(profile)
            collect_trusted(runtime)
            collect_trusted(
                {
                    "location": world_state.get("location"),
                    "time": world_state.get("time"),
                    "scene_summary": world_state.get("scene_summary"),
                    "facts": world_state.get("facts"),
                    "inventory": world_state.get("inventory"),
                    "check_modifiers": world_state.get("check_modifiers"),
                }
            )

            def source_is_proven(source: str) -> bool:
                probe = source
                for prefix in (
                    "专长：",
                    "装备：",
                    "情报：",
                    "环境：",
                    "准备：",
                    "状态：",
                ):
                    if probe.startswith(prefix):
                        probe = probe[len(prefix) :]
                        break
                normalized = " ".join(probe.casefold().split())
                if len(normalized) < 2:
                    return False
                return any(
                    normalized in fact
                    or (
                        len(fact) >= 4
                        and len(normalized) >= 4
                        and fact in normalized
                    )
                    for fact in trusted_texts
                )

            for raw in locked_advantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_advantages.add(source)
            for raw in locked_disadvantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_disadvantages.add(source)
            assist = connection.execute(
                """
                SELECT at.*, source.character_name AS source_name,
                       source.display_name AS source_display
                FROM assist_tokens at
                JOIN participants source ON source.id = at.source_participant_id
                WHERE at.session_id = ? AND at.target_participant_id = ?
                  AND at.status = 'active'
                  AND (at.stat = '' OR lower(at.stat) = lower(?))
                ORDER BY at.created_at
                LIMIT 1
                """,
                (session_id, row["participant_id"], stat),
            ).fetchone()
            assist_token_id = ""
            if assist:
                assist_token_id = str(assist["id"])
                allowed_advantages.add(
                    "协助："
                    + str(
                        assist["source_name"]
                        or assist["source_display"]
                        or "队友"
                    )
                )
            proposed_adv = {
                clean_text(item, max_chars=120)
                for item in [*proposed_advantages, *locked_advantages]
                if clean_text(item, max_chars=120)
            }
            proposed_dis = {
                clean_text(item, max_chars=120)
                for item in [*proposed_disadvantages, *locked_disadvantages]
                if clean_text(item, max_chars=120)
            }
            advantages = sorted(
                allowed_advantages & proposed_adv
                | {
                    item
                    for item in allowed_advantages
                    if item.startswith("协助：")
                }
            )
            disadvantages = sorted(
                allowed_disadvantages & proposed_dis
                | {
                    item
                    for item in allowed_disadvantages
                    if item.startswith("状态：")
                }
            )
            return {
                "participant_id": row["participant_id"],
                "advantages": advantages[:8],
                "disadvantages": disadvantages[:8],
                "assist_token_id": assist_token_id,
                "rejected_advantages": sorted(
                    proposed_adv - allowed_advantages
                ),
                "rejected_disadvantages": sorted(
                    proposed_dis - allowed_disadvantages
                ),
            }

    async def reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_token_usage,
            session_id,
            request_type,
            provider_id,
            expected_tokens,
        )

    def _reserve_token_usage(
        self,
        session_id: str,
        request_type: str,
        provider_id: str,
        expected_tokens: int,
    ) -> dict[str, Any]:
        expected_tokens = bounded_int(
            expected_tokens,
            1,
            1,
            10_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id, group_id FROM sessions WHERE id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                stale = (
                    now_dt - timedelta(minutes=15)
                ).isoformat(timespec="seconds")
                connection.execute(
                    """
                    UPDATE token_usage SET status = 'failed',
                        settled_at = ?
                    WHERE status = 'reserved' AND created_at < ?
                    """,
                    (now, stale),
                )
                policies = connection.execute(
                    """
                    SELECT * FROM token_quota_policies
                    WHERE enabled = 1 AND (
                        (scope_type = 'group' AND scope_id = ?)
                        OR (scope_type = 'session' AND scope_id = ?)
                    )
                    """,
                    (session["group_id"], session_id),
                ).fetchall()
                quota_status: list[dict[str, Any]] = []
                for policy in policies:
                    cutoff = (
                        now_dt - timedelta(
                            seconds=int(policy["window_seconds"])
                        )
                    ).isoformat(timespec="seconds")
                    if policy["scope_type"] == "group":
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE group_id = ? AND created_at >= ?
                                """,
                                (session["group_id"], cutoff),
                            ).fetchone()[0]
                        )
                    else:
                        used = int(
                            connection.execute(
                                """
                                SELECT COALESCE(SUM(
                                    CASE
                                      WHEN status = 'completed'
                                      THEN total_tokens
                                      WHEN status = 'reserved'
                                      THEN reserved_tokens
                                      ELSE 0
                                    END
                                ), 0)
                                FROM token_usage
                                WHERE session_id = ? AND created_at >= ?
                                """,
                                (session_id, cutoff),
                            ).fetchone()[0]
                        )
                    remaining = max(0, int(policy["token_limit"]) - used)
                    quota_status.append(
                        {
                            "scope_type": policy["scope_type"],
                            "used": used,
                            "limit": int(policy["token_limit"]),
                            "remaining": remaining,
                            "window_seconds": int(
                                policy["window_seconds"]
                            ),
                        }
                    )
                    if expected_tokens > remaining:
                        label = (
                            "群"
                            if policy["scope_type"] == "group"
                            else "副本"
                        )
                        raise ValueError(
                            f"{label} Token 限额不足：当前窗口剩余 "
                            f"{remaining}，本次最多需要 {expected_tokens}"
                        )
                usage_id = new_id("usage")
                connection.execute(
                    """
                    INSERT INTO token_usage(
                        id, session_id, group_id, request_type, provider_id,
                        reserved_tokens, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)
                    """,
                    (
                        usage_id,
                        session_id,
                        session["group_id"],
                        clean_text(request_type, max_chars=64),
                        clean_text(provider_id, max_chars=200),
                        expected_tokens,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "id": usage_id,
                    "session_id": session_id,
                    "group_id": session["group_id"],
                    "reserved_tokens": expected_tokens,
                    "quotas": quota_status,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def settle_token_usage(
        self,
        usage_id: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        await self._run(
            self._settle_token_usage,
            usage_id,
            input_tokens,
            cached_input_tokens,
            output_tokens,
            usage_source,
        )

    def _settle_token_usage(
        self,
        usage_id: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
    ) -> None:
        input_tokens = max(0, int(input_tokens or 0))
        cached_input_tokens = max(
            0,
            min(input_tokens, int(cached_input_tokens or 0)),
        )
        output_tokens = max(0, int(output_tokens or 0))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage SET
                    input_tokens = ?, cached_input_tokens = ?,
                    output_tokens = ?, total_tokens = ?,
                    usage_source = ?, status = 'completed',
                    settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (
                    input_tokens,
                    cached_input_tokens,
                    output_tokens,
                    input_tokens + output_tokens,
                    clean_text(usage_source, max_chars=32) or "estimated",
                    utc_now(),
                    usage_id,
                ),
            )

    async def fail_token_usage(self, usage_id: str) -> None:
        await self._run(self._fail_token_usage, usage_id)

    def _fail_token_usage(self, usage_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE token_usage
                SET status = 'failed', settled_at = ?
                WHERE id = ? AND status = 'reserved'
                """,
                (utc_now(), usage_id),
            )

    async def token_usage_summary(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._token_usage_summary, session_id)

    def _token_usage_summary(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id, group_id FROM sessions WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            now_dt = datetime.now(timezone.utc)

            def total(where: str, value: str, seconds: int | None) -> int:
                parameters: list[Any] = [value]
                cutoff_sql = ""
                if seconds is not None:
                    cutoff_sql = " AND created_at >= ?"
                    parameters.append(
                        (
                            now_dt - timedelta(seconds=seconds)
                        ).isoformat(timespec="seconds")
                    )
                return int(
                    connection.execute(
                        f"""
                        SELECT COALESCE(SUM(total_tokens), 0)
                        FROM token_usage
                        WHERE {where} = ? AND status = 'completed'
                        {cutoff_sql}
                        """,
                        tuple(parameters),
                    ).fetchone()[0]
                )

            policies = connection.execute(
                """
                SELECT * FROM token_quota_policies
                WHERE (scope_type = 'group' AND scope_id = ?)
                   OR (scope_type = 'session' AND scope_id = ?)
                ORDER BY scope_type
                """,
                (session["group_id"], session_id),
            ).fetchall()
            quota_items: list[dict[str, Any]] = []
            for row in policies:
                scope_column = (
                    "group_id"
                    if row["scope_type"] == "group"
                    else "session_id"
                )
                scope_value = (
                    session["group_id"]
                    if row["scope_type"] == "group"
                    else session_id
                )
                used = total(
                    scope_column,
                    str(scope_value),
                    int(row["window_seconds"]),
                )
                quota_items.append(
                    {
                        "id": row["id"],
                        "scope_type": row["scope_type"],
                        "scope_id": row["scope_id"],
                        "window_seconds": int(row["window_seconds"]),
                        "token_limit": int(row["token_limit"]),
                        "enabled": bool(row["enabled"]),
                        "used": used,
                        "remaining": max(
                            0,
                            int(row["token_limit"]) - used,
                        ),
                        "revision": int(row["revision"]),
                    }
                )
            by_type = [
                {
                    "request_type": row["request_type"],
                    "tokens": int(row["tokens"]),
                    "requests": int(row["requests"]),
                }
                for row in connection.execute(
                    """
                    SELECT request_type, SUM(total_tokens) AS tokens,
                           COUNT(*) AS requests
                    FROM token_usage
                    WHERE session_id = ? AND status = 'completed'
                    GROUP BY request_type
                    ORDER BY tokens DESC
                    """,
                    (session_id,),
                ).fetchall()
            ]
            return {
                "session_id": session_id,
                "group_id": session["group_id"],
                "session": {
                    "hour": total("session_id", session_id, 3600),
                    "day": total("session_id", session_id, 86400),
                    "all": total("session_id", session_id, None),
                },
                "group": {
                    "hour": total(
                        "group_id",
                        str(session["group_id"]),
                        3600,
                    ),
                    "day": total(
                        "group_id",
                        str(session["group_id"]),
                        86400,
                    ),
                    "all": total(
                        "group_id",
                        str(session["group_id"]),
                        None,
                    ),
                },
                "quotas": quota_items,
                "by_type": by_type,
            }

    async def group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._group_token_usage_summary,
            platform_id,
            group_id,
        )

    def _group_token_usage_summary(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT id FROM sessions
                WHERE platform_id = ? AND group_id = ?
                ORDER BY selected DESC, updated_at DESC
                LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        if not session:
            raise DatabaseNotFoundError("群会话不存在")
        usage = self._token_usage_summary(str(session["id"]))
        group_quota = next(
            (
                item
                for item in usage["quotas"]
                if item["scope_type"] == "group"
            ),
            None,
        )
        return {
            "platform_id": platform_id,
            "group_id": group_id,
            "session_id": str(session["id"]),
            "group": usage["group"],
            "quota": group_quota,
        }

    async def set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_token_quota,
            session_id,
            scope_type,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
        )

    def _set_token_quota(
        self,
        session_id: str,
        scope_type: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        scope_type = str(scope_type or "").strip().lower()
        if scope_type not in {"group", "session"}:
            raise ValueError("限额范围必须为群或副本")
        window_seconds = bounded_int(
            window_seconds,
            3600,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            100_000,
            1,
            1_000_000_000,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT group_id FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("副本不存在")
                scope_id = (
                    str(session["group_id"])
                    if scope_type == "group"
                    else session_id
                )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        scope_type,
                        scope_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.quota",
                    scope_type,
                    {
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._token_usage_summary(session_id)

    async def set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        *,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_group_token_quota,
            platform_id,
            group_id,
            window_seconds,
            token_limit,
            enabled,
            actor_id,
        )

    def _set_group_token_quota(
        self,
        platform_id: str,
        group_id: str,
        window_seconds: int,
        token_limit: int,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        platform_id = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        group_id = validate_platform_id(group_id, label="群 ID")
        window_seconds = bounded_int(
            window_seconds,
            86_400,
            60,
            365 * 24 * 60 * 60,
        )
        token_limit = bounded_int(
            token_limit,
            500_000,
            1,
            1_000_000_000,
        )
        session_id = ""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                    ORDER BY selected DESC, updated_at DESC
                    LIMIT 1
                    """,
                    (platform_id, group_id),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("群会话不存在")
                session_id = str(session["id"])
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO token_quota_policies(
                        id, scope_type, scope_id, window_seconds,
                        token_limit, enabled, revision, updated_by, updated_at
                    ) VALUES (?, 'group', ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(scope_type, scope_id) DO UPDATE SET
                        window_seconds = excluded.window_seconds,
                        token_limit = excluded.token_limit,
                        enabled = excluded.enabled,
                        revision = token_quota_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("quota"),
                        group_id,
                        window_seconds,
                        token_limit,
                        int(bool(enabled)),
                        actor_id,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "token.group_quota",
                    group_id,
                    {
                        "platform_id": platform_id,
                        "window_seconds": window_seconds,
                        "token_limit": token_limit,
                        "enabled": bool(enabled),
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._group_token_usage_summary(platform_id, group_id)

    async def record_provider_result(
        self,
        provider_id: str,
        *,
        success: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._record_provider_result,
            provider_id,
            success,
            reason,
        )

    def _record_provider_result(
        self,
        provider_id: str,
        success: bool,
        reason: str,
    ) -> dict[str, Any]:
        provider_id = clean_text(provider_id, max_chars=200)
        if not provider_id:
            return {}
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            failures = 0 if success else int(
                row["consecutive_failures"] if row else 0
            ) + 1
            status = "healthy"
            circuit_until = ""
            if not success and failures >= 3:
                status = "open"
                minutes = min(60, 5 * (2 ** min(3, failures - 3)))
                circuit_until = (
                    datetime.now(timezone.utc) + timedelta(minutes=minutes)
                ).isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO provider_health(
                    provider_id, status, consecutive_failures,
                    last_failure_reason, last_failure_at, last_success_at,
                    circuit_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider_id) DO UPDATE SET
                    status = excluded.status,
                    consecutive_failures = excluded.consecutive_failures,
                    last_failure_reason = excluded.last_failure_reason,
                    last_failure_at = excluded.last_failure_at,
                    last_success_at = excluded.last_success_at,
                    circuit_until = excluded.circuit_until,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    status,
                    failures,
                    "" if success else clean_text(reason, max_chars=500),
                    "" if success else now,
                    now if success else (row["last_success_at"] if row else ""),
                    circuit_until,
                    now,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            return dict(updated)

    async def filter_healthy_providers(
        self,
        provider_ids: Sequence[str],
    ) -> list[str]:
        return await self._run(
            self._filter_healthy_providers,
            list(provider_ids),
        )

    def _filter_healthy_providers(
        self,
        provider_ids: list[str],
    ) -> list[str]:
        normalized = list(
            dict.fromkeys(
                clean_text(item, max_chars=200)
                for item in provider_ids
                if clean_text(item, max_chars=200)
            )
        )
        if not normalized:
            return []
        now = datetime.now(timezone.utc)
        result: list[str] = []
        blocked: list[str] = []
        with self._connect() as connection:
            for provider_id in normalized:
                row = connection.execute(
                    "SELECT * FROM provider_health WHERE provider_id = ?",
                    (provider_id,),
                ).fetchone()
                if not row or row["status"] != "open":
                    result.append(provider_id)
                    continue
                try:
                    until = datetime.fromisoformat(row["circuit_until"])
                except (TypeError, ValueError):
                    until = now
                if until <= now:
                    connection.execute(
                        """
                        UPDATE provider_health
                        SET status = 'half_open', updated_at = ?
                        WHERE provider_id = ?
                        """,
                        (utc_now(), provider_id),
                    )
                    result.append(provider_id)
                else:
                    blocked.append(provider_id)
        return result or blocked[:1]

    async def list_provider_health(self) -> list[dict[str, Any]]:
        return await self._run(self._list_provider_health)

    def _list_provider_health(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM provider_health
                    ORDER BY
                        CASE status WHEN 'open' THEN 0
                                    WHEN 'half_open' THEN 1 ELSE 2 END,
                        updated_at DESC
                    """
                ).fetchall()
            ]

    async def record_configuration_revision(
        self,
        payload: Mapping[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._record_configuration_revision,
            dict(payload),
            actor_id,
        )

    def _record_configuration_revision(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        encoded = json_dump(payload)
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO configuration_revisions(
                    fingerprint, payload_json, saved_by, saved_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO NOTHING
                """,
                (fingerprint, encoded, actor_id, now),
            )
            row = connection.execute(
                """
                SELECT * FROM configuration_revisions
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            ).fetchone()
            latest = connection.execute(
                """
                SELECT MAX(id) AS latest_id FROM configuration_revisions
                """
            ).fetchone()
            return {
                "revision": row["id"],
                "latest_revision": latest["latest_id"] or row["id"],
                "fingerprint": fingerprint,
                "saved_by": row["saved_by"],
                "saved_at": row["saved_at"],
                "current": row["id"] == latest["latest_id"],
            }

    async def get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_operation_receipt,
            operation_id,
        )

    def _get_operation_receipt(
        self,
        operation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM operation_receipts
                WHERE operation_id = ? AND status = 'completed'
                """,
                (clean_text(operation_id, max_chars=240),),
            ).fetchone()
            if not row:
                return None
            return {
                "operation_id": row["operation_id"],
                "session_id": row["session_id"],
                "operation_type": row["operation_type"],
                "request": json_load(row["request_json"], {}),
                "result": json_load(row["result_json"], {}),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }

    async def lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: Mapping[str, Any],
        result_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._run(
            self._lock_check_result,
            operation_id,
            session_id,
            dict(request_payload),
            dict(result_payload),
        )

    def _lock_check_result(
        self,
        operation_id: str,
        session_id: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240)
        if not operation_id or not session_id:
            raise ValueError("检定操作 ID 与副本 ID 不能为空")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM operation_receipts
                    WHERE operation_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["session_id"] != session_id
                        or existing["operation_type"] != "dice_check"
                    ):
                        raise DatabaseConflictError("检定操作 ID 已被其他请求使用")
                    connection.execute("COMMIT")
                    return {
                        "operation_id": existing["operation_id"],
                        "session_id": existing["session_id"],
                        "operation_type": existing["operation_type"],
                        "request": json_load(existing["request_json"], {}),
                        "result": json_load(existing["result_json"], {}),
                        "status": existing["status"],
                        "created_at": existing["created_at"],
                        "updated_at": existing["updated_at"],
                    }
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, created_at,
                        updated_at
                    ) VALUES (?, ?, 'dice_check', ?, ?, 'completed', ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        json_dump(result_payload),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "operation_id": operation_id,
                    "session_id": session_id,
                    "operation_type": "dice_check",
                    "request": request_payload,
                    "result": result_payload,
                    "status": "completed",
                    "created_at": now,
                    "updated_at": now,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_roster(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_roster, session_id)

    def _list_roster(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    pt.*,
                    d.status AS draft_status,
                    d.current_step AS draft_step,
                    d.expires_at AS draft_expires_at,
                    d.fields_json AS draft_profile_json,
                    d.template_version AS draft_template_version,
                    (
                        SELECT c.code FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_code,
                    (
                        SELECT c.expires_at FROM card_binding_codes c
                        WHERE c.participant_id = pt.id
                          AND c.status = 'active'
                        ORDER BY c.created_at DESC LIMIT 1
                    ) AS binding_expires_at,
                    rs.state_json AS runtime_state_json,
                    rs.revision AS runtime_revision,
                    ccv.profile_json AS card_profile_json,
                    ccv.stats_json AS card_stats_json,
                    ccv.version_no AS card_version_no,
                    ccv.template_version AS card_template_version,
                    ccv.status AS card_version_status,
                    ccv.review_note AS card_review_note,
                    ccv.reviewed_by AS card_reviewed_by,
                    ccv.created_at AS card_version_created_at
                FROM participants pt
                LEFT JOIN character_card_drafts d
                  ON d.participant_id = pt.id
                LEFT JOIN character_runtime_states rs
                  ON rs.participant_id = pt.id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ?
                ORDER BY
                    CASE pt.participation_status
                        WHEN 'active' THEN 0
                        WHEN 'reserved' THEN 1
                        WHEN 'standby' THEN 2
                        WHEN 'away' THEN 3
                        WHEN 'retired' THEN 4
                        ELSE 5
                    END,
                    pt.created_at
                """,
                (session_id,),
            ).fetchall()
            return [self._participant(row) for row in rows]

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
        include_retired: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._get_participant,
            session_id,
            user_id,
            participant_ref,
            include_retired,
        )

    def _get_participant(
        self,
        session_id: str,
        user_id: str,
        participant_ref: str,
        include_retired: bool,
    ) -> dict[str, Any]:
        reference = str(participant_ref or "").strip()
        with self._connect() as connection:
            if user_id:
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家尚未加入当前副本")
                return self._participant(row)
            rows = connection.execute(
                """
                SELECT * FROM participants WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
            matches: list[sqlite3.Row] = []
            lowered = reference.casefold()
            for row in rows:
                if not include_retired and row["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    continue
                aliases = json_load(row["aliases_json"], [])
                names = {
                    str(row["id"]),
                    str(row["character_name"]),
                    str(row["character_code"]),
                    *(str(item) for item in aliases),
                }
                if any(item and item.casefold() == lowered for item in names):
                    matches.append(row)
            if not matches:
                raise DatabaseNotFoundError("未找到精确匹配的角色名或代号")
            if len(matches) > 1:
                raise ValueError("角色标识不唯一，请改用副本内唯一代号")
            return self._participant(matches[0])

    async def authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authoritative_modifier,
            session_id,
            user_id,
            stat_ref,
        )

    def _authoritative_modifier(
        self,
        session_id: str,
        user_id: str,
        stat_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.character_name, pt.display_name,
                       ccv.stats_json
                FROM participants pt
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if not row:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            stats = json_load(row["stats_json"], {})
            modifiers = stats.get("modifiers")
            labels = stats.get("labels")
            modifiers = modifiers if isinstance(modifiers, Mapping) else {}
            labels = labels if isinstance(labels, Mapping) else {}
            reference = clean_text(stat_ref, max_chars=40).casefold()
            matched_key = ""
            for key, label in labels.items():
                candidates = {
                    str(key).casefold(),
                    str(label).casefold(),
                    f"{label}检定".casefold(),
                }
                if reference in candidates:
                    matched_key = str(key)
                    break
            if not matched_key and reference in {
                str(key).casefold() for key in modifiers
            }:
                matched_key = next(
                    str(key)
                    for key in modifiers
                    if str(key).casefold() == reference
                )
            if not matched_key:
                return {
                    "stat": clean_text(stat_ref, max_chars=40) or "通用",
                    "modifier": 0,
                    "matched": False,
                }
            return {
                "stat": str(labels.get(matched_key) or matched_key),
                "modifier": max(
                    -10,
                    min(10, int(modifiers.get(matched_key, 0))),
                ),
                "matched": True,
            }

    @staticmethod
    def _active_ban_for(
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        user_id: str,
    ) -> sqlite3.Row | None:
        now = utc_now()
        connection.execute(
            """
            UPDATE ban_records SET status = 'expired', updated_at = ?
            WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
            """,
            (now, now),
        )
        return connection.execute(
            """
            SELECT * FROM ban_records
            WHERE user_id = ? AND status = 'active'
              AND (
                    scope = 'global'
                 OR (scope = 'group' AND platform_id = ? AND group_id = ?)
                 OR (scope = 'instance' AND session_id = ?)
              )
            ORDER BY
                CASE scope
                    WHEN 'global' THEN 0
                    WHEN 'group' THEN 1
                    ELSE 2
                END
            LIMIT 1
            """,
            (
                user_id,
                session["platform_id"],
                session["group_id"],
                session["id"],
            ),
        ).fetchone()

    async def reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reserve_participant,
            session_id,
            user_id,
            display_name,
        )

    def _reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> dict[str, Any]:
        user_id = validate_platform_id(user_id, label="用户 ID")
        display_name = clean_text(display_name, max_chars=100) or user_id
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有准备大厅开放加入；请先由主持人开启副本"
                    )
                ban = self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                )
                if ban:
                    reason = str(ban["reason"] or "未注明")
                    raise PermissionError(f"当前无法加入：{reason}")

                existing = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing and existing["participation_status"] not in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    code_row = connection.execute(
                        """
                        SELECT * FROM card_binding_codes
                        WHERE participant_id = ? AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (existing["id"],),
                    ).fetchone()
                    connection.execute("COMMIT")
                    result = self._participant(existing)
                    result["joined"] = False
                    result["binding_code"] = (
                        code_row["code"] if code_row else ""
                    )
                    result["binding_expires_at"] = (
                        code_row["expires_at"] if code_row else ""
                    )
                    return result
                if existing:
                    raise ValueError(
                        "该角色已经正式退场；请使用 /酒馆 申请返场"
                    )

                config_row = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not config_row:
                    raise DatabaseNotFoundError("副本配置不存在")
                world = json_load(config_row["world_snapshot_json"], {})
                limits = player_limits(world)
                placeholders = ",".join(
                    "?" for _ in SEAT_HOLDING_STATUSES
                )
                occupied = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM participants
                    WHERE session_id = ?
                      AND participation_status IN ({placeholders})
                    """,
                    (session_id, *sorted(SEAT_HOLDING_STATUSES)),
                ).fetchone()[0]
                if occupied >= limits["maximum"]:
                    raise ValueError(
                        f"当前副本已满（{occupied}/{limits['maximum']}）"
                    )

                now = utc_now()
                player = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not player:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '', '{}', 1, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            now,
                            now,
                        ),
                    )
                else:
                    player_id = player["id"]
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, enabled = 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (display_name, now, player_id),
                    )

                participant_id = new_id("participant")
                connection.execute(
                    """
                    INSERT INTO participants(
                        id, session_id, player_id, group_user_id,
                        display_name, aliases_json, card_status, ready,
                        participation_status, seat_reserved_at, joined_round,
                        consecutive_timeouts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, '[]', 'uncreated', 0,
                              'reserved', ?, 1, 0, ?, ?)
                    """,
                    (
                        participant_id,
                        session_id,
                        player_id,
                        user_id,
                        display_name,
                        now,
                        now,
                        now,
                    ),
                )
                template = card_template(world)
                time_rules = normalize_time_rules(
                    json_load(config_row["time_rules_json"], {})
                )
                draft_id = new_id("draft")
                draft_expires_at = deadline_after(
                    time_rules["card_draft_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO character_card_drafts(
                        id, participant_id, template_version, fields_json,
                        current_step, status, expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, '{}', 0, 'active', ?, ?, ?)
                    """,
                    (
                        draft_id,
                        participant_id,
                        template["version"],
                        draft_expires_at,
                        now,
                        now,
                    ),
                )
                code = ""
                for _ in range(20):
                    candidate = secrets.token_hex(3).upper()
                    if not connection.execute(
                        "SELECT 1 FROM card_binding_codes WHERE code = ?",
                        (candidate,),
                    ).fetchone():
                        code = candidate
                        break
                if not code:
                    raise RuntimeError("无法生成唯一建卡码")
                code_expires_at = deadline_after(
                    time_rules["card_code_ttl_seconds"]
                )
                connection.execute(
                    """
                    INSERT INTO card_binding_codes(
                        id, participant_id, code, status, expires_at,
                        created_at
                    ) VALUES (?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        new_id("cardcode"),
                        participant_id,
                        code,
                        code_expires_at,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_code",
                    timeout_seconds=time_rules["card_code_ttl_seconds"],
                    reminder_seconds=None,
                    action={"code": code},
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    timer_type="card_completion",
                    timeout_seconds=time_rules[
                        "card_completion_timeout_seconds"
                    ],
                    reminder_seconds=None,
                    action={
                        "timeout_action": time_rules[
                            "card_timeout_action"
                        ]
                    },
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.reserve",
                    participant_id,
                    {
                        "occupied": occupied + 1,
                        "maximum": limits["maximum"],
                    },
                )
                row = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result.update(
                    {
                        "joined": True,
                        "binding_code": code,
                        "binding_expires_at": code_expires_at,
                        "limits": limits,
                    }
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _create_timer(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        timer_type: str,
        timeout_seconds: int | None,
        reminder_seconds: int | None,
        action: Mapping[str, Any],
    ) -> str:
        timer_id = new_id("timer")
        now_dt = datetime.now(timezone.utc)
        action_payload = dict(action)
        reminder_interval = timer_reminder_interval(timer_type)
        # 先作废同范围内仍存活的旧计时器，保证「一个范围一个计时器」。
        # 缺少这一步时，重复的 继续 / 读档 / 回合推进 会让 timer_instances
        # 里堆积多条 active 行，process_due_timers 便会一次性吐出多条提醒。
        supersede_sql = """
            UPDATE timer_instances
            SET status = 'cancelled', deadline_at = '',
                reminder_at = '', reminder_sent = 0, updated_at = ?
            WHERE session_id = ? AND timer_type = ?
              AND status IN ('active', 'paused')
        """
        supersede_args: tuple[Any, ...] = (
            now_dt.isoformat(timespec="seconds"),
            session_id,
            timer_type,
        )
        if timer_type not in SESSION_SINGLETON_TIMER_TYPES:
            supersede_sql += " AND participant_id = ?"
            supersede_args += (participant_id,)
        connection.execute(supersede_sql, supersede_args)
        policy = connection.execute(
            """
            SELECT global_enabled, switches_json FROM timer_policies
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        switches = json_load(
            policy["switches_json"] if policy else "",
            {},
        )
        switches = switches if isinstance(switches, Mapping) else {}
        countdown_enabled = bool(
            (policy["global_enabled"] if policy else 1)
            and switches.get(timer_type, True)
        )
        if not countdown_enabled:
            action_payload["paused_by_policy"] = True
        if timer_type == "card_completion":
            action_payload.setdefault("reminder_enabled", True)
            action_payload["reminder_interval_seconds"] = (
                CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
            )
        deadline = (
            now_dt + timedelta(seconds=timeout_seconds)
            if timeout_seconds is not None
            else None
        )
        # Keep ``reminder_seconds`` in the call signature for compatibility
        # with existing time-rule payloads. Role-card creation has its own
        # private two-minute cadence; other player timers remain at 30 seconds.
        reminder = (
            now_dt + timedelta(seconds=reminder_interval)
            if deadline is not None
            and timeout_seconds > reminder_interval
            and timer_reminder_enabled(timer_type, action_payload)
            else None
        )
        status = "active" if countdown_enabled else "paused"
        now = now_dt.isoformat(timespec="seconds")
        connection.execute(
            """
            INSERT INTO timer_instances(
                id, session_id, participant_id, timer_type, status,
                deadline_at, remaining_seconds, reminder_at,
                reminder_sent, action_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                timer_id,
                session_id,
                participant_id,
                timer_type,
                status,
                (
                    deadline.isoformat(timespec="seconds")
                    if deadline is not None and countdown_enabled
                    else ""
                ),
                timeout_seconds,
                (
                    reminder.isoformat(timespec="seconds")
                    if reminder is not None and countdown_enabled
                    else ""
                ),
                json_dump(action_payload),
                now,
                now,
            ),
        )
        return timer_id

    def _start_standby_timer(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
    ) -> str | None:
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        timeout_seconds = rules["standby_timeout_seconds"]
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'standby'
              AND status IN ('active', 'paused')
            """,
            (utc_now(), session_id, participant_id),
        )
        if timeout_seconds is None:
            return None
        return self._create_timer(
            connection,
            session_id=session_id,
            participant_id=participant_id,
            timer_type="standby",
            timeout_seconds=timeout_seconds,
            reminder_seconds=None,
            action={"reason": "standby_timeout"},
        )

    async def bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._bind_card_code,
            code,
            private_user_id,
            private_origin,
        )

    def _bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]:
        normalized_code = str(code or "").strip().upper()
        private_user_id = validate_platform_id(
            private_user_id,
            label="私聊用户 ID",
        )
        private_origin = clean_text(private_origin, max_chars=500)
        if not normalized_code:
            raise ValueError("建卡码不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                code_row = connection.execute(
                    """
                    SELECT c.*, pt.session_id
                    FROM card_binding_codes c
                    JOIN participants pt ON pt.id = c.participant_id
                    WHERE c.code = ?
                    """,
                    (normalized_code,),
                ).fetchone()
                if not code_row or code_row["status"] != "active":
                    raise ValueError("建卡码不存在或已使用")
                if code_row["expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE card_binding_codes
                        SET status = 'expired' WHERE id = ?
                        """,
                        (code_row["id"],),
                    )
                    raise ValueError("建卡码已过期，请回群重新发送 /酒馆 加入")

                conflict = connection.execute(
                    """
                    SELECT pt.id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND pt.id <> ?
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                      AND pt.participation_status
                          NOT IN ('retired', 'archived')
                    LIMIT 1
                    """,
                    (private_origin, code_row["participant_id"]),
                ).fetchone()
                if conflict:
                    raise ValueError(
                        "当前私聊还有另一张未完成的角色卡；"
                        "请先完成或取消后再绑定"
                    )

                connection.execute(
                    """
                    UPDATE participants SET
                        private_user_id = ?, private_origin = ?,
                        card_status = CASE
                            WHEN card_status = 'uncreated' THEN 'draft'
                            ELSE card_status
                        END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["participant_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes SET
                        status = 'used', private_user_id = ?,
                        private_origin = ?, used_at = ?
                    WHERE id = ?
                    """,
                    (
                        private_user_id,
                        private_origin,
                        now,
                        code_row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'card_code'
                      AND status = 'active'
                    """,
                    (now, code_row["participant_id"]),
                )
                self._insert_audit(
                    connection,
                    code_row["session_id"],
                    private_user_id,
                    "card.bind_private",
                    code_row["participant_id"],
                    {"private_origin_recorded": bool(private_origin)},
                )
                row = connection.execute(
                    """
                    SELECT pt.*, d.current_step AS draft_step,
                           d.status AS draft_status,
                           d.expires_at AS draft_expires_at
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    WHERE pt.id = ?
                    """,
                    (code_row["participant_id"],),
                ).fetchone()
                config = connection.execute(
                    """
                    SELECT world_snapshot_json FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (code_row["session_id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(row)
                result["template"] = card_template(
                    json_load(config["world_snapshot_json"], {})
                )
                result["world"] = json_load(
                    config["world_snapshot_json"], {}
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._card_draft_for_private,
            private_origin,
        )

    def _card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.*, d.fields_json, d.current_step,
                       d.status AS draft_status,
                       d.expires_at AS draft_expires_at,
                       ic.world_snapshot_json
                FROM participants pt
                JOIN character_card_drafts d
                  ON d.participant_id = pt.id
                JOIN instance_configs ic
                  ON ic.session_id = pt.session_id
                JOIN sessions s ON s.id = pt.session_id
                WHERE pt.private_origin = ? AND d.status = 'active'
                  AND s.state <> 'finished'
                ORDER BY d.updated_at DESC LIMIT 1
                """,
                (private_origin,),
            ).fetchone()
            if not row:
                return None
            result = self._participant(row)
            result["fields"] = json_load(row["fields_json"], {})
            result["current_step"] = row["current_step"]
            result["template"] = card_template(
                json_load(row["world_snapshot_json"], {})
            )
            result["world"] = json_load(row["world_snapshot_json"], {})
            return result

    async def set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_card_completion_reminder,
            private_origin,
            enabled,
        )

    def _set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        private_origin = clean_text(private_origin, max_chars=500)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT t.*, pt.private_user_id
                    FROM timer_instances t
                    JOIN participants pt ON pt.id = t.participant_id
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ?
                      AND t.timer_type = 'card_completion'
                      AND t.status IN ('active', 'paused')
                      AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY t.created_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有进行中的角色卡创建倒计时"
                    )

                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, dict):
                    payload = {}
                current_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                desired = (
                    current_enabled if enabled is None else bool(enabled)
                )
                reminder_at = str(row["reminder_at"] or "")
                deadline_text = str(row["deadline_at"] or "")
                if enabled is not None:
                    payload["reminder_enabled"] = desired
                    payload["reminder_interval_seconds"] = (
                        CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                    )
                    if row["status"] == "active" and deadline_text:
                        deadline = datetime.fromisoformat(deadline_text)
                        if desired:
                            next_reminder = now_dt + timedelta(
                                seconds=(
                                    CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                                )
                            )
                            reminder_at = (
                                next_reminder.isoformat(timespec="seconds")
                                if next_reminder < deadline
                                else ""
                            )
                        else:
                            # Keep the expiry event active while preventing
                            # periodic reminder scans before the deadline.
                            reminder_at = deadline_text
                    else:
                        reminder_at = ""
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET action_json = ?, reminder_at = ?,
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(payload),
                            reminder_at,
                            now,
                            row["id"],
                        ),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        str(row["private_user_id"] or ""),
                        "card.reminder_toggle",
                        row["participant_id"],
                        {"enabled": desired},
                    )

                remaining_seconds: int | None
                if row["status"] == "active" and deadline_text:
                    remaining_seconds = max(
                        0,
                        int(
                            (
                                datetime.fromisoformat(deadline_text)
                                - now_dt
                            ).total_seconds()
                        ),
                    )
                elif row["status"] == "paused":
                    remaining_seconds = max(
                        0,
                        int(row["remaining_seconds"] or 0),
                    )
                else:
                    remaining_seconds = None
                connection.execute("COMMIT")
                return {
                    "timer_id": row["id"],
                    "session_id": row["session_id"],
                    "participant_id": row["participant_id"],
                    "enabled": desired,
                    "status": row["status"],
                    "remaining_seconds": remaining_seconds,
                    "has_deadline": bool(
                        deadline_text
                        or (
                            row["status"] == "paused"
                            and row["remaining_seconds"] is not None
                        )
                    ),
                    "next_reminder_at": reminder_at,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise


    async def fill_card_draft(
        self,
        private_origin: str,
        value: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._fill_card_draft,
            private_origin,
            value,
        )

    def _allowed_option_values(
        self, definition: Mapping[str, Any]
    ) -> set[str]:
        result: set[str] = set()
        for option in definition.get("options") or []:
            if isinstance(option, Mapping):
                value = option.get("value")
            else:
                value = option
            text = str(value or "")
            if text:
                result.add(text)
        return result

    def _fill_card_draft(
        self,
        private_origin: str,
        value: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有进行中的建卡流程")
                now = utc_now()
                if row["draft_expires_at"] and row["draft_expires_at"] <= now:
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                template = card_template(
                    json_load(row["world_snapshot_json"], {})
                )
                fields_def = template["fields"]
                step = min(max(0, int(row["current_step"])), len(fields_def))
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                # Repair legacy drafts that still carry hand-filled stat_* fields
                # (doc §7): recompute from profession + primary/secondary and fix
                # the cursor to the first non-attribute field.
                fields, step = repair_profession_preset_draft(
                    template, fields, step
                )
                if step >= len(fields_def):
                    raise ValueError("所有字段已填写，请发送 /酒馆 确认建卡")
                definition = fields_def[step]
                text = clean_card_field(
                    value,
                    label=str(definition["label"]),
                    max_chars=int(definition["max_chars"]),
                )
                if definition["required"] and not text:
                    raise ValueError(f"{definition['label']}不能为空")
                stored_value: Any = text
                if definition.get("type") == "integer":
                    try:
                        stored_value = int(text)
                    except ValueError as exc:
                        raise ValueError(
                            f"{definition['label']}必须填写整数"
                        ) from exc
                    minimum = int(definition.get("minimum", -100))
                    maximum = int(definition.get("maximum", 100))
                    allocation = card_stat_allocation(
                        template,
                        fields,
                        step,
                    )
                    current_stat = allocation.get("current")
                    if isinstance(current_stat, Mapping):
                        maximum = int(
                            current_stat["effective_maximum"]
                        )
                        if maximum < minimum:
                            raise ValueError(
                                "当前属性预算无法满足模板最低值，"
                                "请让管理员检查角色卡模板"
                            )
                    if not minimum <= stored_value <= maximum:
                        suffix = ""
                        if isinstance(current_stat, Mapping):
                            suffix = (
                                f"（总预算 {allocation['budget']}，"
                                f"已使用 {current_stat['used_before']}，"
                                f"后续至少预留 "
                                f"{current_stat['reserved_minimum']}）"
                            )
                        raise ValueError(
                            f"{definition['label']}当前必须在 "
                            f"{minimum}—{maximum} 之间{suffix}"
                        )
                _allowed = self._allowed_option_values(definition)
                if _allowed and str(stored_value) not in _allowed:
                    raise ValueError(
                        f"{definition['label']}必须从提示中的预设项选择"
                    )
                profession_mode = uses_profession_preset_stats(template)
                field_key = str(definition["key"])
                if profession_mode and field_key.startswith("stat_"):
                    raise ValueError(
                        "本世界不使用大项/小项自由分配，"
                        "请选择主属性+7与副属性+3。"
                    )
                fields[definition["key"]] = stored_value
                if profession_mode and field_key == "profession":
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    fields.pop("primary_attribute", None)
                    fields.pop("secondary_attribute", None)
                elif profession_mode and field_key == "primary_attribute":
                    if fields.get("secondary_attribute") == fields.get(
                        "primary_attribute"
                    ):
                        fields.pop("secondary_attribute", None)
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                elif profession_mode and field_key == "secondary_attribute":
                    if fields.get("primary_attribute") == fields.get(
                        "secondary_attribute"
                    ):
                        raise ValueError("副属性不能与主属性相同")
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=True
                    )
                    for _k, _v in resolved["raw"].items():
                        fields[f"stat_{_k}"] = _v
                    fields["resolved_stat_total"] = int(resolved["effective_total"])
                if profession_mode:
                    next_step = next_fillable_card_step(
                        template, fields_def, step + 1
                    )
                else:
                    stat_values = [
                        int(fields[f"stat_{item['key']}"])
                        for item in template["stats"]["attributes"]
                        if f"stat_{item['key']}" in fields
                    ]
                    if (
                        len(stat_values)
                        == len(template["stats"]["attributes"])
                        and sum(stat_values)
                        > int(template["stats"]["budget"])
                    ):
                        raise ValueError(
                            f"属性总值 {sum(stat_values)} 超过预算 "
                            f"{template['stats']['budget']}，"
                            "请重新建卡或调整模板"
                        )
                    next_step = step + 1
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        next_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.field_update",
                    row["id"],
                    {
                        "field": definition["key"],
                        "step": next_step,
                    },
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": next_step,
                    "complete": next_step >= len(fields_def),
                    "world": json_load(row["world_snapshot_json"], {}),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._reset_card_draft_stats,
            private_origin,
        )

    def _reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           d.expires_at AS draft_expires_at,
                           ic.world_snapshot_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError(
                        "当前私聊没有可调整的角色卡"
                    )
                now = utc_now()
                if (
                    row["draft_expires_at"]
                    and row["draft_expires_at"] <= now
                ):
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'expired', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["draft_id"]),
                    )
                    raise ValueError("角色卡草稿已过期，请回群重新申请")
                template = card_template(
                    json_load(row["world_snapshot_json"], {})
                )
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                world_obj = json_load(row["world_snapshot_json"], {})
                if uses_profession_preset_stats(template):
                    profession_name = str(fields.get("profession") or "")
                    if not profession_name:
                        raise ValueError("当前角色还没有选择职业")
                    # Keep profession, base stats and all text fields; only clear
                    # the primary/secondary choices and the derived total.
                    fields.pop("primary_attribute", None)
                    fields.pop("secondary_attribute", None)
                    fields.pop("resolved_stat_total", None)
                    resolved = resolve_profession_stats(
                        template, fields, require_complete=False
                    )
                    fields["profession_base_stats"] = resolved["base"]
                    for _k, _v in resolved["base"].items():
                        fields[f"stat_{_k}"] = _v
                    primary_step = next(
                        index
                        for index, _d in enumerate(
                            template.get("fields") or []
                        )
                        if isinstance(_d, Mapping)
                        and str(_d.get("key") or "") == "primary_attribute"
                    )
                    target_step = primary_step
                    connection.execute(
                        """
                        UPDATE character_card_drafts SET
                            fields_json = ?, current_step = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(fields), target_step, now, row["draft_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE participants
                        SET card_status = 'draft', ready = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        row["session_id"],
                        row["private_user_id"],
                        "card.stats_reset",
                        row["id"],
                        {
                            "profession_reset": True,
                            "profession": profession_name,
                        },
                    )
                    connection.execute("COMMIT")
                    return {
                        "participant_id": row["id"],
                        "session_id": row["session_id"],
                        "fields": fields,
                        "template": template,
                        "current_step": target_step,
                        "complete": False,
                        "profession_reset": True,
                        "profession": profession_name,
                        "base_stats": dict(resolved["base"]),
                        "world": world_obj,
                    }
                allocation = card_stat_allocation(template, fields)
                stat_fields = allocation["stat_fields"]
                if not stat_fields:
                    raise ValueError("当前角色卡模板没有可分配数值")
                first_step = int(allocation["first_step"])
                has_stat_values = any(
                    item["field_key"] in fields
                    for item in stat_fields
                )
                if (
                    int(row["current_step"]) < first_step
                    and not has_stat_values
                ):
                    raise ValueError("尚未开始填写角色数值")
                removed = []
                for item in stat_fields:
                    field_key = str(item.get("field_key") or "")
                    if field_key in fields:
                        removed.append(field_key)
                        fields.pop(field_key, None)
                connection.execute(
                    """
                    UPDATE character_card_drafts SET
                        fields_json = ?, current_step = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(fields),
                        first_step,
                        now,
                        row["draft_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET card_status = 'draft', ready = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.stats_reset",
                    row["id"],
                    {"removed_fields": removed},
                )
                connection.execute("COMMIT")
                return {
                    "participant_id": row["id"],
                    "session_id": row["session_id"],
                    "fields": fields,
                    "template": template,
                    "current_step": first_step,
                    "complete": False,
                    "world": json_load(row["world_snapshot_json"], {}),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def preview_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        draft = await self.card_draft_for_private(private_origin)
        if not draft:
            raise DatabaseNotFoundError("当前私聊没有进行中的角色卡")
        return draft

    async def confirm_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._confirm_card_draft,
            private_origin,
        )

    def _confirm_card_draft(
        self,
        private_origin: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id, d.fields_json,
                           d.current_step, d.status AS draft_status,
                           ic.world_snapshot_json, ic.time_rules_json
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN instance_configs ic
                      ON ic.session_id = pt.session_id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    ORDER BY d.updated_at DESC LIMIT 1
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前私聊没有可确认的角色卡")
                template = card_template(
                    json_load(row["world_snapshot_json"], {})
                )
                fields = json_load(row["fields_json"], {})
                if not isinstance(fields, dict):
                    fields = {}
                fields.pop("_alloc", None)
                for definition in template["fields"]:
                    key = str(definition["key"])
                    if key not in fields:
                        continue
                    clean_card_field(
                        fields[key],
                        label=str(definition["label"]),
                        max_chars=(
                            12
                            if key in {"name", "code"}
                            else int(definition["max_chars"])
                        ),
                    )
                missing = [
                    item["label"]
                    for item in template["fields"]
                    if item["required"] and not str(
                        fields.get(item["key"], "")
                    ).strip()
                ]
                if missing:
                    raise ValueError("尚未填写：" + "、".join(missing))
                character_name = clean_card_field(
                    fields.get("name") or row["display_name"],
                    label="角色姓名",
                    max_chars=12,
                )
                character_code = clean_card_field(
                    fields.get("code") or character_name,
                    label="副本代号",
                    max_chars=12,
                )
                if not character_name or not character_code:
                    raise ValueError("角色姓名与副本代号不能为空")
                duplicate = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND id <> ?
                      AND participation_status NOT IN ('retired', 'archived')
                      AND (
                           lower(character_name) = lower(?)
                        OR lower(character_code) = lower(?)
                      )
                    LIMIT 1
                    """,
                    (
                        row["session_id"],
                        row["id"],
                        character_name,
                        character_code,
                    ),
                ).fetchone()
                if duplicate:
                    raise ValueError("角色姓名或副本代号已被使用")
                stat_definition = template["stats"]
                if uses_profession_preset_stats(template):
                    resolved_stats = resolve_profession_stats(
                        template,
                        fields,
                        require_complete=True,
                    )
                    for key, expected_value in resolved_stats[
                        "raw"
                    ].items():
                        actual_value = int(
                            fields.get(f"stat_{key}", -999)
                        )
                        if actual_value != expected_value:
                            raise ValueError(
                                f"{resolved_stats['labels'][key]}"
                                "数值与职业基础属性及主副属性加成不一致，"
                                "请使用「重填数值」重新生成"
                            )
                    final_total = int((stat_definition.get("total_validation") or {}).get("final_total", stat_definition.get("budget", 0)))
                    if int(resolved_stats["effective_total"]) != final_total:
                        raise ValueError(f"角色最终属性总和必须为{final_total}")
                    resolved_stats["budget"] = final_total
                    fields["profession_base_stats"] = dict(
                        resolved_stats["base"]
                    )
                    fields["resolved_stat_total"] = int(
                        resolved_stats["effective_total"]
                    )
                else:
                    raw_stats: dict[str, int] = {}
                    labels: dict[str, str] = {}
                    modifiers: dict[str, int] = {}
                    for attribute in stat_definition["attributes"]:
                        key = str(attribute["key"])
                        value = int(
                            fields.get(
                                f"stat_{key}",
                                attribute.get("default", 0),
                            )
                        )
                        if not int(attribute["minimum"]) <= value <= int(
                            attribute["maximum"]
                        ):
                            raise ValueError(
                                f"{attribute['label']}超出模板允许范围"
                            )
                        raw_stats[key] = value
                        labels[key] = str(attribute["label"])
                        modifiers[key] = int(
                            stat_definition["modifier_table"].get(
                                str(value),
                                0,
                            )
                        )
                    allocation = card_stat_allocation(template, fields)
                    if not allocation.get("total_ok", True):
                        rule = allocation.get("allocation_rule", "maximum")
                        if rule == "exact": raise ValueError("角色属性总值必须刚好等于世界模板预算")
                        if rule == "range": raise ValueError("角色属性总值不在允许区间")
                        raise ValueError("角色属性总值超过世界模板预算")
                    resolved_stats = {
                        "raw": raw_stats,
                        "labels": labels,
                        "modifiers": modifiers,
                        "budget": int(stat_definition["budget"]),
                        "modifier_table": dict(
                            stat_definition["modifier_table"]
                        ),
                    }
                now = utc_now()
                card_id = row["character_card_id"] or new_id("pcard")
                existing_card = connection.execute(
                    "SELECT * FROM character_cards WHERE id = ?",
                    (card_id,),
                ).fetchone()
                if not existing_card:
                    version_no = 1
                    connection.execute(
                        """
                        INSERT INTO character_cards(
                            id, owner_user_id, world_id, display_name,
                            archived, deleted, current_version,
                            created_at, updated_at
                        )
                        SELECT ?, ?, s.world_id, ?, 0, 0, 1, ?, ?
                        FROM sessions s WHERE s.id = ?
                        """,
                        (
                            card_id,
                            row["group_user_id"],
                            character_name,
                            now,
                            now,
                            row["session_id"],
                        ),
                    )
                else:
                    version_no = int(existing_card["current_version"]) + 1
                    connection.execute(
                        """
                        UPDATE character_cards SET
                            display_name = ?, current_version = ?,
                            archived = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (character_name, version_no, now, card_id),
                    )
                status = (
                    CARD_APPROVED
                    if template["auto_approve"]
                    else CARD_PENDING
                )
                version_id = new_id("pcardv")
                connection.execute(
                    """
                    INSERT INTO character_card_versions(
                        id, character_card_id, version_no, template_version,
                        profile_json, stats_json, status, review_note,
                        reviewed_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                    """,
                    (
                        version_id,
                        card_id,
                        version_no,
                        template["version"],
                        json_dump(fields),
                        json_dump(resolved_stats),
                        status,
                        "system" if status == CARD_APPROVED else "",
                        now,
                    ),
                )
                participation_status = (
                    PARTICIPANT_ACTIVE
                    if status == CARD_APPROVED
                    else PARTICIPANT_RESERVED
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        character_card_id = ?, character_version_id = ?,
                        character_name = ?, character_code = ?,
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        card_id,
                        version_id,
                        character_name,
                        character_code,
                        status,
                        participation_status,
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'confirmed', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE players SET
                        character_name = ?, profile_json = ?,
                        enabled = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        character_name,
                        json_dump(fields),
                        now,
                        row["player_id"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO character_runtime_states(
                        id, session_id, participant_id, character_card_id,
                        state_json, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '{}', 1, ?, ?)
                    ON CONFLICT(session_id, participant_id) DO UPDATE SET
                        character_card_id = excluded.character_card_id,
                        revision = revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("runtime"),
                        row["session_id"],
                        row["id"],
                        card_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ?
                      AND timer_type = 'card_completion'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if status == CARD_APPROVED:
                    time_rules = normalize_time_rules(
                        json_load(row["time_rules_json"], {})
                    )
                    self._create_timer(
                        connection,
                        session_id=row["session_id"],
                        participant_id=row["id"],
                        timer_type="ready",
                        timeout_seconds=time_rules["ready_timeout_seconds"],
                        reminder_seconds=None,
                        action={
                            "timeout_action": time_rules[
                                "ready_timeout_action"
                            ]
                        },
                    )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.confirm",
                    row["id"],
                    {
                        "version": version_no,
                        "status": status,
                    },
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["auto_approved"] = status == CARD_APPROVED
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cancel_card_draft(
        self,
        private_origin: str,
    ) -> None:
        await self._run(self._cancel_card_draft, private_origin)

    def _cancel_card_draft(self, private_origin: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT pt.*, d.id AS draft_id
                    FROM participants pt
                    JOIN character_card_drafts d
                      ON d.participant_id = pt.id
                    JOIN sessions s ON s.id = pt.session_id
                    WHERE pt.private_origin = ? AND d.status = 'active'
                      AND s.state <> 'finished'
                    """,
                    (private_origin,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("当前没有进行中的角色卡")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["draft_id"]),
                )
                connection.execute(
                    """
                    UPDATE participants
                    SET participation_status = 'archived',
                        card_status = 'uncreated', ready = 0,
                        exit_reason = 'cancelled_card', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE card_binding_codes
                    SET status = 'expired'
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (row["id"],),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    row["private_user_id"],
                    "card.cancel",
                    row["id"],
                    {},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._review_character_card,
            session_id,
            participant_ref,
            approved,
            actor_id,
            note,
        )

    def _review_character_card(
        self,
        session_id: str,
        participant_ref: str,
        approved: bool,
        actor_id: str,
        note: str,
    ) -> dict[str, Any]:
        participant = self._get_participant(
            session_id,
            "",
            participant_ref,
            True,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                if not row or not row["character_version_id"]:
                    raise ValueError("该玩家尚未提交角色卡")
                status = CARD_APPROVED if approved else CARD_REJECTED
                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_versions SET
                        status = ?, review_note = ?, reviewed_by = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        clean_text(note, max_chars=500),
                        actor_id,
                        row["character_version_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE participants SET
                        card_status = ?, ready = 0,
                        participation_status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        (
                            PARTICIPANT_ACTIVE
                            if approved
                            else PARTICIPANT_RESERVED
                        ),
                        now,
                        row["id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status IN ('active', 'paused')
                    """,
                    (now, row["id"]),
                )
                if approved:
                    config = connection.execute(
                        """
                        SELECT time_rules_json FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    time_rules = normalize_time_rules(
                        json_load(
                            config["time_rules_json"] if config else "",
                            {},
                        )
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id=row["id"],
                        timer_type="ready",
                        timeout_seconds=time_rules["ready_timeout_seconds"],
                        reminder_seconds=None,
                        action={
                            "timeout_action": time_rules[
                                "ready_timeout_action"
                            ]
                        },
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "card.review",
                    row["id"],
                    {"approved": approved, "note": note[:500]},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool = True,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_ready,
            session_id,
            user_id,
            ready,
        )

    async def force_all_ready(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._force_all_ready,
            session_id,
            actor_id,
        )

    def _force_all_ready(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session or session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError(
                        "只有准备大厅可以强制全员准备"
                    )
                now = utc_now()
                eligible = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND card_status = 'approved'
                      AND participation_status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                ids = [str(row["id"]) for row in eligible]
                if ids:
                    placeholders = ",".join("?" for _ in ids)
                    connection.execute(
                        f"""
                        UPDATE participants SET ready = 1, updated_at = ?
                        WHERE id IN ({placeholders})
                        """,
                        (now, *ids),
                    )
                    connection.execute(
                        f"""
                        UPDATE timer_instances
                        SET status = 'completed', deadline_at = '',
                            reminder_at = '', updated_at = ?
                        WHERE participant_id IN ({placeholders})
                          AND timer_type = 'ready'
                          AND status IN ('active', 'paused')
                        """,
                        (now, *ids),
                    )
                skipped = connection.execute(
                    """
                    SELECT display_name, character_name, card_status,
                           participation_status
                    FROM participants
                    WHERE session_id = ?
                      AND NOT (
                        card_status = 'approved'
                        AND participation_status = 'active'
                      )
                      AND participation_status NOT IN ('retired', 'archived')
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "participant.force_ready_all",
                    session_id,
                    {
                        "ready_count": len(ids),
                        "skipped_count": len(skipped),
                    },
                )
                connection.execute("COMMIT")
                return {
                    "session_id": session_id,
                    "ready_count": len(ids),
                    "skipped": [
                        {
                            "name": row["character_name"]
                            or row["display_name"],
                            "card_status": row["card_status"],
                            "participation_status": row[
                                "participation_status"
                            ],
                        }
                        for row in skipped
                    ],
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError("只能在准备大厅确认准备")
                row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if row["card_status"] != CARD_APPROVED:
                    raise ValueError("角色卡尚未通过审核")
                if row["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能进入本次阵容")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        ready = ?, participation_status = 'active',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (int(bool(ready)), now, row["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'completed', updated_at = ?
                    WHERE participant_id = ? AND timer_type = 'ready'
                      AND status = 'active'
                    """,
                    (now, row["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.ready",
                    row["id"],
                    {"ready": bool(ready)},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def opening_preflight(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._opening_preflight, session_id)

    def _opening_preflight(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("会话不存在")
            config = connection.execute(
                """
                SELECT * FROM instance_configs WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if not config:
                raise DatabaseNotFoundError("副本配置不存在")
            world = json_load(config["world_snapshot_json"], {})
            limits = player_limits(world)
            rows = connection.execute(
                """
                SELECT * FROM participants
                WHERE session_id = ?
                  AND participation_status IN (
                      'reserved', 'active', 'standby', 'away'
                  )
                ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            blockers: list[str] = []
            active: list[dict[str, Any]] = []
            seen_names: set[str] = set()
            seen_codes: set[str] = set()
            for row in rows:
                item = self._participant(row)
                label = (
                    item["character_name"]
                    or item["display_name"]
                    or item["group_user_id"]
                )
                if item["participation_status"] == PARTICIPANT_AWAY:
                    continue
                if item["card_status"] != CARD_APPROVED:
                    status_labels = {
                        CARD_UNCREATED: "尚未建卡",
                        CARD_DRAFT: "角色卡仍是草稿",
                        CARD_PENDING: "角色卡待审核",
                        CARD_REJECTED: "角色卡未通过审核",
                    }
                    blockers.append(
                        f"{label}：{status_labels.get(item['card_status'], '角色卡无效')}"
                    )
                    continue
                if item["participation_status"] == PARTICIPANT_STANDBY:
                    continue
                if not item["ready"]:
                    blockers.append(f"{label}：尚未确认准备")
                name_key = item["character_name"].casefold()
                code_key = item["character_code"].casefold()
                if name_key in seen_names:
                    blockers.append(f"{label}：角色名重复")
                if code_key in seen_codes:
                    blockers.append(f"{label}：副本代号重复")
                seen_names.add(name_key)
                seen_codes.add(code_key)
                active.append(item)
            if len(active) < limits["minimum_start"]:
                blockers.append(
                    f"有效出场人数不足：{len(active)}/{limits['minimum_start']}"
                )
            if len(active) > limits["maximum"]:
                blockers.append(
                    f"有效出场人数超过上限：{len(active)}/{limits['maximum']}"
                )
            return {
                "ok": not blockers,
                "blockers": blockers,
                "participants": active,
                "limits": limits,
                "resume_mode": bool(session["turn_no"]),
                "state": session["state"],
            }

    async def activate_story(
        self,
        session_id: str,
        actor_id: str,
        *,
        resume: bool = False,
        choices: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        if not resume and int(session.get("turn_no") or 0) > 0:
            raise InvalidTransitionError(
                "该副本已有剧情进度，不能再次开演；请使用 /酒馆 继续"
            )
        preflight = await self.opening_preflight(session_id)
        if not preflight["ok"]:
            return {"started": False, **preflight}
        return await self._run(
            self._activate_story,
            session_id,
            actor_id,
            resume,
            [dict(item) for item in (choices or ())],
        )

    def _activate_story(
        self,
        session_id: str,
        actor_id: str,
        resume: bool,
        supplied_choices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError("副本当前不在准备阶段")
                if not resume and int(session["turn_no"] or 0) > 0:
                    raise InvalidTransitionError(
                        "该副本已有剧情进度，不能再次开演；请使用 /酒馆 继续"
                    )
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
                limits = player_limits(world)
                participants = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND card_status = 'approved'
                      AND ready = 1 AND participation_status = 'active'
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                blockers: list[str] = []
                if len(participants) < limits["minimum_start"]:
                    blockers.append(
                        f"有效出场人数不足：{len(participants)}/{limits['minimum_start']}"
                    )
                if blockers:
                    connection.execute("ROLLBACK")
                    return {
                        "started": False,
                        "ok": False,
                        "blockers": blockers,
                        "participants": [
                            self._participant(row) for row in participants
                        ],
                        "limits": limits,
                    }

                order = [str(row["group_user_id"]) for row in participants]
                stored_state = json_load(session["world_state_json"], {})
                existing_turn = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=set(order),
                )
                if resume:
                    existing_order = [
                        item for item in existing_turn["order"] if item in order
                    ]
                    order = existing_order + [
                        item for item in order if item not in existing_order
                    ]
                turn_state = replace_turn_order(existing_turn, order)
                turn_state["current_user_id"] = (
                    existing_turn["current_user_id"]
                    if resume
                    and existing_turn["current_user_id"] in order
                    else order[0]
                )
                persisted_state = embed_turn_state(
                    public_world_state(stored_state),
                    turn_state,
                )
                current_user_id = turn_state["current_user_id"]
                current = next(
                    row
                    for row in participants
                    if row["group_user_id"] == current_user_id
                )
                preserved_choice = (
                    connection.execute(
                        """
                        SELECT * FROM choice_sets
                        WHERE session_id = ? AND participant_id = ?
                          AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id, current["id"]),
                    ).fetchone()
                    if resume
                    else None
                )
                preserved_vote = (
                    connection.execute(
                        """
                        SELECT * FROM group_votes
                        WHERE session_id = ? AND status = 'open'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    if resume
                    else None
                )
                selected_choices: list[dict[str, Any]] = []
                if not preserved_choice and not preserved_vote:
                    selected_choices = (
                        normalize_choices(supplied_choices)
                        if supplied_choices
                        else (
                            fallback_choices(stored_state)
                            if resume
                            else opening_choices(world)
                        )
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE sessions SET
                        state = CASE
                            WHEN state = 'running' THEN 'paused'
                            ELSE state
                        END,
                        selected = 0,
                        revision = CASE
                            WHEN state = 'running' THEN revision + 1
                            ELSE revision
                        END,
                        updated_at = CASE
                            WHEN state = 'running' THEN ?
                            ELSE updated_at
                        END
                    WHERE platform_id = ? AND group_id = ? AND id <> ?
                    """,
                    (
                        now,
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE sessions SET
                        state = 'running', selected = 1,
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), now, session_id),
                )
                new_revision = int(session["revision"]) + 1
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                choice_id = ""
                choice_row: sqlite3.Row | None = None
                if preserved_vote:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                elif preserved_choice:
                    choice_id = str(preserved_choice["id"])
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET session_revision = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_revision, now, choice_id),
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                else:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    choice_id = new_id("choices")
                    connection.execute(
                        """
                        INSERT INTO choice_sets(
                            id, session_id, participant_id, round_no,
                            session_revision, choices_json, status,
                            reroll_count, idempotency_key, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                        """,
                        (
                            choice_id,
                            session_id,
                            current["id"],
                            turn_state["round_no"],
                            new_revision,
                            json_dump(selected_choices),
                            f"opening:{session_id}:{new_revision}",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ?
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id=current["id"],
                        timer_type="turn",
                        timeout_seconds=time_rules["turn_timeout_seconds"],
                        reminder_seconds=time_rules["turn_reminder_seconds"],
                        action={
                            "choice_set_id": choice_id,
                            "user_id": current_user_id,
                        },
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                opening = (
                    clean_text(
                        world.get("opening_scene"),
                        max_chars=6000,
                    )
                    if not resume and session["turn_no"] == 0
                    else ""
                )
                if opening:
                    connection.execute(
                        """
                        INSERT INTO events(
                            id, session_id, turn_no, role, actor_id,
                            actor_name, content, meta_json, created_at
                        ) VALUES (?, ?, ?, 'system', 'system', '酒馆系统',
                                  ?, ?, ?)
                        """,
                        (
                            new_id("event"),
                            session_id,
                            session["turn_no"],
                            opening,
                            json_dump({"kind": "opening"}),
                            now,
                        ),
                    )
                phase_meta = json_load(config["phase_meta_json"], {})
                phase_meta.update(
                    {
                        "resume_mode": bool(resume),
                        "started_at": now,
                        "frozen_roster": [
                            str(row["id"]) for row in participants
                        ],
                    }
                )
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET phase_meta_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(phase_meta), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.perform" if not resume else "session.continue",
                    session_id,
                    {
                        "roster": order,
                        "choice_set_id": choice_id,
                    },
                )
                updated = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "started": True,
                    "ok": True,
                    "session": self._session(updated),
                    "choice_set": (
                        self._choice_set(choice_row)
                        if choice_row
                        else None
                    ),
                    "vote": (
                        self._vote(preserved_vote)
                        if preserved_vote
                        else None
                    ),
                    "current_participant": self._participant(current),
                    "participants": [
                        self._participant(row) for row in participants
                    ],
                    "opening": opening,
                }
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    async def active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_choice_set, session_id)

    def _active_choice_set(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cs.* FROM choice_sets cs
                WHERE cs.session_id = ? AND cs.status = 'active'
                ORDER BY cs.created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._choice_set(row)
            participant = connection.execute(
                "SELECT * FROM participants WHERE id = ?",
                (row["participant_id"],),
            ).fetchone()
            result["participant"] = (
                self._participant(participant) if participant else None
            )
            return result

    async def replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: Sequence[Mapping[str, Any]],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        normalized = normalize_choices(choices)
        return await self._run(
            self._replace_active_choices,
            session_id,
            participant_id,
            normalized,
            actor_id,
        )

    def _replace_active_choices(
        self,
        session_id: str,
        participant_id: str,
        choices: list[dict[str, Any]],
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT * FROM choice_sets
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("当前没有可重整的选项")
                if current["participant_id"] != participant_id:
                    raise PermissionError("只能重整自己当前回合的选项")
                if int(current["reroll_count"]) >= 1:
                    raise ValueError("本回合的免费重整次数已经用完")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, current["id"]),
                )
                new_id_value = new_id("choices")
                connection.execute(
                    """
                    INSERT INTO choice_sets(
                        id, session_id, participant_id, round_no,
                        session_revision, choices_json, status,
                        reroll_count, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (
                        new_id_value,
                        session_id,
                        participant_id,
                        current["round_no"],
                        session["revision"],
                        json_dump(choices),
                        f"reroll:{current['id']}",
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "choice.reroll",
                    new_id_value,
                    {"previous_choice_set_id": current["id"]},
                )
                row = connection.execute(
                    "SELECT * FROM choice_sets WHERE id = ?",
                    (new_id_value,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._choice_set(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def active_vote(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_vote, session_id)

    def _active_vote(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_votes
                WHERE session_id = ? AND status = 'open'
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._vote(row)
            ballots = connection.execute(
                """
                SELECT user_id, option_key, created_at, updated_at
                FROM vote_ballots WHERE vote_id = ?
                ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
            result["ballots"] = [dict(item) for item in ballots]
            tally = vote_result(
                eligible_count=len(result["eligible_user_ids"]),
                ballots=result["ballots"],
                option_keys=[
                    str(item.get("key")) for item in result["options"]
                ],
            )
            result["tally"] = tally
            return result

    async def cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._cast_vote,
            session_id,
            user_id,
            option_key,
        )

    def _cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        key = str(option_key or "").strip().upper()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                vote_row = connection.execute(
                    """
                    SELECT * FROM group_votes
                    WHERE session_id = ? AND status = 'open'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not vote_row:
                    raise DatabaseNotFoundError("当前没有进行中的集体投票")
                vote = self._vote(vote_row)
                if user_id not in vote["eligible_user_ids"]:
                    raise PermissionError("你不在本次投票的有效成员名单中")
                valid_keys = {
                    str(item.get("key")) for item in vote["options"]
                }
                if key not in valid_keys:
                    raise ValueError(
                        "请选择：" + " / ".join(sorted(valid_keys))
                    )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO vote_ballots(
                        id, vote_id, user_id, option_key,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vote_id, user_id) DO UPDATE SET
                        option_key = excluded.option_key,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("ballot"),
                        vote["id"],
                        user_id,
                        key,
                        now,
                        now,
                    ),
                )
                ballots = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT user_id, option_key FROM vote_ballots
                        WHERE vote_id = ?
                        """,
                        (vote["id"],),
                    ).fetchall()
                ]
                tally = vote_result(
                    eligible_count=len(vote["eligible_user_ids"]),
                    ballots=ballots,
                    option_keys=sorted(valid_keys),
                )
                status = "open"
                winner = str(tally["winner"] or "")
                stage = int(vote["stage"])
                options = vote["options"]
                if winner:
                    status = "passed"
                elif tally["all_voted"] and tally["quorum"]:
                    counts = tally["counts"]
                    ranking = sorted(
                        options,
                        key=lambda item: (
                            -int(counts.get(str(item.get("key")), 0)),
                            str(item.get("key")),
                        ),
                    )
                    if stage == 1 and len(ranking) > 2:
                        top_count = int(
                            counts.get(str(ranking[0].get("key")), 0)
                        )
                        tied_top = [
                            item
                            for item in ranking
                            if int(counts.get(str(item.get("key")), 0))
                            == top_count
                        ]
                        runoff = (
                            tied_top[:2]
                            if len(tied_top) >= 2
                            else ranking[:2]
                        )
                        connection.execute(
                            "DELETE FROM vote_ballots WHERE vote_id = ?",
                            (vote["id"],),
                        )
                        config = connection.execute(
                            """
                            SELECT time_rules_json FROM instance_configs
                            WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        time_rules = normalize_time_rules(
                            json_load(
                                config["time_rules_json"] if config else "",
                                {},
                            )
                        )
                        new_deadline = deadline_after(
                            time_rules["vote_round_two_seconds"]
                        )
                        connection.execute(
                            """
                            UPDATE group_votes SET
                                options_json = ?, stage = 2,
                                deadline_at = ?, result_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(runoff),
                                new_deadline,
                                json_dump(
                                    {
                                        "round_one": tally,
                                        "reason": "runoff",
                                    }
                                ),
                                now,
                                vote["id"],
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'completed', updated_at = ?
                            WHERE session_id = ? AND timer_type = 'vote'
                              AND status = 'active'
                            """,
                            (now, session_id),
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id="",
                            timer_type="vote",
                            timeout_seconds=time_rules[
                                "vote_round_two_seconds"
                            ],
                            reminder_seconds=time_rules[
                                "vote_reminder_seconds"
                            ],
                            action={"vote_id": vote["id"], "stage": 2},
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "vote.runoff",
                            vote["id"],
                            {"tally": tally},
                        )
                        updated_vote = connection.execute(
                            "SELECT * FROM group_votes WHERE id = ?",
                            (vote["id"],),
                        ).fetchone()
                        connection.execute("COMMIT")
                        return {
                            "vote": self._vote(updated_vote),
                            "tally": tally,
                            "resolved": False,
                            "runoff": True,
                        }
                    status = "rejected"

                if status != "open":
                    connection.execute(
                        """
                        UPDATE group_votes SET
                            status = ?, winner_key = ?,
                            result_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            winner,
                            json_dump(tally),
                            now,
                            vote["id"],
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'completed', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'vote'
                          AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    winning_text = ""
                    for option in vote["options"]:
                        if str(option.get("key")) == winner:
                            winning_text = str(option.get("text") or "")
                            break
                    event_text = (
                        f"【集体决定】{winning_text}"
                        if status == "passed"
                        else "【集体决定】本次表决未形成多数，队伍维持现状。"
                    )
                    session = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if status == "passed" and winning_text:
                        stored_state = json_load(
                            session["world_state_json"],
                            {},
                        )
                        public_state = public_world_state(stored_state)
                        facts = public_state.get("facts")
                        facts = list(facts) if isinstance(facts, list) else []
                        decision_fact = f"队伍多数决定：{winning_text}"
                        if decision_fact not in facts:
                            facts.append(decision_fact)
                        public_state["facts"] = facts[-200:]
                        public_state["scene_summary"] = decision_fact
                        connection.execute(
                            """
                            UPDATE sessions SET
                                world_state_json = ?,
                                revision = revision + 1,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(
                                    embed_turn_state(
                                        public_state,
                                        turn_state_from_world(
                                            stored_state
                                        ),
                                    )
                                ),
                                now,
                                session_id,
                            ),
                        )
                        session = connection.execute(
                            "SELECT * FROM sessions WHERE id = ?",
                            (session_id,),
                        ).fetchone()
                    connection.execute(
                        """
                        INSERT INTO events(
                            id, session_id, turn_no, role, actor_id,
                            actor_name, content, meta_json, created_at
                        ) VALUES (?, ?, ?, 'system', 'vote', '集体表决',
                                  ?, ?, ?)
                        """,
                        (
                            new_id("event"),
                            session_id,
                            session["turn_no"],
                            event_text,
                            json_dump(
                                {
                                    "kind": "group_vote",
                                    "vote_id": vote["id"],
                                    "status": status,
                                    "winner": winner,
                                }
                            ),
                            now,
                        ),
                    )
                    self._resume_after_vote(
                        connection,
                        session=session,
                        vote=vote,
                        now=now,
                    )
                    self._apply_return_vote_result(
                        connection,
                        vote_id=vote["id"],
                        passed=status == "passed",
                        now=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "vote.cast",
                    vote["id"],
                    {
                        "option": key,
                        "status": status,
                        "tally": tally,
                    },
                )
                updated_vote = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "vote": self._vote(updated_vote),
                    "tally": tally,
                    "resolved": status != "open",
                    "runoff": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _resume_after_vote(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        vote: Mapping[str, Any],
        now: str,
    ) -> None:
        user_id = str(vote.get("suspended_user_id") or "")
        if not user_id:
            return
        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], user_id),
        ).fetchone()
        if not participant:
            return
        if connection.execute(
            """
            SELECT 1 FROM choice_sets
            WHERE session_id = ? AND status = 'active'
            """,
            (session["id"],),
        ).fetchone():
            return
        state = json_load(session["world_state_json"], {})
        choices = fallback_choices(state)
        choice_id = new_id("choices")
        turn = turn_state_from_world(state)
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                participant["id"],
                turn["round_no"],
                session["revision"],
                json_dump(choices),
                f"post-vote:{vote['id']}",
                now,
                now,
            ),
        )
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": user_id,
            },
        )

    @staticmethod
    def _apply_return_vote_result(
        connection: sqlite3.Connection,
        *,
        vote_id: str,
        passed: bool,
        now: str,
    ) -> None:
        request_row = connection.execute(
            """
            SELECT * FROM return_requests WHERE vote_id = ?
            """,
            (vote_id,),
        ).fetchone()
        if not request_row:
            return
        if passed:
            config = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (request_row["session_id"],),
            ).fetchone()
            world = json_load(
                config["world_snapshot_json"] if config else "",
                {},
            )
            limits = player_limits(world)
            placeholders = ",".join(
                "?" for _ in SEAT_HOLDING_STATUSES
            )
            occupied = connection.execute(
                f"""
                SELECT COUNT(*) FROM participants
                WHERE session_id = ?
                  AND participation_status IN ({placeholders})
                """,
                (
                    request_row["session_id"],
                    *sorted(SEAT_HOLDING_STATUSES),
                ),
            ).fetchone()[0]
            if occupied >= limits["maximum"]:
                connection.execute(
                    """
                    UPDATE return_requests
                    SET status = 'cancelled',
                        progress_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump({"reason": "seat_unavailable"}),
                        now,
                        request_row["id"],
                    ),
                )
                return
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'quest_active', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )
            connection.execute(
                """
                UPDATE participants
                SET participation_status = 'standby', ready = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["participant_id"]),
            )
        else:
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'rejected', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )

    @staticmethod
    def _record_return_progress(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        request_id: str,
        evidence: str,
        completed: bool,
        round_no: int,
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT rr.*, pt.character_name, pt.display_name
            FROM return_requests rr
            JOIN participants pt ON pt.id = rr.participant_id
            WHERE rr.id = ? AND rr.session_id = ?
              AND rr.status = 'quest_active'
            """,
            (request_id, session_id),
        ).fetchone()
        if not row:
            return None
        progress = json_load(row["progress_json"], {})
        entries = progress.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "turn_no": turn_no,
                "evidence": evidence,
                "created_at": now,
            }
        )
        progress["entries"] = entries[-20:]
        if completed:
            progress["completed_at"] = now
            progress["completion_evidence"] = evidence
            connection.execute(
                """
                UPDATE return_requests SET
                    status = 'completed', progress_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dump(progress), now, request_id),
            )
            connection.execute(
                """
                UPDATE participants SET
                    participation_status = 'active', ready = 1,
                    joined_round = ?, updated_at = ?
                WHERE id = ?
                """,
                (round_no + 1, now, row["participant_id"]),
            )
            name = row["character_name"] or row["display_name"]
            narrative = (
                f"众人完成了约定的寻找条件，并在合理的时机重新找到了{name}。"
                f"{name}将在下一轮队尾重新加入行动。"
            )
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'system', 'system', '返场幕间',
                          ?, ?, ?)
                """,
                (
                    new_id("event"),
                    session_id,
                    turn_no,
                    narrative,
                    json_dump(
                        {
                            "kind": "return_complete",
                            "return_request_id": request_id,
                            "participant_id": row["participant_id"],
                        }
                    ),
                    now,
                ),
            )
            return {
                "request_id": request_id,
                "completed": True,
                "narrative": narrative,
            }
        connection.execute(
            """
            UPDATE return_requests
            SET progress_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dump(progress), now, request_id),
        )
        return {
            "request_id": request_id,
            "completed": False,
            "evidence": evidence,
        }

    async def set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_away,
            session_id,
            user_id,
        )

    def _set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能暂离")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'away', ready = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, participant["id"]),
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                next_turn, removed = leave_turn(turn_state, user_id)
                if removed:
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(
                                embed_turn_state(
                                    public_world_state(stored_state),
                                    next_turn,
                                )
                            ),
                            now,
                            session_id,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.away",
                    participant["id"],
                    {"seat_released": False},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return self._participant(updated)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._return_to_queue,
            session_id,
            user_id,
        )

    def _return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                self._assert_session_writable(connection, session_id)
                if participant["participation_status"] != PARTICIPANT_AWAY:
                    raise ValueError("当前角色并非暂离状态")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                joined_round = int(turn_state["round_no"]) + 1
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'active',
                        joined_round = ?, ready = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (joined_round, now, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.return_queue",
                    participant["id"],
                    {"effective_round": joined_round},
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["effective_round"] = joined_round
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def retire_participant(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        forced: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            actor_id,
            forced,
            reason,
        )

    async def retire_self(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            user_id=user_id,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            user_id,
            False,
            "player_exit",
        )

    def _retire_participant(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                result = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=forced,
                    reason=reason,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _retire_participant_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
    ) -> dict[str, Any]:
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        if not participant or participant["session_id"] != session_id:
            raise DatabaseNotFoundError("角色不存在")
        if participant["participation_status"] in {
            PARTICIPANT_RETIRED,
            PARTICIPANT_ARCHIVED,
        }:
            raise ValueError("该角色已经退场")
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        config = connection.execute(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        character_name = (
            participant["character_name"]
            or participant["display_name"]
        )
        narrative = safe_exit_narrative(
            world,
            character_name,
            forced=forced,
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE participants SET
                participation_status = 'retired', ready = 0,
                exit_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                clean_text(reason or "departure", max_chars=500),
                now,
                participant_id,
            ),
        )
        connection.execute(
            """
            UPDATE players SET enabled = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, participant["player_id"]),
        )
        stored_state = json_load(session["world_state_json"], {})
        turn_state = turn_state_from_world(stored_state)
        next_turn, removed = leave_turn(
            turn_state,
            participant["group_user_id"],
        )
        if removed:
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dump(
                        embed_turn_state(
                            public_world_state(stored_state),
                            next_turn,
                        )
                    ),
                    now,
                    session_id,
                ),
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ?
              AND status IN ('active', 'paused')
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE delegation_grants
            SET status = 'revoked', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE character_card_drafts
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE card_binding_codes
            SET status = 'expired'
            WHERE participant_id = ? AND status = 'active'
            """,
            (participant_id,),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'system', '退场幕间',
                      ?, ?, ?)
            """,
            (
                new_id("event"),
                session_id,
                session["turn_no"],
                narrative,
                json_dump(
                    {
                        "kind": "safe_exit",
                        "participant_id": participant_id,
                        "forced": forced,
                    }
                ),
                now,
            ),
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id,
            "participant.retire",
            participant_id,
            {
                "forced": forced,
                "reason": reason,
                "seat_released": True,
            },
        )
        return {
            "participant": self._participant(participant),
            "narrative": narrative,
            "turn_changed": removed,
        }

    async def create_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        scope: str = "instance",
        duration_seconds: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        retirement = await self._run(
            self._retire_and_ban,
            session_id,
            participant["id"],
            actor_id,
            scope,
            duration_seconds,
            reason,
        )
        return retirement

    def _retire_and_ban(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        scope: str,
        duration_seconds: int | None,
        reason: str,
    ) -> dict[str, Any]:
        if scope not in {"instance", "group", "global"}:
            raise ValueError("封禁范围必须是 instance、group 或 global")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                retirement = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=True,
                    reason=reason or "banned",
                )
                participant = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                expires_at = deadline_after(duration_seconds)
                ban_id = new_id("ban")
                connection.execute(
                    """
                    INSERT INTO ban_records(
                        id, session_id, platform_id, group_id, user_id,
                        participant_id, scope, reason, actor_id, status,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        ban_id,
                        session_id if scope == "instance" else "",
                        (
                            session["platform_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        (
                            session["group_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        participant["group_user_id"],
                        participant_id,
                        scope,
                        clean_text(reason, max_chars=500),
                        actor_id,
                        expires_at,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.create",
                    ban_id,
                    {
                        "scope": scope,
                        "duration_seconds": duration_seconds,
                        "participant_id": participant_id,
                    },
                )
                connection.execute("COMMIT")
                return {
                    **retirement,
                    "ban": {
                        "id": ban_id,
                        "scope": scope,
                        "expires_at": expires_at,
                        "reason": reason,
                    },
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
    ) -> int:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._revoke_ban,
            session_id,
            participant["group_user_id"],
            actor_id,
        )

    def _revoke_ban(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE ban_records
                    SET status = 'revoked', updated_at = ?
                    WHERE user_id = ? AND status = 'active'
                    """,
                    (now, user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.revoke",
                    user_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_bans(
        self,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_bans, session_id)

    def _list_bans(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
                """,
                (now, now),
            )
            if session_id:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' AND (
                           scope = 'global'
                        OR (scope = 'group'
                            AND platform_id = ? AND group_id = ?)
                        OR (scope = 'instance' AND session_id = ?)
                    )
                    ORDER BY created_at DESC
                    """,
                    (
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' ORDER BY created_at DESC
                    """
                ).fetchall()
            return [dict(row) for row in rows]

    async def request_return(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._request_return,
            session_id,
            user_id,
        )

    def _request_return(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("没有可返场的历史角色")
                if participant["participation_status"] != PARTICIPANT_RETIRED:
                    raise ValueError("只有已经正式退场的角色可以申请返场")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                ):
                    raise PermissionError("封禁尚未解除，不能申请返场")
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
                limits = player_limits(world)
                placeholders = ",".join(
                    "?" for _ in SEAT_HOLDING_STATUSES
                )
                occupied = connection.execute(
                    f"""
                    SELECT COUNT(*) FROM participants
                    WHERE session_id = ?
                      AND participation_status IN ({placeholders})
                    """,
                    (session_id, *sorted(SEAT_HOLDING_STATUSES)),
                ).fetchone()[0]
                if occupied >= limits["maximum"]:
                    raise ValueError("当前没有空余席位，暂时无法申请返场")
                existing = connection.execute(
                    """
                    SELECT * FROM return_requests
                    WHERE participant_id = ?
                      AND status IN ('requested', 'voting', 'quest_active')
                    """,
                    (participant["id"],),
                ).fetchone()
                if existing:
                    raise ValueError("该角色已经有进行中的返场流程")
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        """,
                        (session_id,),
                    ).fetchall()
                ]
                if not eligible:
                    raise ValueError("当前没有可参与返场表决的在场玩家")
                name = (
                    participant["character_name"]
                    or participant["display_name"]
                )
                objective = (
                    f"沿着{name}离场时留下的线索，完成一次合理的寻找、"
                    "营救、解除困境或约定会合剧情。"
                )
                now = utc_now()
                vote_id = new_id("vote")
                options = [
                    {"key": "A", "text": f"同意开启{name}的返场支线"},
                    {"key": "B", "text": "暂不开启返场支线"},
                ]
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, question, options_json,
                        eligible_user_ids_json, stage, status,
                        suspended_user_id, deadline_at, result_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 'open', '', ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session_id,
                        f"是否为{name}开启一条需要通过剧情完成的返场支线？",
                        json_dump(options),
                        json_dump(eligible),
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                request_id = new_id("return")
                connection.execute(
                    """
                    INSERT INTO return_requests(
                        id, session_id, participant_id, requested_by,
                        status, exit_type, objective, progress_json,
                        vote_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'voting', 'departure', ?,
                              '{}', ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        participant["id"],
                        user_id,
                        objective,
                        vote_id,
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "return_request_id": request_id},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "return.request",
                    request_id,
                    {"vote_id": vote_id, "objective": objective},
                )
                connection.execute("COMMIT")
                return {
                    "request_id": request_id,
                    "vote_id": vote_id,
                    "objective": objective,
                    "character_name": name,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
    async def get_timer_policy(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        return await self._run(self._get_timer_policy, session_id)

    def _get_timer_policy(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not session:
                raise DatabaseNotFoundError("副本不存在")
            row = connection.execute(
                """
                SELECT * FROM timer_policies WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            switches = json_load(
                row["switches_json"] if row else "",
                {},
            )
            switches = switches if isinstance(switches, Mapping) else {}
            global_enabled = bool(row["global_enabled"] if row else True)
            return {
                "session_id": session_id,
                "global_enabled": global_enabled,
                "switches": {
                    timer_type: bool(switches.get(timer_type, True))
                    for timer_type in COUNTDOWN_TYPES
                },
                "effective": {
                    timer_type: bool(
                        global_enabled and switches.get(timer_type, True)
                    )
                    for timer_type in COUNTDOWN_TYPES
                },
                "revision": int(row["revision"] if row else 0),
                "updated_at": str(row["updated_at"] if row else ""),
            }

    async def set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_timer_policy,
            session_id,
            timer_type,
            enabled,
            actor_id,
        )

    def _set_timer_policy(
        self,
        session_id: str,
        timer_type: str,
        enabled: bool,
        actor_id: str,
    ) -> dict[str, Any]:
        timer_type = str(timer_type or "").strip().lower()
        if timer_type not in {"all", *COUNTDOWN_TYPES}:
            raise ValueError("不支持的倒计时分类")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                row = connection.execute(
                    """
                    SELECT * FROM timer_policies WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                global_enabled = bool(
                    row["global_enabled"] if row else True
                )
                switches = json_load(
                    row["switches_json"] if row else "",
                    {},
                )
                switches = (
                    dict(switches)
                    if isinstance(switches, Mapping)
                    else {}
                )
                before = {
                    item: bool(
                        global_enabled and switches.get(item, True)
                    )
                    for item in COUNTDOWN_TYPES
                }
                if timer_type == "all":
                    global_enabled = bool(enabled)
                else:
                    switches[timer_type] = bool(enabled)
                after = {
                    item: bool(
                        global_enabled and switches.get(item, True)
                    )
                    for item in COUNTDOWN_TYPES
                }
                connection.execute(
                    """
                    INSERT INTO timer_policies(
                        session_id, global_enabled, switches_json,
                        revision, updated_by, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        global_enabled = excluded.global_enabled,
                        switches_json = excluded.switches_json,
                        revision = timer_policies.revision + 1,
                        updated_by = excluded.updated_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        int(global_enabled),
                        json_dump(switches),
                        actor_id,
                        now,
                    ),
                )
                changed = [
                    item for item in COUNTDOWN_TYPES
                    if before[item] != after[item]
                ]
                if changed:
                    placeholders = ",".join("?" for _ in changed)
                    rows = connection.execute(
                        f"""
                        SELECT * FROM timer_instances
                        WHERE session_id = ?
                          AND timer_type IN ({placeholders})
                          AND status IN ('active', 'paused')
                        """,
                        (session_id, *changed),
                    ).fetchall()
                    for timer in rows:
                        payload = json_load(timer["action_json"], {})
                        payload = (
                            dict(payload)
                            if isinstance(payload, Mapping)
                            else {}
                        )
                        current_type = str(timer["timer_type"])
                        if not after[current_type]:
                            if timer["status"] != "active":
                                continue
                            remaining = timer["remaining_seconds"]
                            deadline = str(timer["deadline_at"] or "")
                            if deadline:
                                try:
                                    remaining = max(
                                        0,
                                        int(
                                            (
                                                datetime.fromisoformat(deadline)
                                                - now_dt
                                            ).total_seconds()
                                        ),
                                    )
                                except ValueError:
                                    pass
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'paused', deadline_at = '',
                                    remaining_seconds = ?, reminder_at = '',
                                    reminder_sent = 0, action_json = ?,
                                    updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    remaining,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                        elif (
                            timer["status"] == "paused"
                            and payload.get("paused_by_policy")
                        ):
                            seconds_left = max(
                                1,
                                int(timer["remaining_seconds"] or 1),
                            )
                            deadline_dt = now_dt + timedelta(
                                seconds=seconds_left
                            )
                            interval = timer_reminder_interval(
                                current_type
                            )
                            next_reminder = now_dt + timedelta(
                                seconds=interval
                            )
                            payload.pop("paused_by_policy", None)
                            reminder_at = ""
                            if (
                                timer_reminder_enabled(
                                    current_type,
                                    payload,
                                )
                                and next_reminder < deadline_dt
                            ):
                                reminder_at = next_reminder.isoformat(
                                    timespec="seconds"
                                )
                            connection.execute(
                                """
                                UPDATE timer_instances SET
                                    status = 'active', deadline_at = ?,
                                    reminder_at = ?, reminder_sent = 0,
                                    action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    deadline_dt.isoformat(
                                        timespec="seconds"
                                    ),
                                    reminder_at,
                                    json_dump(payload),
                                    now,
                                    timer["id"],
                                ),
                            )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.policy",
                    timer_type,
                    {
                        "enabled": bool(enabled),
                        "changed_types": changed,
                    },
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._get_timer_policy(session_id)

    async def list_timers(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_timers, session_id)

    def _list_timers(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*, pt.character_name, pt.display_name
                FROM timer_instances t
                LEFT JOIN participants pt ON pt.id = t.participant_id
                WHERE t.session_id = ?
                ORDER BY
                    CASE t.status
                        WHEN 'active' THEN 0
                        WHEN 'paused' THEN 1
                        ELSE 2
                    END,
                    t.deadline_at, t.created_at
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["action"] = json_load(item.pop("action_json"), {})
                result.append(item)
            return result

    async def control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        *,
        seconds: int = 0,
    ) -> dict[str, Any]:
        return await self._run(
            self._control_timer,
            timer_id,
            action,
            actor_id,
            seconds,
        )

    def _control_timer(
        self,
        timer_id: str,
        action: str,
        actor_id: str,
        seconds: int,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in {
            "pause",
            "resume",
            "extend",
            "expire",
            "disable",
        }:
            raise ValueError("不支持的计时器操作")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("计时器不存在")
                payload = json_load(row["action_json"], {})
                if not isinstance(payload, Mapping):
                    payload = {}
                reminder_interval = timer_reminder_interval(
                    row["timer_type"]
                )
                reminders_enabled = timer_reminder_enabled(
                    row["timer_type"],
                    payload,
                )
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                status = row["status"]
                deadline = str(row["deadline_at"] or "")
                remaining = row["remaining_seconds"]
                reminder_at = str(row["reminder_at"] or "")
                reminder_sent = int(row["reminder_sent"] or 0)
                if action == "pause":
                    if status != "active":
                        raise ValueError("只有运行中的计时器可以暂停")
                    if deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        remaining = max(
                            0,
                            int((deadline_dt - now_dt).total_seconds()),
                        )
                    status = "paused"
                    deadline = ""
                    reminder_at = ""
                    reminder_sent = 0
                elif action == "resume":
                    if status != "paused":
                        raise ValueError("只有暂停中的计时器可以恢复")
                    status = "active"
                    seconds_left = max(1, int(remaining or 1))
                    deadline_dt = now_dt + timedelta(seconds=seconds_left)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    next_reminder = now_dt + timedelta(
                        seconds=reminder_interval
                    )
                    if reminders_enabled:
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    reminder_sent = 0
                elif action == "extend":
                    if seconds <= 0:
                        raise ValueError("延长时间必须大于 0")
                    if status == "active" and deadline:
                        deadline_dt = datetime.fromisoformat(deadline)
                        deadline_dt += timedelta(seconds=seconds)
                        deadline = deadline_dt.isoformat(timespec="seconds")
                        next_reminder = now_dt + timedelta(
                            seconds=reminder_interval
                        )
                        if not reminders_enabled:
                            reminder_at = deadline
                            reminder_sent = 0
                        elif not reminder_at and next_reminder < deadline_dt:
                            reminder_at = next_reminder.isoformat(
                                timespec="seconds"
                            )
                            reminder_sent = 0
                    else:
                        remaining = max(0, int(remaining or 0)) + seconds
                elif action == "expire":
                    status = "expired"
                    deadline = now
                    remaining = 0
                    reminder_at = ""
                else:
                    status = "cancelled"
                    deadline = ""
                    reminder_at = ""
                connection.execute(
                    """
                    UPDATE timer_instances SET
                        status = ?, deadline_at = ?,
                        remaining_seconds = ?, reminder_at = ?,
                        reminder_sent = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        deadline,
                        remaining,
                        reminder_at,
                        reminder_sent,
                        now,
                        timer_id,
                    ),
                )
                if action == "expire":
                    if row["timer_type"] == "card_code":
                        connection.execute(
                            """
                            UPDATE card_binding_codes
                            SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (str(payload.get("code") or ""),),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=payload,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            payload.get("timeout_action") or "standby"
                        )
                        if timeout_action != "remind":
                            next_status = (
                                "archived"
                                if row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                                else "standby"
                            )
                            connection.execute(
                                """
                                UPDATE participants SET
                                    participation_status = ?,
                                    ready = 0, updated_at = ?
                                WHERE id = ?
                                  AND participation_status IN (
                                      'reserved', 'active'
                                  )
                                """,
                                (
                                    next_status,
                                    now,
                                    row["participant_id"],
                                ),
                            )
                            if (
                                row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                            ):
                                connection.execute(
                                    """
                                    UPDATE card_drafts SET status = 'cancelled',
                                        updated_at = ?
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (now, row["participant_id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE card_binding_codes
                                    SET status = 'cancelled'
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (row["participant_id"],),
                                )
                            elif next_status == PARTICIPANT_STANDBY:
                                self._start_standby_timer(
                                    connection,
                                    session_id=row["session_id"],
                                    participant_id=row["participant_id"],
                                )
                    elif (
                        row["timer_type"] == "standby"
                        and row["participant_id"]
                    ):
                        self._retire_participant_in_tx(
                            connection,
                            session_id=row["session_id"],
                            participant_id=row["participant_id"],
                            actor_id=actor_id,
                            forced=False,
                            reason="standby_timeout",
                        )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    f"timer.{action}",
                    timer_id,
                    {"seconds": seconds},
                )
                updated = connection.execute(
                    "SELECT * FROM timer_instances WHERE id = ?",
                    (timer_id,),
                ).fetchone()
                connection.execute("COMMIT")
                item = dict(updated)
                item["action"] = json_load(item.pop("action_json"), {})
                return item
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._extend_active_timer,
            session_id,
            target,
            seconds,
            actor_id,
        )

    def _extend_active_timer(
        self,
        session_id: str,
        target: str,
        seconds: int,
        actor_id: str,
    ) -> dict[str, Any]:
        reference = str(target or "").strip()
        with self._connect() as connection:
            if reference in {"准备阶段", "准备", "preparation"}:
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
            else:
                participant = self._get_participant(
                    session_id,
                    "",
                    reference,
                    True,
                )
                row = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND participant_id = ?
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id, participant["id"]),
                ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有找到对应的活动计时器")
        return self._control_timer(
            str(row["id"]),
            "extend",
            actor_id,
            seconds,
        )

    async def pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._pause_session_timers,
            session_id,
            actor_id,
        )

    def _pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                ).fetchall()
                for row in rows:
                    remaining = row["remaining_seconds"]
                    if row["deadline_at"]:
                        remaining = max(
                            0,
                            int(
                                (
                                    datetime.fromisoformat(row["deadline_at"])
                                    - now_dt
                                ).total_seconds()
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'paused', deadline_at = '',
                            remaining_seconds = ?, reminder_at = '',
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (remaining, now, row["id"]),
                    )
                    payload = json_load(row["action_json"], {})
                    if (
                        row["timer_type"] == "vote"
                        and isinstance(payload, Mapping)
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = '', updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.pause_all",
                    session_id,
                    {"count": len(rows)},
                )
                connection.execute("COMMIT")
                return len(rows)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._resume_session_timers,
            session_id,
            actor_id,
        )

    def _resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now_dt = datetime.now(timezone.utc)
                now = now_dt.isoformat(timespec="seconds")
                rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE session_id = ? AND status = 'paused'
                    """,
                    (session_id,),
                ).fetchall()
                # 后台关掉的倒计时不能被「恢复/继续/读档」重新唤醒。
                # 旧实现无条件恢复全部 paused 行，导致管理员关掉倒计时后
                # 只要有人发 /酒馆 继续，提醒就会重新开始刷屏。
                policy = connection.execute(
                    """
                    SELECT global_enabled, switches_json FROM timer_policies
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                switches = json_load(
                    policy["switches_json"] if policy else "",
                    {},
                )
                switches = switches if isinstance(switches, Mapping) else {}
                global_enabled = bool(
                    policy["global_enabled"] if policy else 1
                )
                resumed = 0
                for row in rows:
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    timer_type = str(row["timer_type"])
                    countdown_enabled = bool(
                        global_enabled and switches.get(timer_type, True)
                    )
                    if not countdown_enabled:
                        # 策略仍为关闭：保持暂停，并补齐标记，
                        # 便于之后重新打开时由 _set_timer_policy 正确复活。
                        if not payload.get("paused_by_policy"):
                            payload = dict(payload)
                            payload["paused_by_policy"] = True
                            connection.execute(
                                """
                                UPDATE timer_instances
                                SET action_json = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (json_dump(payload), now, row["id"]),
                            )
                        continue
                    if payload.get("paused_by_policy"):
                        # 策略已重新打开的场景由 _set_timer_policy 负责恢复，
                        # 这里不越权唤醒，避免与策略状态打架。
                        continue
                    resumed += 1
                    remaining = max(1, int(row["remaining_seconds"] or 1))
                    deadline_dt = now_dt + timedelta(seconds=remaining)
                    deadline = deadline_dt.isoformat(timespec="seconds")
                    next_reminder = now_dt + timedelta(
                        seconds=timer_reminder_interval(
                            row["timer_type"]
                        )
                    )
                    if timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        reminder_at = (
                            next_reminder.isoformat(timespec="seconds")
                            if next_reminder < deadline_dt
                            else ""
                        )
                    else:
                        reminder_at = deadline
                    connection.execute(
                        """
                        UPDATE timer_instances SET
                            status = 'active', deadline_at = ?,
                            reminder_at = ?, reminder_sent = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (deadline, reminder_at, now, row["id"]),
                    )
                    if (
                        row["timer_type"] == "vote"
                        and payload.get("vote_id")
                    ):
                        connection.execute(
                            """
                            UPDATE group_votes
                            SET deadline_at = ?, updated_at = ?
                            WHERE id = ? AND status = 'open'
                            """,
                            (deadline, now, str(payload["vote_id"])),
                        )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "timer.resume_all",
                    session_id,
                    {
                        "count": resumed,
                        "skipped_by_policy": len(rows) - resumed,
                    },
                )
                connection.execute("COMMIT")
                return resumed
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def process_due_timers(self) -> list[dict[str, Any]]:
        return await self._run(self._process_due_timers)

    @staticmethod
    def _timer_notice_targets(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> list[dict[str, str]]:
        """Resolve the people who can still satisfy a running timer."""

        session_id = str(row["session_id"] or "")
        participant_id = str(row["participant_id"] or "")
        timer_type = str(row["timer_type"] or "")
        user_ids: list[str] = []

        if participant_id:
            participant = connection.execute(
                """
                SELECT group_user_id FROM participants
                WHERE id = ? AND session_id = ?
                """,
                (participant_id, session_id),
            ).fetchone()
            if participant and participant["group_user_id"]:
                user_ids.append(str(participant["group_user_id"]))
        elif timer_type == "vote":
            action = json_load(row["action_json"], {})
            vote_id = str(action.get("vote_id") or "")
            vote = (
                connection.execute(
                    """
                    SELECT eligible_user_ids_json FROM group_votes
                    WHERE id = ? AND session_id = ? AND status = 'open'
                    """,
                    (vote_id, session_id),
                ).fetchone()
                if vote_id
                else None
            )
            if vote:
                eligible = json_load(
                    vote["eligible_user_ids_json"],
                    [],
                )
                if not isinstance(eligible, list):
                    eligible = []
                voted = {
                    str(item["user_id"])
                    for item in connection.execute(
                        """
                        SELECT user_id FROM vote_ballots
                        WHERE vote_id = ?
                        """,
                        (vote_id,),
                    ).fetchall()
                }
                user_ids.extend(
                    str(user_id)
                    for user_id in eligible
                    if str(user_id) and str(user_id) not in voted
                )
        elif timer_type == "preparation":
            user_ids.extend(
                str(item["group_user_id"])
                for item in connection.execute(
                    """
                    SELECT group_user_id FROM participants
                    WHERE session_id = ? AND ready = 0
                      AND participation_status IN ('reserved', 'active')
                    ORDER BY created_at
                    """,
                    (session_id,),
                ).fetchall()
                if item["group_user_id"]
            )

        targets: list[dict[str, str]] = []
        seen: set[str] = set()
        for user_id in user_ids:
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            participant = connection.execute(
                """
                SELECT display_name, character_name,
                       private_user_id, private_origin
                FROM participants
                WHERE session_id = ? AND group_user_id = ?
                ORDER BY
                    CASE participation_status
                        WHEN 'active' THEN 0
                        WHEN 'reserved' THEN 1
                        WHEN 'standby' THEN 2
                        WHEN 'away' THEN 3
                        ELSE 4
                    END,
                    created_at DESC
                LIMIT 1
                """,
                (session_id, user_id),
            ).fetchone()
            display_name = user_id
            private_user_id = ""
            private_origin = ""
            if participant:
                display_name = str(
                    participant["character_name"]
                    or participant["display_name"]
                    or user_id
                )
                private_user_id = str(
                    participant["private_user_id"] or ""
                )
                private_origin = str(
                    participant["private_origin"] or ""
                )
            targets.append(
                {
                    "user_id": user_id,
                    "display_name": display_name,
                    "private_user_id": private_user_id,
                    "private_origin": private_origin,
                }
            )
        return targets

    def _process_due_timers(self) -> list[dict[str, Any]]:
        notifications: list[dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                now_dt = datetime.fromisoformat(now)
                # Adopt role-card timers created by the previous 30-second
                # release exactly once. Reset their next notice to two minutes
                # from now so upgrading cannot leak one stale group reminder.
                legacy_card_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE timer_type = 'card_completion'
                      AND status IN ('active', 'paused')
                    """
                ).fetchall()
                for row in legacy_card_rows:
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, dict):
                        payload = {}
                    try:
                        stored_interval = int(
                            payload.get(
                                "reminder_interval_seconds",
                                0,
                            )
                            or 0
                        )
                    except (TypeError, ValueError):
                        stored_interval = 0
                    if (
                        stored_interval
                        == CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                        and "reminder_enabled" in payload
                    ):
                        continue
                    payload.setdefault("reminder_enabled", True)
                    payload["reminder_interval_seconds"] = (
                        CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                    )
                    migrated_reminder = ""
                    deadline_text = str(row["deadline_at"] or "")
                    if row["status"] == "active" and deadline_text:
                        deadline = datetime.fromisoformat(deadline_text)
                        if timer_reminder_enabled(
                            row["timer_type"],
                            payload,
                        ):
                            candidate = now_dt + timedelta(
                                seconds=(
                                    CARD_COMPLETION_REMINDER_INTERVAL_SECONDS
                                )
                            )
                            if candidate < deadline:
                                migrated_reminder = candidate.isoformat(
                                    timespec="seconds"
                                )
                        else:
                            migrated_reminder = deadline_text
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET action_json = ?, reminder_at = ?,
                            reminder_sent = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(payload),
                            migrated_reminder,
                            now,
                            row["id"],
                        ),
                    )

                # Timers created by an older release may have no reminder
                # timestamp. Adopt them without changing their deadlines.
                missing_reminder_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active' AND reminder_at = ''
                      AND deadline_at <> '' AND deadline_at > ?
                    """,
                    (now,),
                ).fetchall()
                for row in missing_reminder_rows:
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    deadline = datetime.fromisoformat(row["deadline_at"])
                    if not timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        adopted_reminder = str(row["deadline_at"])
                    else:
                        candidate = now_dt + timedelta(
                            seconds=timer_reminder_interval(
                                row["timer_type"]
                            )
                        )
                        adopted_reminder = (
                            candidate.isoformat(timespec="seconds")
                            if candidate < deadline
                            else ""
                        )
                    if adopted_reminder:
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET reminder_at = ?, reminder_sent = 0,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (adopted_reminder, now, row["id"]),
                        )
                reminder_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active'
                      AND reminder_at <> '' AND reminder_at <= ?
                      AND deadline_at <> '' AND deadline_at > ?
                    """,
                    (now, now),
                ).fetchall()
                # 分发前再查一次策略：即便有历史脏数据或并发写入
                # 让被关闭的计时器仍处于 active，也不会再推送提醒。
                policy_cache: dict[str, tuple[bool, Mapping[str, Any]]] = {}

                def _countdown_allowed(
                    session_key: str,
                    timer_type_key: str,
                ) -> bool:
                    cached = policy_cache.get(session_key)
                    if cached is None:
                        policy_row = connection.execute(
                            """
                            SELECT global_enabled, switches_json
                            FROM timer_policies WHERE session_id = ?
                            """,
                            (session_key,),
                        ).fetchone()
                        policy_switches = json_load(
                            policy_row["switches_json"]
                            if policy_row
                            else "",
                            {},
                        )
                        if not isinstance(policy_switches, Mapping):
                            policy_switches = {}
                        cached = (
                            bool(
                                policy_row["global_enabled"]
                                if policy_row
                                else 1
                            ),
                            policy_switches,
                        )
                        policy_cache[session_key] = cached
                    enabled, switch_map = cached
                    return bool(
                        enabled and switch_map.get(timer_type_key, True)
                    )

                for row in reminder_rows:
                    deadline = datetime.fromisoformat(row["deadline_at"])
                    payload = json_load(row["action_json"], {})
                    if not isinstance(payload, Mapping):
                        payload = {}
                    reminder_interval = timer_reminder_interval(
                        row["timer_type"]
                    )
                    if not _countdown_allowed(
                        str(row["session_id"]),
                        str(row["timer_type"]),
                    ):
                        stale_payload = dict(payload)
                        stale_payload["paused_by_policy"] = True
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, action_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                max(
                                    0,
                                    int(
                                        (deadline - now_dt).total_seconds()
                                    ),
                                ),
                                json_dump(stale_payload),
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    if not timer_reminder_enabled(
                        row["timer_type"],
                        payload,
                    ):
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET reminder_at = ?, reminder_sent = 0,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (row["deadline_at"], now, row["id"]),
                        )
                        continue
                    remaining = max(
                        1,
                        int((deadline - now_dt).total_seconds()),
                    )
                    next_reminder = (
                        now_dt
                        + timedelta(
                            seconds=reminder_interval
                        )
                    )
                    next_reminder_at = (
                        next_reminder.isoformat(timespec="seconds")
                        if next_reminder < deadline
                        else ""
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET reminder_at = ?, reminder_sent = 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (next_reminder_at, now, row["id"]),
                    )
                    notifications.append(
                        {
                            "kind": "reminder",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": remaining,
                            "reminder_interval_seconds": (
                                reminder_interval
                            ),
                            "targets": self._timer_notice_targets(
                                connection,
                                row,
                            ),
                        }
                    )
                due_rows = connection.execute(
                    """
                    SELECT * FROM timer_instances
                    WHERE status = 'active' AND deadline_at <> ''
                      AND deadline_at <= ?
                    ORDER BY deadline_at, created_at
                    """,
                    (now,),
                ).fetchall()
                for row in due_rows:
                    targets = self._timer_notice_targets(connection, row)
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'expired', remaining_seconds = 0,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, row["id"]),
                    )
                    action = json_load(row["action_json"], {})
                    if row["timer_type"] == "card_code":
                        code = str(action.get("code") or "")
                        connection.execute(
                            """
                            UPDATE card_binding_codes SET status = 'expired'
                            WHERE code = ? AND status = 'active'
                            """,
                            (code,),
                        )
                    elif row["timer_type"] == "turn":
                        self._expire_turn_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] == "vote":
                        self._expire_vote_timer(
                            connection,
                            row=row,
                            action=action,
                            now=now,
                        )
                    elif row["timer_type"] in {
                        "card_completion",
                        "ready",
                    } and row["participant_id"]:
                        timeout_action = str(
                            action.get("timeout_action") or "standby"
                        )
                        if timeout_action != "remind":
                            next_status = (
                                "archived"
                                if row["timer_type"] == "card_completion"
                                and timeout_action == "release"
                                else "standby"
                            )
                            connection.execute(
                                """
                                UPDATE participants SET
                                    participation_status = ?,
                                    ready = 0, updated_at = ?
                                WHERE id = ?
                                  AND participation_status IN (
                                      'reserved', 'active'
                                  )
                                """,
                                (
                                    next_status,
                                    now,
                                    row["participant_id"],
                                ),
                            )
                            if next_status == "archived":
                                connection.execute(
                                    """
                                    UPDATE character_card_drafts
                                    SET status = 'expired', updated_at = ?
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (now, row["participant_id"]),
                                )
                                connection.execute(
                                    """
                                    UPDATE card_binding_codes
                                    SET status = 'expired'
                                    WHERE participant_id = ?
                                      AND status = 'active'
                                    """,
                                    (row["participant_id"],),
                                )
                            elif next_status == PARTICIPANT_STANDBY:
                                self._start_standby_timer(
                                    connection,
                                    session_id=row["session_id"],
                                    participant_id=row["participant_id"],
                                )
                    elif (
                        row["timer_type"] == "standby"
                        and row["participant_id"]
                    ):
                        self._retire_participant_in_tx(
                            connection,
                            session_id=row["session_id"],
                            participant_id=row["participant_id"],
                            actor_id="system",
                            forced=False,
                            reason="standby_timeout",
                        )
                    notifications.append(
                        {
                            "kind": "expired",
                            "timer_id": row["id"],
                            "session_id": row["session_id"],
                            "timer_type": row["timer_type"],
                            "participant_id": row["participant_id"],
                            "remaining_seconds": 0,
                            "targets": targets,
                        }
                    )
                running_rows = connection.execute(
                    """
                    SELECT s.id, s.updated_at, ic.time_rules_json,
                           ic.phase_meta_json
                    FROM sessions s
                    JOIN instance_configs ic ON ic.session_id = s.id
                    WHERE s.state = 'running'
                    """
                ).fetchall()
                for session_row in running_rows:
                    rules = normalize_time_rules(
                        json_load(session_row["time_rules_json"], {})
                    )
                    idle_seconds = rules["all_idle_pause_seconds"]
                    if idle_seconds is None:
                        continue
                    phase_meta = json_load(
                        session_row["phase_meta_json"],
                        {},
                    )
                    activity_values = [
                        str(
                            phase_meta.get("started_at")
                            or session_row["updated_at"]
                            or ""
                        )
                    ]
                    activity_values.extend(
                        str(item[0] or "")
                        for item in connection.execute(
                            """
                            SELECT MAX(created_at) FROM events
                            WHERE session_id = ? AND role = 'player'
                            UNION ALL
                            SELECT MAX(vb.updated_at)
                            FROM vote_ballots vb
                            JOIN group_votes gv ON gv.id = vb.vote_id
                            WHERE gv.session_id = ?
                            UNION ALL
                            SELECT MAX(updated_at) FROM choice_sets
                            WHERE session_id = ? AND reroll_count > 0
                            """,
                            (
                                session_row["id"],
                                session_row["id"],
                                session_row["id"],
                            ),
                        ).fetchall()
                    )
                    last_activity = max(
                        (value for value in activity_values if value),
                        default=now,
                    )
                    try:
                        last_dt = datetime.fromisoformat(last_activity)
                    except ValueError:
                        continue
                    if (now_dt - last_dt).total_seconds() < idle_seconds:
                        continue
                    timer_rows = connection.execute(
                        """
                        SELECT * FROM timer_instances
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (session_row["id"],),
                    ).fetchall()
                    for timer_row in timer_rows:
                        remaining = timer_row["remaining_seconds"]
                        deadline = str(timer_row["deadline_at"] or "")
                        if deadline:
                            try:
                                deadline_dt = datetime.fromisoformat(deadline)
                                remaining = max(
                                    0,
                                    int(
                                        (deadline_dt - now_dt).total_seconds()
                                    ),
                                )
                            except ValueError:
                                pass
                        connection.execute(
                            """
                            UPDATE timer_instances SET
                                status = 'paused', deadline_at = '',
                                remaining_seconds = ?, reminder_at = '',
                                reminder_sent = 0, updated_at = ?
                            WHERE id = ?
                            """,
                            (remaining, now, timer_row["id"]),
                        )
                    connection.execute(
                        """
                        UPDATE sessions
                        SET state = 'paused', revision = revision + 1,
                            updated_at = ?
                        WHERE id = ? AND state = 'running'
                        """,
                        (now, session_row["id"]),
                    )
                    self._insert_audit(
                        connection,
                        session_row["id"],
                        "system",
                        "session.idle_pause",
                        session_row["id"],
                        {
                            "idle_seconds": idle_seconds,
                            "last_activity": last_activity,
                            "paused_timers": len(timer_rows),
                        },
                    )
                    notifications.append(
                        {
                            "kind": "idle_pause",
                            "session_id": session_row["id"],
                            "timer_type": "all_idle",
                            "participant_id": "",
                        }
                    )
                connection.execute("COMMIT")
                return notifications
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _expire_turn_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (row["session_id"],),
        ).fetchone()
        if not session or session["state"] != SESSION_RUNNING:
            return
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (row["participant_id"],),
        ).fetchone()
        if not participant:
            return
        state = json_load(session["world_state_json"], {})
        turn = turn_state_from_world(state)
        if turn["current_user_id"] != participant["group_user_id"]:
            return
        timeout_count = int(participant["consecutive_timeouts"]) + 1
        config = connection.execute(
            """
            SELECT time_rules_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        if rules["turn_timeout_action"] == "hold":
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = ?, updated_at = ?
                WHERE id = ?
                """,
                (timeout_count, now, participant["id"]),
            )
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, turn_no, role, actor_id, actor_name,
                    content, meta_json, created_at
                ) VALUES (?, ?, ?, 'system', 'system', '回合计时',
                          ?, ?, ?)
                """,
                (
                    new_id("event"),
                    session["id"],
                    session["turn_no"],
                    (
                        f"{participant['character_name'] or participant['display_name']}"
                        "本回合超时；按副本规则保留行动权与原选项。"
                    ),
                    json_dump(
                        {
                            "kind": "turn_timeout",
                            "participant_id": participant["id"],
                            "consecutive": timeout_count,
                            "action": "hold",
                        }
                    ),
                    now,
                ),
            )
            return
        next_turn = advance_turn(turn, participant["group_user_id"])
        moved_to_standby = (
            timeout_count >= rules["max_consecutive_timeouts"]
        )
        if moved_to_standby:
            next_turn, _ = leave_turn(
                next_turn,
                participant["group_user_id"],
            )
        connection.execute(
            """
            UPDATE participants SET
                consecutive_timeouts = ?,
                participation_status = CASE
                    WHEN ? THEN 'standby' ELSE participation_status
                END,
                ready = CASE WHEN ? THEN 0 ELSE ready END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                timeout_count,
                int(moved_to_standby),
                int(moved_to_standby),
                now,
                participant["id"],
            ),
        )
        if moved_to_standby:
            self._start_standby_timer(
                connection,
                session_id=session["id"],
                participant_id=participant["id"],
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (now, str(action.get("choice_set_id") or "")),
        )
        connection.execute(
            """
            UPDATE sessions SET
                world_state_json = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json_dump(
                    embed_turn_state(public_world_state(state), next_turn)
                ),
                now,
                session["id"],
            ),
        )
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, 'system', 'system', '回合计时',
                      ?, ?, ?)
            """,
            (
                new_id("event"),
                session["id"],
                session["turn_no"],
                (
                    f"{participant['character_name'] or participant['display_name']}"
                    "本回合超时，行动权已安全移交。"
                    + (
                        "连续超时达到上限，已转入候补席。"
                        if moved_to_standby
                        else ""
                    )
                ),
                json_dump(
                    {
                        "kind": "turn_timeout",
                        "participant_id": participant["id"],
                        "consecutive": timeout_count,
                        "standby": moved_to_standby,
                    }
                ),
                now,
            ),
        )
        next_user = str(next_turn["current_user_id"] or "")
        if not next_user:
            return
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
            """,
            (session["id"], next_user),
        ).fetchone()
        if not next_participant:
            return
        choice_id = new_id("choices")
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                next_participant["id"],
                next_turn["round_no"],
                int(session["revision"]) + 1,
                json_dump(fallback_choices(state)),
                f"timeout:{row['id']}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=next_participant["id"],
            timer_type="turn",
            timeout_seconds=rules["turn_timeout_seconds"],
            reminder_seconds=rules["turn_reminder_seconds"],
            action={"choice_set_id": choice_id, "user_id": next_user},
        )

    def _expire_vote_timer(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        action: Mapping[str, Any],
        now: str,
    ) -> None:
        vote_id = str(action.get("vote_id") or "")
        vote_row = connection.execute(
            """
            SELECT * FROM group_votes
            WHERE id = ? AND status = 'open'
            """,
            (vote_id,),
        ).fetchone()
        if not vote_row:
            return
        vote = self._vote(vote_row)
        ballots = [
            dict(item)
            for item in connection.execute(
                """
                SELECT user_id, option_key FROM vote_ballots
                WHERE vote_id = ?
                """,
                (vote_id,),
            ).fetchall()
        ]
        tally = vote_result(
            eligible_count=len(vote["eligible_user_ids"]),
            ballots=ballots,
            option_keys=[
                str(item.get("key")) for item in vote["options"]
            ],
        )
        connection.execute(
            """
            UPDATE group_votes SET
                status = 'rejected', result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json_dump({**tally, "reason": "timeout"}),
                now,
                vote_id,
            ),
        )
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (vote["session_id"],),
        ).fetchone()
        self._resume_after_vote(
            connection,
            session=session,
            vote=vote,
            now=now,
        )
        self._apply_return_vote_result(
            connection,
            vote_id=vote_id,
            passed=False,
            now=now,
        )

    async def grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        *,
        duration_seconds: int | None = None,
    ) -> dict[str, Any]:
        return await self._run(
            self._grant_delegation,
            session_id,
            owner_user_id,
            delegate_user_id,
            actor_id,
            duration_seconds,
        )

    def _grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        duration_seconds: int | None,
    ) -> dict[str, Any]:
        owner_user_id = validate_platform_id(
            owner_user_id,
            label="角色拥有者 ID",
        )
        delegate_user_id = validate_platform_id(
            delegate_user_id,
            label="代控用户 ID",
        )
        if actor_id != owner_user_id:
            raise PermissionError("代控只能由角色本人授权")
        if owner_user_id == delegate_user_id:
            raise ValueError("不能把自己的角色授权给自己")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, owner_user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    raise ValueError("已经退场的角色不能授权代控")
                if duration_seconds is None:
                    config = connection.execute(
                        """
                        SELECT time_rules_json FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    rules = normalize_time_rules(
                        json_load(
                            config["time_rules_json"] if config else "",
                            {},
                        )
                    )
                    duration_seconds = rules["delegation_ttl_seconds"]
                now = utc_now()
                connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (now, participant["id"]),
                )
                grant_id = new_id("delegation")
                expires_at = deadline_after(duration_seconds)
                permissions = ["choose", "reroll", "skip"]
                connection.execute(
                    """
                    INSERT INTO delegation_grants(
                        id, session_id, participant_id, owner_user_id,
                        delegate_user_id, permissions_json, status,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        grant_id,
                        session_id,
                        participant["id"],
                        owner_user_id,
                        delegate_user_id,
                        json_dump(permissions),
                        expires_at,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.grant",
                    grant_id,
                    {
                        "participant_id": participant["id"],
                        "delegate_user_id": delegate_user_id,
                        "expires_at": expires_at,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM delegation_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = dict(row)
                result["permissions"] = json_load(
                    result.pop("permissions_json"),
                    [],
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
    ) -> int:
        return await self._run(
            self._revoke_delegation,
            session_id,
            owner_user_id,
            actor_id,
        )

    def _revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
    ) -> int:
        if actor_id != owner_user_id:
            raise PermissionError("代控只能由角色本人撤销")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND owner_user_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, owner_user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.revoke",
                    owner_user_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authorize_participant_control,
            session_id,
            participant_id,
            controller_user_id,
            permission,
        )

    def _authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE id = ? AND session_id = ?
                    """,
                    (participant_id, session_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("回合角色不存在")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'expired', updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                      AND expires_at <> '' AND expires_at <= ?
                    """,
                    (now, participant_id, now),
                )
                owner_id = str(participant["group_user_id"])
                if controller_user_id == owner_id:
                    connection.execute(
                        """
                        UPDATE delegation_grants
                        SET status = 'revoked', updated_at = ?
                        WHERE participant_id = ? AND status = 'active'
                        """,
                        (now, participant_id),
                    )
                    connection.execute("COMMIT")
                    return {"authorized": True, "mode": "owner"}
                rows = connection.execute(
                    """
                    SELECT * FROM delegation_grants
                    WHERE participant_id = ? AND delegate_user_id = ?
                      AND status = 'active'
                    ORDER BY created_at DESC
                    """,
                    (participant_id, controller_user_id),
                ).fetchall()
                authorized = any(
                    permission in json_load(row["permissions_json"], [])
                    for row in rows
                )
                connection.execute("COMMIT")
                return {
                    "authorized": authorized,
                    "mode": "delegate" if authorized else "none",
                    "owner_user_id": owner_id,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_permission_grants,
            session_id,
        )

    def _list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._grant_permission,
            session_id,
            user_id,
            role,
            actor_id,
        )

    def _grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if role not in {"host", "moderator"}:
            raise ValueError("权限角色必须是 host 或 moderator")
        user_id = validate_platform_id(user_id, label="用户 ID")
        with self._connect() as connection:
            self._assert_session_writable(connection, session_id)
            now = utc_now()
            connection.execute(
                """
                INSERT INTO permission_grants(
                    id, session_id, user_id, role, granted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, user_id, role) DO UPDATE SET
                    granted_by = excluded.granted_by,
                    created_at = excluded.created_at
                """,
                (
                    new_id("permission"),
                    session_id,
                    user_id,
                    role,
                    actor_id,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? AND user_id = ? AND role = ?
                """,
                (session_id, user_id, role),
            ).fetchone()
            return dict(row)

    async def permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        return await self._run(
            self._permission_roles,
            session_id,
            user_id,
        )

    def _permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        with self._connect() as connection:
            return {
                str(row["role"])
                for row in connection.execute(
                    """
                    SELECT role FROM permission_grants
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchall()
            }

    async def list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_return_requests,
            session_id,
        )

    def _list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rr.*, pt.character_name, pt.display_name
                FROM return_requests rr
                JOIN participants pt ON pt.id = rr.participant_id
                WHERE rr.session_id = ?
                ORDER BY rr.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["progress"] = json_load(item.pop("progress_json"), {})
                result.append(item)
            return result

    async def list_audit(
        self,
        session_id: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_audit,
            session_id,
            limit,
            offset,
        )

    def _list_audit(
        self,
        session_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        with self._connect() as connection:
            if session_id:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    WHERE session_id = ?
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (session_id, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_logs
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "actor_id": row["actor_id"],
                    "action": row["action"],
                    "target": row["target"],
                    "detail": json_load(row["detail_json"], {}),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

    async def write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        await self._run(
            self._write_audit,
            session_id,
            actor_id,
            action,
            target,
            dict(detail),
        )

    def _write_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: dict[str, Any],
    ) -> None:
        with self._connect() as connection:
            self._insert_audit(
                connection,
                session_id,
                actor_id,
                action,
                target,
                detail,
            )

    def _insert_audit(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_logs(
                session_id, actor_id, action, target, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                actor_id,
                action,
                target,
                json_dump(dict(detail)),
                utc_now(),
            ),
        )

    async def overview(self) -> dict[str, Any]:
        return await self._run(self._overview)

    def _overview(self) -> dict[str, Any]:
        with self._connect() as connection:
            counts = {
                "worlds": connection.execute(
                    "SELECT COUNT(*) FROM worlds WHERE archived = 0"
                ).fetchone()[0],
                "sessions": connection.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0],
                "running": connection.execute(
                    "SELECT COUNT(*) FROM sessions WHERE state = 'running'"
                ).fetchone()[0],
                "players": connection.execute(
                    """
                    SELECT COUNT(*) FROM participants
                    WHERE participation_status IN (
                        'reserved', 'active', 'standby', 'away'
                    )
                    """
                ).fetchone()[0],
                "memories": connection.execute(
                    "SELECT COUNT(*) FROM memories"
                ).fetchone()[0],
                "snapshots": connection.execute(
                    "SELECT COUNT(*) FROM snapshots"
                ).fetchone()[0],
                "preparing": connection.execute(
                    """
                    SELECT COUNT(*) FROM sessions WHERE state = 'preparing'
                    """
                ).fetchone()[0],
                "open_votes": connection.execute(
                    """
                    SELECT COUNT(*) FROM group_votes WHERE status = 'open'
                    """
                ).fetchone()[0],
                "active_timers": connection.execute(
                    """
                    SELECT COUNT(*) FROM timer_instances
                    WHERE status = 'active'
                    """
                ).fetchone()[0],
            }
            catalog_size = (
                self.path.stat().st_size if self.path.exists() else 0
            )
            instance_paths = list(
                (self.data_dir / "groups").glob(
                    "*/stories/*/instance.sqlite3"
                )
            )
            instance_size = sum(
                item.stat().st_size
                for item in instance_paths
                if item.is_file()
            )
            storage_errors = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM story_storage
                    WHERE sync_status = 'error'
                    """
                ).fetchone()[0]
            )
            return {
                "counts": counts,
                "database_size": catalog_size + instance_size,
                "catalog_size": catalog_size,
                "instance_size": instance_size,
                "instance_database_count": len(instance_paths),
                "storage_errors": storage_errors,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "database_ok": bool(
                    connection.execute(
                        "PRAGMA quick_check"
                    ).fetchone()[0]
                    == "ok"
                )
                and storage_errors == 0,
            }

    async def cleanup(self, audit_retention_days: int) -> dict[str, int]:
        return await self._run(self._cleanup, audit_retention_days)

    def _cleanup(self, audit_retention_days: int) -> dict[str, int]:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=max(1, audit_retention_days))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            audit_cursor = connection.execute(
                "DELETE FROM audit_logs WHERE created_at < ?",
                (cutoff,),
            )
            now = utc_now()
            code_cursor = connection.execute(
                """
                UPDATE card_binding_codes SET status = 'expired'
                WHERE status = 'active' AND expires_at <= ?
                """,
                (now,),
            )
            draft_cursor = connection.execute(
                """
                UPDATE character_card_drafts SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            delegation_cursor = connection.execute(
                """
                UPDATE delegation_grants SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            ban_cursor = connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            return {
                "audit_logs": audit_cursor.rowcount,
                "card_codes": code_cursor.rowcount,
                "card_drafts": draft_cursor.rowcount,
                "delegations": delegation_cursor.rowcount,
                "bans": ban_cursor.rowcount,
            }

    async def export_bundle(self) -> dict[str, Any]:
        return await self._run(self._export_bundle)

    def _export_bundle(self) -> dict[str, Any]:
        with self._connect() as connection:
            tables = {
                "worlds": "SELECT * FROM worlds ORDER BY created_at",
                "characters": "SELECT * FROM characters ORDER BY created_at",
                "sessions": "SELECT * FROM sessions ORDER BY created_at",
                "players": "SELECT * FROM players ORDER BY created_at",
                "events": "SELECT * FROM events ORDER BY seq",
                "memories": "SELECT * FROM memories ORDER BY created_at",
                "snapshots": "SELECT * FROM snapshots ORDER BY created_at",
                "audit_logs": "SELECT * FROM audit_logs ORDER BY id",
                "instance_configs": (
                    "SELECT * FROM instance_configs ORDER BY created_at"
                ),
                "character_cards": (
                    "SELECT * FROM character_cards ORDER BY created_at"
                ),
                "character_card_versions": (
                    "SELECT * FROM character_card_versions ORDER BY created_at"
                ),
                "participants": (
                    "SELECT * FROM participants ORDER BY created_at"
                ),
                "character_runtime_states": (
                    "SELECT * FROM character_runtime_states ORDER BY created_at"
                ),
                "character_card_drafts": (
                    "SELECT * FROM character_card_drafts ORDER BY created_at"
                ),
                "card_binding_codes": (
                    "SELECT * FROM card_binding_codes ORDER BY created_at"
                ),
                "choice_sets": (
                    "SELECT * FROM choice_sets ORDER BY created_at"
                ),
                "rolls": "SELECT * FROM rolls ORDER BY created_at",
                "group_votes": (
                    "SELECT * FROM group_votes ORDER BY created_at"
                ),
                "vote_ballots": (
                    "SELECT * FROM vote_ballots ORDER BY created_at"
                ),
                "selected_world_events": (
                    "SELECT * FROM selected_world_events ORDER BY created_at"
                ),
                "timer_instances": (
                    "SELECT * FROM timer_instances ORDER BY created_at"
                ),
                "delegation_grants": (
                    "SELECT * FROM delegation_grants ORDER BY created_at"
                ),
                "permission_grants": (
                    "SELECT * FROM permission_grants ORDER BY created_at"
                ),
                "ban_records": (
                    "SELECT * FROM ban_records ORDER BY created_at"
                ),
                "return_requests": (
                    "SELECT * FROM return_requests ORDER BY created_at"
                ),
                "snapshot_workflows": (
                    "SELECT * FROM snapshot_workflows ORDER BY snapshot_id"
                ),
                "session_archives": (
                    "SELECT * FROM session_archives ORDER BY ended_at"
                ),
                "session_rule_states": (
                    "SELECT * FROM session_rule_states ORDER BY created_at"
                ),
                "session_characters": (
                    "SELECT * FROM session_characters ORDER BY created_at"
                ),
                "session_character_states": (
                    "SELECT * FROM session_character_states ORDER BY updated_at"
                ),
                "story_ledger": (
                    "SELECT * FROM story_ledger ORDER BY created_at"
                ),
                "scene_clocks": (
                    "SELECT * FROM scene_clocks ORDER BY created_at"
                ),
                "memory_governance": (
                    "SELECT * FROM memory_governance ORDER BY updated_at"
                ),
                "assist_tokens": (
                    "SELECT * FROM assist_tokens ORDER BY created_at"
                ),
                "roll_revisions": (
                    "SELECT * FROM roll_revisions ORDER BY created_at"
                ),
                "inspiration_transactions": (
                    "SELECT * FROM inspiration_transactions ORDER BY created_at"
                ),
                "provider_health": (
                    "SELECT * FROM provider_health ORDER BY updated_at"
                ),
                "configuration_revisions": (
                    "SELECT * FROM configuration_revisions ORDER BY id"
                ),
                "operation_receipts": (
                    "SELECT * FROM operation_receipts ORDER BY created_at"
                ),
                "group_registry": (
                    "SELECT * FROM group_registry ORDER BY created_at"
                ),
                "story_storage": (
                    "SELECT * FROM story_storage ORDER BY created_at"
                ),
                "timer_policies": (
                    "SELECT * FROM timer_policies ORDER BY updated_at"
                ),
                "token_usage": (
                    "SELECT * FROM token_usage ORDER BY created_at"
                ),
                "token_quota_policies": (
                    "SELECT * FROM token_quota_policies ORDER BY updated_at"
                ),
            }
            data: dict[str, list[dict[str, Any]]] = {}
            for name, query in tables.items():
                rows = connection.execute(query).fetchall()
                data[name] = [dict(row) for row in rows]
            return {
                "format": "astrbot-tavern-backup",
                "format_version": 1,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "exported_at": utc_now(),
                "data": data,
            }

    @staticmethod
    def validate_bundle(bundle: Mapping[str, Any]) -> None:
        if bundle.get("format") != "astrbot-tavern-backup":
            raise ValueError("不是有效的 AI 酒馆备份")
        if int(bundle.get("format_version", 0)) != 1:
            raise ValueError("不支持的备份格式版本")
        try:
            schema_version = int(bundle.get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("备份数据库版本无效") from exc
        if schema_version < 1:
            raise ValueError("备份数据库版本无效")
        if schema_version > DATABASE_SCHEMA_VERSION:
            raise ValueError(
                "备份来自更新版本的插件，请先升级插件后再导入"
            )
        data = bundle.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("备份缺少 data")
        required = {
            "worlds",
            "characters",
            "sessions",
            "players",
            "events",
            "memories",
            "snapshots",
            "audit_logs",
        }
        if not required.issubset(data.keys()):
            raise ValueError("备份数据表不完整")
        for table in required:
            if not isinstance(data[table], list):
                raise ValueError(f"备份表 {table} 格式错误")
            if len(data[table]) > 1_000_000:
                raise ValueError(f"备份表 {table} 记录数异常")
        if schema_version >= 3:
            vnext_required = {
                "instance_configs",
                "character_cards",
                "character_card_versions",
                "participants",
                "character_runtime_states",
                "character_card_drafts",
                "card_binding_codes",
                "choice_sets",
                "rolls",
                "group_votes",
                "vote_ballots",
                "selected_world_events",
                "timer_instances",
                "delegation_grants",
                "permission_grants",
                "ban_records",
                "return_requests",
                "snapshot_workflows",
            }
            if not vnext_required.issubset(data.keys()):
                raise ValueError("vNext 备份数据表不完整")
            for table in vnext_required:
                if not isinstance(data[table], list):
                    raise ValueError(f"备份表 {table} 格式错误")
        if schema_version >= 4:
            v05_required = {
                "session_archives",
                "session_rule_states",
                "session_characters",
                "session_character_states",
                "story_ledger",
                "scene_clocks",
                "memory_governance",
                "assist_tokens",
                "roll_revisions",
                "inspiration_transactions",
                "provider_health",
                "configuration_revisions",
                "operation_receipts",
            }
            if not v05_required.issubset(data.keys()):
                raise ValueError("0.5 备份数据表不完整")
            for table in v05_required:
                if not isinstance(data[table], list):
                    raise ValueError(f"备份表 {table} 格式错误")
        if schema_version >= 5:
            v051_required = {
                "group_registry",
                "story_storage",
            }
            if not v051_required.issubset(data.keys()):
                raise ValueError("0.5.1 备份数据表不完整")
            for table in v051_required:
                if not isinstance(data[table], list):
                    raise ValueError(f"备份表 {table} 格式错误")
        if schema_version >= 6:
            v053_required = {
                "timer_policies",
                "token_usage",
                "token_quota_policies",
            }
            if not v053_required.issubset(data.keys()):
                raise ValueError("Schema 6 备份数据表不完整")
            for table in v053_required:
                if not isinstance(data[table], list):
                    raise ValueError(f"备份表 {table} 格式错误")

    async def import_bundle(
        self,
        bundle: Mapping[str, Any],
        mode: str,
        actor_id: str,
    ) -> dict[str, int]:
        return await self._run(
            self._import_bundle,
            dict(bundle),
            mode,
            actor_id,
        )

    def _import_bundle(
        self,
        bundle: dict[str, Any],
        mode: str,
        actor_id: str,
    ) -> dict[str, int]:
        self.validate_bundle(bundle)
        if mode not in {"merge", "replace"}:
            raise ValueError("导入模式必须为 merge 或 replace")
        data = {
            table: [dict(row) for row in rows]
            for table, rows in bundle["data"].items()
        }
        if int(bundle.get("schema_version", 1)) < 2:
            worlds = {
                str(row.get("id") or ""): row
                for row in data["worlds"]
            }
            for row in data["sessions"]:
                world = worlds.get(str(row.get("world_id") or ""), {})
                row["instance_slug"] = str(
                    world.get("slug") or "legacy-instance"
                )
                row["instance_name"] = str(
                    world.get("name") or "旧版副本"
                )
                row["selected"] = 1
        vnext_tables = (
            "instance_configs",
            "character_cards",
            "character_card_versions",
            "participants",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "choice_sets",
            "rolls",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "ban_records",
            "return_requests",
            "snapshot_workflows",
        )
        v05_tables = (
            "session_archives",
            "session_rule_states",
            "session_characters",
            "session_character_states",
            "story_ledger",
            "scene_clocks",
            "memory_governance",
            "assist_tokens",
            "roll_revisions",
            "inspiration_transactions",
            "provider_health",
            "configuration_revisions",
            "operation_receipts",
        )
        v051_tables = (
            "group_registry",
            "story_storage",
        )
        v053_tables = (
            "timer_policies",
            "token_usage",
            "token_quota_policies",
        )
        if int(bundle.get("schema_version", 1)) < 3:
            for row in data["sessions"]:
                if row.get("state") in {"running", "maintenance"}:
                    row["state"] = "paused"
            for table in vnext_tables:
                data.setdefault(table, [])
        if int(bundle.get("schema_version", 1)) < 4:
            for table in v05_tables:
                data.setdefault(table, [])
        if int(bundle.get("schema_version", 1)) < 5:
            for table in v051_tables:
                data.setdefault(table, [])
        if int(bundle.get("schema_version", 1)) < 6:
            for table in v053_tables:
                data.setdefault(table, [])
        counts: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if mode == "merge":
                    self._validate_merge_conflicts(connection, data)
                if mode == "replace":
                    for table in (
                        "audit_logs",
                        "token_usage",
                        "token_quota_policies",
                        "timer_policies",
                        "story_storage",
                        "group_registry",
                        "operation_receipts",
                        "configuration_revisions",
                        "provider_health",
                        "session_archives",
                        "inspiration_transactions",
                        "roll_revisions",
                        "assist_tokens",
                        "memory_governance",
                        "session_character_states",
                        "scene_clocks",
                        "story_ledger",
                        "session_characters",
                        "session_rule_states",
                        "snapshot_workflows",
                        "return_requests",
                        "ban_records",
                        "permission_grants",
                        "delegation_grants",
                        "timer_instances",
                        "selected_world_events",
                        "vote_ballots",
                        "group_votes",
                        "rolls",
                        "choice_sets",
                        "card_binding_codes",
                        "character_card_drafts",
                        "character_runtime_states",
                        "participants",
                        "character_card_versions",
                        "character_cards",
                        "instance_configs",
                        "snapshots",
                        "memories",
                        "events",
                        "players",
                        "sessions",
                        "characters",
                        "worlds",
                    ):
                        connection.execute(f"DELETE FROM {table}")

                self._import_rows(
                    connection,
                    "worlds",
                    data["worlds"],
                    (
                        "id",
                        "slug",
                        "name",
                        "description",
                        "system_prompt",
                        "rules_json",
                        "opening_scene",
                        "initial_state_json",
                        "archived",
                        "revision",
                        "created_at",
                        "updated_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "characters",
                    data["characters"],
                    (
                        "id",
                        "world_id",
                        "slug",
                        "name",
                        "role",
                        "profile_json",
                        "prompt",
                        "enabled",
                        "sort_order",
                        "revision",
                        "created_at",
                        "updated_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "sessions",
                    data["sessions"],
                    (
                        "id",
                        "platform_id",
                        "group_id",
                        "unified_origin",
                        "instance_slug",
                        "instance_name",
                        "selected",
                        "world_id",
                        "state",
                        "turn_no",
                        "revision",
                        "world_state_json",
                        "history_floor_seq",
                        "created_at",
                        "updated_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "players",
                    data["players"],
                    (
                        "id",
                        "session_id",
                        "user_id",
                        "display_name",
                        "character_name",
                        "profile_json",
                        "enabled",
                        "created_at",
                        "updated_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "events",
                    data["events"],
                    (
                        "seq",
                        "id",
                        "session_id",
                        "turn_no",
                        "role",
                        "actor_id",
                        "actor_name",
                        "content",
                        "meta_json",
                        "created_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "memories",
                    data["memories"],
                    (
                        "id",
                        "session_id",
                        "scope",
                        "scope_id",
                        "kind",
                        "content",
                        "importance",
                        "salience",
                        "tags_json",
                        "fingerprint",
                        "source_event_id",
                        "created_at",
                        "updated_at",
                        "last_accessed_at",
                    ),
                    merge=mode == "merge",
                )
                self._import_rows(
                    connection,
                    "snapshots",
                    data["snapshots"],
                    (
                        "id",
                        "session_id",
                        "name",
                        "kind",
                        "turn_no",
                        "session_revision",
                        "world_id",
                        "world_state_json",
                        "created_by",
                        "created_at",
                    ),
                    merge=mode == "merge",
                )
                vnext_columns: dict[str, tuple[str, ...]] = {
                    "instance_configs": (
                        "session_id", "world_revision",
                        "world_snapshot_json", "time_rules_json",
                        "phase_meta_json", "created_at", "updated_at",
                    ),
                    "character_cards": (
                        "id", "owner_user_id", "world_id", "display_name",
                        "archived", "deleted", "current_version",
                        "created_at", "updated_at",
                    ),
                    "character_card_versions": (
                        "id", "character_card_id", "version_no",
                        "template_version", "profile_json", "stats_json",
                        "status", "review_note", "reviewed_by", "created_at",
                    ),
                    "participants": (
                        "id", "session_id", "player_id", "group_user_id",
                        "private_user_id", "private_origin", "display_name",
                        "character_card_id", "character_version_id",
                        "character_name", "character_code", "aliases_json",
                        "card_status", "ready", "participation_status",
                        "seat_reserved_at", "joined_round",
                        "consecutive_timeouts", "exit_reason",
                        "created_at", "updated_at",
                    ),
                    "character_runtime_states": (
                        "id", "session_id", "participant_id",
                        "character_card_id", "state_json", "revision",
                        "created_at", "updated_at",
                    ),
                    "character_card_drafts": (
                        "id", "participant_id", "template_version",
                        "fields_json", "current_step", "status",
                        "expires_at", "created_at", "updated_at",
                    ),
                    "card_binding_codes": (
                        "id", "participant_id", "code", "status",
                        "expires_at", "private_user_id", "private_origin",
                        "created_at", "used_at",
                    ),
                    "choice_sets": (
                        "id", "session_id", "participant_id", "round_no",
                        "session_revision", "choices_json", "status",
                        "reroll_count", "selected_key", "flavor_text",
                        "idempotency_key", "created_at", "updated_at",
                    ),
                    "rolls": (
                        "id", "session_id", "choice_set_id",
                        "participant_id", "roll_json", "created_at",
                    ),
                    "group_votes": (
                        "id", "session_id", "source_event_id", "question",
                        "options_json", "eligible_user_ids_json", "stage",
                        "status", "winner_key", "suspended_user_id",
                        "deadline_at", "result_json",
                        "created_at", "updated_at",
                    ),
                    "vote_ballots": (
                        "id", "vote_id", "user_id", "option_key",
                        "created_at", "updated_at",
                    ),
                    "selected_world_events": (
                        "id", "session_id", "round_no", "pool_item_id",
                        "payload_json", "status", "narrative",
                        "created_at", "resolved_at",
                    ),
                    "timer_instances": (
                        "id", "session_id", "participant_id", "timer_type",
                        "status", "deadline_at", "remaining_seconds",
                        "reminder_at", "reminder_sent", "action_json",
                        "created_at", "updated_at",
                    ),
                    "delegation_grants": (
                        "id", "session_id", "participant_id",
                        "owner_user_id", "delegate_user_id",
                        "permissions_json", "status", "expires_at",
                        "created_at", "updated_at",
                    ),
                    "permission_grants": (
                        "id", "session_id", "user_id", "role",
                        "granted_by", "created_at",
                    ),
                    "ban_records": (
                        "id", "session_id", "platform_id", "group_id",
                        "user_id", "participant_id", "scope", "reason",
                        "actor_id", "status", "expires_at",
                        "created_at", "updated_at",
                    ),
                    "return_requests": (
                        "id", "session_id", "participant_id",
                        "requested_by", "status", "exit_type", "objective",
                        "progress_json", "vote_id", "created_at", "updated_at",
                    ),
                    "snapshot_workflows": (
                        "snapshot_id", "workflow_json",
                    ),
                }
                if int(bundle.get("schema_version", 1)) >= 3:
                    for table in (
                        "instance_configs",
                        "character_cards",
                        "character_card_versions",
                        "participants",
                        "character_runtime_states",
                        "character_card_drafts",
                        "card_binding_codes",
                        "choice_sets",
                        "rolls",
                        "group_votes",
                        "vote_ballots",
                        "selected_world_events",
                        "timer_instances",
                        "delegation_grants",
                        "permission_grants",
                        "ban_records",
                        "return_requests",
                        "snapshot_workflows",
                    ):
                        self._import_rows(
                            connection,
                            table,
                            data[table],
                            vnext_columns[table],
                            merge=mode == "merge",
                        )
                else:
                    self._initialize_vnext_rows(connection)
                v05_columns: dict[str, tuple[str, ...]] = {
                    "session_archives": (
                        "session_id", "termination_type", "reason",
                        "final_snapshot_id", "ended_by", "ended_at", "readonly",
                    ),
                    "session_rule_states": (
                        "session_id", "progress_json",
                        "content_boundaries_json", "npc_policy_json",
                        "context_budget_json", "dice_rules_json",
                        "recovery_json", "revision", "created_at", "updated_at",
                    ),
                    "session_characters": (
                        "id", "session_id", "stable_key", "name",
                        "aliases_json", "role_type", "public_profile_json",
                        "known_facts_json", "misconceptions_json", "source",
                        "review_status", "lifecycle_status", "persistent",
                        "first_event_id", "last_event_id", "first_turn",
                        "last_turn", "revision", "created_at", "updated_at",
                    ),
                    "session_character_states": (
                        "character_id", "state_json", "revision", "updated_at",
                    ),
                    "story_ledger": (
                        "id", "session_id", "stable_key", "kind", "title",
                        "description", "status", "visibility",
                        "source_event_id", "completed_event_id", "revision",
                        "created_at", "updated_at",
                    ),
                    "scene_clocks": (
                        "id", "session_id", "stable_key", "title", "segments",
                        "current_value", "visibility", "trigger_text", "status",
                        "triggered_event_id", "revision", "created_at",
                        "updated_at",
                    ),
                    "memory_governance": (
                        "memory_id", "visibility", "locked", "pinned",
                        "invalidated", "supersedes_id", "conflict_status",
                        "note", "updated_by", "updated_at",
                    ),
                    "assist_tokens": (
                        "id", "session_id", "source_participant_id",
                        "target_participant_id", "stat", "method", "status",
                        "expires_round", "source_event_id", "created_at",
                        "consumed_at",
                    ),
                    "roll_revisions": (
                        "id", "roll_id", "revision_no", "reason",
                        "previous_json", "revised_json", "actor_id", "created_at",
                    ),
                    "inspiration_transactions": (
                        "id", "session_id", "participant_id", "delta",
                        "balance_after", "reason", "operation_id", "created_at",
                    ),
                    "provider_health": (
                        "provider_id", "status", "consecutive_failures",
                        "last_failure_reason", "last_failure_at",
                        "last_success_at", "circuit_until", "updated_at",
                    ),
                    "configuration_revisions": (
                        "id", "fingerprint", "payload_json", "saved_by",
                        "saved_at",
                    ),
                    "operation_receipts": (
                        "operation_id", "session_id", "operation_type",
                        "request_json", "result_json", "status",
                        "created_at", "updated_at",
                    ),
                }
                if int(bundle.get("schema_version", 1)) >= 4:
                    for table in (
                        "session_rule_states",
                        "session_characters",
                        "session_character_states",
                        "story_ledger",
                        "scene_clocks",
                        "memory_governance",
                        "assist_tokens",
                        "roll_revisions",
                        "inspiration_transactions",
                        "provider_health",
                        "configuration_revisions",
                        "operation_receipts",
                        "session_archives",
                    ):
                        self._import_rows(
                            connection,
                            table,
                            data[table],
                            v05_columns[table],
                            merge=mode == "merge",
                        )
                else:
                    self._initialize_v05_rows(connection)
                if int(bundle.get("schema_version", 1)) >= 5:
                    self._import_rows(
                        connection,
                        "group_registry",
                        data["group_registry"],
                        (
                            "id", "platform_id", "group_id", "remark",
                            "revision", "created_at", "updated_at",
                        ),
                        merge=mode == "merge",
                    )
                    # Physical paths and synchronization checksums are never
                    # trusted from an imported document. InstanceStorage
                    # rebuilds them from platform/group/session identities
                    # after this transaction, which prevents a modified
                    # backup from writing outside the plugin data directory.
                    data["story_storage"] = []
                self._initialize_v051_rows(connection)
                if int(bundle.get("schema_version", 1)) >= 6:
                    v053_columns: dict[str, tuple[str, ...]] = {
                        "timer_policies": (
                            "session_id", "global_enabled", "switches_json",
                            "revision", "updated_by", "updated_at",
                        ),
                        "token_usage": (
                            "id", "session_id", "group_id", "request_type",
                            "provider_id", "input_tokens",
                            "cached_input_tokens", "output_tokens",
                            "total_tokens", "reserved_tokens",
                            "usage_source", "status", "created_at",
                            "settled_at",
                        ),
                        "token_quota_policies": (
                            "id", "scope_type", "scope_id",
                            "window_seconds", "token_limit", "enabled",
                            "revision", "updated_by", "updated_at",
                        ),
                    }
                    for table in v053_tables:
                        self._import_rows(
                            connection,
                            table,
                            data[table],
                            v053_columns[table],
                            merge=mode == "merge",
                        )
                self._initialize_v053_rows(connection)
                if mode == "replace":
                    self._import_rows(
                        connection,
                        "audit_logs",
                        data["audit_logs"],
                        (
                            "id",
                            "session_id",
                            "actor_id",
                            "action",
                            "target",
                            "detail_json",
                            "created_at",
                        ),
                    )
                for table, rows in data.items():
                    counts[table] = (
                        len(rows)
                        if table != "audit_logs" or mode == "replace"
                        else 0
                    )
                self._seed_default_world(connection)
                self._initialize_vnext_rows(connection)
                self._initialize_v05_rows(connection)
                self._initialize_v051_rows(connection)
                self._initialize_v053_rows(connection)
                self._insert_audit(
                    connection,
                    "",
                    actor_id,
                    "backup.import",
                    mode,
                    counts,
                )
                connection.execute("COMMIT")
                return counts
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_merge_conflicts(
        connection: sqlite3.Connection,
        data: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        """Reject ambiguous identities before a non-destructive merge.

        A backup row may update an existing entity only when its stable ID is
        unchanged. If a natural unique key points at another ID, merging would
        silently join unrelated worlds, groups, players, or timeline records.
        """

        identity_specs: dict[str, tuple[str, ...]] = {
            "worlds": ("slug",),
            "characters": ("world_id", "slug"),
            "sessions": (
                "platform_id",
                "group_id",
                "instance_slug",
            ),
            "players": ("session_id", "user_id"),
            "events": ("seq",),
            "memories": ("session_id", "fingerprint"),
            "snapshots": ("session_id", "name"),
            "group_registry": ("platform_id", "group_id"),
        }
        for table, identity_columns in identity_specs.items():
            seen_ids: set[str] = set()
            seen_identities: dict[tuple[Any, ...], str] = {}
            for row in data.get(table, ()):
                if not isinstance(row, Mapping):
                    raise ValueError(f"备份表 {table} 含非法记录")
                required = ("id", *identity_columns)
                missing = [column for column in required if column not in row]
                if missing:
                    raise ValueError(
                        f"备份表 {table} 缺少字段 {missing[0]}"
                    )
                row_id = str(row["id"])
                if row_id in seen_ids:
                    raise ValueError(
                        f"备份表 {table} 含重复 ID：{row_id}"
                    )
                seen_ids.add(row_id)

                identity = tuple(row[column] for column in identity_columns)
                previous_id = seen_identities.get(identity)
                if previous_id and previous_id != row_id:
                    raise ValueError(
                        f"备份表 {table} 含重复唯一标识，"
                        "请检查备份或改用覆盖导入"
                    )
                seen_identities[identity] = row_id

                where = " AND ".join(
                    f"{column} = ?" for column in identity_columns
                )
                existing = connection.execute(
                    f"SELECT id FROM {table} WHERE {where}",
                    identity,
                ).fetchone()
                if existing and str(existing["id"]) != row_id:
                    raise ValueError(
                        f"备份表 {table} 的唯一标识已属于其他记录，"
                        "为避免串档已取消合并"
                    )

                if table == "events":
                    by_id = connection.execute(
                        "SELECT seq FROM events WHERE id = ?",
                        (row_id,),
                    ).fetchone()
                    if by_id and int(by_id["seq"]) != int(row["seq"]):
                        raise ValueError(
                            "时间线事件 ID 与序号不一致，"
                            "为避免历史错位已取消合并"
                        )

    @staticmethod
    def _import_rows(
        connection: sqlite3.Connection,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[str],
        *,
        merge: bool = False,
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        insert_sql = (
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
        )
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"备份表 {table} 含非法记录")
            values: list[Any] = []
            for column in columns:
                if column not in row:
                    raise ValueError(f"备份表 {table} 缺少字段 {column}")
                values.append(row[column])
            existing = None
            if merge:
                identity_column = {
                    "instance_configs": "session_id",
                    "snapshot_workflows": "snapshot_id",
                    "session_archives": "session_id",
                    "session_rule_states": "session_id",
                    "session_character_states": "character_id",
                    "memory_governance": "memory_id",
                    "provider_health": "provider_id",
                    "configuration_revisions": "id",
                    "operation_receipts": "operation_id",
                    "group_registry": "id",
                    "story_storage": "session_id",
                    "timer_policies": "session_id",
                }.get(table, "id")
                if identity_column not in row:
                    raise ValueError(
                        f"备份表 {table} 缺少字段 {identity_column}"
                    )
                existing = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {identity_column} = ?",
                    (row[identity_column],),
                ).fetchone()
            if existing:
                # Merge is deliberately insert-only. Existing records are the
                # authoritative live copy; restoring an older backup must not
                # silently roll back a session, player profile, or event.
                continue
            connection.execute(insert_sql, values)
