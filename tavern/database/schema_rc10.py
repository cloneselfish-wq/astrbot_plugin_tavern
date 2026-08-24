"""Schema 30 strict world-protocol archive overlay and 29 -> 30 migration."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any


ARCHIVE_SCHEMA = "tavern-protocol-archive/1.0.0-rc10"
BACKUP_SCHEMA = "tavern-schema30-backup/1.0.0-rc10"
TARGET_PLUGIN_VERSION = "1.0.0-rc10"
TARGET_WORLD_SCHEMA = 12


def _archive_guard(table: str, session_expression: str) -> list[str]:
    statements: list[str] = []
    for operation, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        expression = session_expression.replace("{row}", row)
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_rc10_archive_{table}_{operation.lower()}
            BEFORE {operation} ON {table}
            WHEN EXISTS (
                SELECT 1 FROM protocol_archive_receipts archive
                WHERE archive.target_kind='session'
                  AND archive.target_id=({expression})
                  AND archive.readonly=1
            )
            BEGIN
                SELECT RAISE(ABORT, 'WORLD_PROTOCOL_UNSUPPORTED');
            END;
            """
        )
    return statements


def _world_guard(table: str, world_expression: str) -> list[str]:
    statements: list[str] = []
    for operation, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
        expression = world_expression.replace("{row}", row)
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_rc10_archive_{table}_{operation.lower()}
            BEFORE {operation} ON {table}
            WHEN EXISTS (
                SELECT 1 FROM protocol_archive_receipts archive
                WHERE archive.target_kind='world'
                  AND archive.target_id=({expression})
                  AND archive.readonly=1
            )
            BEGIN
                SELECT RAISE(ABORT, 'WORLD_PROTOCOL_UNSUPPORTED');
            END;
            """
        )
    return statements


_SESSION_GUARDS = {
    "sessions": "{row}.id",
    "instance_configs": "{row}.session_id",
    "players": "{row}.session_id",
    "participants": "{row}.session_id",
    "events": "{row}.session_id",
    "memories": "{row}.session_id",
    "snapshots": "{row}.session_id",
    "character_runtime_states": "{row}.session_id",
    "choice_sets": "{row}.session_id",
    "group_votes": "{row}.session_id",
    "selected_world_events": "{row}.session_id",
    "timer_instances": "{row}.session_id",
    "delegation_grants": "{row}.session_id",
    "permission_grants": "{row}.session_id",
    "ban_records": "{row}.session_id",
    "return_requests": "{row}.session_id",
    "session_rule_states": "{row}.session_id",
    "dm_control_states": "{row}.session_id",
    "session_characters": "{row}.session_id",
    "session_narrative_styles": "{row}.session_id",
    "gameplay_states": "{row}.session_id",
    "gameplay_receipts": "{row}.session_id",
    "story_ledger": "{row}.session_id",
    "scene_clocks": "{row}.session_id",
    "session_events": "{row}.session_id",
    "projection_checkpoints": "{row}.session_id",
    "delivery_targets": "{row}.session_id",
    "actor_fate_states": "{row}.session_id",
    "actor_fate_transitions": "{row}.session_id",
    "rescue_windows": "{row}.session_id",
    "terminal_receipts": "{row}.session_id",
    "session_finalizations": "{row}.session_id",
    "session_opening_decisions": "{row}.session_id",
    "principal_bindings": "{row}.session_id",
    "room_invites": "{row}.session_id",
    "choice_recovery_receipts": "{row}.session_id",
    "world_module_runtime_status": "{row}.session_id",
    "character_card_drafts": "(SELECT session_id FROM participants WHERE id={row}.participant_id)",
    "rolls": "(SELECT session_id FROM choice_sets WHERE id={row}.choice_set_id)",
    "vote_ballots": "(SELECT session_id FROM group_votes WHERE id={row}.vote_id)",
}

_WORLD_GUARDS = {
    "worlds": "{row}.id",
    "characters": "{row}.world_id",
    "world_feature_versions": "{row}.world_id",
    "world_entity_registry": "{row}.world_id",
    "world_rule_revisions": "{row}.world_id",
    "world_snapshots": "{row}.world_id",
    "world_module_runtime_status": "(SELECT world_id FROM sessions WHERE id={row}.session_id)",
}

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS protocol_archive_receipts (
    target_kind TEXT NOT NULL CHECK(target_kind IN ('world', 'session')),
    target_id TEXT NOT NULL,
    source_database_schema INTEGER NOT NULL,
    source_world_schema INTEGER NOT NULL,
    source_protocol TEXT NOT NULL,
    archive_schema TEXT NOT NULL,
    backup_ref TEXT NOT NULL,
    backup_bytes INTEGER NOT NULL,
    backup_sha256 TEXT NOT NULL,
    inventory_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    readonly INTEGER NOT NULL DEFAULT 1 CHECK(readonly=1),
    created_at TEXT NOT NULL,
    PRIMARY KEY(target_kind, target_id)
);
CREATE INDEX IF NOT EXISTS idx_protocol_archive_receipts_created
ON protocol_archive_receipts(created_at DESC);
"""
TRIGGER_STATEMENTS = tuple(
    statement
    for table, expression in _SESSION_GUARDS.items()
    for statement in _archive_guard(table, expression)
) + tuple(
    statement
    for table, expression in _WORLD_GUARDS.items()
    for statement in _world_guard(table, expression)
)
TRIGGER_SQL = "\n".join(TRIGGER_STATEMENTS)
SCHEMA_SQL = TABLE_SQL + "\n" + TRIGGER_SQL


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _world_contract_identity(value: Any) -> tuple[int, str]:
    if not isinstance(value, dict):
        return 0, ""
    rules = value.get("rules") if isinstance(value.get("rules"), dict) else {}
    protocol = value.get("protocol") if isinstance(value.get("protocol"), dict) else {}
    try:
        schema = int(value.get("internal_world_model_revision") or rules.get("internal_world_model_revision") or 0)
    except (TypeError, ValueError):
        schema = 0
    version = str(protocol.get("version") or rules.get("protocol", {}).get("version") if isinstance(rules.get("protocol"), dict) else "")
    return schema, version


