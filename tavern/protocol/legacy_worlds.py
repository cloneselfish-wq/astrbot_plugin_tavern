from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

MANIFEST_SCHEMA = "tavern-legacy-backup-manifest/1.0.0"
RECEIPT_SCHEMA = "tavern-legacy-backup-receipt/1.0.0"
LEGACY_ROOT_NAME = "legacy-pre-rc10"
BACKUP_NAME = "world-packages"
_METADATA_NAMES = frozenset({"manifest.json", "receipt.json"})
_METADATA_KEYS = frozenset(
    unicodedata.normalize("NFC", name).casefold() for name in _METADATA_NAMES
)


class LegacyWorldBackupError(RuntimeError):
    """A world-package inventory could not be preserved safely."""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_link_or_reparse(path: Path) -> bool:
    metadata = os.lstat(path)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def is_link_or_reparse(path: Path | str) -> bool:
    """Expose the same no-follow link/reparse check to store consumers."""
    return _is_link_or_reparse(Path(path))


def _relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise LegacyWorldBackupError(
            f"旧世界成员越出 inventory 根目录：{path}"
        ) from exc
    pure = PurePosixPath(relative.as_posix())
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise LegacyWorldBackupError(f"旧世界成员路径不安全：{relative}")
    return pure.as_posix()


def _inventory_hash(members: Iterable[Mapping[str, object]]) -> str:
    canonical = [
        {
            "path": str(member["path"]),
            "bytes": int(member["bytes"]),
            "sha256": str(member["sha256"]),
        }
        for member in members
    ]
    return hashlib.sha256(_canonical(canonical)).hexdigest()


def inventory_legacy_worlds(
    sources: Iterable[Path | str],
    *,
    source_root: Path | str | None = None,
) -> dict[str, object]:
    """Build a collision-safe, relative, byte-bound source inventory."""
    source_paths = [_absolute(value) for value in sources]
    if not source_paths:
        raise LegacyWorldBackupError("旧世界 inventory 没有来源")

    if source_root is not None:
        root = _absolute(source_root)
    elif len(source_paths) == 1:
        try:
            metadata = os.lstat(source_paths[0])
        except OSError as exc:
            raise LegacyWorldBackupError("旧世界 inventory 来源不存在") from exc
        root = source_paths[0] if stat.S_ISDIR(metadata.st_mode) else source_paths[0].parent
    else:
        root = Path(os.path.commonpath([os.fspath(path.parent) for path in source_paths]))

    try:
        root_metadata = os.lstat(root)
        if _is_link_or_reparse(root) or not stat.S_ISDIR(root_metadata.st_mode):
            raise LegacyWorldBackupError("旧世界 inventory 根目录不是安全的普通目录")
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界 inventory 根目录不可读取") from exc

    members: list[dict[str, object]] = []
    seen_exact: set[str] = set()
    seen_portable: dict[str, str] = {}

    def register(path: Path, *, directory: bool) -> None:
        relative = _relative_path(path, root)
        portable = unicodedata.normalize("NFC", relative).casefold()
        if relative in seen_exact:
            raise LegacyWorldBackupError(f"旧世界 inventory 路径重复：{relative}")
        prior = seen_portable.get(portable)
        if prior is not None and prior != relative:
            raise LegacyWorldBackupError(
                f"旧世界 inventory 存在大小写或 Unicode 冲突：{prior} / {relative}"
            )
        seen_exact.add(relative)
        seen_portable[portable] = relative
        if directory:
            return

        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise LegacyWorldBackupError(f"旧世界成员不是普通文件：{relative}")
        digest = _sha(path)
        after = os.lstat(path)
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise LegacyWorldBackupError(f"旧世界成员在 inventory 期间发生变化：{relative}")
        members.append(
            {"path": relative, "bytes": before.st_size, "sha256": digest}
        )

    def visit(path: Path, *, include_directory: bool = True) -> None:
        try:
            metadata = os.lstat(path)
            if _is_link_or_reparse(path):
                raise LegacyWorldBackupError(f"旧世界 inventory 禁止链接或 reparse：{path.name}")
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
        except OSError as exc:
            raise LegacyWorldBackupError(f"旧世界成员不可读取：{path}") from exc
        except ValueError as exc:
            raise LegacyWorldBackupError(f"旧世界成员解析后越界：{path}") from exc

        if stat.S_ISDIR(metadata.st_mode):
            if include_directory:
                register(path, directory=True)
            try:
                children = sorted(
                    path.iterdir(),
                    key=lambda item: (
                        unicodedata.normalize("NFC", item.name).casefold(),
                        item.name,
                    ),
                )
            except OSError as exc:
                raise LegacyWorldBackupError(f"旧世界目录不可枚举：{path}") from exc
            for child in children:
                visit(child)
            return
        register(path, directory=False)

    for source in source_paths:
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise LegacyWorldBackupError(f"旧世界来源越出 inventory 根目录：{source}") from exc
        visit(source, include_directory=source != root)

    members.sort(key=lambda item: str(item["path"]))
    inventory_sha256 = _inventory_hash(members)
    return {
        "source_root": root,
        "resolved_source_root": resolved_root,
        "members": members,
        "source_inventory_sha256": inventory_sha256,
    }


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyWorldBackupError(f"旧世界备份元数据不可读取：{path.name}") from exc
    if not isinstance(value, dict):
        raise LegacyWorldBackupError(f"旧世界备份元数据格式错误：{path.name}")
    return value


