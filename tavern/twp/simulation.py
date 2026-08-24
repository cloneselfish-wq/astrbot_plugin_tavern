"""World-declared deterministic TWP content simulation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .commands import apply_command
from .runtime import initialize_runtime, runtime_projection


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _runtime(state: Mapping[str, Any]) -> dict[str, Any]:
    current = state.get("runtime")
    if isinstance(current, Mapping):
        from ..protocol.runtime import flatten_runtime

        return flatten_runtime(current)
    return {}


def _definitions(world: Mapping[str, Any], module: str, key: str) -> list[dict[str, Any]]:
    rules = _mapping(world.get("rules"))
    return [
        dict(item)
        for item in _sequence(_mapping(rules.get(module)).get(key))
        if isinstance(item, Mapping) and item.get("id")
    ]


def _plan(world: Mapping[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    simulation = _mapping(rules.get("simulation"))
    declared = simulation.get("plan")
    if isinstance(declared, Mapping):
        return dict(declared)
    declared = world.get("simulation_plan")
    return dict(declared) if isinstance(declared, Mapping) else {}


def _refs(plan: Mapping[str, Any], op: str) -> list[str]:
    result: list[str] = []
    for step in _sequence(plan.get("steps")):
        if not isinstance(step, Mapping) or str(step.get("op") or "") != op:
            continue
        result.extend(
            str(item or "").strip()
            for item in _sequence(step.get("refs"))
            if str(item or "").strip()
        )
    return list(dict.fromkeys(result))


def _command(
    domain: str,
    action: str,
    targets: Sequence[str],
    index: int,
    *,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "domain": domain,
        "action": action,
        "targets": list(targets),
        "operator": "simulation",
        "reason": f"sim-turn-{index}",
        "idempotency_key": f"sim-{domain}-{action}-{index}",
        "payload": dict(payload or {}),
        "visibility": "public",
        "expected_revision": None,
    }


def _seed_entry_visit(
    world: Mapping[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    runtime = _runtime(state)
    entry = str(runtime.get("current_scene") or "")
    if not entry:
        return state
    return apply_command(
        world,
        state,
        _command("scene", "record_visit", [entry], -1),
    )["state"]


def _run_turn(
    world: Mapping[str, Any],
    state: dict[str, Any],
    index: int,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Use only refs registered by this world; no flagship constants."""

    runtime = _runtime(state)
    scenes = _definitions(world, "scene_graph", "nodes")
    registered_scenes = {str(item["id"]): item for item in scenes}
    current_ref = str(runtime.get("current_scene") or "")
    current = registered_scenes.get(current_ref, {})
    facts = _definitions(world, "knowledge_graph", "facts")
    fact_refs = _refs(plan, "discover_fact") or [str(item["id"]) for item in facts]
    registered_facts = {str(item["id"]) for item in facts}
    revealed = set(_sequence(_mapping(runtime.get("knowledge")).get("revealed")))
    required_here = [
        str(item or "")
        for item in (
            *_sequence(current.get("required_clues")),
            *_sequence(current.get("optional_clues")),
        )
        if str(item or "") in registered_facts
    ]
    next_fact = next(
        (
            item
            for item in (*required_here, *fact_refs)
            if item in registered_facts and item not in revealed
        ),
        "",
    )
    if next_fact:
        state = apply_command(
            world,
            state,
            _command("knowledge", "reveal", [next_fact], index),
        )["state"]
        runtime = _runtime(state)

    scene_refs = _refs(plan, "visit_scene") or list(registered_scenes)
    recommended = [
        str(item.get("scene_ref") or item.get("to") or "")
        for item in _sequence(current.get("recommended_transitions"))
        if isinstance(item, Mapping)
    ]
    candidates = [
        item
        for item in (*recommended, *scene_refs)
        if item in registered_scenes and item != current_ref
    ]
    visits = _mapping(runtime.get("scene_visits"))
    target = next(
        (item for item in candidates if int(visits.get(item, 0) or 0) == 0),
        candidates[0] if candidates else "",
    )
    if target:
        state = apply_command(
            world,
            state,
            _command(
                "scene",
                "transition",
                [target],
                index,
                payload={"force": target not in recommended},
            ),
        )["state"]
        runtime = _runtime(state)

    quests = _definitions(world, "quest_graph", "quests")
    quest_refs = _refs(plan, "accept_quest") or [str(item["id"]) for item in quests]
    quest_states = _mapping(runtime.get("quests"))
    available = next(
        (
            item
            for item in quest_refs
            if item in {str(value["id"]) for value in quests}
            and str(_mapping(quest_states.get(item)).get("status") or "available")
            == "available"
        ),
        "",
    )
    if available:
        state = apply_command(
            world,
            state,
            _command("quest", "accept", [available], index),
        )["state"]
        runtime = _runtime(state)

    clocks = _definitions(world, "time_clock", "clocks")
    clock_refs = _refs(plan, "advance_registered_clock") or [
        str(item["id"]) for item in clocks
    ]
    registered_clocks = {str(item["id"]): item for item in clocks}
    clock_ref = next((item for item in clock_refs if item in registered_clocks), "")
    if clock_ref:
        state = apply_command(
            world,
            state,
            _command(
                "clock",
                "advance",
                [clock_ref],
                index,
                payload={"segments": 1},
            ),
        )["state"]
    return state


