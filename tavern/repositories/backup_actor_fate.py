"""Logical-backup contract for authoritative actor-fate state."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any


ACTOR_FATE_BACKUP_QUERIES: dict[str, str] = {
    "actor_fate_states": (
        "SELECT * FROM actor_fate_states ORDER BY session_id, character_id"
    ),
    "actor_fate_transitions": (
        "SELECT * FROM actor_fate_transitions ORDER BY session_id, created_at, id"
    ),
    "rescue_windows": (
        "SELECT * FROM rescue_windows ORDER BY session_id, created_at, id"
    ),
    "terminal_receipts": (
        "SELECT * FROM terminal_receipts ORDER BY session_id, created_at, id"
    ),
}

ACTOR_FATE_BACKUP_COLUMNS: dict[str, tuple[str, ...]] = {
    "actor_fate_states": (
        "character_id", "session_id", "state", "state_label", "can_act",
        "terminal", "transitioned_at", "rescue_window_until",
        "rescue_window_kind", "reason", "source", "revision", "updated_at",
    ),
    "actor_fate_transitions": (
        "id", "session_id", "character_id", "from_state", "to_state",
        "reason", "source", "reversible", "rescue_window",
        "protection_consumed", "event_id", "created_at",
    ),
    "rescue_windows": (
        "id", "session_id", "character_id", "kind", "status", "opened_at",
        "expires_on", "allowed_rescue_commands_json", "success_transition_json",
        "failure_transition_json", "command_labels_json", "command", "outcome",
        "completed_at", "revision", "created_at", "updated_at",
    ),
    "terminal_receipts": (
        "id", "session_id", "condition_id", "condition_label", "priority",
        "ending_ref", "termination_type", "archive_policy", "trigger_revision",
        "payload_json", "status", "idempotency_key", "created_at", "updated_at",
    ),
}

ACTOR_FATE_REPLACE_DELETE_ORDER = (
    "rescue_windows",
    "actor_fate_transitions",
    "actor_fate_states",
    "terminal_receipts",
)

ACTOR_FATE_IMPORT_ORDER = (
    "actor_fate_states",
    "actor_fate_transitions",
    "rescue_windows",
    "terminal_receipts",
)


def _rows(data: Mapping[str, Any], table: str) -> Sequence[Mapping[str, Any]]:
    value = data.get(table)
    if not isinstance(value, list):
        raise ValueError(f"备份表 {table} 格式错误")
    if len(value) > 1_000_000:
        raise ValueError(f"备份表 {table} 记录数异常")
    return value


def _same_authoritative_row(
    table: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> bool:
    return all(
        left[column] == right[column]
        for column in ACTOR_FATE_BACKUP_COLUMNS[table]
    )


def _validate_live_primary_row(
    connection: sqlite3.Connection,
    *,
    table: str,
    row: Mapping[str, Any],
    primary_column: str,
    label: str,
) -> None:
    existing = connection.execute(
        f"SELECT * FROM {table} WHERE {primary_column}=?",
        (row[primary_column],),
    ).fetchone()
    if existing is None:
        return
    if not _same_authoritative_row(table, dict(existing), row):
        raise ValueError(
            f"{label}主键已存在但权威内容不一致；为避免部分拼接已取消合并"
        )


def validate_actor_fate_backup_rows(data: Mapping[str, Any]) -> None:
    """Reject cross-session actor links before SQLite can accept them.

    The schema has independent FKs for ``session_id`` and ``character_id``;
    without this check a crafted bundle could attach one session's character
    fate to another valid session.
    """

    session_ids = {
        str(row.get("id") or "")
        for row in data.get("sessions", ())
        if isinstance(row, Mapping)
    }
    character_sessions: dict[str, str] = {}
    for row in data.get("session_characters", ()):
        if not isinstance(row, Mapping):
            raise ValueError("备份角色表含非法记录")
        character_id = str(row.get("id") or "")
        session_id = str(row.get("session_id") or "")
        if not character_id or not session_id or character_id in character_sessions:
            raise ValueError("备份角色表含空值或重复角色")
        character_sessions[character_id] = session_id

    seen_primary: dict[str, set[str]] = {
        table: set() for table in ACTOR_FATE_BACKUP_COLUMNS
    }
    open_windows: set[tuple[str, str, str]] = set()
    terminal_keys: set[str] = set()
    transition_events: set[tuple[str, str, str]] = set()
    fate_state_identities: set[tuple[str, str]] = set()
    for table in ACTOR_FATE_IMPORT_ORDER:
        primary_column = "character_id" if table == "actor_fate_states" else "id"
        for row in _rows(data, table):
            if not isinstance(row, Mapping):
                raise ValueError(f"备份表 {table} 含非法记录")
            missing = [
                column
                for column in ACTOR_FATE_BACKUP_COLUMNS[table]
                if column not in row
            ]
            if missing:
                raise ValueError(f"备份表 {table} 缺少字段 {missing[0]}")
            primary = str(row.get(primary_column) or "")
            if not primary or primary in seen_primary[table]:
                raise ValueError(f"备份表 {table} 含空值或重复主键")
            seen_primary[table].add(primary)
            session_id = str(row.get("session_id") or "")
            if not session_id or session_id not in session_ids:
                raise ValueError(f"备份表 {table} 引用了不存在的副本")
            if table != "terminal_receipts":
                character_id = str(row.get("character_id") or "")
                if character_sessions.get(character_id) != session_id:
                    raise ValueError(
                        f"备份表 {table} 的角色与副本归属不一致"
                    )
                identity = (session_id, character_id)
                if table == "actor_fate_states":
                    fate_state_identities.add(identity)
                elif identity not in fate_state_identities:
                    raise ValueError(
                        f"备份表 {table} 缺少对应的角色命运状态"
                    )
            if table == "rescue_windows" and str(row.get("status")) == "open":
                identity = (
                    session_id,
                    str(row.get("character_id") or ""),
                    str(row.get("kind") or "default"),
                )
                if identity in open_windows:
                    raise ValueError("备份含重复的开放救援窗口")
                open_windows.add(identity)
            if table == "actor_fate_transitions":
                event_id = str(row.get("event_id") or "")
                if event_id:
                    identity = (
                        session_id,
                        str(row.get("character_id") or ""),
                        event_id,
                    )
                    if identity in transition_events:
                        raise ValueError("备份含重复的命运事件流转")
                    transition_events.add(identity)
            if table == "terminal_receipts":
                key = str(row.get("idempotency_key") or "")
                if key and key in terminal_keys:
                    raise ValueError("备份含重复的终局防重复凭证")
                if key:
                    terminal_keys.add(key)


def validate_actor_fate_merge_conflicts(
    connection: sqlite3.Connection,
    data: Mapping[str, Any],
) -> None:
    """Preflight the authoritative fate aggregate before insert-only merge.

    A primary-key replay is safe only when every persisted column is equal.
    The generic insert-only importer must not preserve a diverged state while
    backfilling stale transitions, rescue windows, or receipts.
    """

    checked_characters: set[tuple[str, str]] = set()
    for table in ("actor_fate_states", "actor_fate_transitions", "rescue_windows"):
        for row in _rows(data, table):
            identity = (str(row["character_id"]), str(row["session_id"]))
            if identity in checked_characters:
                continue
            checked_characters.add(identity)
            existing = connection.execute(
                "SELECT session_id FROM session_characters WHERE id=?",
                (identity[0],),
            ).fetchone()
            if existing is not None and str(existing["session_id"]) != identity[1]:
                raise ValueError(
                    "当前角色标识属于另一副本；为避免跨副本串档已取消合并"
                )

    primary_specs = (
        ("actor_fate_states", "character_id", "角色命运状态"),
        ("actor_fate_transitions", "id", "角色命运流转"),
        ("rescue_windows", "id", "角色救援窗口"),
        ("terminal_receipts", "id", "终局回执"),
    )
    for table, primary_column, label in primary_specs:
        for row in _rows(data, table):
            _validate_live_primary_row(
                connection,
                table=table,
                row=row,
                primary_column=primary_column,
                label=label,
            )

    for row in _rows(data, "actor_fate_transitions"):
        event_id = str(row.get("event_id") or "")
        if not event_id:
            continue
        existing = connection.execute(
            """
            SELECT id FROM actor_fate_transitions
            WHERE session_id=? AND character_id=? AND event_id=?
            """,
            (row["session_id"], row["character_id"], event_id),
        ).fetchone()
        if existing is not None and str(existing["id"]) != str(row["id"]):
            raise ValueError(
                "命运事件已属于另一流转；为避免重复历史已取消合并"
            )
    for row in _rows(data, "rescue_windows"):
        if str(row.get("status") or "") != "open":
            continue
        existing = connection.execute(
            """
            SELECT id FROM rescue_windows
            WHERE session_id=? AND character_id=? AND kind=? AND status='open'
            """,
            (
                row["session_id"],
                row["character_id"],
                str(row.get("kind") or "default"),
            ),
        ).fetchone()
        if existing is not None and str(existing["id"]) != str(row["id"]):
            raise ValueError(
                "当前角色已有另一开放救援窗口；为避免串档已取消合并"
            )
    for row in _rows(data, "terminal_receipts"):
        key = str(row.get("idempotency_key") or "")
        if not key:
            continue
        existing = connection.execute(
            "SELECT id FROM terminal_receipts WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None and str(existing["id"]) != str(row["id"]):
            raise ValueError(
                "终局防重复凭证已属于另一回执；为避免串档已取消合并"
            )


__all__ = [
    "ACTOR_FATE_BACKUP_COLUMNS",
    "ACTOR_FATE_BACKUP_QUERIES",
    "ACTOR_FATE_IMPORT_ORDER",
    "ACTOR_FATE_REPLACE_DELETE_ORDER",
    "validate_actor_fate_backup_rows",
    "validate_actor_fate_merge_conflicts",
]
