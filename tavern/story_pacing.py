"""C5 deterministic story pacing plans.

This module is pure computation.  Repository code owns authorization,
transactions, snapshots and audit persistence.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .story_context import (
    build_story_condition_context,
    evaluate_story_condition,
    scene_transition_blockers,
)


PACING_ACTIONS = frozenset(
    {
        "host_beat",
        "close_scene",
        "skip_routine",
        "transition",
        "next_clue",
        "advance_chapter",
    }
)

# D1 六类停滞指示物（规格 16 §9）。D1 为 clean-break，
# 历史版本的进展字段不再作为别名接受。
PROGRESS_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("new_facts", ("new_facts",)),
    ("scene_changes", ("scene_changes",)),
    ("quest_changes", ("quest_changes",)),
    ("resource_changes", ("resource_changes",)),
    ("npc_changes", ("npc_changes",)),
    ("irreversible_choices", ("irreversible_choices",)),
)
PROGRESS_CATEGORY_KEYS = frozenset(
    key for _, keys in PROGRESS_CATEGORIES for key in keys
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pacing_preview_hash(plan: Mapping[str, Any]) -> str:
    payload = {
        str(key): deepcopy(value)
        for key, value in plan.items()
        if key not in {"preview_hash", "plan_id", "created_at"}
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _turn_progress_categories(turn: Mapping[str, Any]) -> set[str]:
    return {
        category
        for category, keys in PROGRESS_CATEGORIES
        if any(bool(turn.get(key)) for key in keys)
    }


def _flatten_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """把 TWP 分层运行态（runtime.modules.<id>.state）展平为模块字段视图。

    Repository 与纯函数测试也可直接传入模块字段视图；无法识别 runtime
    模块时保留输入参与比对。这里支持的是 D1 内部调用形态，不承担旧世界
    字段兼容。
    """
    value = _mapping(state)
    runtime = _mapping(value.get("runtime"))
    modules = _mapping(runtime.get("modules"))
    if not modules:
        return dict(value)
    flat: dict[str, Any] = dict(value)
    for module_id, payload in modules.items():
        module = _mapping(payload)
        if str(module.get("status") or "") not in {
            "initialized",
            "ready",
            "active",
        }:
            continue
        for key, item in _mapping(module.get("state")).items():
            flat[f"{module_id}.{key}"] = item
            flat[key] = item
    return flat


def _non_empty(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    return value not in (None, "", [], {})


def _slice_changed(
    previous_state: Mapping[str, Any],
    next_state: Mapping[str, Any],
    keys: Sequence[str],
) -> bool:
    previous = {
        key: value
        for key, value in _flatten_state(previous_state).items()
        if key in keys and _non_empty(value)
    }
    next_slice = {
        key: value
        for key, value in _flatten_state(next_state).items()
        if key in keys and _non_empty(value)
    }
    return _canonical(previous) != _canonical(next_slice)


def compute_turn_progress_indicators(
    previous_state: Mapping[str, Any],
    next_state: Mapping[str, Any],
    workflow: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """D1 六类进展指示物：只把“真实状态变化或已声明的结构化变更”记为进展。

    同一叙事文案反复提交但状态不变时，六项全部为 False；连续 N 轮后由
    detect_story_stall 判定停滞并生成可执行引导。workflow 中的结构化
    操作（clock/status/npc/economy ops、不可逆标记）与状态差异共同作为
    权威信号，避免模型只在正文里“推进”却不改变任何状态。
    """
    workflow_data = _mapping(workflow)
    selected_choice = _mapping(workflow_data.get("selected_choice"))
    return {
        "new_facts": _slice_changed(
            previous_state,
            next_state,
            (
                "knowledge",
                "facts",
                "clues",
                "evidence",
                "revealed",
                "discovered",
            ),
        ),
        "scene_changes": _slice_changed(
            previous_state,
            next_state,
            ("current_scene", "scene_ref", "scene_id"),
        ),
        "quest_changes": _slice_changed(
            previous_state,
            next_state,
            ("quests", "quest_log", "active_quests"),
        ),
        "resource_changes": (
            _slice_changed(
                previous_state,
                next_state,
                (
                    "resources",
                    "clocks",
                    "clock",
                    "time_clock",
                    "countdown",
                    "statuses",
                    "status",
                    "ledger",
                    "wallets",
                ),
            )
            or bool(_sequence(workflow_data.get("clock_ops")))
            or bool(_sequence(workflow_data.get("status_ops")))
            or bool(_sequence(workflow_data.get("economy_ops")))
        ),
        "npc_changes": (
            _slice_changed(
                previous_state,
                next_state,
                ("npcs", "npc_state", "npc_stances"),
            )
            or bool(_sequence(workflow_data.get("npc_ops")))
        ),
        "irreversible_choices": bool(
            workflow_data.get("irreversible")
            or selected_choice.get("irreversible")
            or _sequence(workflow_data.get("irreversible_ops"))
        ),
    }


def stall_policy_for_scene(
    world: Mapping[str, Any],
    scene_ref: str,
) -> dict[str, Any]:
    """读取当前场景节点声明的停滞策略，缺省回退到图级默认与 3 轮阈值。"""
    rules = _mapping(world.get("rules"))
    scene_graph = _mapping(rules.get("scene_graph"))
    nodes = {
        str(item.get("id") or ""): dict(item)
        for item in _sequence(scene_graph.get("nodes"))
        if isinstance(item, Mapping) and item.get("id")
    }
    node = nodes.get(str(scene_ref or ""), {})
    policy = _mapping(node.get("stall_policy"))
    if not policy:
        policy = _mapping(scene_graph.get("stall_policy"))
    if not policy:
        policy = {"turn_threshold": 3}
    return policy


def detect_story_stall(
    history: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """D1 停滞检测：场景内连续 N 轮六类指示物全空，保留 RP 豁免。

    N 由 stall_policy.turn_threshold 决定（D1 默认 3；旧世界可按声明
    保留更高阈值）。六类指示物：新事实、场景变化、任务变化、资源/状态
    变化、NPC 立场变化、玩家不可逆选择。
    """

    policy_data = _mapping(policy)
    threshold = max(2, int(policy_data.get("turn_threshold", 3) or 3))
    normalized = [dict(item) for item in history if isinstance(item, Mapping)]
    if not normalized:
        return {"stalled": False, "turns": 0, "reason": "no_history", "suggestions": []}
    last_turn = int(normalized[-1].get("turn_no", len(normalized)) or len(normalized))
    snooze_until = int(policy_data.get("snooze_until_turn", 0) or 0)
    if last_turn <= snooze_until:
        return {
            "stalled": False,
            "turns": 0,
            "reason": "snoozed",
            "snooze_until_turn": snooze_until,
            "suggestions": [],
        }
    current_scene = str(normalized[-1].get("scene_ref") or "")
    recent: list[dict[str, Any]] = []
    for item in reversed(normalized):
        if current_scene and str(item.get("scene_ref") or "") != current_scene:
            break
        recent.append(item)
        if len(recent) >= threshold:
            break
    recent.reverse()
    if len(recent) < threshold:
        return {
            "stalled": False,
            "turns": len(recent),
            "reason": "below_threshold",
            "suggestions": [],
        }
    if any(bool(item.get("roleplay_active") or item.get("rp_active")) for item in recent):
        return {
            "stalled": False,
            "turns": len(recent),
            "reason": "roleplay_active",
            "suggestions": [],
        }
    progress_categories: set[str] = set()
    for item in recent:
        progress_categories.update(_turn_progress_categories(item))
    if progress_categories:
        return {
            "stalled": False,
            "turns": len(recent),
            "reason": "structured_progress",
            "progress_categories": sorted(progress_categories),
            "suggestions": [],
        }
    return {
        "stalled": True,
        "turns": len(recent),
        "reason": "no_progress",
        "scene_ref": current_scene,
        "threshold": threshold,
        "progress_categories": [],
        "empty_categories": sorted(
            category for category, _ in PROGRESS_CATEGORIES
        ),
        "action_fingerprints": [
            str(item.get("action_fingerprint") or "") for item in recent
        ],
        "narrative_fingerprints": [
            str(item.get("narrative_fingerprint") or "") for item in recent
        ],
        "suggestions": list(
            policy_data.get("suggestions")
            or ["host_beat", "close_scene", "next_clue", "keep_pace"]
        ),
    }


def _explicit_cost(value: Any) -> dict[str, Any] | None:
    """一个明确的代价：resource+amount 或 label+description。"""

    cost = _mapping(value)
    if not cost:
        return None
    resource = str(cost.get("resource") or "").strip()
    label = str(cost.get("label") or "").strip()
    description = str(cost.get("description") or "").strip()
    has_amount = (
        cost.get("amount") is not None
        and str(cost.get("amount") or "").strip() != ""
    )
    if (resource and has_amount) or (label and description):
        return dict(cost)
    return None


def _intervention_cost(
    scene_ref: str,
    transition: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    cost = transition.get("cost")
    if cost is not None:
        return _explicit_cost(cost)
    intervention_costs = policy.get("intervention_costs")
    if isinstance(intervention_costs, Mapping):
        cost = intervention_costs.get(scene_ref) or intervention_costs.get("default")
        if cost is not None:
            return _explicit_cost(cost)
    return None


def build_stall_intervention(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] = (),
    history: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """D1 停滞干预：摘要 + 2-4 个可达行动（≥1 转场、≥1 明确代价）。

    纯计算层不落库、不投递。DM 可暂停干预由 can_pause/dm_action 表达，
    仓储层负责持久化与群聊投递。
    """

    detection = detect_story_stall(history, policy)
    if not detection.get("stalled"):
        return {
            "stalled": False,
            "reason": str(detection.get("reason") or "not_stalled"),
            "actions": [],
            "intervention": None,
        }
    policy_data = _mapping(policy)
    runtime_data = _mapping(runtime)
    session_data = _mapping(session)
    current_scene = str(detection.get("scene_ref") or runtime_data.get("current_scene") or "")
    rules = _mapping(world.get("rules"))
    scene_graph = _mapping(rules.get("scene_graph"))
    nodes = {
        str(item.get("id") or ""): dict(item)
        for item in _sequence(scene_graph.get("nodes"))
        if isinstance(item, Mapping) and item.get("id")
    }
    context = build_story_condition_context(
        world=world,
        runtime=runtime_data,
        session=session_data,
        squad=squad,
    )
    current = nodes.get(current_scene, {})
    actions: list[dict[str, Any]] = []
    for order, raw in enumerate(_sequence(current.get("recommended_transitions"))):
        if not isinstance(raw, Mapping):
            continue
        transition = dict(raw)
        target_ref = str(transition.get("scene_ref") or transition.get("target") or "").strip()
        if not target_ref:
            continue
        when = transition.get("when")
        if when:
            result = evaluate_story_condition(when, world=world, context=context)
            if not result.get("matched"):
                continue
        blockers = scene_transition_blockers(
            world=world,
            runtime=runtime_data,
            target_scene=target_ref,
            session=session_data,
            squad=squad,
        )
        target_node = nodes.get(target_ref, {})
        actions.append(
            {
                "kind": "transition",
                "target_ref": target_ref,
                "label": str(target_node.get("label") or target_ref),
                "priority": int(transition.get("priority", order) or order),
                "cost": _intervention_cost(target_ref, transition, policy_data),
                "reachable": not blockers,
                "blockers": [
                    dict(item)
                    for item in blockers
                    if isinstance(item, Mapping)
                ],
            }
        )
    opportunities = _sequence(runtime_data.get("story_opportunities"))
    next_clue = str(runtime_data.get("next_clue_ref") or "").strip()
    if next_clue and next_clue not in opportunities:
        opportunities.insert(0, next_clue)
    for clue_ref in opportunities[:2]:
        actions.append(
            {
                "kind": "offer_clue",
                "target_ref": str(clue_ref),
                "label": "推进一条线索入口",
                "priority": 50,
                "cost": None,
                "reachable": True,
                "blockers": [],
            }
        )
    actions.append(
        {
            "kind": "host_beat",
            "target_ref": "",
            "label": "主持人推进一个不替玩家做决定的事件节拍",
            "priority": 100,
            "cost": None,
            "reachable": True,
            "blockers": [],
        }
    )
    transition_actions = [item for item in actions if item["kind"] == "transition"]
    transition_actions.sort(
        key=lambda item: (not item["reachable"], int(item["priority"]))
    )
    selected: list[dict[str, Any]] = []
    for item in transition_actions:
        if item["reachable"] and len(selected) < 4:
            selected.append(item)
    for item in actions:
        if item["kind"] != "transition" and len(selected) < 4:
            selected.append(item)
        if len(selected) >= 4:
            break
    if len(selected) < 2:
        selected = actions[:4]
    selected = selected[:4]
    revealed = _sequence(
        _mapping(runtime_data.get("knowledge")).get("revealed")
    )
    active_quests = sum(
        1
        for item in _mapping(runtime_data.get("quests")).values()
        if isinstance(item, Mapping)
        and str(item.get("status") or "") in {"active", "available"}
    )
    summary = (
        f"当前场景〔{current.get('label') or current_scene}〕已连续 "
        f"{int(detection.get('turns') or 0)} 轮没有新的状态变化。"
        f"已揭示事实 {len(revealed)} 项，进行中任务 {active_quests} 项；"
        "缺少可直接推进的冲突、任务或线索进展。"
    )
    return {
        "stalled": True,
        "reason": "no_progress",
        "scene_ref": current_scene,
        "turns": int(detection.get("turns") or 0),
        "summary": summary,
        "actions": selected,
        "can_pause": True,
        "dm_action": "pause_stall_intervention",
        "requires_confirmation": True,
        "guarantees": {
            "transition": any(
                item["kind"] == "transition" and item["reachable"]
                for item in selected
            ),
            "explicit_cost": any(
                _explicit_cost(item.get("cost")) is not None
                for item in selected
            ),
        },
    }


def validate_intervention_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """验证干预计划契约：2-4 行动、≥1 可达转场、≥1 明确代价、可暂停。"""

    problems: list[dict[str, Any]] = []
    if not bool(plan.get("stalled")):
        return {"ok": True, "problems": []}
    actions = [dict(item) for item in _sequence(plan.get("actions")) if isinstance(item, Mapping)]
    if not 2 <= len(actions) <= 4:
        problems.append(
            {
                "code": "intervention.action_count",
                "message": f"停滞干预必须提供 2-4 个行动，当前 {len(actions)} 个",
            }
        )
    transitions = [
        item
        for item in actions
        if item.get("kind") == "transition"
    ]
    reachable_transitions = [
        item for item in transitions if bool(item.get("reachable"))
    ]
    if not reachable_transitions:
        problems.append(
            {
                "code": "intervention.no_reachable_transition",
                "message": "停滞干预必须至少包含一个可直接转场的行动",
            }
        )
    if not any(_explicit_cost(item.get("cost")) for item in actions):
        problems.append(
            {
                "code": "intervention.no_explicit_cost",
                "message": "停滞干预必须至少包含一个带明确代价的行动",
            }
        )
    if not bool(plan.get("can_pause")):
        problems.append(
            {
                "code": "intervention.not_pausable",
                "message": "DM 必须能够暂停停滞干预",
            }
        )
    if not str(plan.get("dm_action") or "").strip():
        problems.append(
            {
                "code": "intervention.no_dm_action",
                "message": "停滞干预缺少 DM 可执行动作",
            }
        )
    return {"ok": not problems, "problems": problems}


def validate_pacing_blockers(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in _sequence(plan.get("blockers"))
        if isinstance(item, Mapping)
    ]


def build_pacing_plan(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    session: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]] = (),
    action: str,
    target_ref: str = "",
    expected_session_revision: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic, confirmation-required pacing preview."""

    action = str(action or "").strip().lower()
    if action not in PACING_ACTIONS:
        raise ValueError(f"不支持的剧情节奏操作：{action or '（空）'}")
    runtime_data = _mapping(runtime)
    session_data = _mapping(session)
    current_scene = str(runtime_data.get("current_scene") or "")
    revision = int(
        expected_session_revision
        if expected_session_revision is not None
        else session_data.get("revision", 0) or 0
    )
    target_scene = str(target_ref or "")
    blockers: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    automatic_settlements: list[dict[str, Any]] = []
    clock_changes: list[dict[str, Any]] = []
    lost_opportunities: list[dict[str, Any]] = []

    if action == "transition":
        if not target_scene:
            blockers.append(
                {
                    "code": "unknown_target_scene",
                    "message": "直接转场必须选择目标场景。",
                }
            )
        else:
            blockers.extend(
                scene_transition_blockers(
                    world=world,
                    runtime=runtime_data,
                    target_scene=target_scene,
                    session=session_data,
                    squad=squad,
                )
            )
            operations.append(
                {
                    "domain": "scene",
                    "action": "transition",
                    "target_ref": target_scene,
                }
            )
    elif action == "close_scene":
        target_scene = target_scene or str(
            runtime_data.get("recommended_next_scene") or ""
        )
        if target_scene:
            blockers.extend(
                scene_transition_blockers(
                    world=world,
                    runtime=runtime_data,
                    target_scene=target_scene,
                    session=session_data,
                    squad=squad,
                )
            )
            operations.append(
                {
                    "domain": "scene",
                    "action": "transition",
                    "target_ref": target_scene,
                }
            )
        else:
            blockers.append(
                {
                    "code": "unknown_target_scene",
                    "message": "当前场景没有可收束到的合法下一场景。",
                }
            )
    elif action == "next_clue":
        clue_ref = target_ref or str(runtime_data.get("next_clue_ref") or "")
        if not clue_ref:
            blockers.append(
                {
                    "code": "no_next_clue",
                    "message": "当前场景没有可推进的下一线索入口。",
                }
            )
        else:
            operations.append(
                {
                    "domain": "knowledge",
                    "action": "offer",
                    "target_ref": clue_ref,
                    "reveal_content": False,
                }
            )
    elif action == "advance_chapter":
        chapter_ref = target_ref or str(runtime_data.get("next_chapter_ref") or "")
        if not chapter_ref:
            blockers.append(
                {
                    "code": "chapter_condition_failed",
                    "message": "当前章节的前置条件尚未满足。",
                }
            )
        else:
            operations.append(
                {
                    "domain": "story",
                    "action": "advance_chapter",
                    "target_ref": chapter_ref,
                }
            )
    elif action == "skip_routine":
        if bool(runtime_data.get("pending_resource_cost")):
            blockers.append(
                {
                    "code": "resource_cost_blocked",
                    "message": "当前过程仍有未结算的资源消耗，不能静默跳过。",
                }
            )
        if bool(runtime_data.get("pending_private_secret")):
            blockers.append(
                {
                    "code": "private_secret_blocked",
                    "message": "当前过程涉及未处理的私密信息，不能公开跳过。",
                }
            )
        operations.append({"domain": "story", "action": "skip_routine"})
    else:
        operations.append({"domain": "story", "action": "host_beat"})

    base = {
        "schema": "tavern-story-pacing-plan/1.0.0-rc10",
        "action": action,
        "session_id": str(session_data.get("id") or session_data.get("session_id") or ""),
        "expected_session_revision": revision,
        "current_scene": current_scene,
        "target_scene": target_scene,
        "operations": operations,
        "automatic_settlements": automatic_settlements,
        "clock_changes": clock_changes,
        "lost_opportunities": lost_opportunities,
        "blockers": blockers,
        "requires_confirmation": True,
    }
    digest = pacing_preview_hash(base)
    return {
        **base,
        "plan_id": f"pacing:{digest[:24]}",
        "preview_hash": digest,
    }


__all__ = [
    "PACING_ACTIONS",
    "PROGRESS_CATEGORIES",
    "PROGRESS_CATEGORY_KEYS",
    "build_stall_intervention",
    "build_pacing_plan",
    "compute_turn_progress_indicators",
    "detect_story_stall",
    "pacing_preview_hash",
    "stall_policy_for_scene",
    "validate_intervention_plan",
    "validate_pacing_blockers",
]