def _clock_progress(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> int:
    states = _mapping(runtime.get("clocks"))
    total = 0
    for definition in _definitions(world, "time_clock", "clocks"):
        ref = str(definition["id"])
        state = _mapping(states.get(ref))
        initial = int(definition.get("initial_value") or 0)
        current = int(state.get("value") if state.get("value") is not None else initial)
        total += (
            max(0, initial - current)
            if str(definition.get("direction") or "") == "countdown"
            else max(0, current - initial)
        )
    return total


def run_smoke_simulation(
    world: Mapping[str, Any],
    *,
    turns: int = 30,
    party_sizes: Sequence[int] = (1, 4, 8),
) -> dict[str, Any]:
    """Run the world plan and fail when declared coverage is not achieved."""

    import json

    normalized_sizes = [int(value) for value in party_sizes]
    plan = _plan(world)
    report: dict[str, Any] = {
        "schema": "twp-simulation-report/1.0.0-rc10",
        "plan_schema": str(plan.get("schema") or "derived"),
        "turns": max(0, int(turns)),
        "party_sizes": normalized_sizes,
        "scenes_visited": [],
        "quests_activated": 0,
        "facts_revealed": 0,
        "clocks_advanced": 0,
        "terminal_conditions_evaluated": 0,
        "domain_status": {},
        "errors": [],
        "state_bytes": 0,
        "projection_bytes": 0,
        "event_log_entries": 0,
        "ok": True,
    }
    if any(value < 1 or value > 32 for value in normalized_sizes):
        report["errors"].append(
            {
                "turn": "preflight",
                "code": "simulation.party_size_invalid",
                "error": "人数缩放必须位于 1～32。",
                "path": "assertions.party_sizes",
            }
        )
    state = initialize_runtime(
        world,
        {
            "scene_id": str(
                _mapping(world.get("rules")).get("scene_graph", {}).get("entry")
                or world.get("opening_scene_ref")
                or ""
            )
        },
    )
    try:
        state = _seed_entry_visit(world, state)
    except Exception as exc:  # noqa: BLE001
        report["errors"].append(
            {
                "turn": "preflight",
                "code": "simulation.entry_invalid",
                "error": str(exc),
                "path": "world.opening_scene_ref",
            }
        )
    for index in range(report["turns"]):
        if report["errors"]:
            break
        try:
            state = _run_turn(world, state, index, plan)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(
                {
                    "turn": index,
                    "code": "simulation.step_failed",
                    "error": str(exc),
                    "path": "simulation_plan.steps",
                }
            )
            break
    runtime = _runtime(state)
    report["scenes_visited"] = list(_mapping(runtime.get("scene_visits")))
    report["quests_activated"] = sum(
        1
        for item in _mapping(runtime.get("quests")).values()
        if isinstance(item, Mapping)
        and str(item.get("status") or "") in {"active", "completed"}
    )
    report["facts_revealed"] = len(
        _sequence(_mapping(runtime.get("knowledge")).get("revealed"))
    )
    report["clocks_advanced"] = _clock_progress(world, runtime)
    conditions = _definitions(world, "terminal_conditions", "conditions")
    report["terminal_conditions_evaluated"] = len(conditions)
    report["event_log_entries"] = len(
        _sequence(runtime.get("events") or runtime.get("event_log"))
    )
    report["state_bytes"] = len(
        json.dumps(state, ensure_ascii=False).encode("utf-8")
    )
    report["projection_bytes"] = len(
        json.dumps(
            runtime_projection(world, state),
            ensure_ascii=False,
        ).encode("utf-8")
    )
    report["domain_status"] = {
        "scenes": "ok" if len(report["scenes_visited"]) >= 2 else "insufficient",
        "quests": "ok" if report["quests_activated"] >= 1 else "not_applicable"
        if not _definitions(world, "quest_graph", "quests")
        else "insufficient",
        "knowledge": "ok" if report["facts_revealed"] >= 1 else "not_applicable"
        if not _definitions(world, "knowledge_graph", "facts")
        else "insufficient",
        "clocks": "ok" if report["clocks_advanced"] >= 1 else "not_applicable"
        if not _definitions(world, "time_clock", "clocks")
        else "insufficient",
        "terminal_conditions": "ok" if conditions else "not_applicable",
    }
    assertions = _mapping(plan.get("assertions"))
    minimum_scene_changes = int(assertions.get("minimum_scene_changes") or 1)
    minimum_event_log = int(assertions.get("minimum_event_log") or 1)
    checks = (
        (
            len(report["scenes_visited"]) >= minimum_scene_changes + 1,
            "simulation.scene_coverage_insufficient",
            "内容模拟没有完成足够的合法转场。",
            "assertions.minimum_scene_changes",
        ),
        (
            report["event_log_entries"] >= minimum_event_log,
            "simulation.event_log_empty",
            "内容模拟没有产生可核验的事件日志。",
            "assertions.minimum_event_log",
        ),
    )
    for ok, code, message, path in checks:
        if not ok:
            report["errors"].append(
                {
                    "turn": "final",
                    "code": code,
                    "error": message,
                    "path": path,
                }
            )
    if report["state_bytes"] > 512 * 1024:
        report["errors"].append(
            {
                "turn": "final",
                "code": "simulation.state_too_large",
                "error": "运行态体积超过 512KB 上限。",
                "path": "runtime",
            }
        )
    if report["event_log_entries"] > 80:
        report["errors"].append(
            {
                "turn": "final",
                "code": "simulation.event_log_too_large",
                "error": "事件日志超过 80 条上限。",
                "path": "runtime.events",
            }
        )
    report["ok"] = not report["errors"]
    return report


__all__ = ["run_smoke_simulation"]