def _verify_backup(
    backup_dir: Path,
    expected_members: list[dict[str, object]],
    inventory_sha256: str,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        if is_link_or_reparse(backup_dir) or not backup_dir.is_dir():
            raise LegacyWorldBackupError("旧世界备份目录不是安全的普通目录")
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界备份目录不可读取") from exc
    manifest_path = backup_dir / "manifest.json"
    receipt_path = backup_dir / "receipt.json"
    for metadata_path in (manifest_path, receipt_path):
        try:
            if _is_link_or_reparse(metadata_path) or not metadata_path.is_file():
                raise LegacyWorldBackupError("旧世界备份元数据不是普通文件")
        except OSError as exc:
            raise LegacyWorldBackupError("旧世界备份元数据缺失") from exc

    manifest = _read_json(manifest_path)
    receipt = _read_json(receipt_path)
    manifest_inventory = str(
        manifest.get("source_inventory_sha256")
        or manifest.get("source_inventory_hash")
        or ""
    )
    receipt_inventory = str(
        receipt.get("source_inventory_sha256")
        or receipt.get("source_inventory_hash")
        or ""
    )
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or receipt.get("schema") != RECEIPT_SCHEMA
        or receipt.get("verified") is not True
        or manifest_inventory != inventory_sha256
        or receipt_inventory != inventory_sha256
        or manifest.get("members") != expected_members
        or int(
            receipt.get("member_count")
            if receipt.get("member_count") is not None
            else -1
        )
        != len(expected_members)
        or receipt.get("manifest_sha256") != _sha(manifest_path)
    ):
        raise LegacyWorldBackupError("旧世界备份 manifest/receipt 与来源 inventory 不一致")

    try:
        member_sources = [
            path
            for path in backup_dir.iterdir()
            if path.name not in _METADATA_NAMES
        ]
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界备份目录不可枚举") from exc
    actual_members: list[dict[str, object]] = []
    if member_sources:
        backup_inventory = inventory_legacy_worlds(
            member_sources, source_root=backup_dir
        )
        actual_members = list(backup_inventory["members"])
    if actual_members != expected_members:
        raise LegacyWorldBackupError("旧世界备份成员 path/bytes/SHA 复核失败")
    return manifest, receipt


def _find_reusable_backup(
    legacy_root: Path,
    members: list[dict[str, object]],
    inventory_sha256: str,
) -> tuple[Path, dict[str, object], dict[str, object]] | None:
    if not os.path.lexists(legacy_root):
        return None
    try:
        if is_link_or_reparse(legacy_root) or not legacy_root.is_dir():
            raise LegacyWorldBackupError("旧世界备份根目录不是安全的普通目录")
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界备份根目录不可读取") from exc
    try:
        stamp_dirs = sorted(
            legacy_root.iterdir(), key=lambda path: path.name, reverse=True
        )
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界备份根目录不可枚举") from exc
    for stamp_dir in stamp_dirs:
        try:
            if is_link_or_reparse(stamp_dir) or not stamp_dir.is_dir():
                raise LegacyWorldBackupError("旧世界备份 UTC 目录含链接或 reparse")
        except OSError as exc:
            raise LegacyWorldBackupError("旧世界备份 UTC 目录不可读取") from exc
        receipt_path = stamp_dir / BACKUP_NAME / "receipt.json"
        if not os.path.lexists(receipt_path):
            continue
        try:
            receipt = _read_json(receipt_path)
        except LegacyWorldBackupError:
            continue
        receipt_inventory = str(
            receipt.get("source_inventory_sha256")
            or receipt.get("source_inventory_hash")
            or ""
        )
        if receipt_inventory != inventory_sha256:
            continue
        backup_dir = receipt_path.parent
        manifest, verified_receipt = _verify_backup(
            backup_dir, members, inventory_sha256
        )
        return backup_dir, manifest, verified_receipt
    return None


