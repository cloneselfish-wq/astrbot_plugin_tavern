"""Generic TWP runtime initialization and narrative projection."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def initialize_runtime(
    world: Mapping[str, Any],
    initial_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Initialize enabled modules from their compiled runtime declarations."""

    from ..protocol.runtime import (
        hydrate_runtime,
        is_twp_runtime,
        runtime_contract_from_world,
    )

    state = deepcopy(_mapping(initial_state))
    existing = state.get("runtime")
    if existing is not None:
        if not is_twp_runtime(existing):
            raise ValueError("C6 不接受旧版或扁平运行态")
        state["runtime"] = deepcopy(dict(existing))
        return state

    rules = _mapping(world.get("rules"))
    contract = runtime_contract_from_world(world)
    declarations = {
        str(item.get("module_id") or item.get("id") or ""): dict(item)
        for item in world.get("twp_modules") or []
        if isinstance(item, Mapping)
    }
    enabled = sorted(
        module_id
        for module_id, declaration in declarations.items()
        if module_id and bool(declaration.get("enabled", True))
    )
    module_states: dict[str, dict[str, Any]] = {}
    for module_id in enabled:
        definition = _mapping(rules.get(module_id))
        initial = definition.get("runtime_initial_state")
        module_states[module_id] = (
            deepcopy(dict(initial)) if isinstance(initial, Mapping) else {}
        )

    runtime = hydrate_runtime(
        {
            "package_id": str(world.get("package_id") or ""),
            "content_version": str(
                world.get("world_content_version")
                or world.get("content_version")
                or ""
            ),
            "module_states": module_states,
        },
        artifact_id=str(world.get("artifact_id") or ""),
        enabled_modules=enabled,
        module_contract=contract,
    )
    state["runtime"] = runtime
    return state


def runtime_projection(
    world: Mapping[str, Any],
    world_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the module-owned narrative projection without flattening state."""

    from ..protocol.projections import project_runtime

    return project_runtime(
        world,
        world_state,
        viewer_role="dm",
        purpose="narrative",
    )


__all__ = ["initialize_runtime", "runtime_projection"]
