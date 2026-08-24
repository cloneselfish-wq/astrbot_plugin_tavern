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




class FilesMixin:
    def __init__(
        self,
        *,
        data_dir: Path,
        catalog_path: Path,
        connect_catalog: Callable[[], sqlite3.Connection],
        schema_version: int,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.catalog_path = Path(catalog_path)
        self.groups_dir = self.data_dir / "groups"
        self.connect_catalog = connect_catalog
        self.schema_version = int(schema_version)
        self._lock = threading.RLock()
    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temporary, path)
        finally:
            unlink_with_retry(temporary, suppress_errors=True)
    @classmethod
    def _atomic_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
        cls._atomic_write(
            path,
            (
                json.dumps(
                    dict(payload),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        return dict(row) if row else {}
    def _ensure_index(self, session_id: str) -> dict[str, Any]:
        with self.connect_catalog() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    """
                    SELECT s.*, w.slug AS world_slug, w.name AS world_name,
                           ic.world_revision, ic.world_snapshot_json,
                           ic.phase_meta_json
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN instance_configs ic ON ic.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not session:
                    raise LookupError("副本不存在")
                platform_id = str(session["platform_id"])
                group_id = str(session["group_id"])
                registry_id = (
                    "group_"
                    + hashlib.sha256(
                        f"{platform_id}\0{group_id}".encode("utf-8")
                    ).hexdigest()[:24]
                )
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
                group = connection.execute(
                    """
                    SELECT * FROM group_registry
                    WHERE platform_id = ? AND group_id = ?
                    """,
                    (platform_id, group_id),
                ).fetchone()
                storage = connection.execute(
                    """
                    SELECT * FROM story_storage WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                group_folder = stable_group_folder(
                    platform_id,
                    group_id,
                )
                story_folder = stable_story_folder(
                    str(session["instance_slug"]),
                    str(session["created_at"]),
                    session_id,
                )
                expected_relative_path = (
                    Path("groups")
                    / group_folder
                    / "stories"
                    / story_folder
                ).as_posix()
                if not storage:
                    playthrough_no = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM sessions previous
                            WHERE previous.platform_id = ?
                              AND previous.group_id = ?
                              AND previous.world_id = ?
                              AND (
                                previous.created_at < ?
                                OR (
                                  previous.created_at = ?
                                  AND previous.rowid <= (
                                    SELECT current.rowid
                                    FROM sessions current
                                    WHERE current.id = ?
                                  )
                                )
                              )
                            """,
                            (
                                platform_id,
                                group_id,
                                session["world_id"],
                                session["created_at"],
                                session["created_at"],
                                session_id,
                            ),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        INSERT INTO story_storage(
                            session_id, group_registry_id, relative_path,
                            playthrough_no, created_stamp,
                            last_synced_revision, last_checksum,
                            sync_status, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, '', 'pending', '', ?, ?)
                        """,
                        (
                            session_id,
                            group["id"],
                            expected_relative_path,
                            max(1, playthrough_no),
                            timestamp14(str(session["created_at"])),
                            now,
                            now,
                        ),
                    )
                    storage = connection.execute(
                        """
                        SELECT * FROM story_storage WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                elif (
                    str(storage["relative_path"]) != expected_relative_path
                    or str(storage["group_registry_id"]) != str(group["id"])
                ):
                    connection.execute(
                        """
                        UPDATE story_storage SET
                            group_registry_id = ?, relative_path = ?,
                            created_stamp = ?, sync_status = 'pending',
                            last_checksum = '', last_error = '',
                            updated_at = ?
                        WHERE session_id = ?
                        """,
                        (
                            group["id"],
                            expected_relative_path,
                            timestamp14(str(session["created_at"])),
                            now,
                            session_id,
                        ),
                    )
                    storage = connection.execute(
                        """
                        SELECT * FROM story_storage WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                connection.execute("COMMIT")
                return {
                    "session": self._row_dict(session),
                    "group": self._row_dict(group),
                    "storage": self._row_dict(storage),
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
    def _group_payload(
        self,
        group: Mapping[str, Any],
        *,
        story_count: int,
    ) -> dict[str, Any]:
        return {
            "format": "astrbot-tavern-group",
            "format_version": INSTANCE_FORMAT_VERSION,
            "platform_id": str(group.get("platform_id") or ""),
            "group_id": str(group.get("group_id") or ""),
            "remark": str(group.get("remark") or ""),
            "revision": int(group.get("revision") or 1),
            "story_count": int(story_count),
            "created_at": str(group.get("created_at") or ""),
            "updated_at": str(group.get("updated_at") or ""),
        }
    def _write_group_manifest(
        self,
        indexed: Mapping[str, Any],
    ) -> Path:
        group = indexed["group"]
        storage = indexed["storage"]
        story_dir = self.data_dir / str(storage["relative_path"])
        group_dir = story_dir.parent.parent
        with self.connect_catalog() as connection:
            story_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM story_storage
                    WHERE group_registry_id = ?
                    """,
                    (group["id"],),
                ).fetchone()[0]
            )
        group_path = group_dir / "group.json"
        self._atomic_json(
            group_path,
            self._group_payload(group, story_count=story_count),
        )
        return group_path

