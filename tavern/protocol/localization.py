from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any


TEXT_FIELDS = frozenset(
    {
        "label",
        "name",
        "title",
        "summary",
        "description",
        "help",
        "text",
        "advantages",
        "limitations",
        "narrative_benefits",
        "costs_and_limits",
        "story_hooks",
        "failure_forward",
        "goal",
        "public_goal",
        "provides",
        "conflict",
        "counterplay",
        "does_not_grant",
        "known_consequences",
    }
)

NON_PLAYER_VISIBILITY = frozenset({"dm", "dm_only", "author", "internal", "system"})
STABLE_ID_FIELDS = ("id", "key", "slug", "field_id")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _identity(
    value: Mapping[str, Any],
    fallback: object,
    *,
    strategy: str,
    path: str,
    identity_fields: dict[str, int],
) -> str:
    if strategy == "index":
        return str(fallback)
    identity_candidates = [
        *STABLE_ID_FIELDS,
        *sorted(
            str(field)
            for field in value
            if str(field).endswith("_id")
            and str(field) not in {*STABLE_ID_FIELDS, "text_id"}
        ),
    ]
    for field in identity_candidates:
        identity = str(value.get(field) or "").strip()
        if identity:
            identity_fields[field] = identity_fields.get(field, 0) + 1
            return identity
    expected = "/".join(STABLE_ID_FIELDS)
    raise ValueError(
        f"{path} 在 stable_id 策略下缺少稳定标识字段"
        f"（需要 {expected} 或协议专用 *_id，text_id 不能替代实体标识）"
    )


def _contains_visible_text(
    value: Any,
    *,
    include_protected_direct: bool,
) -> bool:
    if isinstance(value, Mapping):
        visibility = str(value.get("visibility") or "public").strip().lower()
        if visibility in NON_PLAYER_VISIBILITY:
            return include_protected_direct and any(
                key in TEXT_FIELDS and isinstance(item, str) and bool(item.strip())
                for key, item in value.items()
            )
        for key, item in value.items():
            if key in TEXT_FIELDS:
                if isinstance(item, str) and item.strip():
                    return True
                if isinstance(item, (Mapping, list, tuple)) and item:
                    return True
            elif isinstance(item, (Mapping, list, tuple)) and _contains_visible_text(
                item,
                include_protected_direct=include_protected_direct,
            ):
                return True
        return False
    return any(
        _contains_visible_text(
            item,
            include_protected_direct=include_protected_direct,
        )
        for item in _sequence(value)
    )