def _copy_verified_backup(data_dir: Path, database_path: Path, legacy_sessions: list[str]) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = data_dir / "legacy-pre-rc10" / stamp / "protocol-archive"
    stage = backup_dir.with_name(f".{backup_dir.name}.stage")
    stage.mkdir(parents=True, exist_ok=False)
    members: list[dict[str, Any]] = []
    try:
        database_target = stage / "database" / database_path.name
        database_target.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database_path)) as source:
            with closing(sqlite3.connect(database_target)) as target:
                source.backup(target)
        with closing(sqlite3.connect(database_target)) as check:
            integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
            foreign = list(check.execute("PRAGMA foreign_key_check").fetchall())
        if integrity.casefold() != "ok" or foreign:
            raise RuntimeError("Schema 30 迁移备份完整性检查失败")
        sources = [(database_target, "database/" + database_target.name)]
        groups = data_dir / "groups"
        if groups.exists():
            for source in sorted(groups.rglob("*")):
                if source.is_symlink():
                    raise RuntimeError("旧副本存档包含链接，迁移已停止")
                if source.is_file():
                    relative = "saves/groups/" + source.relative_to(groups).as_posix()
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    sources.append((target, relative))
        for source, relative in sources:
            members.append({"path": relative, "bytes": source.stat().st_size, "sha256": _sha(source)})
        inventory_sha256 = hashlib.sha256(_canonical(members)).hexdigest()
        manifest = {
            "schema": BACKUP_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_database_schema": 29,
            "target_database_schema": 30,
            "legacy_sessions": sorted(legacy_sessions),
            "members": members,
            "inventory_sha256": inventory_sha256,
        }
        manifest_path = stage / "manifest.json"
        manifest_path.write_bytes(_canonical(manifest) + b"\n")
        receipt = {
            "schema": BACKUP_SCHEMA,
            "verified": True,
            "manifest_sha256": _sha(manifest_path),
            "inventory_sha256": inventory_sha256,
            "member_count": len(members),
        }
        (stage / "receipt.json").write_bytes(_canonical(receipt) + b"\n")
        for member in members:
            path = stage / str(member["path"])
            if path.stat().st_size != member["bytes"] or _sha(path) != member["sha256"]:
                raise RuntimeError("Schema 30 迁移备份复核失败")
        backup_dir.parent.mkdir(parents=True, exist_ok=True)
        stage.replace(backup_dir)
        return {
            "backup_dir": str(backup_dir),
            "database_path": str(backup_dir / "database" / database_path.name),
            "database_bytes": (backup_dir / "database" / database_path.name).stat().st_size,
            "database_sha256": _sha(backup_dir / "database" / database_path.name),
            "inventory_sha256": inventory_sha256,
            "manifest_sha256": receipt["manifest_sha256"],
        }
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def migrate_schema_29_to_30(data_dir: Path, database_path: Path) -> dict[str, Any]:
    """Backup Schema 29, preserve rows, and overlay old protocol data as readonly."""

    legacy_sessions: list[dict[str, Any]] = []
    legacy_worlds: list[dict[str, Any]] = []
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        version_row = connection.execute("SELECT value FROM tavern_meta WHERE key='schema_version'").fetchone()
        if not version_row or int(version_row[0]) != 29:
            raise RuntimeError("仅支持从 Schema 29 原地迁移到 Schema 30")
        for row in connection.execute("SELECT session_id, world_snapshot_json FROM instance_configs ORDER BY session_id"):
            try:
                snapshot = json.loads(str(row["world_snapshot_json"] or "{}"))
            except json.JSONDecodeError:
                snapshot = {}
            schema, protocol = _world_contract_identity(snapshot)
            if schema != TARGET_WORLD_SCHEMA or protocol != TARGET_PLUGIN_VERSION:
                legacy_sessions.append({"id": str(row["session_id"]), "world_schema": schema, "protocol": protocol})
        for row in connection.execute("SELECT id, rules_json FROM worlds ORDER BY id"):
            try:
                rules = json.loads(str(row["rules_json"] or "{}"))
            except json.JSONDecodeError:
                rules = {}
            schema, protocol = _world_contract_identity({"rules": rules, "protocol": rules.get("protocol", {}) if isinstance(rules, dict) else {}})
            if schema != TARGET_WORLD_SCHEMA or protocol != TARGET_PLUGIN_VERSION:
                legacy_worlds.append({"id": str(row["id"]), "world_schema": schema, "protocol": protocol})
    backup = _copy_verified_backup(data_dir, database_path, [item["id"] for item in legacy_sessions])
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + TABLE_SQL)
            rows = [("session", item) for item in legacy_sessions] + [("world", item) for item in legacy_worlds]
            for kind, item in rows:
                result = {
                    "status": "readonly",
                    "reason": "RC10 只运行 World Schema 12；旧数据已完整备份并保留只读",
                    "automatic_action": "系统保留原记录并阻止后续写入，没有猜测或迁移字段",
                    "next_step": "导出旧档案查看，或在 RC10 世界中新建副本和角色",
                }
                connection.execute(
                    """
                    INSERT OR IGNORE INTO protocol_archive_receipts(
                        target_kind,target_id,source_database_schema,
                        source_world_schema,source_protocol,archive_schema,
                        backup_ref,backup_bytes,backup_sha256,inventory_sha256,
                        result_json,readonly,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        kind,
                        item["id"],
                        29,
                        int(item["world_schema"] or 0),
                        str(item["protocol"] or "unknown"),
                        ARCHIVE_SCHEMA,
                        backup["backup_dir"],
                        int(backup["database_bytes"]),
                        backup["database_sha256"],
                        backup["inventory_sha256"],
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        1,
                        now,
                    ),
                )
            connection.execute("UPDATE tavern_meta SET value='30' WHERE key='schema_version'")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
    return {
        "schema": "tavern-schema-migration/29-to-30",
        "backup": backup,
        "archived_sessions": len(legacy_sessions),
        "archived_worlds": len(legacy_worlds),
        "readonly": True,
    }


__all__ = [
    "SCHEMA_SQL",
    "TABLE_SQL",
    "TRIGGER_SQL",
    "TRIGGER_STATEMENTS",
    "migrate_schema_29_to_30",
]
