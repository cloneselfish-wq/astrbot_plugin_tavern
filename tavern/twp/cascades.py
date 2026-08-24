"""B2（A9）跨命令自动事件链（声明式，禁止脚本）。

世界包 rules.event_cascades 声明「事件 → 下游世界命令」规则；命令执行后按
已发出事件匹配并递归应用下游命令，全部共享同一根操作 ID，受
EVENT_MAX_DEPTH / EVENT_MAX_TRIGGERS 限制，预览可递归展开。
"""
from __future__ import annotations

# TWP runtime module.
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .commands import (
    EVENT_MAX_DEPTH,
    EVENT_MAX_TRIGGERS,
    apply_command,
    validate_command,
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip()[:maximum]


def cascade_rules(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    rules = _mapping(world.get("rules"))
    raw = rules.get("event_cascades")
    return [dict(item) for item in _sequence(raw) if isinstance(item, Mapping)]


def _event_matches(event: Mapping[str, Any], when: Mapping[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type != str(when.get("event") or ""):
        return False
    refs = when.get("refs")
    refs = refs if isinstance(refs, Mapping) else {}
    for key, expected in refs.items():
        actual = event.get(str(key))
        if str(actual or "") != str(expected or ""):
            return False
    return True


def cascade_commands_for(
    world: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """按已发出事件匹配并展平所有下游命令（未做深度限制，供预览）。"""
    commands: list[dict[str, Any]] = []
    for rule in cascade_rules(world):
        when = _mapping(rule.get("when"))
        for event in events:
            if not _event_matches(event, when):
                continue
            for raw in _sequence(rule.get("then")):
                if isinstance(raw, Mapping):
                    commands.append(dict(raw))
    return commands


def apply_cascades(
    world: Mapping[str, Any],
    state: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    root_operation_id: str,
    operator: str,
    cause: str,
    depth: int = 1,
    budget: dict[str, int] | None = None,
) -> dict[str, Any]:
    """递归应用事件级联；返回 {state, events, applied, depth, triggers}。

    全部下游命令共享 root_operation_id，受 EVENT_MAX_DEPTH 与
    EVENT_MAX_TRIGGERS 限制；命令本身仍带独立幂等键（根+序号+深度）。
    """
    budget = budget if budget is not None else {"triggers": 0}
    working = deepcopy(dict(state))
    emitted: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    if depth > EVENT_MAX_DEPTH:
        return {"state": working, "events": emitted, "applied": applied, "depth": depth, "triggers": budget["triggers"]}
    commands = cascade_commands_for(world, events)
    for index, raw in enumerate(commands[:EVENT_MAX_TRIGGERS - budget["triggers"]]):
        command = dict(raw)
        command.setdefault("operator", operator)
        command["reason"] = f"\u7ea7\u8054[{cause}] {command.get('reason') or command.get('action') or ''}"
        command["idempotency_key"] = f"{root_operation_id}:casc:{index}:{depth}"
        command["expected_revision"] = None
        try:
            validated = validate_command(command)
        except Exception:
            continue
        result = apply_command(
            world,
            working,
            validated,
            root_operation_id=root_operation_id,
        )
        working = result["state"]
        emitted.extend(result["events"])
        applied.append(
            {
                "domain": validated["domain"],
                "action": validated["action"],
                "targets": validated["targets"],
                "summary": result["summary"],
                "depth": depth,
                "index": index,
            }
        )
        budget["triggers"] += 1
        if budget["triggers"] >= EVENT_MAX_TRIGGERS:
            break
        nested = apply_cascades(
            world,
            working,
            result["events"],
            root_operation_id=root_operation_id,
            operator=operator,
            cause=cause,
            depth=depth + 1,
            budget=budget,
        )
        working = nested["state"]
        emitted.extend(nested["events"])
        applied.extend(nested["applied"])
    return {
        "state": working,
        "events": emitted,
        "applied": applied,
        "depth": depth,
        "triggers": budget["triggers"],
    }


def cascade_preview(
    world: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """只读递归预览：展开首层级联命令及其摘要。"""
    result: list[dict[str, Any]] = []
    for command in cascade_commands_for(world, events):
        try:
            validated = validate_command({**command, "idempotency_key": "preview", "operator": "preview", "reason": "preview"})
        except Exception:
            continue
        result.append(
            {
                "domain": validated["domain"],
                "action": validated["action"],
                "targets": validated["targets"],
                "payload": validated["payload"],
                "visibility": validated["visibility"],
            }
        )
    return result


__all__ = [
    "apply_cascades",
    "cascade_preview",
    "cascade_rules",
]
