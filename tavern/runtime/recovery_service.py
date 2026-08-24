"""staged backup restore with automatic rollback.

Format 2 backups are full-fidelity physical restores.  They are deliberately
not merged row-by-row because evidence, receipts, outboxes and projections form
one causal dataset and cannot be safely spliced into a live catalog.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..backup_service import _normalize_backup_catalog
from ..constants import DATABASE_SCHEMA_VERSION, PLUGIN_VERSION
from ..storage import file_sha256, replace_with_retry, unlink_with_retry


MAX_BACKUP_UNCOMPRESSED = 4 * 1024 * 1024 * 1024
PREVIEW_TTL_SECONDS = 30 * 60
CONFIRM_TEXT = "恢复备份"
MANAGED_ROOTS = ("groups", "world_packages_twp")
MANAGED_FILES = ("plugin_modules.json",)


def _read_schema_version(path: Path) -> int:
    """Read the catalog version without importing retired migration code."""
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute(
            "SELECT value FROM tavern_meta WHERE key='schema_version'"
        ).fetchone()
    return int(row[0]) if row else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_member(name: str) -> PurePosixPath:
    if not name or "\\" in name:
        raise ValueError("ZIP 备份含非法文件路径")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("ZIP 备份含非法文件路径")
    return path


def _checksums(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        info = archive.getinfo("checksum.sha256")
    except KeyError as exc:
        raise ValueError("ZIP 备份缺少 checksum.sha256") from exc
    if info.file_size > 16 * 1024 * 1024:
        raise ValueError("ZIP 备份校验清单过大")
    try:
        text = archive.read(info).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("ZIP 备份校验清单编码错误") from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("  ", 1)
        if len(parts) != 2:
            raise ValueError("ZIP 备份校验清单格式错误")
        digest, name = parts[0].strip().lower(), parts[1].strip()
        _safe_member(name)
        if (
            len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or name in result
        ):
            raise ValueError("ZIP 备份校验清单格式错误")
        result[name] = digest
    return result


def verify_backup_archive(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP 备份已损坏或格式无效") from exc
    with archive:
        names: set[str] = set()
        total = 0
        for info in archive.infolist():
            member = _safe_member(info.filename.rstrip("/"))
            name = member.as_posix()
            if name in names:
                raise ValueError("ZIP 备份含重复文件名")
            names.add(name)
            if info.flag_bits & 0x1:
                raise ValueError("不支持加密 ZIP 备份")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise ValueError("ZIP 备份不能包含符号链接")
            if not info.is_dir():
                total += int(info.file_size)
        if total > MAX_BACKUP_UNCOMPRESSED:
            raise ValueError("ZIP 解压后的总大小不能超过 4 GiB")
        checksums = _checksums(archive)
        payload_names = {
            info.filename
            for info in archive.infolist()
            if not info.is_dir() and info.filename != "checksum.sha256"
        }
        if payload_names != set(checksums):
            raise ValueError("ZIP 备份文件与校验清单不一致")
        for info in archive.infolist():
            if info.is_dir() or info.filename == "checksum.sha256":
                continue
            digest = hashlib.sha256()
            with archive.open(info, "r") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != checksums[info.filename]:
                raise ValueError(f"ZIP 备份文件校验失败：{info.filename}")
        try:
            bundle_info = archive.getinfo("bundle.json")
            catalog_info = archive.getinfo("catalog.sqlite3")
        except KeyError as exc:
            raise ValueError("完整备份缺少 bundle.json 或 catalog.sqlite3") from exc
        if bundle_info.file_size > 25 * 1024 * 1024:
            raise ValueError("ZIP 内的 bundle.json 过大")
        try:
            bundle = json.loads(archive.read(bundle_info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份清单无法解析") from exc
        if not isinstance(bundle, dict):
            raise ValueError("备份清单必须是 JSON 对象")
        if bundle.get("format") != "astrbot-tavern-backup":
            raise ValueError("不是有效的 321开团备份")
        version = int(bundle.get("format_version") or 0)
        if version != 2:
            raise ValueError(
                "完整恢复只接受格式 2 ZIP；旧格式请使用“旧格式安全合并”"
            )
        if catalog_info.file_size < 1:
            raise ValueError("备份数据库为空")
        return bundle, checksums


def _database_integrity(path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        quick = connection.execute("PRAGMA quick_check").fetchone()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if quick is None or str(quick[0]).lower() != "ok":
            raise ValueError("备份数据库完整性检查失败")
        if foreign:
            raise ValueError(f"备份数据库外键检查失败：{len(foreign)} 项")
        counts = {}
        for table in (
            "worlds",
            "sessions",
            "actors",
            "ai_companion_instances",
            "item_instances",
            "session_events",
            "delivery_outbox",
            "storage_sync_outbox",
            "event_outbox",
            "author_jobs",
            "world_analysis_artifacts",
        ):
            exists = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name=?
                """,
                (table,),
            ).fetchone()
            counts[table] = (
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if exists is not None
                else 0
            )
    return counts


