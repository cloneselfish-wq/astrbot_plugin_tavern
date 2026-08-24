from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INSTANCE_FORMAT = "astrbot-tavern-instance"
INSTANCE_FORMAT_VERSION = 1
TIMESTAMP_PATTERN = re.compile(r"^\d{14}$")
FILE_OPERATION_ATTEMPTS = 6
FILE_OPERATION_INITIAL_DELAY = 0.05


def _retry_file_operation(
    operation: Callable[[], Any],
    *,
    attempts: int = FILE_OPERATION_ATTEMPTS,
) -> Any:
    """Retry a short-lived Windows sharing violation without hiding failures."""

    delay = FILE_OPERATION_INITIAL_DELAY
    for attempt in range(max(1, int(attempts))):
        try:
            return operation()
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or getattr(
                exc,
                "winerror",
                None,
            ) in {5, 32, 33}
            if not retryable or attempt + 1 >= attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.4)
    raise RuntimeError("文件操作重试状态异常")


def replace_with_retry(source: Path, destination: Path) -> None:
    _retry_file_operation(lambda: os.replace(source, destination))


def unlink_with_retry(
    path: Path,
    *,
    missing_ok: bool = True,
    suppress_errors: bool = False,
) -> None:
    try:
        _retry_file_operation(
            lambda: path.unlink(missing_ok=missing_ok)
        )
    except OSError:
        if not suppress_errors:
            raise


def timestamp14(value: str | datetime | None = None) -> str:
    """Return a sortable fourteen-digit timestamp in the server timezone."""

    moment: datetime
    if isinstance(value, datetime):
        moment = value
    elif value:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            # Corrupt imported metadata must not produce a different folder
            # name on every retry. A fixed epoch is safer than "now".
            moment = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        moment = datetime.now().astimezone()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y%m%d%H%M%S")


def safe_component(value: Any, fallback: str, maximum: int = 64) -> str:
    text = re.sub(
        r"[^a-z0-9_-]+",
        "-",
        str(value or "").strip().casefold(),
    )
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    return (text or fallback)[:maximum]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def stable_group_folder(platform_id: str, group_id: str) -> str:
    platform = safe_component(platform_id, "platform", 32)
    digest = hashlib.sha256(
        f"{platform_id}\0{group_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{platform}_g_{digest}"


def stable_story_folder(
    instance_slug: str,
    created_at: str,
    session_id: str,
) -> str:
    slug = safe_component(instance_slug, "story", 52)
    stamp = timestamp14(created_at)
    token_source = str(session_id).removeprefix("session_")
    token = safe_component(token_source, "", 8)
    if len(token) < 6:
        token = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:8]
    return f"{slug}_{stamp}_i-{token}"


def next_timestamped_path(
    directory: Path,
    prefix: str,
    suffix: str,
    *,
    stamp: str | None = None,
) -> Path:
    normalized_stamp = stamp or timestamp14()
    if not TIMESTAMP_PATTERN.fullmatch(normalized_stamp):
        raise ValueError("存档时间必须是 YYYYMMDDHHMMSS 十四位数字")
    base = directory / f"{prefix}_{normalized_stamp}{suffix}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = directory / (
            f"{prefix}_{normalized_stamp}_{index:02d}{suffix}"
        )
        if not candidate.exists():
            return candidate
    raise RuntimeError("同一秒内生成的同名存档过多")




