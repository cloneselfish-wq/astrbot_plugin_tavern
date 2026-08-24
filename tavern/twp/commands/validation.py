from .common import *

class WorldCommandError(ValueError):
    """命令非法、目标缺失或运行态冲突。"""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip()[:maximum]


def _targets(value: Any) -> list[str]:
    return [
        _text(item, 128)
        for item in _sequence(value)
        if _text(item, 128)
    ]


def _index_by(defs: Sequence[Any]) -> dict[str, dict[str, Any]]:
    return {
        str(raw.get("id") or ""): dict(raw)
        for raw in defs
        if isinstance(raw, Mapping) and raw.get("id")
    }


def _append_event(
    runtime: dict[str, Any],
    event: Mapping[str, Any],
) -> None:
    events = runtime.setdefault("events", [])
    if not isinstance(events, list):
        events = []
    events.append(dict(event))
    del events[: max(0, len(events) - RUNTIME_EVENT_LOG_LIMIT)]
    runtime["events"] = events


def _bump_revision(runtime: dict[str, Any]) -> int:
    revision = int(runtime.get("revision", 0) or 0) + 1
    runtime["revision"] = revision
    return revision


def validate_command(command: Mapping[str, Any]) -> dict[str, Any]:
    """规范化并校验一个世界命令的基础结构。"""
    if not isinstance(command, Mapping):
        raise WorldCommandError("世界命令必须是对象")
    domain = _text(command.get("domain"), 40).lower()
    if domain not in COMMAND_DOMAINS:
        raise WorldCommandError(f"不支持的世界命令域：{domain or '<empty>'}")
    action = _text(command.get("action"), 80)
    if not action:
        raise WorldCommandError("世界命令缺少 action")
    targets = _targets(command.get("targets"))
    if not targets:
        raise WorldCommandError("世界命令缺少稳定目标 ID（targets）")
    operator = _text(command.get("operator"), 128)
    if not operator:
        raise WorldCommandError("世界命令缺少操作者（operator）")
    reason = _text(command.get("reason"), 400)
    if not reason:
        raise WorldCommandError("世界命令必须说明原因（reason）")
    idempotency_key = _text(command.get("idempotency_key"), 200)
    if not idempotency_key:
        raise WorldCommandError("世界命令缺少幂等键（idempotency_key）")
    payload = command.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    visibility = _text(command.get("visibility") or "public", 20).lower()
    if visibility not in {"public", "vague", "hidden"}:
        raise WorldCommandError("visibility 只能是 public/vague/hidden")
    expected_revision = command.get("expected_revision")
    if expected_revision is not None:
        try:
            expected_revision = int(expected_revision)
        except (TypeError, ValueError):
            raise WorldCommandError("expected_revision 必须是整数或 null")
    return {
        "domain": domain,
        "action": action,
        "targets": targets,
        "operator": operator,
        "reason": reason,
        "idempotency_key": idempotency_key,
        "payload": dict(payload),
        "visibility": visibility,
        "expected_revision": expected_revision,
    }


def _require_target(target: str, definitions: Mapping[str, Any], label: str) -> None:
    if target not in definitions:
        raise WorldCommandError(f"{label} 不存在或未注册：{target}")


