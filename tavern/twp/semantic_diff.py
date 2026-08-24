"""Seven-layer semantic world diff.

The diff operates on frozen mappings (installed revisions, compiled candidates
or registered baselines).  It never reads or mutates a live author directory.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA_ID = "tavern-semantic-world-diff/1.0.0-rc10"
LAYERS = (
    "artifact",
    "manifest",
    "runtime_contract",
    "stable_reference",
    "player_content",
    "data_impact",
    "release",
)
_VISIBLE_KEYS = frozenset(
    {
        "label",
        "name",
        "title",
        "summary",
        "description",
        "hint",
        "purpose",
        "best_for",
        "limitations",
        "story_hooks",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "protocol_version",
        "package_format",
        "minimum_plugin_version",
        "dependencies",
        "identity",
        "content_version",
        "package_id",
        "slug",
    }
)
_RUNTIME_KEYS = frozenset(
    {
        "modules",
        "twp_modules",
        "commands",
        "events",
        "definition_schema",
        "runtime_schema",
        "state_schema",
        "operations",
        "conditions",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _flatten(value: Any, path: str = "$") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, Mapping):
        if not value:
            result[path] = {}
        for key in sorted(value, key=lambda item: str(item)):
            result.update(_flatten(value[key], f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            result[path] = []
        for index, item in enumerate(value):
            result.update(_flatten(item, f"{path}[{index}]"))
    else:
        result[path] = value
    return result


def _stable_refs(value: Any, path: str = "$") -> dict[str, dict[str, Any]]:
    refs: dict[str, dict[str, Any]] = {}
    if isinstance(value, Mapping):
        candidate = str(value.get("id") or value.get("ref") or "").strip()
        if ":" in candidate:
            refs[candidate] = {
                "path": path,
                "label": str(
                    value.get("label")
                    or value.get("name")
                    or value.get("title")
                    or ""
                ),
                "hash": canonical_hash(value),
            }
        for key, item in value.items():
            refs.update(_stable_refs(item, f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            refs.update(_stable_refs(item, f"{path}[{index}]"))
    return refs


def _visible_projection(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _visible_projection(item)
            for key, item in value.items()
            if str(key) in _VISIBLE_KEYS
            or isinstance(item, (Mapping, list, tuple))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_visible_projection(item) for item in value]
    return value


def _subset(value: Mapping[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    result = {key: value[key] for key in keys if key in value}
    rules = _mapping(value.get("rules"))
    for key in keys:
        if key in rules:
            result[f"rules.{key}"] = rules[key]
    return result


def _item(
    *,
    layer: str,
    change_type: str,
    path: str,
    before: Any = None,
    after: Any = None,
    stable_ref: str = "",
    severity: str = "info",
    message: str = "",
) -> dict[str, Any]:
    return {
        "layer": layer,
        "change_type": change_type,
        "severity": severity,
        "path": path,
        "stable_ref": stable_ref,
        "before_hash": canonical_hash(before) if before is not None else "",
        "after_hash": canonical_hash(after) if after is not None else "",
        "message": message,
    }


def _mapping_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    layer: str,
    severity: str,
) -> list[dict[str, Any]]:
    left = _flatten(before)
    right = _flatten(after)
    items: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        if path not in left:
            items.append(
                _item(
                    layer=layer,
                    change_type="added",
                    path=path,
                    after=right[path],
                    severity="info",
                    message="新增语义内容。",
                )
            )
        elif path not in right:
            items.append(
                _item(
                    layer=layer,
                    change_type="removed",
                    path=path,
                    before=left[path],
                    severity=severity,
                    message="删除既有语义内容。",
                )
            )
        elif left[path] != right[path]:
            items.append(
                _item(
                    layer=layer,
                    change_type="changed",
                    path=path,
                    before=left[path],
                    after=right[path],
                    severity=severity,
                    message="语义内容发生变化。",
                )
            )
    return items


def semantic_world_diff(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    reviewed: bool = False,
) -> dict[str, Any]:
    """Compare two frozen world sources and return seven-layer evidence."""

    old = _mapping(before)
    new = _mapping(after)
    items: list[dict[str, Any]] = []

    old_members = _mapping(old.get("_artifact_members"))
    new_members = _mapping(new.get("_artifact_members"))
    items.extend(
        _mapping_diff(
            old_members,
            new_members,
            layer="artifact",
            severity="warning",
        )
    )
    items.extend(
        _mapping_diff(
            _subset(old, _MANIFEST_KEYS),
            _subset(new, _MANIFEST_KEYS),
            layer="manifest",
            severity="warning",
        )
    )
    runtime_items = _mapping_diff(
        _subset(old, _RUNTIME_KEYS),
        _subset(new, _RUNTIME_KEYS),
        layer="runtime_contract",
        severity="breaking",
    )
    items.extend(runtime_items)

    old_refs = _stable_refs(old)
    new_refs = _stable_refs(new)
    removed_refs = sorted(set(old_refs) - set(new_refs))
    added_refs = sorted(set(new_refs) - set(old_refs))
    for ref in removed_refs:
        items.append(
            _item(
                layer="stable_reference",
                change_type="removed",
                path=old_refs[ref]["path"],
                stable_ref=ref,
                before=old_refs[ref],
                severity="breaking",
                message="稳定引用被删除，已有角色、快照或任务可能失效。",
            )
        )
    for ref in added_refs:
        items.append(
            _item(
                layer="stable_reference",
                change_type="added",
                path=new_refs[ref]["path"],
                stable_ref=ref,
                after=new_refs[ref],
                severity="info",
                message="新增稳定引用。",
            )
        )
    for ref in sorted(set(old_refs) & set(new_refs)):
        if old_refs[ref]["hash"] != new_refs[ref]["hash"]:
            items.append(
                _item(
                    layer="stable_reference",
                    change_type="changed",
                    path=new_refs[ref]["path"],
                    stable_ref=ref,
                    before=old_refs[ref],
                    after=new_refs[ref],
                    severity="warning",
                    message="稳定引用保留，但其定义发生变化。",
                )
            )

    items.extend(
        _mapping_diff(
            _mapping(_visible_projection(old)),
            _mapping(_visible_projection(new)),
            layer="player_content",
            severity="warning",
        )
    )
    for ref in removed_refs:
        items.append(
            _item(
                layer="data_impact",
                change_type="reference_invalidated",
                path=old_refs[ref]["path"],
                stable_ref=ref,
                before=old_refs[ref],
                severity="breaking",
                message="需要迁移或保留兼容定义，禁止对运行副本热替换。",
            )
        )

    has_breaking = any(item["severity"] == "breaking" for item in items)
    semantic_changed = canonical_hash(old) != canonical_hash(new)
    old_version = str(
        _mapping(old.get("identity")).get("content_version")
        or old.get("content_version")
        or ""
    )
    new_version = str(
        _mapping(new.get("identity")).get("content_version")
        or new.get("content_version")
        or ""
    )
    if semantic_changed and old_version == new_version:
        items.append(
            _item(
                layer="release",
                change_type="version_not_advanced",
                path="$.identity.content_version",
                before=old_version,
                after=new_version,
                severity="breaking",
                message="语义内容变化但世界内容版本未推进。",
            )
        )
        has_breaking = True
    recommendation = (
        "breaking_version_and_migration"
        if has_breaking
        else ("prerelease_or_patch" if semantic_changed else "unchanged")
    )
    blockers = [
        item for item in items if item["severity"] == "breaking"
    ]
    if semantic_changed and not reviewed:
        blockers.append(
            _item(
                layer="release",
                change_type="review_required",
                path="$",
                severity="breaking",
                message="语义差异尚未完成人工复核。",
            )
        )
    return {
        "schema": SCHEMA_ID,
        "before_hash": canonical_hash(old),
        "after_hash": canonical_hash(new),
        "semantic_changed": semantic_changed,
        "reviewed": bool(reviewed),
        "version": {
            "before": old_version,
            "after": new_version,
            "recommendation": recommendation,
        },
        "summary": {
            "items": len(items),
            "breaking": sum(
                item["severity"] == "breaking" for item in items
            ),
            "warnings": sum(
                item["severity"] == "warning" for item in items
            ),
            "added_refs": len(added_refs),
            "removed_refs": len(removed_refs),
        },
        "layers": {
            layer: [item for item in items if item["layer"] == layer]
            for layer in LAYERS
        },
        "items": items,
        "blockers": blockers,
        "compatible": not blockers,
    }


__all__ = [
    "LAYERS",
    "SCHEMA_ID",
    "canonical_hash",
    "semantic_world_diff",
]