class ArchivesMixin:
    def create_archive(
        self,
        session_id: str,
        *,
        kind: str,
        reason: str,
        refresh: bool = True,
    ) -> Path:
        with self._lock:
            if refresh:
                self.sync_session(session_id)
            return self._archive_synced(
                session_id,
                kind=kind,
                reason=reason,
            )
    def bootstrap(self) -> list[dict[str, Any]]:
        with self.connect_catalog() as connection:
            session_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM sessions ORDER BY created_at, id"
                ).fetchall()
            ]
        results: list[dict[str, Any]] = []
        for session_id in session_ids:
            indexed = self._ensure_index(session_id)
            story_dir = (
                self.data_dir
                / str(indexed["storage"]["relative_path"])
            )
            database_path = story_dir / "instance.sqlite3"
            manifest_path = story_dir / "manifest.json"
            expected = str(
                indexed["storage"].get("last_checksum") or ""
            )
            # A14（审计 #14）：副本修订号未变化时跳过校验和读取（快路径）。
            last_revision = int(indexed["storage"].get("last_synced_revision") or 0)
            current_revision = int(indexed["session"].get("revision") or 0)
            if (
                last_revision >= current_revision
                and indexed["storage"].get("sync_status") == "ready"
                and expected
                and database_path.exists()
                and manifest_path.exists()
                and file_sha256(database_path) == expected
            ):
                results.append(
                    {
                        "session_id": session_id,
                        "relative_path": str(
                            indexed["storage"]["relative_path"]
                        ),
                        "database": database_path,
                        "manifest": manifest_path,
                        "checksum": expected,
                        "skipped": True,
                    }
                )
                continue
            try:
                result = self.sync_session(session_id)
                results.append(result)
            except (sqlite3.DatabaseError, OSError) as exc:
                # 单个副本的存储同步失败不应阻断插件整体加载：
                # 记录错误并继续，控制台「群会话 / 跑团现场」会展示
                # storage_sync_status=error 与 last_error，便于后续修复。
                self._record_sync_error(session_id, exc)
                results.append(
                    {
                        "session_id": session_id,
                        "error": str(exc)[:1000],
                    }
                )
        return results
    def sync_group(self, platform_id: str, group_id: str) -> Path | None:
        with self.connect_catalog() as connection:
            row = connection.execute(
                """
                SELECT ss.session_id
                FROM story_storage ss
                JOIN group_registry gr ON gr.id = ss.group_registry_id
                WHERE gr.platform_id = ? AND gr.group_id = ?
                ORDER BY ss.created_at LIMIT 1
                """,
                (platform_id, group_id),
            ).fetchone()
        if not row:
            return None
        indexed = self._ensure_index(str(row[0]))
        return self._write_group_manifest(indexed)
    def storage_info(self, session_id: str) -> dict[str, Any]:
        indexed = self._ensure_index(session_id)
        storage = dict(indexed["storage"])
        story_dir = self.data_dir / str(storage["relative_path"])
        storage.update(
            {
                "database_exists": (story_dir / "instance.sqlite3").exists(),
                "manifest_exists": (story_dir / "manifest.json").exists(),
                "save_files": self.list_archives(session_id, kind="save"),
                "backup_files": self.list_archives(
                    session_id,
                    kind="backup",
                ),
            }
        )
        return storage
    def trash_relative_path(
        self,
        relative_path: str,
        *,
        label: str,
    ) -> Path | None:
        """Move a deleted story tree to a recoverable local trash folder."""

        relative = Path(str(relative_path or ""))
        if (
            not relative.parts
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.parts[0] != "groups"
        ):
            raise ValueError("副本存储路径无效，已拒绝移动")
        source = self.data_dir / relative
        if not source.exists():
            return None
        trash_dir = self.data_dir / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        safe_label = safe_component(label, "story", maximum=52)
        target = next_timestamped_path(
            trash_dir,
            f"deleted_{safe_label}",
            "",
        )
        replace_with_retry(source, target)
        return target
    def trash_archive(
        self,
        session_id: str,
        *,
        kind: str,
        filename: str,
    ) -> dict[str, Any]:
        """Move one independent save archive to the story-local trash."""

        if kind != "save":
            raise ValueError("只能手动删除独立命名存档")
        safe_name = Path(str(filename or "")).name
        if (
            safe_name != str(filename or "")
            or not safe_name.startswith("save_")
            or not safe_name.endswith(".zip")
        ):
            raise ValueError("独立存档文件名无效")
        indexed = self._ensure_index(session_id)
        story_dir = self.data_dir / str(
            indexed["storage"]["relative_path"]
        )
        source = story_dir / "saves" / safe_name
        if not source.is_file():
            raise FileNotFoundError("独立存档文件不存在")
        try:
            with zipfile.ZipFile(source, "r") as archive:
                manifest = json.loads(
                    archive.read("manifest.json").decode("utf-8")
                )
        except (
            KeyError,
            OSError,
            UnicodeDecodeError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise ValueError("独立存档无法验证，已拒绝删除") from exc
        reason = str(
            (manifest.get("archive") or {}).get("reason") or ""
        )
        if "最终" in reason:
            raise ValueError("最终保护存档不能手动删除")
        trash_dir = story_dir / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = next_timestamped_path(
            trash_dir,
            f"deleted_{safe_component(source.stem, 'save', maximum=80)}",
            ".zip",
        )
        replace_with_retry(source, target)
        return {
            "session_id": session_id,
            "filename": safe_name,
            "reason": reason,
            "trash_path": str(target),
        }
    def list_archives(
        self,
        session_id: str,
        *,
        kind: str,
    ) -> list[dict[str, Any]]:
        if kind not in {"save", "backup"}:
            raise ValueError("独立存档类型必须为 save 或 backup")
        indexed = self._ensure_index(session_id)
        story_dir = self.data_dir / str(
            indexed["storage"]["relative_path"]
        )
        directory = story_dir / (
            "saves" if kind == "save" else "backups"
        )
        if not directory.exists():
            return []
        items = sorted(
            directory.glob(f"{kind}_*.zip"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return [
            {
                "filename": item.name,
                "kind": kind,
                "size": item.stat().st_size,
                "created_at": datetime.fromtimestamp(
                    item.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(timespec="seconds"),
            }
            for item in items
        ]