def _set_maintenance(path: Path, enabled: bool) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT INTO tavern_meta(key, value) VALUES ('maintenance_mode', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            ("1" if enabled else "0",),
        )
        connection.commit()


def _operation_receipt(
    path: Path,
    *,
    operation_id: str,
    input_hash: str,
    result: dict[str, Any],
) -> None:
    now = _now()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO operation_commits(
                operation_id, session_id, input_hash, status,
                result_json, rollback_json, created_at, updated_at
            ) VALUES (?, '', ?, 'completed', ?, '{}', ?, ?)
            """,
            (
                operation_id,
                input_hash,
                json.dumps(
                    result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_logs(
                session_id, actor_id, action, target, detail_json, created_at
            ) VALUES ('', ?, 'backup.restore', '', ?, ?)
            """,
            (
                str(result.get("actor_id") or "system"),
                json.dumps(
                    {
                        "operation_id": operation_id,
                        "archive_sha256": result["archive_sha256"],
                        "schema_version": result["schema_version"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
        connection.commit()


def _existing_receipt(
    path: Path,
    operation_id: str,
    input_hash: str,
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute(
                """
                SELECT input_hash, result_json FROM operation_commits
                WHERE operation_id=? AND status='completed'
                """,
                (operation_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    if str(row[0] or "") != input_hash:
        raise ValueError("该防重复凭据已用于另一项恢复操作")
    try:
        result = json.loads(str(row[1] or "{}"))
    except json.JSONDecodeError:
        result = {}
    return {**dict(result), "replayed": True}


class BackupRecoveryService:
    def __init__(self, data_dir: Path, database: Any) -> None:
        self.data_dir = Path(data_dir)
        self.database = database
        self.imports_dir = self.data_dir / "imports"
        self.rollback_dir = self.data_dir / "recovery_backups"

    def _stage_dir(self, token: str) -> Path:
        if (
            len(token) != 32
            or any(character not in "0123456789abcdef" for character in token)
        ):
            raise ValueError("恢复预览凭据无效")
        return self.imports_dir / f".restore-{token}"

    def preview(self, archive_path: Path, *, mode: str) -> dict[str, Any]:
        if mode != "replace":
            raise ValueError(
                "完整备份只能使用“全部覆盖”；"
                "旧格式合并请使用原导入入口"
            )
        bundle, checksums = verify_backup_archive(archive_path)
        archive_sha = file_sha256(archive_path)
        token = hashlib.sha256(
            f"{archive_sha}:{mode}".encode("utf-8")
        ).hexdigest()[:32]
        stage = self._stage_dir(token)
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True, exist_ok=False)
        try:
            catalog = stage / "catalog.sqlite3"
            data_stage = stage / "data"
            data_stage.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                with archive.open("catalog.sqlite3", "r") as source, catalog.open(
                    "wb"
                ) as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    relative = _safe_member(info.filename)
                    if (
                        relative.parts[0] not in {*MANAGED_ROOTS, *MANAGED_FILES}
                    ):
                        continue
                    target = data_stage.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info, "r") as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            expected_database_sha = str(bundle.get("database_sha256") or "")
            if expected_database_sha and file_sha256(catalog) != expected_database_sha:
                raise ValueError("备份数据库与清单 SHA-256 不一致")
            source_schema = _read_schema_version(catalog)
            if source_schema != DATABASE_SCHEMA_VERSION:
                raise ValueError(
                    f"备份使用旧版数据结构 Schema {source_schema}；当前仅接受 "
                    f"Schema {DATABASE_SCHEMA_VERSION}。系统未改动现有数据，请先在旧版中导出可迁移内容。"
                )
            _normalize_backup_catalog(catalog)
            target_schema = _read_schema_version(catalog)
            counts = _database_integrity(catalog)
            format_version = int(bundle.get("format_version") or 0)
            if format_version != 2:
                raise ValueError("完整恢复只接受格式 2 ZIP")
            current_counts = (
                _database_integrity(Path(self.database.path))
                if Path(self.database.path).is_file()
                else {}
            )
            preview = {
                "schema": "tavern-backup-restore-preview/1.0.0-rc10",
                "token": token,
                "mode": mode,
                "format_version": format_version,
                "archive_sha256": archive_sha,
                "source_schema": source_schema,
                "target_schema": target_schema,
                "plugin_version": str(bundle.get("plugin_version") or ""),
                "counts": counts,
                "current_counts": current_counts,
                "will_replace": [
                    "目录数据库",
                    "副本实例存储",
                    "世界包索引",
                    "模块状态",
                ],
                "rollback": "执行前自动生成目录数据库与托管文件回退点",
                "confirm_text": CONFIRM_TEXT,
                "expires_at": (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=PREVIEW_TTL_SECONDS)
                ).isoformat(timespec="seconds"),
                "created_at": _now(),
            }
            (stage / "preview.json").write_text(
                json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            shutil.copyfile(archive_path, stage / "source.zip")
            return preview
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def execute(
        self,
        token: str,
        *,
        confirm_text: str,
        operation_id: str,
        actor_id: str,
        fault_after_catalog_replace: bool = False,
    ) -> dict[str, Any]:
        if confirm_text != CONFIRM_TEXT:
            raise ValueError(f"请输入“{CONFIRM_TEXT}”确认执行")
        if not operation_id:
            raise ValueError("恢复执行缺少防重复凭据")
        input_hash = hashlib.sha256(
            json.dumps(
                {"token": token, "confirm_text": confirm_text},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        existing = _existing_receipt(
            Path(self.database.path),
            operation_id,
            input_hash,
        )
        if existing is not None:
            return existing
        stage = self._stage_dir(token)
        preview_path = stage / "preview.json"
        catalog = stage / "catalog.sqlite3"
        data_stage = stage / "data"
        if not preview_path.is_file() or not catalog.is_file():
            raise ValueError("恢复预览已失效，请重新上传备份")
        preview = json.loads(preview_path.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(str(preview["expires_at"]))
        if datetime.now(timezone.utc) > expires.astimezone(timezone.utc):
            raise ValueError("恢复预览已过期，请重新上传备份")
        candidate = stage / "candidate.sqlite3"
        shutil.copyfile(catalog, candidate)
        _set_maintenance(candidate, True)
        _database_integrity(candidate)
        self.rollback_dir.mkdir(parents=True, exist_ok=True)
        rollback = self.rollback_dir / (
            "restore-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + token[:8]
        )
        rollback.mkdir(parents=True, exist_ok=False)
        rollback_catalog = rollback / "catalog_d1.sqlite3"
        current_catalog = Path(self.database.path)
        managed_backups: list[tuple[Path, Path]] = []
        installed_targets: list[Path] = []
        result = {
            "summary": "备份恢复已完成，完整性检查通过。",
            "archive_sha256": str(preview["archive_sha256"]),
            "schema_version": DATABASE_SCHEMA_VERSION,
            "rollback_label": rollback.name,
            "actor_id": actor_id,
            "replayed": False,
        }
        try:
            if current_catalog.is_file():
                with closing(sqlite3.connect(current_catalog)) as source:
                    source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    with closing(sqlite3.connect(rollback_catalog)) as target:
                        source.backup(target)
                _database_integrity(rollback_catalog)
                _set_maintenance(current_catalog, True)
            for name in (*MANAGED_ROOTS, *MANAGED_FILES):
                current = self.data_dir / name
                backup = rollback / name
                if current.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(current, backup)
                    managed_backups.append((current, backup))
            replace_with_retry(candidate, current_catalog)
            for suffix in ("-wal", "-shm"):
                unlink_with_retry(
                    Path(str(current_catalog) + suffix),
                    suppress_errors=True,
                )
            if fault_after_catalog_replace:
                raise OSError("fault injection after catalog replace")
            for name in (*MANAGED_ROOTS, *MANAGED_FILES):
                staged = data_stage / name
                target = self.data_dir / name
                if not staged.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, target)
                installed_targets.append(target)
            _database_integrity(current_catalog)
            if _read_schema_version(current_catalog) != DATABASE_SCHEMA_VERSION:
                raise RuntimeError("恢复后的数据库版本不正确")
            _operation_receipt(
                current_catalog,
                operation_id=operation_id,
                input_hash=input_hash,
                result=result,
            )
            _set_maintenance(current_catalog, False)
            if hasattr(self.database, "storage"):
                self.database.storage.bootstrap()
            shutil.rmtree(stage, ignore_errors=True)
            return result
        except Exception:
            for target in reversed(installed_targets):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    unlink_with_retry(target, suppress_errors=True)
            for current, backup in reversed(managed_backups):
                if backup.exists():
                    current.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, current)
            if rollback_catalog.is_file():
                failed = rollback / "failed-candidate.sqlite3"
                if current_catalog.is_file():
                    replace_with_retry(current_catalog, failed)
                shutil.copyfile(rollback_catalog, current_catalog)
                _set_maintenance(current_catalog, False)
                _database_integrity(current_catalog)
            raise


__all__ = [
    "BackupRecoveryService",
    "CONFIRM_TEXT",
    "PREVIEW_TTL_SECONDS",
    "verify_backup_archive",
]