def _add_text_value(
    result: dict[str, str],
    path: str,
    value: Any,
    *,
    array_id_strategy: str,
    identity_fields: dict[str, int],
) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            result[path] = text
        return
    if isinstance(value, Mapping):
        root = str(value.get("text_id") or path).strip()
        for key, item in value.items():
            key_text = str(key)
            if key_text in TEXT_FIELDS:
                _add_text_value(
                    result,
                    f"{root}.{key_text}",
                    item,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
        return
    seen: set[str] = set()
    for index, item in enumerate(_sequence(value)):
        if isinstance(item, Mapping):
            segment = _identity(
                item,
                index,
                strategy=array_id_strategy,
                path=f"{path}[{index}]",
                identity_fields=identity_fields,
            )
            if array_id_strategy == "stable_id":
                if segment in seen:
                    raise ValueError(
                        f"{path} 在 stable_id 策略下存在重复标识：{segment}"
                    )
                seen.add(segment)
            root = str(item.get("text_id") or f"{path}.{segment}").strip()
            found = False
            for key, nested in item.items():
                key_text = str(key)
                if key_text in TEXT_FIELDS:
                    _add_text_value(
                        result,
                        f"{root}.{key_text}",
                        nested,
                        array_id_strategy=array_id_strategy,
                        identity_fields=identity_fields,
                    )
                    found = True
            if not found and isinstance(item.get("value"), str):
                _add_text_value(
                    result,
                    root,
                    item.get("value"),
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
        else:
            _add_text_value(
                result,
                f"{path}.{index}",
                item,
                array_id_strategy=array_id_strategy,
                identity_fields=identity_fields,
            )


def _walk_visible_text(
    value: Any,
    path: str,
    result: dict[str, str],
    *,
    include_protected_direct: bool = False,
    array_id_strategy: str = "stable_id",
    identity_fields: dict[str, int] | None = None,
) -> None:
    identity_fields = identity_fields if identity_fields is not None else {}
    if isinstance(value, Mapping):
        visibility = str(value.get("visibility") or "public").strip().lower()
        if visibility in NON_PLAYER_VISIBILITY:
            # Actor field/candidate labels are still rendered to the owning
            # player, DM or reviewer. Freeze their direct human-facing text,
            # but do not recurse into protected story details.
            if include_protected_direct:
                root = str(value.get("text_id") or path).strip()
                for key, item in value.items():
                    if key in TEXT_FIELDS and isinstance(item, str):
                        _add_text_value(
                            result,
                            f"{root}.{key}",
                            item,
                            array_id_strategy=array_id_strategy,
                            identity_fields=identity_fields,
                        )
            return
        root = str(value.get("text_id") or path).strip()
        for key, item in value.items():
            key_text = str(key)
            if key_text == "text_id":
                continue
            child = f"{root}.{key_text}" if root else key_text
            if key_text in TEXT_FIELDS:
                _add_text_value(
                    result,
                    child,
                    item,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
            elif isinstance(item, (Mapping, list, tuple)):
                _walk_visible_text(
                    item,
                    child,
                    result,
                    include_protected_direct=include_protected_direct,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
        return
    seen: set[str] = set()
    for index, item in enumerate(_sequence(value)):
        if isinstance(item, Mapping):
            if _contains_visible_text(
                item,
                include_protected_direct=include_protected_direct,
            ):
                segment = _identity(
                    item,
                    index,
                    strategy=array_id_strategy,
                    path=f"{path}[{index}]",
                    identity_fields=identity_fields,
                )
                if array_id_strategy == "stable_id":
                    if segment in seen:
                        raise ValueError(
                            f"{path} 在 stable_id 策略下存在重复标识：{segment}"
                        )
                    seen.add(segment)
            else:
                segment = str(index)
        else:
            segment = str(index)
        _walk_visible_text(
            item,
            f"{path}.{segment}",
            result,
            include_protected_direct=include_protected_direct,
            array_id_strategy=array_id_strategy,
            identity_fields=identity_fields,
        )


def _collection_value(value: Any, path: str) -> Any:
    if path == "$":
        return value
    if not path.startswith("$."):
        raise ValueError(f"文本集路径必须从 $ 开始：{path}")
    current = value
    for segment in path[2:].split("."):
        if not segment or not isinstance(current, Mapping) or segment not in current:
            raise ValueError(f"文本集路径不存在：{path}")
        current = current[segment]
    return current


def _collect_text_catalog(
    world: Mapping[str, Any],
    module_text_collections: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    result: dict[str, str] = {}
    identity_reports: list[dict[str, Any]] = []
    for key in ("name", "summary", "description"):
        value = world.get(key)
        if isinstance(value, str) and value.strip():
            result[f"world.{key}"] = value.strip()

    rules = _mapping(world.get("rules"))
    declared = module_text_collections
    if declared is None:
        declared = {
            str(module_id): (
                {
                    "path": "$",
                    "audience": "player",
                    "array_id_strategy": "stable_id",
                },
            )
            for module_id in rules
            if str(module_id) not in {"protocol", "internal_world_model_revision"}
        }
    for module_id, collections in declared.items():
        module_key = str(module_id)
        value = rules.get(module_key)
        if not isinstance(value, (Mapping, list, tuple)):
            continue
        for collection in collections:
            if not isinstance(collection, Mapping):
                continue
            if str(collection.get("audience") or "player") != "player":
                continue
            collection_path = str(collection.get("path") or "$")
            array_id_strategy = str(
                collection.get("array_id_strategy") or "stable_id"
            ).strip()
            if array_id_strategy not in {"stable_id", "index"}:
                raise ValueError(
                    f"{module_key}:{collection_path} 的 array_id_strategy 无效："
                    f"{array_id_strategy}"
                )
            selected = _collection_value(value, collection_path)
            suffix = collection_path[2:] if collection_path != "$" else ""
            root = f"{module_key}.{suffix}".rstrip(".")
            identity_fields: dict[str, int] = {}
            if module_key == "actor" and collection_path == "$":
                actor = _mapping(selected)
                _walk_visible_text(
                    actor.get("fields") or [],
                    "actor.fields",
                    result,
                    include_protected_direct=True,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
                _walk_visible_text(
                    actor.get("preset_sets") or {},
                    "preset_sets",
                    result,
                    include_protected_direct=True,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
                for key, nested in actor.items():
                    if key in {"fields", "preset_sets", "content_audit"}:
                        continue
                    if isinstance(nested, (Mapping, list, tuple)):
                        _walk_visible_text(
                            nested,
                            f"actor.{key}",
                            result,
                            include_protected_direct=True,
                            array_id_strategy=array_id_strategy,
                            identity_fields=identity_fields,
                        )
            else:
                _walk_visible_text(
                    selected,
                    root,
                    result,
                    array_id_strategy=array_id_strategy,
                    identity_fields=identity_fields,
                )
            identity_reports.append(
                {
                    "module_id": module_key,
                    "path": collection_path,
                    "array_id_strategy": array_id_strategy,
                    "identity_fields": dict(sorted(identity_fields.items())),
                }
            )
    return dict(sorted(result.items())), identity_reports


def collect_text_catalog(
    world: Mapping[str, Any],
    module_text_collections: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, str]:
    """Collect public text plus protected actor labels and descriptions."""

    return _collect_text_catalog(world, module_text_collections)[0]


def _catalog_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compile_localization(
    world: Mapping[str, Any],
    *,
    bundle_loader: Callable[[str], Mapping[str, Any]],
    module_text_collections: Mapping[
        str, Sequence[Mapping[str, Any]]
    ] | None = None,
) -> dict[str, Any]:
    """Resolve localization bundles and produce a deterministic frozen catalog."""

    inline, identity_reports = _collect_text_catalog(
        world,
        module_text_collections,
    )
    required_keys = tuple(inline)
    rules = _mapping(world.get("rules"))
    config = _mapping(rules.get("localization"))
    default_locale = str(config.get("default_locale") or "zh-CN").strip() or "zh-CN"
    fallback_locale = str(config.get("fallback_locale") or default_locale).strip() or default_locale
    declared = _mapping(config.get("bundles"))
    glossary = _mapping(config.get("glossary"))

    resources: dict[str, Any] = {
        "default_locale": default_locale,
        "fallback_locale": fallback_locale,
        "bundles": {},
        "glossary_term_count": len(glossary),
        "array_identity_reports": identity_reports,
    }
    direct_catalogs: dict[str, dict[str, str]] = {}
    invalid_by_locale: dict[str, list[str]] = {}
    empty_by_locale: dict[str, list[str]] = {}

    for locale, raw_path in sorted(declared.items()):
        locale_key = str(locale).strip()
        bundle_path = str(raw_path).strip()
        if not locale_key or not bundle_path:
            raise ValueError("localization.bundles 的语言和路径不能为空")
        raw_bundle = bundle_loader(bundle_path)
        if not isinstance(raw_bundle, Mapping):
            raise ValueError(f"语言包 {bundle_path} 的根节点必须是对象")
        valid: dict[str, str] = {}
        invalid: list[str] = []
        empty: list[str] = []
        for key, value in raw_bundle.items():
            text_key = str(key).strip()
            if not text_key or not isinstance(value, str):
                invalid.append(text_key or "<empty-key>")
                continue
            if not value.strip():
                empty.append(text_key)
                continue
            valid[text_key] = value
        direct_catalogs[locale_key] = dict(sorted(valid.items()))
        invalid_by_locale[locale_key] = sorted(invalid)
        empty_by_locale[locale_key] = sorted(empty)
        resources["bundles"][locale_key] = {
            "path": bundle_path,
            "declared": True,
            "loaded": True,
            "key_count": len(valid),
            "sha256": _catalog_hash(valid),
        }

    if default_locale not in direct_catalogs:
        raise ValueError(f"默认语言包 {default_locale} 未声明")

    reports: dict[str, dict[str, Any]] = {}
    resolved: dict[str, dict[str, str]] = {}
    known = set(required_keys)
    fallback_catalog = direct_catalogs.get(fallback_locale) or direct_catalogs[default_locale]

    for locale in sorted(direct_catalogs):
        direct = direct_catalogs[locale]
        direct_keys = set(direct)
        uncovered = sorted(known - direct_keys)
        unknown = sorted(direct_keys - known)
        fallback_hits = sorted(key for key in uncovered if key in fallback_catalog)
        merged = dict(fallback_catalog)
        merged.update(direct)
        resolved[locale] = dict(sorted(merged.items()))
        source_mismatches = sorted(
            key
            for key in known & direct_keys
            if locale == default_locale and direct[key] != inline[key]
        )
        covered = len(known & direct_keys)
        reports[locale] = {
            "locale": locale,
            "bundle_declared": bool(resources["bundles"][locale]["declared"]),
            "bundle_path": resources["bundles"][locale]["path"],
            "bundle_loaded": True,
            "bundle_key_count": len(direct),
            "required_key_count": len(required_keys),
            "covered": covered,
            "uncovered": len(uncovered),
            "coverage_percent": round((covered / len(required_keys) * 100), 2)
            if required_keys
            else 100.0,
            "uncovered_keys": uncovered,
            "unknown_bundle_keys": unknown,
            "invalid_values": invalid_by_locale.get(locale, []),
            "empty_values": empty_by_locale.get(locale, []),
            "fallback_hits": fallback_hits,
            "source_mismatches": source_mismatches,
            "array_identity_reports": identity_reports,
            "problems": [],
        }

    default_report = reports[default_locale]
    default_problems: list[dict[str, Any]] = []
    for code, values, message in (
        ("localization.missing", default_report["uncovered_keys"], "默认语言缺少必需文本"),
        ("localization.invalid", default_report["invalid_values"], "默认语言存在非字符串文本"),
        ("localization.empty", default_report["empty_values"], "默认语言存在空文本"),
        (
            "localization.source_mismatch",
            default_report["source_mismatches"],
            "默认语言包与世界定义内联文本不一致",
        ),
    ):
        if values:
            default_problems.append(
                {
                    "code": code,
                    "message": message,
                    "count": len(values),
                    "sample": list(values[:20]),
                }
            )
    default_report["problems"] = default_problems
    if default_problems:
        summary = "；".join(
            f"{item['message']} {item['count']} 项" for item in default_problems
        )
        raise ValueError(summary)

    return {
        "resources": resources,
        "required_text_keys": list(required_keys),
        "inline_default_catalog": inline,
        "resolved_text_catalog": resolved,
        "reports": reports,
        "catalog_hash": _catalog_hash(resolved),
    }


__all__ = [
    "TEXT_FIELDS",
    "collect_text_catalog",
    "compile_localization",
]
