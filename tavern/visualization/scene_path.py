"""ScenePath projection from frozen scene definitions and safe runtime state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import display_label, mapping, sequence, text, visible
from .keys import OpaqueKeyFactory


def _scene_definitions(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rules = mapping(world.get("rules"))
    block = mapping(rules.get("scene_graph"))
    raw_nodes = block.get("nodes")
    if isinstance(raw_nodes, Mapping):
        return {
            str(ref): {**mapping(raw), "id": str(ref)}
            for ref, raw in raw_nodes.items()
            if isinstance(raw, Mapping)
        }
    result: dict[str, dict[str, Any]] = {}
    for raw in sequence(raw_nodes):
        if not isinstance(raw, Mapping):
            continue
        ref = text(raw.get("id"), limit=160)
        if ref:
            result[ref] = dict(raw)
    return result


def _runtime_module(runtime: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    modules = mapping(runtime.get("modules"))
    block = mapping(modules.get(module_id))
    state = block.get("state")
    return mapping(state) if isinstance(state, Mapping) else block


def project_scene_path(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any] | None,
    *,
    keys: OpaqueKeyFactory,
    privileged: bool,
    limit: int = 7,
) -> dict[str, Any]:
    runtime = mapping(runtime)
    scene_runtime = _runtime_module(runtime, "scene_graph")
    definitions = _scene_definitions(world)

    def allowed(ref: str) -> bool:
        definition = definitions.get(ref)
        return bool(
            definition
            and visible(
                definition.get("visibility"),
                privileged=privileged,
            )
            and display_label(
                definition.get("label") or definition.get("name")
            )
        )

    current_ref = text(
        scene_runtime.get("current_scene")
        or runtime.get("current_scene"),
        limit=160,
    )
    history = [
        text(item, limit=160)
        for item in (
            scene_runtime.get("scene_history")
            or runtime.get("scene_history")
            or ()
        )
        if text(item, limit=160)
    ]
    ordered_refs: list[str] = []
    for ref in history:
        if allowed(ref) and ref not in ordered_refs:
            ordered_refs.append(ref)
    if allowed(current_ref) and current_ref not in ordered_refs:
        ordered_refs.append(current_ref)

    next_refs: list[str] = []
    current_definition = definitions.get(current_ref, {})
    for transition in sequence(current_definition.get("recommended_transitions")):
        if not isinstance(transition, Mapping):
            continue
        target = text(
            transition.get("scene_ref") or transition.get("target"),
            limit=160,
        )
        if allowed(target) and target not in ordered_refs and target not in next_refs:
            next_refs.append(target)
    safe_refs = ordered_refs + next_refs
    safe_total = len(safe_refs)
    limit = max(1, min(20, int(limit)))
    visible_refs = safe_refs[-limit:] if len(ordered_refs) > limit else safe_refs[:limit]
    visible_ref_set = set(visible_refs)
    nodes: list[dict[str, Any]] = []
    key_by_ref: dict[str, str] = {}
    for ref in visible_refs:
        definition = definitions[ref]
        key = keys.key("scene", ref)
        key_by_ref[ref] = key
        state = (
            "current"
            if ref == current_ref
            else "visible_next"
            if ref in next_refs
            else "past"
        )
        nodes.append(
            {
                "key": key,
                "label": display_label(
                    definition.get("label") or definition.get("name")
                ),
                "state": state,
                "time_label": text(definition.get("time_label"), limit=60),
            }
        )
    edges: list[dict[str, Any]] = []
    walked = [ref for ref in ordered_refs if ref in visible_ref_set]
    for source, target in zip(walked, walked[1:]):
        edges.append(
            {
                "source": key_by_ref[source],
                "target": key_by_ref[target],
                "kind": "traversed",
                "label": "已走路径",
            }
        )
    if current_ref in key_by_ref:
        for target in next_refs:
            if target not in key_by_ref:
                continue
            edges.append(
                {
                    "source": key_by_ref[current_ref],
                    "target": key_by_ref[target],
                    "kind": "visible_connection",
                    "label": "可见连接",
                }
            )
    return {
        "current_key": key_by_ref.get(current_ref, ""),
        "nodes": nodes,
        "edges": edges,
        "truncated": safe_total > len(nodes),
        "total_nodes": safe_total,
        "problems": [],
    }


__all__ = ["project_scene_path"]