def backup_legacy_worlds(
    data_dir: Path | str,
    sources: Iterable[Path | str],
    *,
    timestamp: str | None = None,
    source_root: Path | str | None = None,
) -> dict[str, object]:
    """Copy a relative inventory, verify it, then commit a reusable receipt."""
    root = _absolute(data_dir)
    inventory = inventory_legacy_worlds(sources, source_root=source_root)
    inventory_root = inventory["source_root"]
    resolved_inventory_root = inventory["resolved_source_root"]
    if not isinstance(inventory_root, Path) or not isinstance(
        resolved_inventory_root, Path
    ):
        raise LegacyWorldBackupError("旧世界 inventory 根目录无效")
    members = list(inventory["members"])
    inventory_sha256 = str(inventory["source_inventory_sha256"])
    reserved = {
        str(member["path"])
        for member in members
        if len(PurePosixPath(str(member["path"])).parts) == 1
        and unicodedata.normalize("NFC", str(member["path"])).casefold()
        in _METADATA_KEYS
    }
    if reserved:
        raise LegacyWorldBackupError(
            "旧世界 inventory 占用备份元数据保留路径："
            + "、".join(sorted(reserved))
        )
    legacy_root = root / LEGACY_ROOT_NAME

    reusable = _find_reusable_backup(legacy_root, members, inventory_sha256)
    if reusable is not None:
        backup_dir, manifest, receipt = reusable
        return {
            "backup_dir": backup_dir,
            "manifest": manifest,
            "receipt": receipt,
            "reused": True,
        }

    stamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    if not re.fullmatch(r"\d{8}T\d{6}(?:\.\d{6})?Z", stamp):
        raise LegacyWorldBackupError("旧世界备份 UTC 目录名无效")
    stamp_dir = legacy_root / stamp
    destination = stamp_dir / BACKUP_NAME
    if os.path.lexists(destination):
        raise LegacyWorldBackupError("旧世界备份目标已存在但与当前 inventory 不匹配")

    try:
        if os.path.lexists(stamp_dir) and (
            is_link_or_reparse(stamp_dir) or not stamp_dir.is_dir()
        ):
            raise LegacyWorldBackupError("旧世界备份 UTC 目录含链接或 reparse")
        stamp_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{BACKUP_NAME}-", dir=stamp_dir))
    except LegacyWorldBackupError:
        raise
    except OSError as exc:
        raise LegacyWorldBackupError("旧世界备份目录无法创建；活动 store 未移除") from exc

    try:
        for member in members:
            relative = PurePosixPath(str(member["path"]))
            source = inventory_root.joinpath(*relative.parts)
            target = staging.joinpath(*relative.parts)
            try:
                cursor = source.parent
                while True:
                    if _is_link_or_reparse(cursor):
                        raise LegacyWorldBackupError(
                            f"旧世界成员父目录在复制前变为链接：{relative}"
                        )
                    if cursor == inventory_root:
                        break
                    cursor = cursor.parent
                source.resolve(strict=True).relative_to(resolved_inventory_root)
                if _is_link_or_reparse(source) or not source.is_file():
                    raise LegacyWorldBackupError(
                        f"旧世界成员在复制前失效：{relative}"
                    )
            except OSError as exc:
                raise LegacyWorldBackupError(
                    f"旧世界成员在复制前不可读取：{relative}"
                ) from exc
            except ValueError as exc:
                raise LegacyWorldBackupError(
                    f"旧世界成员在复制前解析越界：{relative}"
                ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if (
                source.stat().st_size != member["bytes"]
                or _sha(source) != member["sha256"]
                or target.stat().st_size != member["bytes"]
                or _sha(target) != member["sha256"]
            ):
                raise LegacyWorldBackupError(f"旧世界备份校验失败：{relative}")

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_inventory_sha256": inventory_sha256,
            "source_inventory_hash": inventory_sha256,
            "members": members,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_bytes(_canonical(manifest) + b"\n")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "verified": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "source_inventory_sha256": inventory_sha256,
            "source_inventory_hash": inventory_sha256,
            "manifest_sha256": _sha(manifest_path),
            "member_count": len(members),
        }
        (staging / "receipt.json").write_bytes(_canonical(receipt) + b"\n")
        _verify_backup(staging, members, inventory_sha256)
        os.replace(staging, destination)
        manifest, receipt = _verify_backup(destination, members, inventory_sha256)
    except (OSError, LegacyWorldBackupError) as exc:
        if os.path.lexists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, LegacyWorldBackupError):
            raise
        raise LegacyWorldBackupError(
            "旧世界备份写入或复核失败；活动 store 未移除"
        ) from exc

    return {
        "backup_dir": destination,
        "manifest": manifest,
        "receipt": receipt,
        "reused": False,
    }


__all__ = [
    "BACKUP_NAME",
    "LEGACY_ROOT_NAME",
    "MANIFEST_SCHEMA",
    "RECEIPT_SCHEMA",
    "LegacyWorldBackupError",
    "backup_legacy_worlds",
    "inventory_legacy_worlds",
    "is_link_or_reparse",
]
