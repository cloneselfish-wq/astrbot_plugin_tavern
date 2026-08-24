"""Read-only localization health projection for compiled TWP worlds."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def localization_report(
    world: Mapping[str, Any],
    requested_locale: str | None = None,
) -> dict[str, Any]:
    """Return the frozen bundle report embedded by the current compiler."""

    rules = _mapping(world.get("rules"))
    localization = _mapping(rules.get("localization"))
    resources = _mapping(
        world.get("localization_metadata") or localization.get("resources")
    )
    reports = _mapping(
        world.get("localization_reports") or localization.get("reports")
    )
    default_locale = str(
        resources.get("default_locale")
        or localization.get("default_locale")
        or "zh-CN"
    )
    fallback_locale = str(
        resources.get("fallback_locale")
        or localization.get("fallback_locale")
        or default_locale
    )
    locale = str(requested_locale or default_locale).strip() or default_locale
    selected_locale = locale if locale in reports else fallback_locale
    if selected_locale not in reports:
        selected_locale = default_locale
    report = deepcopy(_mapping(reports.get(selected_locale)))

    required_keys = list(world.get("required_text_keys") or [])
    if not report:
        report = {
            "locale": selected_locale,
            "bundle_declared": False,
            "bundle_path": "",
            "bundle_loaded": False,
            "bundle_key_count": 0,
            "required_key_count": len(required_keys),
            "covered": 0,
            "uncovered": len(required_keys),
            "coverage_percent": 0.0 if required_keys else 100.0,
            "uncovered_keys": required_keys,
            "unknown_bundle_keys": [],
            "invalid_values": [],
            "empty_values": [],
            "fallback_hits": [],
            "source_mismatches": [],
            "problems": [
                {
                    "code": "localization.not_compiled",
                    "message": "当前世界没有编译后的本地化目录",
                    "count": len(required_keys),
                    "sample": required_keys[:20],
                }
            ],
        }

    bundles = _mapping(resources.get("bundles"))
    selected_resource = _mapping(bundles.get(selected_locale))
    report.update(
        {
            "requested_locale": locale,
            "selected_locale": selected_locale,
            "default_locale": default_locale,
            "fallback_locale": fallback_locale,
            "bundle_declared": bool(
                report.get("bundle_declared")
                or selected_resource.get("declared")
            ),
            "bundle_loaded": bool(
                report.get("bundle_loaded")
                or selected_resource.get("loaded")
            ),
            "bundle_path": str(
                report.get("bundle_path")
                or selected_resource.get("path")
                or ""
            ),
            "glossary_term_count": int(
                resources.get("glossary_term_count") or 0
            ),
            "used_locale_fallback": locale != selected_locale,
            "sample_keys": required_keys[:40],
            "uncovered_sample": list(report.get("uncovered_keys") or [])[:20],
        }
    )
    report["total_keys"] = int(
        report.get("required_key_count") or len(required_keys)
    )
    return report


__all__ = ["localization_report"]
