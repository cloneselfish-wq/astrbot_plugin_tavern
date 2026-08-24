from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .constants import TWP_VERSION, VIEWER_ROLES
from .runtime import runtime_from_state


PRIVATE_KEYS = frozenset(
    {
        "private",
        "private_knowledge",
        "dm_only",
        "dm_notes",
        "secret",
        "secrets",
        "raw_payload",
        "source_path",
        "server_path",
        "audit_payload",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _scrub(value: Any, *, allow_private: bool, allow_diagnostic: bool) -> Any:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if not allow_private and (
                lowered in PRIVATE_KEYS
                or "secret" in lowered
                or lowered.startswith("dm_")
            ):
                continue
            if not allow_diagnostic and lowered in {
                "artifact_hash",
                "source_hash",
                "input_hash",
                "plan_hash",
                "path",
                "file",
                "payload",
            }:
                continue
            result[key_text] = _scrub(
                item,
                allow_private=allow_private,
                allow_diagnostic=allow_diagnostic,
            )
        return result
    if isinstance(value, list):
        return [
            _scrub(
                item,
                allow_private=allow_private,
                allow_diagnostic=allow_diagnostic,
            )
            for item in value
        ]
    return deepcopy(value)


def project_runtime(
    world: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    viewer_role: str,
    purpose: str = "web",
    character_ref: str = "",
) -> dict[str, Any]:
    """Project module-owned runtime state without flattening or guessing fields."""

    role = str(viewer_role or "player").lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知投影视角：{viewer_role}")
    root = runtime_from_state(state)
    allow_private = role in {"character", "dm", "admin", "author"}
    allow_diagnostic = role in {"admin", "author"}
    modules: dict[str, Any] = {}
    for module_id, raw in _mapping(root.get("modules")).items():
        module = _mapping(raw)
        status = str(module.get("status") or "corrupt")
        projected = {
            "status": status,
            "schema": str(module.get("schema") or ""),
            "state": deepcopy(_mapping(module.get("state"))),
        }
        modules[str(module_id)] = projected

    capability_index = _mapping(world.get("capability_index"))
    projection = {
        "schema": f"twp-projection/{TWP_VERSION}",
        "viewer": {
            "role": role,
            "character_ref": character_ref if role == "character" else "",
        },
        "purpose": str(purpose or "web"),
        "artifact": {
            "artifact_id": str(
                root.get("artifact_id") or world.get("artifact_id") or ""
            ),
            "content_version": str(
                root.get("content_version")
                or world.get("world_content_version")
                or world.get("content_version")
                or ""
            ),
        },
        "revision": int(root.get("revision") or 0),
        "event_sequence": int(root.get("event_sequence") or 0),
        "modules": modules,
        "available_modules": [
            module_id
            for module_id, module in modules.items()
            if module.get("status") not in {"disabled", "not_applicable"}
        ],
        "capabilities": [
            {"id": capability, "module_id": str(module_id)}
            for capability, module_id in sorted(capability_index.items())
            if str(module_id) in modules
            and modules[str(module_id)].get("status")
            not in {"disabled", "not_applicable"}
        ],
    }
    return _scrub(
        projection,
        allow_private=allow_private,
        allow_diagnostic=allow_diagnostic,
    )


__all__ = ["project_runtime"]
