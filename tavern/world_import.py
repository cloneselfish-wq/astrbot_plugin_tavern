"""World-package import payload helpers."""

from __future__ import annotations

from typing import Any


def world_import_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the database payload without dropping world-contract fields."""
    rules = payload.get("rules")
    initial_state = payload.get("initial_state")
    return {
        "slug": payload["slug"],
        "name": payload["name"],
        "description": payload.get("description", ""),
        "system_prompt": payload["system_prompt"],
        "opening_scene": payload.get("opening_scene", ""),
        "rules": rules if isinstance(rules, dict) else {},
        "initial_state": (
            initial_state if isinstance(initial_state, dict) else {}
        ),
        "world_schema_version": payload.get("world_schema_version", 0),
        "capabilities": payload.get("capabilities", {}),
        "minimum_plugin_version": payload.get(
            "minimum_plugin_version", ""
        ),
    }
