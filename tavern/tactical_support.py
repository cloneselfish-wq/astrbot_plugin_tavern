"""Shared pure helpers for tactical references, zones, and state rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

ACTION_KINDS = frozenset(
    {"strike", "guard", "maneuver", "cast", "interact", "aid", "retreat", "parley"}
)

def _clone(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False))

def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []

def _ref(raw: Mapping[str, Any], *names: str) -> str:
    return next((str(raw.get(name) or "").strip() for name in names if raw.get(name)), "")

def _state_refs(state: Mapping[str, Any]) -> dict[str, set[str]]:
    participants = {str(key) for key in (state.get("participants") or {})}
    zones = {
        _ref(dict(item), "zone_id", "zone_ref", "id", "ref")
        for item in _sequence(state.get("zones"))
        if isinstance(item, Mapping)
    }
    objectives = {
        _ref(dict(item), "id", "objective_id", "objective_ref", "ref")
        for item in _sequence(state.get("objectives"))
        if isinstance(item, Mapping)
    }
    threats = {
        _ref(dict(item), "threat_id", "id", "ref")
        for item in _sequence(state.get("threats") or state.get("known_threats"))
        if isinstance(item, Mapping)
    }
    capabilities = {
        _ref(dict(item), "id", "capability_id", "ref")
        for item in _sequence(state.get("available_capabilities"))
        if isinstance(item, Mapping)
    }
    items = {
        _ref(dict(item), "id", "item_id", "ref")
        for item in _sequence(state.get("available_items"))
        if isinstance(item, Mapping)
    }
    return {
        "participants": {value for value in participants if value},
        "zones": {value for value in zones if value},
        "objectives": {value for value in objectives if value},
        "threats": {value for value in threats if value},
        "capabilities": {value for value in capabilities if value},
        "items": {value for value in items if value},
    }

def _zone_graph(state: Mapping[str, Any]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {ref: set() for ref in _state_refs(state)["zones"]}
    for raw in _sequence(state.get("zone_edges")):
        if not isinstance(raw, Mapping):
            continue
        source = str(raw.get("from") or raw.get("from_ref") or "").strip()
        target = str(raw.get("to") or raw.get("to_ref") or "").strip()
        if source in graph and target in graph:
            graph[source].add(target)
            if bool(raw.get("bidirectional", True)):
                graph[target].add(source)
    return graph

def _zone_reachable(state: Mapping[str, Any], source: str, target: str) -> bool:
    if source == target:
        return True
    graph = _zone_graph(state)
    if not state.get("zone_edges"):
        return target in graph
    pending = [source]
    visited = {source}
    while pending:
        current = pending.pop(0)
        for candidate in graph.get(current, set()):
            if candidate == target:
                return True
            if candidate not in visited:
                visited.add(candidate)
                pending.append(candidate)
    return False

def _default_zone(state: Mapping[str, Any], actor: Mapping[str, Any], action: str) -> str:
    current = str(actor.get("zone_ref") or "")
    routes = [dict(item) for item in _sequence(state.get("escape_routes")) if isinstance(item, Mapping)]
    if action == "retreat":
        route = next(
            (
                _ref(item, "zone_ref", "zone_id", "id", "ref")
                for item in routes
                if _ref(item, "zone_ref", "zone_id", "id", "ref") != current
                and _zone_reachable(state, current, _ref(item, "zone_ref", "zone_id", "id", "ref"))
            ),
            "",
        )
        if route:
            return route
    graph = _zone_graph(state)
    return next(iter(sorted(graph.get(current, set()))), current)

def _zone_label(state: Mapping[str, Any], ref: str) -> str:
    for item in _sequence(state.get("zones")):
        if isinstance(item, Mapping) and _ref(dict(item), "zone_id", "zone_ref", "id", "ref") == ref:
            return str(item.get("label") or item.get("name") or "区域").strip()[:120]
    return "当前区域" if ref else "未指定区域"

def _choice(state: Mapping[str, Any], ref: str) -> tuple[str, dict[str, Any]]:
    for kind, source in (
        ("capability", state.get("available_capabilities")),
        ("item", state.get("available_items")),
    ):
        for item in _sequence(source):
            if isinstance(item, Mapping) and _ref(dict(item), "id", "capability_id", "item_id", "ref") == ref:
                return kind, dict(item)
    return "", {}

def _objective_rows(current: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in _sequence(current.get("objectives")) if isinstance(item, Mapping)]
    current["objectives"] = rows
    return rows

def _threat_rows(current: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [dict(item) for item in _sequence(current.get("threats")) if isinstance(item, Mapping)]
    current["threats"] = rows
    current["known_threats"] = rows
    return rows

def _find(rows: list[dict[str, Any]], ref: str, *keys: str) -> dict[str, Any] | None:
    return next((item for item in rows if _ref(item, *keys) == ref), None)