def _command_scene(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("scene_graph"))
    nodes = _index_by(module.get("nodes", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    summary = ""
    if action == "transition":
        _require_target(target, nodes, "目标场景")
        previous = str(runtime.get("current_scene") or "")
        blockers = scene_transition_blockers(
            world=world,
            runtime=runtime,
            target_scene=target,
        )
        current = nodes.get(previous)
        force = bool(cmd["payload"].get("force", False))
        recommendations = (
            _sequence(current.get("recommended_transitions"))
            if isinstance(current, Mapping)
            else []
        )
        if (
            previous
            and previous != target
            and recommendations
            and not recommended_transition(current, target)
            and not force
        ):
            blockers.append(
                {
                    "code": "non_recommended_transition",
                    "message": "目标不是当前场景推荐转场；主持人确认风险后可使用 force=true。",
                }
            )
        if blockers:
            details = "\n".join(
                f"- {str(item.get('message') or item.get('code') or '未知阻塞')}"
                for item in blockers
                if isinstance(item, Mapping)
            )
            raise WorldCommandError(
                f"无法转入目标场景 {target}：\n{details}\n"
                "可执行：保持当前场景、推进到下一线索入口，或处理上述阻塞。"
            )
        runtime["current_scene"] = target
        history = runtime.setdefault("scene_history", [])
        if not isinstance(history, list):
            history = []
        history.append(target)
        del history[: max(0, len(history) - 200)]
        runtime["scene_history"] = history
        visits = runtime.setdefault("scene_visits", {})
        if not isinstance(visits, dict):
            visits = {}
        visits[target] = int(visits.get(target, 0) or 0) + 1
        runtime["scene_visits"] = visits
        changes.append(
            {
                "domain": "scene",
                "action": "transition",
                "target": target,
                "from": previous,
                "forced": bool(force and current and not recommended_transition(current, target)),
            }
        )
        events.append(
            {
                "type": "scene.entered",
                "target": target,
                "from": previous,
                "visibility": cmd["visibility"],
            }
        )
        if previous and previous != target:
            events.append({"type": "scene.exited", "target": previous})
        summary = f"队伍转入「{nodes[target].get('label') or target}」"
    elif action == "set_exit":
        exit_ref = _text(cmd["payload"].get("exit"), 120)
        if not exit_ref:
            raise WorldCommandError("scene.set_exit 需要 payload.exit")
        open_state = bool(cmd["payload"].get("open", True))
        exits = runtime.setdefault("exit_states", {})
        if not isinstance(exits, dict):
            exits = {}
        exits[exit_ref] = "open" if open_state else "closed"
        runtime["exit_states"] = exits
        changes.append({"domain": "scene", "action": "set_exit", "target": exit_ref, "open": open_state})
        events.append({"type": "scene.exit_changed", "target": exit_ref, "open": open_state})
        summary = f"出口「{exit_ref}」已{'开放' if open_state else '封锁'}"
    elif action == "record_visit":
        _require_target(target, nodes, "目标场景")
        visits = runtime.setdefault("scene_visits", {})
        if not isinstance(visits, dict):
            visits = {}
        visits[target] = int(visits.get(target, 0) or 0) + 1
        runtime["scene_visits"] = visits
        history = runtime.setdefault("scene_history", [])
        if not isinstance(history, list):
            history = []
        if not history or str(history[-1]) != target:
            history.append(target)
        runtime["scene_history"] = history[-200:]
        changes.append({"domain": "scene", "action": "record_visit", "target": target})
        events.append({"type": "scene.revisited", "target": target})
        summary = f"已记录回访：{target}"
    else:
        raise WorldCommandError(f"不支持的场景命令：{action}")
    return {"changes": changes, "events": events, "summary": summary}


def _command_quest(world: Mapping[str, Any], runtime: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("quest_graph"))
    quests = _index_by(module.get("quests", []))
    action = cmd["action"]
    target = cmd["targets"][0]
    _require_target(target, quests, "任务")
    state = runtime.setdefault("quests", {})
    if not isinstance(state, dict):
        state = {}
        runtime["quests"] = state
    quest_state = state.get(target)
    quest_state = dict(quest_state) if isinstance(quest_state, Mapping) else {"status": "available", "completed_objectives": []}
    changes: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    definition = quests[target]
    objective_ids = {str(item.get("id")) for item in definition.get("objectives", []) if isinstance(item, Mapping)}
    if action == "accept":
        quest_state["status"] = "active"
        quest_state["accepted_by"] = cmd["operator"]
        summary = f"队伍接取任务「{definition.get('label') or target}」"
    elif action == "complete_objective":
        objective = _text(cmd["targets"][1] if len(cmd["targets"]) > 1 else "", 128)
        if not objective:
            raise WorldCommandError("quest.complete_objective 需要第二个目标 ID（objective）")
        if objective_ids and objective not in objective_ids:
            raise WorldCommandError(f"任务 {target} 没有目标 {objective}")
        completed = quest_state.setdefault("completed_objectives", [])
        if not isinstance(completed, list):
            completed = []
        if objective not in completed:
            completed.append(objective)
        quest_state["completed_objectives"] = completed
        objective_label = next(
            (
                str(item.get("label") or objective)
                for item in definition.get("objectives", [])
                if isinstance(item, Mapping) and str(item.get("id")) == objective
            ),
            objective,
        )
        events.append({"type": "quest.objective_completed", "quest": target, "objective": objective})
        summary = f"任务「{definition.get('label') or target}」目标完成：{objective_label}"
    elif action == "fail":
        quest_state["status"] = "failed"
        quest_state["failed_by"] = cmd["operator"]
        events.append({"type": "quest.failed", "quest": target})
        summary = f"任务「{definition.get('label') or target}」失败"
    elif action == "settle":
        quest_state["status"] = "completed"
        quest_state["settled_at"] = cmd["reason"]
        events.append({"type": "quest.completed", "quest": target})
        summary = f"任务「{definition.get('label') or target}」结算完成"
    elif action == "set_visible":
        quest_state["visible"] = bool(cmd["payload"].get("visible", True))
        summary = f"任务「{definition.get('label') or target}」可见性已调整"
    else:
        raise WorldCommandError(f"不支持的 quest 命令：{action}")
    state[target] = quest_state
    runtime["quests"] = state
    changes.append({"domain": "quest", "action": action, "target": target})
    return {"changes": changes, "events": events, "summary": summary}


__all__ = [name for name in globals() if not name.startswith('__')]

