from .common import *
from .validation import *
from .scenes import *
from .entities import *

def _command_node(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    action = cmd["action"]
    target = cmd["targets"][0]
    nodes = runtime.setdefault("nodes", {})
    if not isinstance(nodes, dict):
        nodes = {}
        runtime["nodes"] = nodes
    node_state = nodes.get(target)
    node_state = dict(node_state) if isinstance(node_state, Mapping) else {}
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "set_status":
        status = _text(cmd["payload"].get("status"), 40)
        if not status:
            raise WorldCommandError("node.set_status 需要 payload.status")
        node_state["status"] = status
        events.append({"type": "node.status_changed", "node": target, "status": status})
        changes.append({"domain": "node", "action": "set_status", "target": target, "status": status})
        summary = f"节点 {target} 状态→ {status}"
    else:
        raise WorldCommandError(f"不支持的 node 命令：{action}")
    nodes[target] = node_state
    runtime["nodes"] = nodes
    return {"changes": changes, "events": events, "summary": summary}


def _command_alliance(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    action = cmd["action"]
    target = cmd["targets"][0]
    alliance = runtime.setdefault("alliance", {})
    if not isinstance(alliance, dict):
        alliance = {}
        runtime["alliance"] = alliance
    signatories = alliance.setdefault("signatories", [])
    if not isinstance(signatories, list):
        signatories = []
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "sign":
        if target not in signatories:
            signatories.append(target)
        events.append({"type": "alliance.signed", "faction": target})
        summary = f"阵营 {target} 签署联盟"
    elif action == "withdraw":
        if target in signatories:
            signatories.remove(target)
        events.append({"type": "alliance.withdrew", "faction": target})
        summary = f"阵营 {target} 退出联盟"
    else:
        raise WorldCommandError(f"不支持的 alliance 命令：{action}")
    alliance["signatories"] = signatories
    runtime["alliance"] = alliance
    changes.append({"domain": "alliance", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


def _command_crown(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    action = cmd["action"]
    crown = runtime.setdefault("crown", {})
    if not isinstance(crown, dict):
        crown = {}
        runtime["crown"] = crown
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    if action == "nominate":
        candidate = _text(cmd["payload"].get("candidate") or cmd["targets"][0], 128)
        crown["candidate"] = candidate
        events.append({"type": "crown.nominated", "candidate": candidate})
        summary = f"加冕候选人确定：{candidate}"
    elif action == "recognize":
        node_ref = _text(cmd["payload"].get("node") or (cmd["targets"][1] if len(cmd["targets"]) > 1 else ""), 128)
        if not node_ref:
            raise WorldCommandError("crown.recognize 需要 payload.node")
        recognized = crown.setdefault("nodes_recognized", [])
        if not isinstance(recognized, list):
            recognized = []
        if node_ref not in recognized:
            recognized.append(node_ref)
        crown["nodes_recognized"] = recognized
        events.append({"type": "crown.node_recognized", "node": node_ref})
        summary = f"节点 {node_ref} 认可加冕"
    elif action == "accept_price":
        crown["price_accepted"] = bool(cmd["payload"].get("accepted", True))
        events.append({"type": "crown.price_accepted", "accepted": crown["price_accepted"]})
        summary = "乘载者已承认个人代价"
    else:
        raise WorldCommandError(f"不支持的 crown 命令：{action}")
    runtime["crown"] = crown
    changes.append({"domain": "crown", "action": action, "target": cmd["targets"][0]})
    return {"changes": changes, "events": events, "summary": summary}


def _command_ending(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    from ..endings import ending_readiness

    action = cmd["action"]
    if action != "commit":
        raise WorldCommandError(f"不支持的 ending 命令：{action}")
    choice = _text(cmd["targets"][0], 60)
    readiness = ending_readiness(runtime, world)
    ending = readiness.get("endings", {}).get(choice)
    if ending is None:
        raise WorldCommandError(f"未知结局：{choice}")
    if not ending.get("met"):
        raise WorldCommandError(
            f"结局「{ending.get('label') or choice}」条件未满足："
            + "；".join(ending.get("missing") or [])
        )
    runtime["ending"] = {
        "choice": choice,
        "label": ending.get("label") or choice,
        "committed_by": cmd["operator"],
        "at": cmd["reason"],
        "readiness": readiness,
    }
    events = [{"type": "ending.committed", "choice": choice, "label": ending.get("label") or choice}]
    changes = [{"domain": "ending", "action": "commit", "target": choice}]
    return {"changes": changes, "events": events, "summary": f"结局已结算：{ending.get('label') or choice}"}


_DISPATCH = {
    "scene": _command_scene,
    "quest": _command_quest,
    "knowledge": _command_knowledge,
    "npc": _command_npc,
    "faction": _command_faction,
    "clock": _command_clock,
    "challenge": _command_challenge,
    "progression": _command_progression,
    "crafting": _command_crafting,
    "handout": _command_handout,
    "node": _command_node,
    "alliance": _command_alliance,
    "crown": _command_crown,
    "ending": _command_ending,
}


def apply_command(
    world: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    command: Mapping[str, Any],
    *,
    root_operation_id: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """在运行时状态上应用一条世界命令。

    world: 编译后的世界快照（rules 为各模块 config）
    state: 会话 world_state_json（TWP runtime）
    返回 {ok, state, summary, changes, events, revision, root_operation_id}
    """
    cmd = validate_command(command)
    working: dict[str, Any] = deepcopy(dict(state or {}))
    twp_runtime = working.get("runtime")
    if isinstance(twp_runtime, Mapping):
        from ...protocol.runtime import flatten_runtime

        runtime = flatten_runtime(twp_runtime)
    else:
        runtime = {}
    revision = int(runtime.get("revision", 0) or 0)
    if cmd["expected_revision"] is not None and cmd["expected_revision"] != revision:
        raise WorldCommandError(
            f"运行态修订冲突：预期 {cmd['expected_revision']}，当前 {revision}"
        )
    handler = _DISPATCH[cmd["domain"]]
    result = handler(world, runtime, cmd)
    root = root_operation_id or cmd["idempotency_key"]
    events: list[dict[str, Any]] = []
    for index, event in enumerate(result["events"]):
        events.append(
            {
                **event,
                "root_operation_id": root,
                "cause": cmd["reason"],
                "operator": cmd["operator"],
                "depth": 0,
                "seq": index,
            }
        )
    new_revision = _bump_revision(runtime)
    if events:
        for event in events:
            _append_event(runtime, event)
    from ...protocol.runtime import hydrate_runtime, runtime_contract_from_world

    working["runtime"] = hydrate_runtime(
        runtime,
        artifact_id=str(
            (twp_runtime or {}).get("artifact_id")
            if isinstance(twp_runtime, Mapping)
            else world.get("artifact_id") or ""
        ),
        enabled_modules=list(
            (twp_runtime or {}).get("enabled_modules") or []
            if isinstance(twp_runtime, Mapping)
            else [
                str(item.get("module_id") or item.get("id") or "")
                for item in world.get("twp_modules") or []
                if isinstance(item, Mapping) and item.get("enabled", True)
            ]
        ),
        module_contract=runtime_contract_from_world(world),
    )
    return {
        "ok": True,
        "state": working,
        "revision": new_revision,
        "summary": result["summary"],
        "changes": result["changes"],
        "events": events,
        "root_operation_id": root,
        "dry_run": bool(dry_run),
        "affected": _affected_targets(result),
    }


def _affected_targets(result: dict[str, Any]) -> dict[str, list[str]]:
    affected: dict[str, list[str]] = {
        "quests": [], "npcs": [], "factions": [], "handouts": [], "notifications": []
    }
    for event in result.get("events", []):
        event_type = str(event.get("type") or "")
        if event_type.startswith("quest"):
            affected["quests"].append(str(event.get("quest") or ""))
        elif event_type.startswith("npc"):
            affected["npcs"].append(str(event.get("npc") or ""))
        elif event_type.startswith("faction"):
            affected["factions"].append(str(event.get("faction") or ""))
        elif event_type.startswith("handout"):
            affected["handouts"].append(str(event.get("handout") or ""))
        affected["notifications"].append(event_type)
    return {key: sorted({item for item in values if item}) for key, values in affected.items()}


__all__ = [name for name in globals() if not name.startswith('__')]

