from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


WORLD_SCHEMA_VERSION = 2
STAT_MODES = {"none", "manual", "preset"}
RESOLUTION_MODES = {"none", "narrative", "dice_only", "attribute"}

DEFAULT_DANGER_LEVELS = (
    {"id": "safe", "label": "安全"},
    {"id": "controlled", "label": "可控"},
    {"id": "dangerous", "label": "危险"},
    {"id": "desperate", "label": "绝境"},
    {
        "id": "lethal",
        "label": "致命",
        "requires_visible_consequence": True,
    },
)


def stats_mode(stats: Mapping[str, Any] | None) -> str:
    stats = stats if isinstance(stats, Mapping) else {}
    mode = str(stats.get("mode") or "").strip().lower()
    if mode in STAT_MODES:
        return mode
    if (
        stats.get("input_mode")
        == "automatic_profession_base_plus_two_fixed_bonus_choices"
        or stats.get("allocation_mode")
        == "profession_base_plus_primary7_secondary3"
    ):
        return "preset"
    return "manual"


def world_contract(world: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the runtime contract for the current world protocol."""

    source = world if isinstance(world, Mapping) else {}
    rules = source.get("rules")
    rules = rules if isinstance(rules, Mapping) else source
    card = rules.get("character_card")
    card = card if isinstance(card, Mapping) else {}
    stats = card.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    try:
        version = int(
            source.get(
                "world_schema_version",
                rules.get("world_schema_version", 0),
            )
        )
    except (TypeError, ValueError):
        version = 0

    resolution = rules.get("resolution")
    if isinstance(resolution, Mapping):
        resolution_raw = dict(resolution)
        resolution_mode = str(
            resolution_raw.get("mode") or "none"
        ).lower()
    else:
        resolution_raw = {"dice_system": str(resolution or "d20")}
        resolution_mode = (
            "attribute" if stats_mode(stats) != "none" else "dice_only"
        )
    if resolution_mode not in RESOLUTION_MODES:
        resolution_mode = "none"

    attributes = [
        dict(item)
        for item in stats.get("attributes", [])
        if isinstance(item, Mapping) and item.get("key")
    ]
    allowed = resolution_raw.get("allowed_attributes")
    if not isinstance(allowed, Sequence) or isinstance(
        allowed, (str, bytes)
    ):
        allowed = [str(item["key"]) for item in attributes]
    danger_levels = rules.get("danger_levels")
    if not isinstance(danger_levels, Sequence) or isinstance(
        danger_levels, (str, bytes)
    ):
        danger_levels = DEFAULT_DANGER_LEVELS
    presentation = rules.get("option_presentation")
    presentation = (
        presentation if isinstance(presentation, Mapping) else {}
    )
    annotation = presentation.get("annotation")
    annotation = annotation if isinstance(annotation, Mapping) else {}
    return {
        "version": version,
        "stats": {**dict(stats), "mode": stats_mode(stats)},
        "attributes": attributes,
        "resolution": {
            "mode": resolution_mode,
            "dice_system": str(
                resolution_raw.get("dice_system") or "d20"
            ),
            "unknown_attribute": str(
                resolution_raw.get("unknown_attribute") or "reject"
            ),
            "generic_check": dict(
                resolution_raw.get("generic_check") or {}
            ),
            "allowed_attributes": [str(item) for item in allowed],
        },
        "danger_levels": [
            dict(item)
            for item in danger_levels
            if isinstance(item, Mapping)
        ],
        "annotation": {
            "enabled": bool(annotation.get("enabled", True)),
            "show_danger": bool(annotation.get("show_danger", True)),
            "show_check_attribute": bool(
                annotation.get("show_check_attribute", True)
            ),
            "show_failure_consequence": bool(
                annotation.get("show_failure_consequence", False)
            ),
        },
        "capabilities": dict(
            source.get("capabilities")
            or rules.get("capabilities")
            or {}
        ),
    }


def validate_world_contract(world: Mapping[str, Any]) -> dict[str, Any]:
    contract = world_contract(world)
    if contract["version"] != WORLD_SCHEMA_VERSION:
        raise ValueError(
            f"v0.9.0 仅接受世界包协议 v{WORLD_SCHEMA_VERSION}；"
            f"当前为 v{contract['version']}，不再执行旧协议自动转换"
        )
    stats = contract["stats"]
    mode = stats["mode"]
    resolution_mode = contract["resolution"]["mode"]
    if mode == "none" and resolution_mode == "attribute":
        raise ValueError("无数值世界不能启用 attribute 属性检定")
    if mode == "none" and contract["attributes"]:
        raise ValueError("stats.mode=none 时不得声明角色属性")
    capabilities = contract["capabilities"]
    expected = {
        "character_stats": mode != "none",
        "attribute_checks": resolution_mode == "attribute",
        "dice_resolution": resolution_mode in {"dice_only", "attribute"},
    }
    for key, value in expected.items():
        if key in capabilities and bool(capabilities[key]) != value:
            raise ValueError(f"capabilities.{key} 与实际规则冲突")
    return contract


def attribute_lookup(
    contract: Mapping[str, Any],
    reference: str,
) -> tuple[str, str] | None:
    value = str(reference or "").strip().casefold()
    for item in contract.get("attributes", []):
        key = str(item.get("key") or "")
        label = str(item.get("label") or key)
        if value in {key.casefold(), label.casefold(), f"{label}检定".casefold()}:
            return key, label
    return None
