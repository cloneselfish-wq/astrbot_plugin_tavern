from .common import *
from .validation import *

def _command_knowledge(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("knowledge_graph"))
    facts = _index_by(module.get("facts", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, facts, "知识事实")
    knowledge = runtime.setdefault("knowledge", {})
    if not isinstance(knowledge, dict):
        knowledge = {}
        runtime["knowledge"] = knowledge
    revealed = knowledge.setdefault("revealed", [])
    if not isinstance(revealed, list):
        revealed = []
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    definition = facts[target]
    if action == "reveal":
        if target not in revealed:
            revealed.append(target)
        events.append({"type": "knowledge.revealed", "fact": target, "visibility": cmd["visibility"]})
        summary = f"已向队伍揭示事实：「{definition.get('text') or target}」"
    elif action == "correct":
        corrected = knowledge.setdefault("corrected", [])
        if not isinstance(corrected, list):
            corrected = []
        if target not in corrected:
            corrected.append(target)
        knowledge["corrected"] = corrected
        events.append({"type": "knowledge.corrected", "fact": target})
        summary = f"事实已纠正：{target}"
    elif action == "discover":
        recipient = _text(cmd["payload"].get("recipient") or cmd["operator"], 128)
        discoveries = knowledge.setdefault("discoveries", [])
        if not isinstance(discoveries, list):
            discoveries = []
        entry = {"fact": target, "recipient": recipient}
        if entry not in discoveries:
            discoveries.append(entry)
        knowledge["discoveries"] = discoveries
        events.append({"type": "knowledge.discovered", "fact": target, "recipient": recipient})
        summary = f"定向获知：{definition.get('text') or target}"
    else:
        raise WorldCommandError(f"不支持的 knowledge 命令：{action}")
    knowledge["revealed"] = revealed[-500:]
    runtime["knowledge"] = knowledge
    changes.append({"domain": "knowledge", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


def _command_npc(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("npc_lifecycle"))
    npcs = _index_by(module.get("npcs", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, npcs, "NPC")
    state = runtime.setdefault("npcs", {})
    if not isinstance(state, dict):
        state = {}
        runtime["npcs"] = state
    npc_state = state.get(target)
    npc_state = dict(npc_state) if isinstance(npc_state, Mapping) else {"status": "active", "scene": ""}
    definition = npcs[target]
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "move":
        scene_ref = _text(cmd["payload"].get("scene") or (cmd["targets"][1] if len(cmd["targets"]) > 1 else ""), 128)
        if not scene_ref:
            raise WorldCommandError("npc.move 需要 payload.scene")
        npc_state["scene"] = scene_ref
        events.append({"type": "npc.moved", "npc": target, "scene": scene_ref})
        summary = f"NPC「{definition.get('name') or target}」移动至 {scene_ref}"
    elif action == "set_intent":
        intent = _text(cmd["payload"].get("intent"), 240)
        npc_state["intent"] = intent
        events.append({"type": "npc.intent_changed", "npc": target, "intent": intent})
        summary = f"NPC「{definition.get('name') or target}」意图更新"
    elif action == "leave":
        npc_state["status"] = "left"
        npc_state["leave_reason"] = cmd["reason"]
        events.append({"type": "npc.left", "npc": target})
        summary = f"NPC「{definition.get('name') or target}」离场"
    elif action == "die":
        npc_state["status"] = "dead"
        npc_state["death_reason"] = cmd["reason"]
        events.append({"type": "npc.died", "npc": target})
        summary = f"NPC「{definition.get('name') or target}」死亡"
    elif action == "set_faction":
        faction_ref = _text(cmd["payload"].get("faction"), 128)
        npc_state["faction"] = faction_ref
        events.append({"type": "npc.faction_changed", "npc": target, "faction": faction_ref})
        summary = f"NPC「{definition.get('name') or target}」阵营变化"
    else:
        raise WorldCommandError(f"不支持的 npc 命令：{action}")
    state[target] = npc_state
    runtime["npcs"] = state
    changes.append({"domain": "npc", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


def _command_clock(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    definitions = _index_by(_mapping(rules.get("time_clock")).get("clocks", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, definitions, "时钟")
    definition = definitions[target]
    clocks = runtime.setdefault("clocks", {})
    if not isinstance(clocks, dict):
        clocks = {}
    current = clocks.get(target)
    clock_state = (
        dict(current)
        if isinstance(current, Mapping)
        else {
            "value": int(definition.get("initial_value") or 0),
            "max": max(1, int(definition.get("segments") or 8)),
            "filled": False,
            "owner_ref": str(definition.get("owner_ref") or ""),
            "scope": str(definition.get("scope") or "world"),
            "visibility": str(definition.get("visibility") or cmd["visibility"]),
        }
    )
    label = str(definition.get("label") or target)
    if action == "read":
        return {
            "changes": [],
            "events": [],
            "summary": f"时钟〈{label}〉当前 {int(clock_state.get('value') or 0)}/{max(1, int(clock_state.get('max') or 1))}",
        }
    if action != "advance":
        raise WorldCommandError(f"不支持的 clock 命令：{action}")
    try:
        segments = int(cmd["payload"].get("segments", 1) or 1)
    except (TypeError, ValueError):
        raise WorldCommandError("clock.advance 需要整数 segments")
    if segments < 1:
        raise WorldCommandError("clock.advance 的 segments 必须大于 0")
    previous = int(clock_state.get("value") or 0)
    maximum = max(1, int(clock_state.get("max") or definition.get("segments") or 8))
    countdown = str(definition.get("direction") or "").lower() == "countdown"
    current_value = (
        max(0, previous - min(segments, 8))
        if countdown
        else min(maximum, previous + min(segments, 8))
    )
    clock_state["value"] = current_value
    events: list[dict[str, Any]] = []
    for threshold in _sequence(definition.get("thresholds")):
        if not isinstance(threshold, Mapping):
            continue
        try:
            threshold_value = int(threshold.get("value"))
        except (TypeError, ValueError):
            continue
        crossed = (
            current_value <= threshold_value < previous
            if countdown
            else previous < threshold_value <= current_value
        )
        if crossed:
            events.append(
                {
                    "type": str(threshold.get("event") or "clock.threshold_reached"),
                    "clock": target,
                    "value": threshold_value,
                }
            )
    completed = current_value <= 0 if countdown else current_value >= maximum
    if completed and not bool(clock_state.get("filled")):
        clock_state["filled"] = True
        events.append(
            {
                "type": str(definition.get("completion_event") or "clock.completed"),
                "clock": target,
                "value": current_value,
            }
        )
    sources = _sequence(clock_state.get("sources")) + [cmd["reason"]]
    clock_state["sources"] = sources[-50:]
    clocks[target] = clock_state
    runtime["clocks"] = clocks
    return {
        "changes": [
            {
                "domain": "clock",
                "action": "advance",
                "target": target,
                "segments": current_value - previous,
            }
        ],
        "events": events,
        "summary": f"时钟〈{label}〉推进 {current_value - previous} 格（{current_value}/{maximum}）",
    }


def _command_faction(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("faction_state"))
    factions = _index_by(module.get("factions", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, factions, "阵营")
    state = runtime.setdefault("factions", {})
    if not isinstance(state, dict):
        state = {}
        runtime["factions"] = state
    faction_state = state.get(target)
    faction_state = dict(faction_state) if isinstance(faction_state, Mapping) else {"stance": "neutral", "resources": {}, "clocks": {}}
    definition = factions[target]
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "set_stance":
        stance = _text(cmd["payload"].get("stance"), 60)
        if not stance:
            raise WorldCommandError("faction.set_stance 需要 payload.stance")
        faction_state["stance"] = stance
        events.append({"type": "faction.stance_changed", "faction": target, "stance": stance})
        summary = f"阵营「{definition.get('label') or target}」立场 → {stance}"
    elif action == "adjust_resource":
        resource = _text(cmd["payload"].get("resource"), 60)
        try:
            delta = int(cmd["payload"].get("delta", 0) or 0)
        except (TypeError, ValueError):
            raise WorldCommandError("faction.adjust_resource 需要整数 delta")
        if not resource:
            raise WorldCommandError("faction.adjust_resource 需要 payload.resource")
        resources = faction_state.setdefault("resources", {})
        if not isinstance(resources, dict):
            resources = {}
        resources[resource] = max(0, int(resources.get(resource, 0) or 0) + delta)
        faction_state["resources"] = resources
        events.append({"type": "faction.resource_changed", "faction": target, "resource": resource, "delta": delta})
        summary = f"阵营「{definition.get('label') or target}」资源 {resource} {delta:+d}"
    elif action == "advance_clock":
        clock = _text(cmd["payload"].get("clock"), 80)
        try:
            segments = int(cmd["payload"].get("segments", 1) or 1)
        except (TypeError, ValueError):
            raise WorldCommandError("faction.advance_clock 需要整数 segments")
        if not clock:
            raise WorldCommandError("faction.advance_clock 需要 payload.clock")
        clock_definitions = _index_by(
            _mapping(rules.get("time_clock")).get("clocks", [])
        )
        definition_key = clock if clock.startswith("clock:") else f"clock:{clock}"
        clock_definition = clock_definitions.get(definition_key, {})
        root_clocks = runtime.setdefault("clocks", {})
        if not isinstance(root_clocks, dict):
            root_clocks = {}
        clocks = faction_state.setdefault("clocks", {})
        if not isinstance(clocks, dict):
            clocks = {}
        clock_state = root_clocks.get(definition_key) or clocks.get(clock)
        clock_state = (
            dict(clock_state)
            if isinstance(clock_state, Mapping)
            else {
                "value": int(clock_definition.get("initial_value") or 0),
                "max": max(1, int(clock_definition.get("segments") or 8)),
                "filled": False,
                "owner_ref": str(clock_definition.get("owner_ref") or target),
                "scope": str(clock_definition.get("scope") or "faction"),
                "visibility": str(clock_definition.get("visibility") or cmd["visibility"]),
            }
        )
        maximum = max(1, int(clock_state.get("max", 8) or 8))
        clock_state["value"] = int(clock_state.get("value", 0) or 0) + max(1, min(8, segments))
        if clock_state["value"] >= maximum:
            if not clock_state.get("filled"):
                events.append({"type": "faction.clock_completed", "faction": target, "clock": clock})
                clock_state["filled"] = True
            clock_state["value"] = maximum
        sources = _sequence(clock_state.get("sources")) + [cmd["reason"]]
        clock_state["sources"] = sources[-50:]
        root_clocks[definition_key] = dict(clock_state)
        runtime["clocks"] = root_clocks
        clocks[clock] = clock_state
        faction_state["clocks"] = clocks
        changes.append({"domain": "faction", "action": "advance_clock", "target": target, "clock": clock, "segments": segments})
        summary = f"阵营「{definition.get('label') or target}」时钟 {clock} 推进 {segments} 格"
    elif action == "set_control":
        region = _text(cmd["payload"].get("region"), 80)
        controlled = bool(cmd["payload"].get("controlled", True))
        if not region:
            raise WorldCommandError("faction.set_control 需要 payload.region")
        control = faction_state.setdefault("control_regions", [])
        if not isinstance(control, list):
            control = []
        if controlled and region not in control:
            control.append(region)
        elif not controlled and region in control:
            control.remove(region)
        faction_state["control_regions"] = control
        events.append({"type": "faction.control_changed", "faction": target, "region": region, "controlled": controlled})
        summary = f"阵营「{definition.get('label') or target}」地区控制变更：{region}"
    else:
        raise WorldCommandError(f"不支持的 faction 命令：{action}")
    state[target] = faction_state
    runtime["factions"] = state
    changes.append({"domain": "faction", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


__all__ = [name for name in globals() if not name.startswith('__')]

