"""A15：完整备份导出与自动备份调度（复用控制台导出逻辑）。

``build_backup_archive`` 生成与控制台「备份导出」完全一致的 ZIP：
``bundle.json``（Schema 数据）+ ``catalog.sqlite3``（物理库）+ 各群独立存档
目录 + 世界包目录 + 模块状态 + ``checksum.sha256``。
``prune_backups`` 保留最近 N 份自动备份。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import DATABASE_SCHEMA_VERSION, PLUGIN_VERSION
from .repositories.configuration_revision_security import (
    sanitize_configuration_revisions,
)
from .storage import (
    file_sha256,
    next_timestamped_path,
    replace_with_retry,
    unlink_with_retry,
)

BACKUP_PREFIX = "backup_tavern"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_backup_catalog(path: Path) -> dict[str, Any]:
    """Clear process-owned leases in the offline backup copy."""

    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        def columns(table: str) -> set[str]:
            if table not in tables:
                return set()
            return {
                str(row["name"])
                for row in connection.execute(
                    f'PRAGMA table_info("{table}")'
                ).fetchall()
            }

        schema_row = (
            connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()
            if "tavern_meta" in tables
            else None
        )
        try:
            schema_version = int(schema_row["value"] if schema_row else 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"备份数据库版本无效；当前仅接受 Schema "
                f"{DATABASE_SCHEMA_VERSION}，系统未继续处理。"
            ) from exc
        if schema_version != DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                f"备份数据库版本不受支持：检测到 Schema {schema_version}，"
                f"当前仅接受 Schema {DATABASE_SCHEMA_VERSION}；"
                "系统未继续处理旧备份。"
            )

        required_receipt_tables = {
            "card_review_receipts",
            "supplement_action_receipts",
        }
        missing_receipt_tables = sorted(required_receipt_tables - tables)
        if missing_receipt_tables:
            raise RuntimeError(
                f"Schema {DATABASE_SCHEMA_VERSION} 备份缺少必要回执表："
                + "、".join(missing_receipt_tables)
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            now = _utc_now()
            if {
                "status",
                "lease_owner",
                "leased_at",
                "next_retry_at",
            }.issubset(columns("delivery_outbox")):
                connection.execute(
                    """
                    UPDATE delivery_outbox SET
                        status=CASE
                            WHEN next_part_index>0 THEN 'partially_sent'
                            ELSE 'retry_wait'
                        END,
                        lease_owner='', leased_at='',
                        next_retry_at=CASE
                            WHEN next_retry_at='' THEN ? ELSE next_retry_at
                        END,
                        updated_at=?
                    WHERE status='leased'
                    """,
                    (now, now),
                )
            for table in ("storage_sync_outbox", "event_outbox"):
                if {
                    "status",
                    "lease_owner",
                    "leased_at",
                    "lease_expires_at",
                    "next_retry_at",
                }.issubset(columns(table)):
                    connection.execute(
                        f"""
                        UPDATE {table} SET
                            status='retry_wait',
                            lease_owner='', leased_at='', lease_expires_at='',
                            next_retry_at=CASE
                                WHEN next_retry_at='' THEN ? ELSE next_retry_at
                            END,
                            updated_at=?
                        WHERE status='leased'
                        """,
                        (now, now),
                    )
            if {
                "status",
                "lease_owner",
                "leased_at",
                "lease_expires_at",
                "next_retry_at",
            }.issubset(columns("author_jobs")):
                connection.execute(
                    """
                    UPDATE author_jobs SET
                        status='retry_wait',
                        lease_owner='', leased_at='', lease_expires_at='',
                        next_retry_at=CASE
                            WHEN next_retry_at='' THEN ? ELSE next_retry_at
                        END,
                        last_error_code='backup.lease_cleared',
                        last_error='备份副本已清理旧进程租约',
                        updated_at=?
                    WHERE status IN ('leased', 'running')
                    """,
                    (now, now),
                )
            if {
                "status",
                "phase",
                "lease_expires_at",
                "last_error_code",
            }.issubset(columns("operation_receipts")):
                connection.execute(
                    """
                    UPDATE operation_receipts SET
                        status='failed_retryable',
                        phase='lease_cleared_for_backup',
                        lease_expires_at='',
                        last_error_code='backup.lease_cleared',
                        updated_at=?
                    WHERE status IN (
                        'reserved', 'generating', 'dice_locked',
                        'ready_to_commit'
                    )
                    """,
                    (now,),
                )
            sanitize_configuration_revisions(
                connection,
                abandon_reserved=True,
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        quick = connection.execute("PRAGMA quick_check").fetchone()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick is None or str(quick[0]).lower() != "ok" or foreign:
            raise RuntimeError("备份数据库副本完整性检查失败")
        outbox_summary: dict[str, Any] = {}
        for table, label in (
            ("delivery_outbox", "delivery"),
            ("storage_sync_outbox", "storage"),
            ("event_outbox", "event"),
            ("author_jobs", "author_jobs"),
        ):
            if "status" in columns(table):
                outbox_summary[label] = {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        f"""
                        SELECT status, COUNT(*) AS count
                        FROM {table} GROUP BY status
                        """
                    ).fetchall()
                }
            elif table in tables:
                outbox_summary[label] = {
                    "legacy_pending": int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                    )
                }
            else:
                outbox_summary[label] = {}
        projection_summary = {
            "checkpoints": int(
                connection.execute(
                    "SELECT COUNT(*) FROM projection_checkpoints"
                ).fetchone()[0]
            ) if "projection_checkpoints" in tables else 0,
            "tendency_profiles": int(
                connection.execute(
                    "SELECT COUNT(*) FROM player_tendency_profiles"
                ).fetchone()[0]
            ) if "player_tendency_profiles" in tables else 0,
            "knowledge_evidence": int(
                connection.execute(
                    "SELECT COUNT(*) FROM npc_knowledge_evidence"
                ).fetchone()[0]
            ) if "npc_knowledge_evidence" in tables else 0,
        }
    return {
        "outbox_summary": outbox_summary,
        "projection_summary": projection_summary,
        "schema_version": schema_version,
        "receipt_tables": {
            table: table in tables
            for table in sorted(required_receipt_tables)
        },
    }


def _write_archive(
    *,
    data_dir: Path,
    database_path: Path,
    bundle: dict[str, Any],
    export_dir: Path,
    prefix: str,
) -> Path:
    """Write a full backup ZIP to ``export_dir`` and return its path."""
    export_dir.mkdir(parents=True, exist_ok=True)
    path = next_timestamped_path(export_dir, prefix, ".zip")
    temporary = path.with_name(f".{path.name}.{uuid4_hex()}.tmp")
    catalog_copy = export_dir / f".catalog.{uuid4_hex()}.sqlite3"
    bundle_bytes = (
        json.dumps(
            bundle,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    checksum_lines = [
        (hashlib.sha256(bundle_bytes).hexdigest(), "bundle.json")
    ]
    try:
        with closing(sqlite3.connect(str(database_path))) as source:
            with closing(sqlite3.connect(catalog_copy)) as target:
                source.backup(target)
        catalog_summary = _normalize_backup_catalog(catalog_copy)
        database_sha256 = file_sha256(catalog_copy)
        bundle = {
            **dict(bundle),
            "format": "astrbot-tavern-backup",
            "format_version": 2,
            "plugin_version": PLUGIN_VERSION,
            "schema_version": int(
                catalog_summary.get("schema_version")
                or DATABASE_SCHEMA_VERSION
            ),
            "database_sha256": database_sha256,
            "outbox_summary": catalog_summary["outbox_summary"],
            "projection_summary": catalog_summary["projection_summary"],
            "created_at": str(
                bundle.get("created_at")
                or bundle.get("exported_at")
                or _utc_now()
            ),
        }
        bundle.pop("exported_at", None)
        bundle_bytes = (
            json.dumps(
                bundle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        checksum_lines = [
            (hashlib.sha256(bundle_bytes).hexdigest(), "bundle.json")
        ]
        checksum_lines.append(
            (database_sha256, "catalog.sqlite3")
        )
        data_files: list[tuple[Path, str]] = []
        for managed_dir in (
            data_dir / "groups",
            data_dir / "world_packages_twp",
        ):
            if not managed_dir.exists():
                continue
            for item in sorted(managed_dir.rglob("*")):
                if (
                    not item.is_file()
                    or item.is_symlink()
                    or item.name.endswith(("-wal", "-shm", ".tmp"))
                    or item.name.startswith(".")
                ):
                    continue
                archive_name = item.relative_to(data_dir).as_posix()
                data_files.append((item, archive_name))
        module_state = data_dir / "plugin_modules.json"
        if module_state.is_file() and not module_state.is_symlink():
            data_files.append((module_state, "plugin_modules.json"))
        managed_members = [
            {
                "path": archive_name,
                "size": int(item.stat().st_size),
                "sha256": file_sha256(item),
            }
            for item, archive_name in data_files
        ]
        bundle = {
            **bundle,
            "world_artifacts": [
                item
                for item in managed_members
                if item["path"].startswith("world_packages_twp/")
            ],
            "instance_storage": [
                item
                for item in managed_members
                if item["path"].startswith("groups/")
            ],
            "managed_state": [
                item
                for item in managed_members
                if item["path"] == "plugin_modules.json"
            ],
        }
        bundle_bytes = (
            json.dumps(
                bundle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        checksum_lines[0] = (
            hashlib.sha256(bundle_bytes).hexdigest(),
            "bundle.json",
        )
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            archive.writestr("bundle.json", bundle_bytes)
            archive.write(catalog_copy, "catalog.sqlite3")
            for item, archive_name in data_files:
                digest = hashlib.sha256()
                with item.open("rb") as source, archive.open(
                    archive_name,
                    "w",
                    force_zip64=True,
                ) as output:
                    for chunk in iter(
                        lambda: source.read(1024 * 1024),
                        b"",
                    ):
                        digest.update(chunk)
                        output.write(chunk)
                checksum_lines.append(
                    (digest.hexdigest(), archive_name)
                )
            archive.writestr(
                "checksum.sha256",
                "".join(
                    f"{digest}  {name}\n"
                    for digest, name in checksum_lines
                ).encode("utf-8"),
            )
        replace_with_retry(temporary, path)
    finally:
        unlink_with_retry(temporary, suppress_errors=True)
        unlink_with_retry(catalog_copy, suppress_errors=True)
    return path


async def build_backup_archive(
    *,
    data_dir: Path,
    database: Any,
    export_dir: Path | None = None,
    prefix: str = BACKUP_PREFIX,
) -> Path:
    """异步构建完整备份 ZIP（bundle 导出在事件循环内，文件写入在线程池）。"""
    bundle = await database.export_bundle()
    target_dir = Path(export_dir or Path(data_dir) / "exports")
    return await asyncio.to_thread(
        _write_archive,
        data_dir=Path(data_dir),
        database_path=Path(database.path),
        bundle=bundle,
        export_dir=target_dir,
        prefix=prefix,
    )


def prune_backups(export_dir: Path, keep_count: int) -> list[Path]:
    """删除最早的自动备份，仅保留最近 ``keep_count`` 份。"""
    export_dir = Path(export_dir)
    if not export_dir.exists():
        return []
    keep = max(1, int(keep_count or 1))
    candidates = sorted(
        (
            path
            for path in export_dir.glob(f"{BACKUP_PREFIX}_*.zip")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for stale in candidates[keep:]:
        unlink_with_retry(stale, suppress_errors=True)
        removed.append(stale)
    return removed


def uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex
