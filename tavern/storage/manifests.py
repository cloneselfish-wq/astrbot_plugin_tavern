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




class ManifestsMixin:
    @staticmethod
    def _world_snapshot(indexed: Mapping[str, Any]) -> dict[str, Any]:
        raw = indexed["session"].get("world_snapshot_json")
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}
    def _manifest_payload(
        self,
        indexed: Mapping[str, Any],
        *,
        checksum: str,
        counts: Mapping[str, int],
        revision: int | None = None,
    ) -> dict[str, Any]:
        session = indexed["session"]
        group = indexed["group"]
        storage = indexed["storage"]
        world_snapshot = self._world_snapshot(indexed)
        try:
            phase = json.loads(session.get("phase_meta_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            phase = {}
        if not isinstance(phase, Mapping):
            phase = {}
        return {
            "format": INSTANCE_FORMAT,
            "format_version": INSTANCE_FORMAT_VERSION,
            "schema_version": self.schema_version,
            "session": {
                "id": str(session.get("id") or ""),
                "platform_id": str(session.get("platform_id") or ""),
                "group_id": str(session.get("group_id") or ""),
                "instance_slug": str(session.get("instance_slug") or ""),
                "instance_name": str(session.get("instance_name") or ""),
                "state": str(session.get("state") or ""),
                "turn_no": int(session.get("turn_no") or 0),
                # C6：manifest 的 revision 必须来自被复制快照本身，
                # 不能读取复制完成后的 catalog 新值（并发写入会标错版本）。
                "revision": (
                    int(revision)
                    if revision is not None
                    else int(session.get("revision") or 0)
                ),
                "selected": bool(session.get("selected")),
                "created_at": str(session.get("created_at") or ""),
                "updated_at": str(session.get("updated_at") or ""),
                "playthrough_no": int(storage.get("playthrough_no") or 1),
                "branched_from_session_id": str(
                    phase.get("branched_from_session_id") or ""
                ),
                "branched_from_snapshot_id": str(
                    phase.get("branched_from_snapshot_id") or ""
                ),
            },
            "group": {
                "registry_id": str(group.get("id") or ""),
                "remark": str(group.get("remark") or ""),
                "revision": int(group.get("revision") or 1),
            },
            "world": {
                "id": str(session.get("world_id") or ""),
                "slug": str(
                    world_snapshot.get("slug")
                    or session.get("world_slug")
                    or ""
                ),
                "name": str(
                    world_snapshot.get("name")
                    or session.get("world_name")
                    or ""
                ),
                "revision": int(
                    world_snapshot.get("revision")
                    or session.get("world_revision")
                    or 1
                ),
            },
            "storage": {
                "relative_path": str(storage.get("relative_path") or ""),
                "database": "instance.sqlite3",
                "saves_directory": "saves",
                "backups_directory": "backups",
                "database_sha256": checksum,
                "counts": dict(counts),
            },
        }
    @staticmethod
    def _database_counts(path: Path) -> dict[str, int]:
        result: dict[str, int] = {}
        with closing(sqlite3.connect(path)) as connection:
            for table in (
                "sessions",
                "participants",
                "events",
                "memories",
                "snapshots",
                "session_characters",
                "story_ledger",
                "scene_clocks",
                "rolls",
                # D1 Schema 20：新增领域表纳入实例清单统计（备份可见）。
                "session_events",
                "projection_checkpoints",
                "delivery_targets",
                "character_capabilities",
                "character_resources",
                "actor_fate_states",
                "actor_fate_transitions",
                "rescue_windows",
                "terminal_receipts",
                "session_finalizations",
                "delivery_outbox",
            ):
                exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = ?
                    """,
                    (table,),
                ).fetchone()
                if exists:
                    result[table] = int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    )
        return result
    @staticmethod
    def _delete_not_in(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        values: Sequence[str],
    ) -> None:
        if values:
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f'DELETE FROM "{table}" '
                f'WHERE "{column}" NOT IN ({placeholders})',
                tuple(values),
            )
        else:
            connection.execute(f'DELETE FROM "{table}"')
    def _prune_instance(
        self,
        path: Path,
        indexed: Mapping[str, Any],
    ) -> None:
        session = indexed["session"]
        session_id = str(session["id"])
        world_id = str(session["world_id"])
        group_registry_id = str(indexed["group"]["id"])
        world_snapshot = self._world_snapshot(indexed)
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM sessions WHERE id <> ?",
                    (session_id,),
                )
                tables = [
                    str(row["name"])
                    for row in connection.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    ).fetchall()
                ]
                columns_by_table: dict[str, set[str]] = {}
                for table in tables:
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            f'PRAGMA table_info("{table}")'
                        ).fetchall()
                    }
                    columns_by_table[table] = columns
                    if "session_id" in columns and table != "sessions":
                        connection.execute(
                            f'DELETE FROM "{table}" '
                            "WHERE session_id <> ?",
                            (session_id,),
                        )

                # 世界级子表必须先于 DELETE FROM worlds 清理：
                # world_snapshots 使用 ON DELETE RESTRICT（database.py），
                # 直接删其它世界会触发 FOREIGN KEY constraint failed，
                # 在克隆/多世界场景下表现为“副本文件同步异常”。
                for table, columns in columns_by_table.items():
                    if (
                        "world_id" in columns
                        and table not in {"worlds", "sessions"}
                    ):
                        connection.execute(
                            f'DELETE FROM "{table}" '
                            "WHERE world_id <> ?",
                            (world_id,),
                        )

                if "group_registry" in tables:
                    connection.execute(
                        "DELETE FROM group_registry WHERE id <> ?",
                        (group_registry_id,),
                    )
                if "story_storage" in tables:
                    connection.execute(
                        "DELETE FROM story_storage WHERE session_id <> ?",
                        (session_id,),
                    )

                used_card_ids = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT character_card_id FROM participants
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        UNION
                        SELECT character_card_id FROM character_runtime_states
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        UNION
                        SELECT character_card_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND character_card_id IS NOT NULL
                          AND character_card_id <> ''
                        """,
                        (session_id, session_id, session_id),
                    ).fetchall()
                }
                used_version_ids = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT character_version_id FROM participants
                        WHERE session_id = ?
                          AND character_version_id IS NOT NULL
                          AND character_version_id <> ''
                        UNION
                        SELECT base_version_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND base_version_id IS NOT NULL
                          AND base_version_id <> ''
                        UNION
                        SELECT candidate_version_id FROM card_revision_requests
                        WHERE session_id = ?
                          AND candidate_version_id IS NOT NULL
                          AND candidate_version_id <> ''
                        """,
                        (session_id, session_id, session_id),
                    ).fetchall()
                }
                self._delete_not_in(
                    connection,
                    "character_card_versions",
                    "id",
                    sorted(used_version_ids),
                )
                self._delete_not_in(
                    connection,
                    "character_cards",
                    "id",
                    sorted(used_card_ids),
                )

                connection.execute(
                    "DELETE FROM worlds WHERE id <> ?",
                    (world_id,),
                )
                if world_snapshot:
                    connection.execute(
                        """
                        UPDATE worlds SET
                            slug = ?, name = ?, description = ?,
                            system_prompt = ?, rules_json = ?,
                            opening_scene = ?, initial_state_json = ?,
                            revision = ?
                        WHERE id = ?
                        """,
                        (
                            str(
                                world_snapshot.get("slug")
                                or session.get("world_slug")
                                or "instance-world"
                            ),
                            str(
                                world_snapshot.get("name")
                                or session.get("world_name")
                                or "副本世界"
                            ),
                            str(world_snapshot.get("description") or ""),
                            str(world_snapshot.get("system_prompt") or ""),
                            json.dumps(
                                world_snapshot.get("rules") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            str(world_snapshot.get("opening_scene") or ""),
                            json.dumps(
                                world_snapshot.get("initial_state") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            int(world_snapshot.get("revision") or 1),
                            world_id,
                        ),
                    )

                for table in (
                    "provider_health",
                    "configuration_revisions",
                ):
                    if table in tables:
                        connection.execute(f'DELETE FROM "{table}"')
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('storage_kind', 'instance')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )
                connection.execute(
                    """
                    INSERT INTO tavern_meta(key, value)
                    VALUES ('instance_session_id', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (session_id,),
                )
                violations = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
                if violations:
                    raise RuntimeError("副本数据库外键校验失败")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("VACUUM")
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if quick != "ok":
                raise RuntimeError(f"副本数据库完整性校验失败：{quick}")

