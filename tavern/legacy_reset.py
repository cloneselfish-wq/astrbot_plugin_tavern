"""Verified, clean-only RC8 reset for legacy database and save data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Iterable


MANIFEST_SCHEMA = "tavern-legacy-backup-manifest/1.0.0"
RECEIPT_SCHEMA = "tavern-legacy-backup-receipt/1.0.0"
LEGACY_ROOT_NAME = "legacy-pre-rc8"


class LegacyResetError(RuntimeError):
    """The legacy inventory could not be preserved and verified safely."""


@dataclass(frozen=True, slots=True)
class LegacyResetResult:
    status: str
    backup_category: str | None = None
    inventory_sha256: str | None = None
    member_count: int = 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_json_bytes(payload))
    temporary.replace(path)


def _inventory_hash(members: Iterable[dict[str, object]]) -> str:
    canonical = [
        {
            "path": item["path"],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
            "kind": item["kind"],
        }
        for item in members
    ]
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _source_members(data_dir: Path, database_path: Path) -> list[tuple[Path, str, str]]:
    sources: list[tuple[Path, str, str]] = []
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(database_path) + suffix)
        if candidate.exists():
            sources.append((candidate, f"database/{candidate.name}", "database"))

    groups = data_dir / "groups"
    if groups.exists():
        for candidate in sorted(groups.rglob("*"), key=lambda item: item.as_posix()):
            if candidate.is_symlink():
                raise LegacyResetError("旧存档包含符号链接，无法安全备份；系统未创建新数据库。")
            if candidate.is_file():
                relative = candidate.relative_to(groups).as_posix()
                sources.append((candidate, f"saves/groups/{relative}", "save"))
    return sources


def _existing_receipt(legacy_root: Path, inventory_sha256: str) -> Path | None:
    if not legacy_root.exists():
        return None
    for receipt_path in sorted(legacy_root.glob("*/receipt.json"), reverse=True):
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if (
            receipt.get("schema") == RECEIPT_SCHEMA
            and receipt.get("verified") is True
            and receipt.get("source_inventory_sha256") == inventory_sha256
        ):
            return receipt_path
    return None


def _verify_backup(backup_dir: Path, members: list[dict[str, object]]) -> None:
    for member in members:
        destination = backup_dir / str(member["path"])
        if not destination.is_file():
            raise LegacyResetError(
                f"旧数据备份缺少成员 {member['path']}；系统未创建新数据库。"
            )
        if destination.stat().st_size != member["bytes"] or _sha256(destination) != member["sha256"]:
            raise LegacyResetError(
                f"旧数据备份校验失败：{member['path']}；系统未创建新数据库。"
            )


def backup_and_remove_legacy(data_dir: Path, database_path: Path) -> LegacyResetResult:
    """Copy, verify and only then remove the active pre-RC8 inventory.

    A verified receipt for the exact same source inventory may be reused after an
    interrupted first start.  No live Schema 29 database is created here.
    """

    data_dir = Path(data_dir)
    database_path = Path(database_path)
    source_rows = _source_members(data_dir, database_path)
    if not source_rows:
        return LegacyResetResult(status="clean")

    members = [
        {
            "path": relative,
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
            "kind": kind,
        }
        for source, relative, kind in source_rows
    ]
    inventory_sha256 = _inventory_hash(members)
    legacy_root = data_dir / LEGACY_ROOT_NAME
    reused_receipt = _existing_receipt(legacy_root, inventory_sha256)

    if reused_receipt is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_dir = legacy_root / stamp
        index = 1
        while backup_dir.exists():
            index += 1
            backup_dir = legacy_root / f"{stamp}-{index:02d}"
        try:
            for (source, relative, _kind), member in zip(source_rows, members):
                destination = backup_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if destination.stat().st_size != member["bytes"] or _sha256(destination) != member["sha256"]:
                    raise LegacyResetError(
                        f"旧数据备份校验失败：{relative}；系统未创建新数据库。"
                    )
            _verify_backup(backup_dir, members)
            manifest = {
                "schema": MANIFEST_SCHEMA,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_inventory_sha256": inventory_sha256,
                "members": members,
            }
            _atomic_json(backup_dir / "manifest.json", manifest)
            manifest_sha256 = _sha256(backup_dir / "manifest.json")
            _atomic_json(
                backup_dir / "receipt.json",
                {
                    "schema": RECEIPT_SCHEMA,
                    "verified": True,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                    "source_inventory_sha256": inventory_sha256,
                    "manifest_sha256": manifest_sha256,
                    "member_count": len(members),
                },
            )
        except (OSError, LegacyResetError) as exc:
            if isinstance(exc, LegacyResetError):
                raise
            raise LegacyResetError(
                "旧数据备份无法写入或复核；系统已中止启动且未创建新数据库。"
            ) from exc
        status = "backed_up"
    else:
        backup_dir = reused_receipt.parent
        try:
            receipt = json.loads(reused_receipt.read_text(encoding="utf-8"))
            manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
            if manifest.get("source_inventory_sha256") != inventory_sha256:
                raise LegacyResetError("旧数据备份清单与当前数据不一致；系统未创建新数据库。")
            if receipt.get("manifest_sha256") != _sha256(backup_dir / "manifest.json"):
                raise LegacyResetError("旧数据备份清单哈希与回执不一致；系统未创建新数据库。")
            _verify_backup(backup_dir, list(manifest.get("members") or []))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LegacyResetError("旧数据备份回执无法复核；系统未创建新数据库。") from exc
        status = "reused_verified_backup"

    try:
        for source, _relative, _kind in source_rows:
            source.unlink(missing_ok=True)
        groups = data_dir / "groups"
        if groups.exists():
            shutil.rmtree(groups)
    except OSError as exc:
        raise LegacyResetError(
            "旧数据已安全备份，但活动副本无法移除；系统未创建新数据库。"
        ) from exc

    return LegacyResetResult(
        status=status,
        backup_category=f"{LEGACY_ROOT_NAME}/{backup_dir.name}",
        inventory_sha256=inventory_sha256,
        member_count=len(members),
    )
