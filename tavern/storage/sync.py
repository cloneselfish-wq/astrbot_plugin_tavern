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




class SyncMixin:
    def _record_sync_error(self, session_id: str, error: Exception) -> None:
        try:
            with self.connect_catalog() as connection:
                connection.execute(
                    """
                    UPDATE story_storage SET
                        sync_status = 'error', last_error = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        str(error)[:1000],
                        datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        session_id,
                    ),
                )
        except sqlite3.DatabaseError:
            return
    @staticmethod
    def _instance_session_revision(
        path: Path,
        session_id: str,
    ) -> int:
        """从快照（instance）数据库自身读取其 session revision。"""
        with closing(sqlite3.connect(path)) as connection:
            row = connection.execute(
                "SELECT revision FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else 0
    def _requeue_sync(
        self,
        session_id: str,
        requested_revision: int,
        snapshot_revision: int,
    ) -> None:
        """复制期间发生并发写入：不写入新版本 manifest，
        标记 pending 并重新排队，等待下一次 drain 以最新内容重做。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect_catalog() as connection:
            connection.execute(
                """
                UPDATE story_storage SET
                    sync_status = 'pending',
                    last_error = ?,
                    updated_at = ?
                WHERE session_id = ?
                """,
                (
                    "快照 revision 与请求不一致，已重新排队："
                    f"snapshot={snapshot_revision} "
                    f"requested={requested_revision}",
                    now,
                    session_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO storage_sync_outbox(
                    session_id, kind, payload_json, attempts,
                    last_error, created_at, updated_at
                ) VALUES (?, 'sync', '{}', 0, '', ?, ?)
                ON CONFLICT(session_id, kind) DO UPDATE SET
                    last_error = '',
                    updated_at = excluded.updated_at
                """,
                (session_id, now, now),
            )
    def sync_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            indexed = self._ensure_index(session_id)
            story_dir = (
                self.data_dir
                / str(indexed["storage"]["relative_path"])
            )
            story_dir.mkdir(parents=True, exist_ok=True)
            (story_dir / "saves").mkdir(exist_ok=True)
            (story_dir / "backups").mkdir(exist_ok=True)
            group_path = self._write_group_manifest(indexed)
            destination = story_dir / "instance.sqlite3"
            temporary = story_dir / (
                f".instance.{uuid.uuid4().hex}.sqlite3"
            )
            try:
                requested_revision = 0
                with closing(
                    sqlite3.connect(self.catalog_path)
                ) as source:
                    row = source.execute(
                        "SELECT revision FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    requested_revision = int(row[0]) if row else 0
                    with closing(sqlite3.connect(temporary)) as target:
                        source.backup(target)
                self._prune_instance(temporary, indexed)
                snapshot_revision = self._instance_session_revision(
                    temporary,
                    session_id,
                )
                if snapshot_revision != requested_revision:
                    # 复制期间 catalog 有新写入：丢弃本次不一致快照，
                    # 重新排队（manifest 永远只标注与快照内容一致的 revision）。
                    unlink_with_retry(temporary, suppress_errors=True)
                    self._requeue_sync(
                        session_id,
                        requested_revision,
                        snapshot_revision,
                    )
                    return {
                        "session_id": session_id,
                        "queued": True,
                        "requested_revision": requested_revision,
                        "snapshot_revision": snapshot_revision,
                    }
                for suffix in ("-wal", "-shm"):
                    unlink_with_retry(
                        destination.with_name(
                            destination.name + suffix
                        )
                    )
                replace_with_retry(temporary, destination)
                checksum = file_sha256(destination)
                counts = self._database_counts(destination)
                manifest = self._manifest_payload(
                    indexed,
                    checksum=checksum,
                    counts=counts,
                    revision=snapshot_revision,
                )
                manifest_path = story_dir / "manifest.json"
                self._atomic_json(manifest_path, manifest)
                now = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                with self.connect_catalog() as connection:
                    connection.execute(
                        """
                        UPDATE story_storage SET
                            last_synced_revision = ?,
                            last_checksum = ?,
                            sync_status = 'ready',
                            last_error = '',
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            snapshot_revision,
                            checksum,
                            now,
                            session_id,
                        ),
                    )
                return {
                    "session_id": session_id,
                    "relative_path": str(
                        indexed["storage"]["relative_path"]
                    ),
                    "database": destination,
                    "manifest": manifest_path,
                    "group_manifest": group_path,
                    "checksum": checksum,
                    "counts": counts,
                }
            except Exception as exc:
                unlink_with_retry(temporary, suppress_errors=True)
                for suffix in ("-wal", "-shm"):
                    unlink_with_retry(
                        temporary.with_name(temporary.name + suffix),
                        suppress_errors=True,
                    )
                self._record_sync_error(session_id, exc)
                raise
    def _archive_synced(
        self,
        session_id: str,
        *,
        kind: str,
        reason: str,
    ) -> Path:
        if kind not in {"save", "backup"}:
            raise ValueError("独立存档类型必须为 save 或 backup")
        indexed = self._ensure_index(session_id)
        session = indexed["session"]
        story_dir = (
            self.data_dir / str(indexed["storage"]["relative_path"])
        )
        destination_dir = story_dir / (
            "saves" if kind == "save" else "backups"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        slug = safe_component(
            session.get("instance_slug"),
            "story",
            maximum=52,
        )
        target = next_timestamped_path(
            destination_dir,
            f"{kind}_{slug}",
            ".zip",
        )
        database_path = story_dir / "instance.sqlite3"
        manifest_path = story_dir / "manifest.json"
        group_path = story_dir.parent.parent / "group.json"
        if not database_path.exists() or not manifest_path.exists():
            raise RuntimeError("副本实时数据库或清单不存在")
        manifest_payload = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        manifest_payload["archive"] = {
            "kind": kind,
            "reason": str(reason or ""),
            "created_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "filename": target.name,
        }
        manifest_bytes = (
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        group_bytes = group_path.read_bytes()
        checksums = (
            f"{file_sha256(database_path)}  instance.sqlite3\n"
            f"{bytes_sha256(manifest_bytes)}  manifest.json\n"
            f"{bytes_sha256(group_bytes)}  group_snapshot.json\n"
        ).encode("utf-8")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                archive.write(database_path, "instance.sqlite3")
                archive.writestr("manifest.json", manifest_bytes)
                archive.writestr("group_snapshot.json", group_bytes)
                archive.writestr("checksum.sha256", checksums)
            replace_with_retry(temporary, target)
        finally:
            unlink_with_retry(temporary, suppress_errors=True)
        if kind == "backup":
            archives = sorted(
                destination_dir.glob(f"backup_{slug}_*.zip"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            for stale in archives[20:]:
                stale.unlink(missing_ok=True)
        return target

