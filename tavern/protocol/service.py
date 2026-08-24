from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    MAX_ARCHIVE_BYTES,
    MAX_FILES,
    MAX_MEMBER_BYTES,
    MAX_UNCOMPRESSED_BYTES,
)
from .references import inspect_twp_archive
from .constants import (
    CORE_CAPABILITIES,
    MODULE_METADATA,
    OPTIONAL_MODULES,
    STANDARD_MODULES,
    TWP_ARTIFACT_SCHEMA,
    TWP_COMPILER_ABI,
    TWP_COMPILED_WORLD_SCHEMA,
    TWP_CORE_VERSION,
    TWP_FORMAT,
    TWP_PACKAGE_FORMAT,
    TWP_MATURITY,
    TWP_RUNTIME_SCHEMA,
    TWP_VERSION,
)
from .errors import TwpPackageError, TwpValidationIssue
from .legacy_worlds import (
    LegacyWorldBackupError,
    backup_legacy_worlds,
    inventory_legacy_worlds,
    is_link_or_reparse,
)


def _issue(code: str, message: str, path: str = "") -> TwpPackageError:
    return TwpPackageError(TwpValidationIssue(code, message, path))


class _ResolvedPackageId(str):
    """Unforgeable-by-transport marker for an already resolved internal id."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class TwpPackageService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "world_packages_twp"
        self.archives = self.root / "archives"
        self.artifacts = self.root / "artifacts"
        self.compiled = self.root / "compiled"
        self.index_path = self.root / "index.json"
        self.last_rejection_receipt: dict[str, Any] | None = None
        self._prepare_existing_store()
        self._lock = asyncio.Lock()

    @staticmethod
    def _managed_filename(value: object, field: str) -> str:
        name = str(value or "").strip()
        pure = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or pure.is_absolute()
            or len(pure.parts) != 1
            or pure.name in {"", ".", ".."}
        ):
            raise _issue(
                "protocol.integrity_mismatch",
                f"世界包索引 {field} 路径无效",
                field,
            )
        return name

    @staticmethod
    def _sha256_value(value: object) -> bool:
        text = str(value or "")
        return len(text) == 64 and all(character in "0123456789abcdef" for character in text)

    @staticmethod
    def _read_json_file(path: Path, label: str) -> dict[str, Any]:
        try:
            if is_link_or_reparse(path) or not path.is_file():
                raise OSError("not a regular managed file")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _issue(
                "protocol.integrity_mismatch",
                f"{label} 丢失、链接或损坏",
            ) from exc
        if not isinstance(value, dict):
            raise _issue("protocol.integrity_mismatch", f"{label} 格式损坏")
        return value

    @staticmethod
    def _normalized_artifact(report: Mapping[str, Any]) -> dict[str, Any]:
        raw = report.get("artifact")
        value = dict(raw) if isinstance(raw, Mapping) else {}
        value.update(
            {
                "artifact_schema": TWP_ARTIFACT_SCHEMA,
                "protocol": f"twp@{TWP_VERSION}",
                "compiler_abi": TWP_COMPILER_ABI,
            }
        )
        return value

    def _validate_record_contract(
        self,
        package_id: str,
        item: Mapping[str, Any],
    ) -> None:
        if (
            str(item.get("id") or "") != package_id
            or str(item.get("protocol") or "") != TWP_VERSION
            or not self._sha256_value(item.get("artifact_hash"))
            or not self._sha256_value(item.get("source_hash"))
            or not str(item.get("artifact_id") or "")
        ):
            raise _issue(
                "protocol.integrity_mismatch",
                "世界包索引不属于当前 RC8 合同",
            )

    def _validate_compiled_value(
        self,
        package_id: str,
        item: Mapping[str, Any],
        value: Mapping[str, Any],
    ) -> None:
        protocol = value.get("protocol")
        protocol = protocol if isinstance(protocol, Mapping) else {}
        valid = (
            value.get("compiled_world_schema") == TWP_COMPILED_WORLD_SCHEMA
            and value.get("artifact_schema") == TWP_ARTIFACT_SCHEMA
            and value.get("package_format") == TWP_PACKAGE_FORMAT
            and protocol.get("version") == TWP_VERSION
            and protocol.get("core") == TWP_CORE_VERSION
            and protocol.get("compiler_abi") == TWP_COMPILER_ABI
            and str(value.get("package_id") or "") == package_id
            and value.get("artifact_id") == item.get("artifact_id")
            and value.get("artifact_hash") == item.get("artifact_hash")
            and value.get("source_hash") == item.get("source_hash")
        )
        if not valid:
            raise _issue(
                "protocol.integrity_mismatch",
                "编译世界不是当前 RC8 Schema/Protocol/Hash 合同",
            )

    def _validate_artifact_value(
        self,
        item: Mapping[str, Any],
        value: Mapping[str, Any],
        compiled_value: Mapping[str, Any],
    ) -> None:
        valid = (
            value.get("artifact_schema") == TWP_ARTIFACT_SCHEMA
            and value.get("protocol") == f"twp@{TWP_VERSION}"
            and value.get("compiler_abi") == TWP_COMPILER_ABI
            and value.get("artifact_id") == item.get("artifact_id")
            and value.get("artifact_hash") == item.get("artifact_hash")
            and value.get("source_hash") == item.get("source_hash")
            and value.get("compiled_world_hash")
            == _canonical_sha256(compiled_value)
        )
        if not valid:
            raise _issue(
                "protocol.integrity_mismatch",
                "Artifact 不是当前 RC8 Schema/Protocol/Hash 合同",
            )

    def _validate_current_store(
        self,
        members: list[dict[str, object]],
    ) -> None:
        paths = {str(member["path"]) for member in members}
        if "index.json" not in paths:
            raise _issue("protocol.integrity_mismatch", "世界包 store 缺少 index.json")
        index = self._read_json_file(self.index_path, "世界包索引")
        packages = index.get("packages")
        if index.get("format") != TWP_PACKAGE_FORMAT or not isinstance(packages, dict):
            raise _issue("protocol.integrity_mismatch", "世界包索引不是当前 RC8 合同")

        expected_paths = {"index.json"}
        for package_key, raw_item in packages.items():
            package_id = str(package_key)
            if not isinstance(raw_item, Mapping):
                raise _issue("protocol.integrity_mismatch", "世界包索引记录损坏")
            item = dict(raw_item)
            self._validate_record_contract(package_id, item)
            archive_name = self._managed_filename(item.get("archive"), "archive")
            compiled_name = self._managed_filename(item.get("compiled"), "compiled")
            artifact_name = self._managed_filename(item.get("artifact"), "artifact")
            package_paths = {
                f"archives/{archive_name}",
                f"compiled/{compiled_name}",
                f"artifacts/{artifact_name}",
            }
            if expected_paths.intersection(package_paths):
                raise _issue("protocol.integrity_mismatch", "世界包索引路径重复")
            expected_paths.update(package_paths)

            archive_path = self.archives / archive_name
            if is_link_or_reparse(archive_path) or not archive_path.is_file():
                raise _issue("protocol.integrity_mismatch", "世界包 archive 丢失或链接")
            report = inspect_twp_archive(
                archive_path,
                overrides=dict(item.get("module_overrides") or {}),
            )
            identity = report.get("manifest", {}).get("identity", {})
            if (
                not isinstance(identity, Mapping)
                or str(identity.get("package_id") or "") != package_id
                or item.get("namespace") != identity.get("namespace")
                or item.get("name") != identity.get("name")
                or item.get("content_version") != identity.get("content_version")
                or item.get("version") != identity.get("content_version")
                or item.get("slug") != report.get("compiled_world", {}).get("slug")
                or item.get("modules") != report.get("modules")
                or item.get("artifact_id")
                != report.get("artifact", {}).get("artifact_id")
                or report.get("artifact_hash") != item.get("artifact_hash")
                or report.get("source_hash") != item.get("source_hash")
            ):
                raise _issue("protocol.integrity_mismatch", "世界包 archive 与索引不一致")

            compiled_value = self._read_json_file(
                self.compiled / compiled_name, "编译世界"
            )
            artifact_value = self._read_json_file(
                self.artifacts / artifact_name, "Artifact"
            )
            self._validate_compiled_value(package_id, item, compiled_value)
            self._validate_artifact_value(item, artifact_value, compiled_value)
            if (
                _canonical(compiled_value) != _canonical(report.get("compiled_world"))
                or _canonical(artifact_value)
                != _canonical(self._normalized_artifact(report))
            ):
                raise _issue(
                    "protocol.integrity_mismatch",
                    "世界包 store 无法由当前 RC8 archive 复核",
                )

        missing_paths = expected_paths - paths
        if missing_paths:
            raise _issue(
                "protocol.integrity_mismatch",
                "世界包 store 缺失索引成员，无法按当前 RC8 合同验证",
            )
        for extra_path in sorted(paths - expected_paths):
            pure = PurePosixPath(extra_path)
            if len(pure.parts) != 2 or pure.parts[0] != "archives":
                raise _issue(
                    "protocol.integrity_mismatch",
                    "世界包 store 含不可验证的未索引成员",
                )
            extra_name = self._managed_filename(pure.name, "retired archive")
            extra_archive = self.archives / extra_name
            if is_link_or_reparse(extra_archive) or not extra_archive.is_file():
                raise _issue("protocol.integrity_mismatch", "历史 archive 丢失或链接")
            extra_report = inspect_twp_archive(extra_archive)
            extra_identity = extra_report.get("manifest", {}).get("identity", {})
            if (
                not isinstance(extra_identity, Mapping)
                or str(extra_identity.get("package_id") or "") not in packages
            ):
                raise _issue(
                    "protocol.integrity_mismatch",
                    "历史 archive 不属于当前 RC8 已安装世界",
                )

    def _store_is_current(self, members: list[dict[str, object]]) -> bool:
        try:
            self._validate_current_store(members)
            return True
        except Exception:
            return False

    def _prepare_existing_store(self) -> None:
        if not os.path.lexists(self.root):
            return
        inventory = inventory_legacy_worlds([self.root], source_root=self.root)
        members = list(inventory["members"])
        if not members:
            return
        if self._store_is_current(members):
            return

        backup = backup_legacy_worlds(
            self.data_dir,
            [self.root],
            source_root=self.root,
        )
        receipt = backup.get("receipt")
        if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
            raise LegacyWorldBackupError(
                "旧世界包 store 没有通过 receipt 复核；活动 store 未移除"
            )
        current_inventory = inventory_legacy_worlds(
            [self.root], source_root=self.root
        )
        receipt_inventory = str(
            receipt.get("source_inventory_sha256")
            or receipt.get("source_inventory_hash")
            or ""
        )
        if (
            current_inventory["members"] != members
            or receipt_inventory != inventory["source_inventory_sha256"]
        ):
            raise LegacyWorldBackupError(
                "旧世界包 store 在备份后发生变化；活动 store 未移除"
            )
        try:
            shutil.rmtree(self.root)
        except OSError as exc:
            raise LegacyWorldBackupError(
                "旧世界包已备份，但活动 store 无法移除；未初始化新 store"
            ) from exc
        if os.path.lexists(self.root):
            raise LegacyWorldBackupError(
                "旧世界包活动 store 移除后仍存在；未初始化新 store"
            )

    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            if os.path.lexists(self.root):
                try:
                    inventory = inventory_legacy_worlds(
                        [self.root], source_root=self.root
                    )
                except LegacyWorldBackupError as exc:
                    raise _issue(
                        "protocol.integrity_mismatch",
                        "世界包 store 已损坏，无法读取",
                    ) from exc
                if inventory["members"]:
                    raise _issue(
                        "protocol.integrity_mismatch",
                        "世界包 store 有成员但缺少索引",
                    )
            return {"format": TWP_PACKAGE_FORMAT, "packages": {}}
        try:
            if is_link_or_reparse(self.index_path) or not self.index_path.is_file():
                raise OSError("index is not a regular managed file")
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _issue("protocol.integrity_mismatch", "世界包索引损坏") from exc
        if (
            not isinstance(value, dict)
            or value.get("format") != TWP_PACKAGE_FORMAT
            or not isinstance(value.get("packages"), dict)
        ):
            raise _issue("protocol.integrity_mismatch", "世界包索引不是当前 RC8 合同")
        return value

    def _write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}-",
            suffix=".json",
            dir=path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list_packages(self) -> list[dict[str, Any]]:
        return sorted(
            self._read_index()["packages"].values(),
            key=lambda item: (str(item.get("name") or ""), str(item.get("id") or "")),
        )

    @staticmethod
    def package_reference(package_id: object) -> str:
        """Return a stable opaque reference for browser and public DTO use."""
        normalized = str(package_id or "").strip()
        if not normalized:
            return ""
        digest = hashlib.sha256(
            f"twp-package-ref/1.0.0-rc10:{normalized}".encode("utf-8")
        ).hexdigest()
        return f"twp_{digest[:24]}"

    def resolve_reference(self, package_ref: object) -> _ResolvedPackageId:
        """Resolve an installed opaque package reference to its internal id."""
        normalized = str(package_ref or "").strip()
        if not normalized:
            raise _issue("world.package.missing", "缺少世界包引用")
        packages = self._read_index()["packages"]
        for package_id in packages:
            if self.package_reference(package_id) == normalized:
                return _ResolvedPackageId(str(package_id))
        raise _issue("world.package.not_found", "世界包引用已失效，请刷新世界库")

    def _resolve_exact_package_id(self, package_id: object) -> _ResolvedPackageId:
        """Resolve an internal id only through a transport-unforgeable marker."""
        normalized = str(package_id or "").strip()
        packages = self._read_index()["packages"]
        if not normalized or normalized not in packages:
            raise _issue("world.package.not_found", "世界包引用已失效，请刷新世界库")
        return _ResolvedPackageId(normalized)

    def _resolve_access_reference(self, value: object) -> _ResolvedPackageId:
        if isinstance(value, _ResolvedPackageId):
            return self._resolve_exact_package_id(value)
        return self.resolve_reference(value)

    def public_packages(self) -> list[dict[str, Any]]:
        """Project package records without ids, hashes, paths or actor details."""
        projected: list[dict[str, Any]] = []
        for item in self.list_packages():
            package_id = str(item.get("id") or "")
            projected.append(
                {
                    "package_ref": self.package_reference(package_id),
                    "name": str(item.get("name") or ""),
                    "slug": str(item.get("slug") or ""),
                    "version": str(item.get("version") or ""),
                    "protocol": str(item.get("protocol") or ""),
                    "modules": [
                        {
                            key: entry.get(key)
                            for key in (
                                "module_id",
                                "id",
                                "enabled",
                                "required",
                                "depends_on",
                                "api_version",
                            )
                            if key in entry
                        }
                        for entry in item.get("modules") or []
                        if isinstance(entry, Mapping)
                    ],
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        return projected

    def _get_exact(self, package_id: object) -> dict[str, Any]:
        item = self._read_index()["packages"].get(str(package_id))
        if not isinstance(item, dict):
            raise _issue("protocol.manifest_invalid", "世界包不存在")
        return item

    def get(self, package_ref: object) -> dict[str, Any]:
        package_id = self._resolve_access_reference(package_ref)
        return self._get_exact(package_id)

    async def install(self, path: Path, actor: str) -> dict[str, Any]:
        report = self._inspect_for_install(path)
        artifact_value = self._normalized_artifact(report)
        report["artifact"] = artifact_value
        package_id = str(report["manifest"]["identity"]["package_id"])
        world_slug = str(report["compiled_world"].get("slug") or "")
        async with self._lock:
            index = self._read_index()
            duplicate_slug = next(
                (
                    str(other_id)
                    for other_id, other in index["packages"].items()
                    if str(other_id) != package_id
                    and isinstance(other, Mapping)
                    and str(other.get("slug") or "") == world_slug
                ),
                "",
            )
            if duplicate_slug:
                raise _issue(
                    "world.slug_duplicate",
                    "世界 slug 已被另一个已安装世界使用",
                    "world/core.json.slug",
                )
            self.archives.mkdir(parents=True, exist_ok=True)
            self.compiled.mkdir(parents=True, exist_ok=True)
            self.artifacts.mkdir(parents=True, exist_ok=True)
            archive_name = f"{package_id}-{report['artifact_hash'][:12]}.zip"
            archive_path = self.archives / archive_name
            if not archive_path.exists():
                partial = self.archives / f".{archive_name}.part"
                shutil.copyfile(path, partial)
                os.replace(partial, archive_path)
            compiled_name = f"{package_id}.json"
            artifact_name = f"{package_id}-{report['artifact_hash'][:16]}.json"
            self._write_json(self.compiled / compiled_name, report["compiled_world"])
            self._write_json(self.artifacts / artifact_name, artifact_value)
            previous_raw = index["packages"].get(package_id, {})
            previous = dict(previous_raw) if isinstance(previous_raw, Mapping) else {}
            now = datetime.now(timezone.utc).isoformat()
            identity = report["manifest"]["identity"]
            item = {
                "id": package_id,
                "namespace": identity["namespace"],
                "name": identity["name"],
                "slug": world_slug,
                "version": identity["content_version"],
                "content_version": identity["content_version"],
                "protocol": TWP_VERSION,
                "artifact_id": report["artifact"]["artifact_id"],
                "artifact_hash": report["artifact_hash"],
                "source_hash": report["source_hash"],
                "archive": archive_name,
                "artifact": artifact_name,
                "compiled": compiled_name,
                "modules": report["modules"],
                "module_overrides": {},
                "installed_at": previous.get("installed_at") or now,
                "updated_at": now,
                "actor": str(actor),
                "conformance": report["conformance"],
            }
            index["packages"][package_id] = item
            self._write_json(self.index_path, index)
            old_artifact = str(previous.get("artifact") or "")
            if old_artifact and old_artifact != artifact_name:
                try:
                    safe_old = self._managed_filename(old_artifact, "retired")
                    (self.artifacts / safe_old).unlink(missing_ok=True)
                except (OSError, TwpPackageError):
                    pass
            return {"package": item, "report": report, "changed": True}

    async def restore_package_record(
        self,
        package_id: str,
        previous: Mapping[str, Any] | None,
    ) -> None:
        """Restore the last successful package index record after a later stage fails."""

        async with self._lock:
            index = self._read_index()
            current_raw = index["packages"].get(str(package_id))
            current = dict(current_raw) if isinstance(current_raw, Mapping) else {}
            if previous:
                restored = dict(previous)
                restored_id = str(restored.get("id") or package_id)
                self._validate_record_contract(restored_id, restored)
                archive_name = self._managed_filename(
                    restored.get("archive"), "restore archive"
                )
                compiled_name = self._managed_filename(
                    restored.get("compiled"), "restore compiled"
                )
                artifact_name = self._managed_filename(
                    restored.get("artifact"), "restore artifact"
                )
                report = inspect_twp_archive(
                    self.archives / archive_name,
                    overrides=dict(restored.get("module_overrides") or {}),
                )
                identity = report.get("manifest", {}).get("identity", {})
                if (
                    not isinstance(identity, Mapping)
                    or str(identity.get("package_id") or "") != restored_id
                    or report.get("artifact_hash") != restored.get("artifact_hash")
                    or report.get("source_hash") != restored.get("source_hash")
                ):
                    raise _issue(
                        "protocol.integrity_mismatch",
                        "无法从原始 RC8 archive 恢复世界包记录",
                    )
                artifact_value = self._normalized_artifact(report)
                report["artifact"] = artifact_value
                self._validate_compiled_value(
                    restored_id, restored, report["compiled_world"]
                )
                self._validate_artifact_value(
                    restored, artifact_value, report["compiled_world"]
                )
                self._write_json(
                    self.compiled / compiled_name, report["compiled_world"]
                )
                self._write_json(self.artifacts / artifact_name, artifact_value)
                if restored_id != str(package_id):
                    index["packages"].pop(str(package_id), None)
                index["packages"][restored_id] = restored
            else:
                index["packages"].pop(str(package_id), None)
            self._write_json(self.index_path, index)

            keep = dict(previous) if previous else {}
            for directory, field in (
                (self.archives, "archive"),
                (self.compiled, "compiled"),
                (self.artifacts, "artifact"),
            ):
                old_name = str(current.get(field) or "")
                keep_name = str(keep.get(field) or "")
                if old_name and old_name != keep_name:
                    try:
                        safe_old = self._managed_filename(old_name, f"retired {field}")
                        (directory / safe_old).unlink(missing_ok=True)
                    except (OSError, TwpPackageError):
                        pass

    async def ensure_installed(self, path: Path, actor: str) -> dict[str, Any]:
        """Install a package only when its content or managed files changed."""
        report = self._inspect_for_install(path)
        report["artifact"] = self._normalized_artifact(report)
        package_id = str(report["manifest"]["identity"]["package_id"])
        try:
            current = self._get_exact(package_id)
            self._validate_record_contract(package_id, current)
            archive_name = self._managed_filename(current.get("archive"), "archive")
            compiled_name = self._managed_filename(current.get("compiled"), "compiled")
            artifact_name = self._managed_filename(current.get("artifact"), "artifact")
            unchanged = (
                current.get("artifact_hash") == report["artifact_hash"]
                and current.get("source_hash") == report["source_hash"]
                and (self.archives / archive_name).is_file()
                and (self.compiled / compiled_name).is_file()
                and (self.artifacts / artifact_name).is_file()
            )
        except TwpPackageError as exc:
            if exc.issue.code != "protocol.manifest_invalid":
                raise
            current = {}
            unchanged = False
        if unchanged:
            stored_archive = self.archives / archive_name
            if is_link_or_reparse(stored_archive):
                raise _issue("protocol.integrity_mismatch", "原始 TWP ZIP 不能是链接")
            stored_report = inspect_twp_archive(
                stored_archive,
                overrides=dict(current.get("module_overrides") or {}),
            )
            if (
                stored_report.get("artifact_hash") != current.get("artifact_hash")
                or stored_report.get("source_hash") != current.get("source_hash")
            ):
                raise _issue("protocol.integrity_mismatch", "原始 TWP ZIP 与索引不一致")
            compiled_value = self._read_json_file(
                self.compiled / compiled_name, "编译世界"
            )
            artifact_value = self._read_json_file(
                self.artifacts / artifact_name, "Artifact"
            )
            self._validate_compiled_value(package_id, current, compiled_value)
            self._validate_artifact_value(current, artifact_value, compiled_value)
            return {"package": current, "report": report, "changed": False}
        return await self.install(path, actor)

    def _inspect_for_install(self, path: Path) -> dict[str, Any]:
        """Compile a current package or preserve an unsupported one before rejection."""

        source = Path(path).resolve()
        try:
            return inspect_twp_archive(source)
        except TwpPackageError as exc:
            if exc.issue.code != "protocol.unsupported":
                raise
            try:
                backup = backup_legacy_worlds(
                    self.data_dir,
                    [source],
                    source_root=source.parent,
                )
            except LegacyWorldBackupError as backup_exc:
                raise _issue(
                    "world.archive.required",
                    "导入旧世界包失败：包协议不受 RC10 支持，且安全备份未完成；"
                    "系统未安装、修改或删除该文件。请检查数据目录权限后重试。",
                    "world-package",
                ) from backup_exc
            receipt = backup.get("receipt")
            if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
                raise _issue(
                    "world.archive.required",
                    "导入旧世界包失败：备份回执无法复核；系统未安装或修改该文件。",
                    "world-package",
                )
            self.last_rejection_receipt = {
                "verified": True,
                "inventory_sha256": str(
                    receipt.get("source_inventory_sha256")
                    or receipt.get("source_inventory_hash")
                    or ""
                ),
                "member_count": int(receipt.get("member_count") or 0),
                "reused": bool(backup.get("reused")),
            }
            receipt_key = self.last_rejection_receipt["inventory_sha256"][:12]
            raise _issue(
                "protocol.unsupported",
                "导入旧世界包失败：RC10 只接受 World Schema 12 和 TWP 1.0.0-rc10；"
                f"系统已完成可校验备份（回执 {receipt_key}），没有安装或修改旧包。"
                "下一步请安装该世界的 RC10 重制版本。",
                "world-package",
            ) from exc

    async def set_module(
        self,
        package_id: str,
        module_id: str,
        enabled: bool,
        actor: str,
    ) -> dict[str, Any]:
        package_id = self._resolve_access_reference(package_id)
        async with self._lock:
            index = self._read_index()
            item = index["packages"].get(str(package_id))
            if not isinstance(item, dict):
                raise _issue("protocol.manifest_invalid", "世界包不存在")
            self._validate_record_contract(str(package_id), item)
            archive_name = self._managed_filename(item.get("archive"), "archive")
            compiled_name = self._managed_filename(item.get("compiled"), "compiled")
            archive_path = self.archives / archive_name
            if is_link_or_reparse(archive_path):
                raise _issue("protocol.integrity_mismatch", "原始 TWP ZIP 不能是链接")
            declared = {
                str(entry.get("module_id") or entry.get("id")): entry
                for entry in item.get("modules") or []
                if isinstance(entry, Mapping)
            }
            declaration = declared.get(str(module_id))
            if declaration is None:
                raise _issue("protocol.manifest_invalid", f"世界包未声明模块 {module_id}")
            if declaration.get("required") and not enabled:
                raise _issue("module.dependency_disabled", f"必需模块 {module_id} 不能关闭")
            overrides = dict(item.get("module_overrides") or {})
            overrides[str(module_id)] = bool(enabled)
            report = inspect_twp_archive(
                archive_path,
                overrides=overrides,
            )
            identity = report.get("manifest", {}).get("identity", {})
            if (
                not isinstance(identity, Mapping)
                or str(identity.get("package_id") or "") != str(package_id)
                or report.get("source_hash") != item.get("source_hash")
            ):
                raise _issue("protocol.integrity_mismatch", "原始 TWP ZIP 与索引不一致")
            artifact_value = self._normalized_artifact(report)
            report["artifact"] = artifact_value
            previous_artifact = str(item.get("artifact") or "")
            item.update(
                {
                    "module_overrides": overrides,
                    "modules": report["modules"],
                    "artifact_id": report["artifact"]["artifact_id"],
                    "artifact_hash": report["artifact_hash"],
                    "source_hash": report["source_hash"],
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "actor": str(actor),
                    "conformance": report["conformance"],
                }
            )
            self._write_json(self.compiled / compiled_name, report["compiled_world"])
            artifact_name = f"{package_id}-{report['artifact_hash'][:16]}.json"
            item["artifact"] = artifact_name
            self._write_json(self.artifacts / artifact_name, artifact_value)
            index["packages"][str(package_id)] = item
            self._write_json(self.index_path, index)
            if previous_artifact and previous_artifact != artifact_name:
                try:
                    safe_previous = self._managed_filename(
                        previous_artifact, "retired artifact"
                    )
                    (self.artifacts / safe_previous).unlink(missing_ok=True)
                except (OSError, TwpPackageError):
                    pass
            return {"package": item, "report": report}

    def compiled_world(self, package_id: str) -> dict[str, Any]:
        package_id = self._resolve_access_reference(package_id)
        item = self._get_exact(package_id)
        self._validate_record_contract(str(package_id), item)
        compiled_name = self._managed_filename(item.get("compiled"), "compiled")
        artifact_name = self._managed_filename(item.get("artifact"), "artifact")
        value = self._read_json_file(self.compiled / compiled_name, "编译世界")
        artifact_value = self._read_json_file(
            self.artifacts / artifact_name, "Artifact"
        )
        self._validate_compiled_value(str(package_id), item, value)
        self._validate_artifact_value(item, artifact_value, value)
        return value

    def artifact(self, package_id: str) -> dict[str, Any]:
        package_id = self._resolve_access_reference(package_id)
        item = self._get_exact(package_id)
        self._validate_record_contract(str(package_id), item)
        compiled_name = self._managed_filename(item.get("compiled"), "compiled")
        artifact_name = self._managed_filename(item.get("artifact"), "artifact")
        compiled_value = self._read_json_file(
            self.compiled / compiled_name, "编译世界"
        )
        value = self._read_json_file(self.artifacts / artifact_name, "Artifact")
        self._validate_compiled_value(str(package_id), item, compiled_value)
        self._validate_artifact_value(item, value, compiled_value)
        return value

    def archive_path(self, package_id: str) -> Path:
        package_id = self._resolve_access_reference(package_id)
        item = self._get_exact(package_id)
        archive_name = self._managed_filename(item.get("archive"), "archive")
        source = self.archives / archive_name
        try:
            linked = is_link_or_reparse(source)
        except OSError:
            linked = True
        path = source.resolve()
        if linked or self.archives.resolve() not in path.parents or not path.is_file():
            raise _issue("asset.missing", "原始 TWP ZIP 已丢失")
        return path

    def protocol_info(self) -> dict[str, Any]:
        modules = []
        for module_id in STANDARD_MODULES:
            label, description = MODULE_METADATA.get(
                module_id,
                (module_id, "世界包标准模块。"),
            )
            optional = module_id in OPTIONAL_MODULES
            modules.append(
                {
                    "id": module_id,
                    "label": label,
                    "description": description,
                    "category": "optional" if optional else "required",
                    "required": not optional,
                    "optional": optional,
                    "default_enabled": True,
                    "depends_on": [],
                }
            )
        required_count = sum(1 for item in modules if item["required"])
        optional_count = len(modules) - required_count
        return {
            "format": TWP_FORMAT,
            "package_format": TWP_PACKAGE_FORMAT,
            "compiler_abi": TWP_COMPILER_ABI,
            "artifact_schema": TWP_ARTIFACT_SCHEMA,
            "compiled_world_schema": TWP_COMPILED_WORLD_SCHEMA,
            "runtime_schema": TWP_RUNTIME_SCHEMA,
            "protocol": {
                "name": "TWP",
                "core": TWP_CORE_VERSION,
                "version": TWP_VERSION,
                "maturity": TWP_MATURITY,
            },
            "core_capabilities": [dict(item) for item in CORE_CAPABILITIES],
            "modules": modules,
            "summary": {
                "core_capabilities": len(CORE_CAPABILITIES),
                "required_modules": required_count,
                "optional_modules": optional_count,
                "standard_modules": len(modules),
            },
            "limits": {
                "archive_bytes": MAX_ARCHIVE_BYTES,
                "uncompressed_bytes": MAX_UNCOMPRESSED_BYTES,
                "member_bytes": MAX_MEMBER_BYTES,
                "files": MAX_FILES,
            },
        }


__all__ = ["TwpPackageService", "inspect_twp_archive"]
