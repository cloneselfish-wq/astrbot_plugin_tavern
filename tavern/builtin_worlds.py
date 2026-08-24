"""Authoritative catalog for player-facing built-in TWP worlds.

The catalog deliberately contains install metadata only.  Compilation and
database writes stay in the existing protocol and repository services so that
``main.py`` can install every built-in through one uniform path.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .constants import DEFAULT_WORLD_CONTENT_VERSION, DEFAULT_WORLD_SLUG
from .protocol.constants import TWP_PACKAGE_FORMAT
from .projections.session import project_world_summary_view


_WORLD_TECHNICAL_FIELDS = (
    "id",
    "slug",
    "revision",
    "rules",
    "system_prompt",
    "source_artifact_hash",
    "internal_world_model_revision",
    "package_format",
    "created_at",
    "updated_at",
    "migration_status",
    "source_kind",
    "is_modified",
    "previous_content_version",
    "source_package_id",
    "package_id",
    "artifact_id",
    "artifact_hash",
    "twp_modules",
    "opening_scene",
    "initial_state",
)


@dataclass(frozen=True, slots=True)
class BuiltinWorldSpec:
    key: str
    display_name: str
    archive_name: str
    package_id: str
    slug: str
    content_version: str
    is_default: bool
    player_visible: bool
    developer_fixture: bool
    install_order: int
    seed_characters: tuple[dict[str, Any], ...] = ()

    def export(self) -> dict[str, Any]:
        return asdict(self)


_BUILTIN_WORLD_SPECS = (
    BuiltinWorldSpec(
        key="thirteenth_seat",
        display_name="第十三席：万邦新纪",
        archive_name=(
            f"thirteenth-seat-new-era-twp-{DEFAULT_WORLD_CONTENT_VERSION}.zip"
        ),
        package_id="builtin.thirteenth-seat-new-era",
        slug=DEFAULT_WORLD_SLUG,
        content_version=DEFAULT_WORLD_CONTENT_VERSION,
        is_default=True,
        player_visible=True,
        developer_fixture=False,
        install_order=10,
    ),
)


def validate_builtin_world_specs(
    specs: Sequence[BuiltinWorldSpec],
) -> None:
    """Reject ambiguous identities and archive paths before installation."""

    if not specs:
        raise ValueError("内置世界目录不能为空")
    seen: dict[str, set[str]] = {
        "key": set(),
        "package_id": set(),
        "slug": set(),
        "archive_name": set(),
    }
    defaults = 0
    orders: set[int] = set()
    for spec in specs:
        for field in ("key", "package_id", "slug", "archive_name"):
            value = str(getattr(spec, field) or "").strip()
            if not value:
                raise ValueError(f"内置世界 {field} 不能为空")
            if value in seen[field]:
                raise ValueError(f"内置世界 {field} 重复：{value}")
            seen[field].add(value)
        archive = PurePosixPath(spec.archive_name)
        if (
            archive.is_absolute()
            or len(archive.parts) != 1
            or any(part in {"", ".", ".."} for part in archive.parts)
            or "\\" in spec.archive_name
            or archive.suffix.lower() != ".zip"
        ):
            raise ValueError(f"内置世界归档路径非法：{spec.archive_name}")
        if not str(spec.content_version or "").strip():
            raise ValueError(f"内置世界内容版本为空：{spec.key}")
        if spec.install_order in orders:
            raise ValueError(f"内置世界安装顺序重复：{spec.install_order}")
        orders.add(spec.install_order)
        if spec.player_visible and spec.developer_fixture:
            raise ValueError(f"玩家世界不能同时是开发夹具：{spec.key}")
        if spec.is_default:
            defaults += 1
            if not spec.player_visible or spec.developer_fixture:
                raise ValueError("默认世界必须面向玩家且不能是开发夹具")
    if defaults != 1:
        raise ValueError(f"内置世界目录必须恰有一个默认世界，当前为 {defaults}")


def builtin_world_specs() -> tuple[BuiltinWorldSpec, ...]:
    validate_builtin_world_specs(_BUILTIN_WORLD_SPECS)
    return tuple(sorted(_BUILTIN_WORLD_SPECS, key=lambda item: item.install_order))


def builtin_world_spec_by_key(key: object) -> BuiltinWorldSpec:
    normalized = str(key or "").strip()
    for spec in builtin_world_specs():
        if spec.key == normalized:
            return spec
    raise ValueError(f"未知内置世界：{normalized or '空'}")


def builtin_world_spec_for(
    world: Mapping[str, Any],
) -> BuiltinWorldSpec | None:
    """Resolve one persisted world back to its authoritative catalog entry."""

    slug = str(world.get("slug") or "").strip()
    package_id = str(
        world.get("source_package_id")
        or world.get("package_id")
        or ""
    ).strip()
    for spec in builtin_world_specs():
        if spec.slug == slug and spec.package_id == package_id:
            return spec
    return None


def project_world_catalog_item(
    world: Mapping[str, Any],
    *,
    default_slug: str = DEFAULT_WORLD_SLUG,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """Add player-facing preset state without leaking database field names.

    D1-UX-006：普通视图只包含外显字段与 ``module_summary``；数据库修订号、
    包 ID、原始规则 JSON 与 artifact 哈希仅进入 ``technical_details`` 且
    仅授权角色可见。模块数量一律来自权威 manifest，前端不得自行推断。
    """

    item = dict(world)
    admin_refs = {
        "id": str(world.get("id") or ""),
        "slug": str(world.get("slug") or ""),
        "source_package_id": str(
            world.get("source_package_id")
            or world.get("package_id")
            or ""
        ),
        "package_format": int(world.get("package_format") or 0),
    }
    package_id = str(
        world.get("source_package_id")
        or world.get("package_id")
        or ""
    ).strip()
    package_ref = ""
    if package_id:
        package_ref = "twp_" + hashlib.sha256(
            f"twp-package-ref/1.0.0-rc10:{package_id}".encode("utf-8")
        ).hexdigest()[:24]
    world_slug = str(item.get("slug") or "")
    spec = builtin_world_spec_for(world)
    summary = project_world_summary_view(
        item,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    item["module_summary"] = summary["module_summary"]
    item["protocol_display"] = summary["protocol_display"]
    item["minimum_plugin_version"] = summary["minimum_plugin_version"]
    item["actor_stats"] = summary["actor_stats"]
    item["content_stats"] = summary["content_stats"]
    item["player_limits"] = summary["player_limits"]
    item["technical_details"] = summary["technical_details"]
    item["source_package_ref"] = package_ref
    for key in _WORLD_TECHNICAL_FIELDS:
        item.pop(key, None)
    if include_technical_refs and viewer_role == "admin":
        item.update(admin_refs)
    item["enabled"] = not bool(item.get("archived"))
    item["default"] = world_slug == str(default_slug or "")

    item["builtin"] = spec is not None
    if spec is None:
        item.setdefault("player_visible", item["enabled"])
        return item

    item.update(
        {
            "builtin_key": spec.key,
            "player_visible": spec.player_visible,
            "developer_fixture": spec.developer_fixture,
            "install_state": "installed",
            "install_error": None,
        }
    )
    return item


def project_world_catalog(
    worlds: Sequence[Mapping[str, Any]],
    *,
    default_slug: str = DEFAULT_WORLD_SLUG,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> list[dict[str, Any]]:
    """Project a world list for BOT/WebUI preset selection."""

    return [
        project_world_catalog_item(
            world,
            default_slug=default_slug,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )
        for world in worlds
    ]


def merge_builtin_world_statuses(
    items: list[dict[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    *,
    default_slug: str = DEFAULT_WORLD_SLUG,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
    can_retry: bool = False,
) -> None:
    """Merge install state and expose a recovery card when persistence is missing."""

    by_key = {
        str(item.get("builtin_key") or ""): item
        for item in items
        if item.get("builtin_key")
    }
    for raw_status in statuses:
        status = dict(raw_status)
        key = str(status.get("key") or "")
        if not key:
            continue
        target = by_key.get(key)
        reported_state = str(status.get("state") or "pending")
        if target is None:
            if bool(status.get("archived")):
                continue
            try:
                spec = builtin_world_spec_by_key(key)
            except ValueError:
                continue
            if reported_state in {"ready", "installed"}:
                reported_state = "blocked"
                status["last_error"] = (
                    str(status.get("last_error") or "")
                    or "builtin_world_record_missing"
                )
                status["message"] = "世界包已验证，但世界库记录缺失；系统将尝试自动恢复。"
            target = project_world_catalog_item(
                {
                    "id": "",
                    "slug": spec.slug,
                    "source_package_id": spec.package_id,
                    "package_format": TWP_PACKAGE_FORMAT,
                    "name": spec.display_name,
                    "description": "该内置世界尚未完成写入，恢复成功后即可启用。",
                    "content_version": spec.content_version,
                    "archived": False,
                    "rules": {},
                },
                default_slug=default_slug,
                viewer_role=viewer_role,
                include_technical_refs=include_technical_refs,
            )
            target["enabled"] = False
            items.append(target)
            by_key[key] = target
        elif (
            reported_state in {"pending", "ready"}
            and target.get("install_state") == "installed"
        ):
            reported_state = "installed"
        target["install_state"] = reported_state
        target["install_error"] = str(status.get("last_error") or "") or None
        target["install_message"] = str(status.get("message") or "")
        target["using_previous_version"] = bool(status.get("using_previous_version"))
        target["installed_content_version"] = str(
            status.get("installed_content_version")
            or target.get("content_version")
            or ""
        )
        target["can_retry"] = can_retry

def resolve_builtin_archive(
    plugin_root: Path,
    spec: BuiltinWorldSpec,
) -> Path:
    """Resolve a catalog archive and prove it stays below ``worlds/``."""

    # Validate the concrete spec against the authoritative catalog.  Calling
    # the catalog validator on a lone non-default spec would incorrectly fail
    # the global "exactly one default" invariant.
    catalog = builtin_world_specs()
    if spec not in catalog:
        raise ValueError(f"未知内置世界目录项：{spec.key}")
    worlds_root = (Path(plugin_root).resolve() / "worlds").resolve()
    target = (worlds_root / spec.archive_name).resolve()
    if target.parent != worlds_root:
        raise ValueError(f"内置世界归档越界：{spec.archive_name}")
    return target


def validate_installed_builtin(
    spec: BuiltinWorldSpec,
    installed: dict[str, Any],
) -> None:
    """Prove that an archive compiled to the catalog identity it claims."""

    package = dict(installed.get("package") or {})
    report = dict(installed.get("report") or {})
    compiled_world = dict(report.get("compiled_world") or {})
    actual_package_id = str(
        package.get("id")
        or package.get("package_id")
        or compiled_world.get("package_id")
        or ""
    )
    actual_version = str(
        package.get("content_version")
        or package.get("version")
        or compiled_world.get("content_version")
        or ""
    )
    actual_slug = str(compiled_world.get("slug") or "")
    if actual_package_id != spec.package_id:
        raise ValueError(
            f"内置世界包身份不匹配：目录={spec.package_id}，归档={actual_package_id or '空'}"
        )
    if actual_version != spec.content_version:
        raise ValueError(
            f"内置世界内容版本不匹配：目录={spec.content_version}，归档={actual_version or '空'}"
        )
    if actual_slug != spec.slug:
        raise ValueError(
            f"内置世界 slug 不匹配：目录={spec.slug}，归档={actual_slug or '空'}"
        )
    artifact_hash = str(
        package.get("artifact_hash")
        or compiled_world.get("artifact_hash")
        or ""
    )
    if not artifact_hash:
        raise ValueError(f"内置世界 {spec.key} 缺少 Artifact 哈希")


__all__ = [
    "BuiltinWorldSpec",
    "builtin_world_spec_by_key",
    "builtin_world_spec_for",
    "builtin_world_specs",
    "merge_builtin_world_statuses",
    "project_world_catalog",
    "project_world_catalog_item",
    "resolve_builtin_archive",
    "validate_installed_builtin",
    "validate_builtin_world_specs",
]
