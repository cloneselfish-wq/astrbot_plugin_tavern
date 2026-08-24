"""C5 story condition context and transition safety checks.

The story layer deliberately reuses the package condition engine.  C5 scene
data may also use the compact ``path/op/value`` notation from the authoring
specification; it is normalized and evaluated here instead of creating a
second, unrelated expression language in command handlers.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .condition_engine import ConditionEngine
from .entity_registry import EntityRegistry


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _scene_nodes(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rules = _mapping(world.get("rules"))
    scene_graph = _mapping(rules.get("scene_graph"))
    return {
        str(item.get("id") or ""): dict(item)
        for item in _sequence(scene_graph.get("nodes"))
        if isinstance(item, Mapping) and item.get("id")
    }


def _path_value(context: Mapping[str, Any], path: str) -> tuple[Any, bool]:
    current: Any = context
    for segment in (item for item in str(path or "").split(".") if item):
        if not isinstance(current, Mapping) or segment not in current:
            return None, False
        current = current[segment]
    return current, True


def _compare(left: Any, operator: str, right: Any) -> bool:
    normalized = {
        "eq": "==",
        "ne": "!=",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
    }.get(str(operator or "").lower(), str(operator or "==").lower())
    return ConditionEngine._compare(left, normalized, right)


def build_story_condition_context(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the one condition context used by scenes, pacing and health checks."""

    runtime_data = _mapping(runtime)
    session_data = _mapping(session)
    squad_data = [dict(item) for item in (squad or []) if isinstance(item, Mapping)]
    current_scene = str(runtime_data.get("current_scene") or "")
    scene_definition = _scene_nodes(world).get(current_scene, {})
    party_refs: dict[str, Any] = {}
    for member in squad_data:
        for key in (
            "region",
            "region_ref",
            "identity",
            "identity_ref",
            "profession",
            "profession_ref",
            "faction",
            "faction_ref",
            "core_belief",
            "core_belief_ref",
        ):
            value = member.get(key)
            if value not in (None, "", [], {}):
                party_refs.setdefault(key, []).append(value)
    return {
        "runtime": runtime_data,
        "world": dict(world),
        "session": session_data,
        "squad": squad_data,
        "party": {"members": squad_data, "refs": party_refs},
        "scene": {
            **scene_definition,
            "ref": current_scene,
            "refs": {current_scene: scene_definition} if current_scene else {},
        },
        "actor": _mapping(session_data.get("actor")),
        "target": {},
        "action": {},
        "location": {},
        "object": {},
        "relationship": {},
        "event": {},
        "runtime_effect": {},
        "custom": {"runtime": runtime_data, "session": session_data},
    }


def evaluate_story_condition(
    condition: Any,
    *,
    world: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a story condition and return a stable diagnostic payload."""

    reads: list[dict[str, Any]] = []

    def visit(node: Any) -> bool:
        if node in (None, {}):
            return True
        if isinstance(node, bool):
            return node
        if not isinstance(node, Mapping):
            raise TypeError("故事条件必须是对象")
        if "all" in node:
            values = _sequence(node.get("all"))
            return all(visit(item) for item in values)
        if "any" in node:
            values = _sequence(node.get("any"))
            return any(visit(item) for item in values)
        if "not" in node:
            return not visit(node.get("not"))
        if "path" in node:
            path = str(node.get("path") or "")
            left, found = _path_value(context, path)
            reads.append({"path": path, "found": found, "value": left})
            return _compare(left, str(node.get("op") or node.get("operator") or "eq"), node.get("value"))
        engine = ConditionEngine(EntityRegistry(world))
        result = engine.evaluate(node, context)
        reads.extend(result.reads)
        return result.matched

    try:
        matched = visit(condition)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "matched": False,
            "reads": reads,
            "problem": {
                "code": "invalid_story_condition",
                "message": str(exc),
            },
        }
    return {"matched": bool(matched), "reads": reads, "problem": None}


def evaluate_story_condition_detail(
    condition: Any,
    *,
    world: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """D1-RUN-004/014：故事与终局条件使用同一 Condition Engine 求值。

    返回允许结果（allowed/code/message/recovery/technical_refs），
    供命令前置、候选依赖和终局核对共用。
    """

    engine = ConditionEngine(EntityRegistry(world))
    try:
        evaluation = engine.evaluate_with_detail(condition, context)
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "allowed": False,
            "code": "condition.invalid",
            "message": str(exc),
            "recovery": "",
            "technical_refs": [],
        }
    payload = evaluation.to_payload()
    payload["reads"] = [
        dict(item) for item in evaluation.reads if isinstance(item, Mapping)
    ]
    return payload


def scene_transition_blockers(
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    target_scene: str,
    session: Mapping[str, Any] | None = None,
    squad: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return structured blockers for a requested deterministic transition."""

    nodes = _scene_nodes(world)
    runtime_data = _mapping(runtime)
    current_ref = str(runtime_data.get("current_scene") or "")
    blockers: list[dict[str, Any]] = []
    target = nodes.get(str(target_scene or ""))
    if target is None:
        return [
            {
                "code": "unknown_target_scene",
                "message": f"目标场景未在当前世界注册：{target_scene or '（空）'}",
                "target_ref": str(target_scene or ""),
            }
        ]
    context = build_story_condition_context(
        world=world,
        runtime=runtime_data,
        session=session,
        squad=squad,
    )
    current = nodes.get(current_ref)
    if current:
        result = evaluate_story_condition(
            current.get("exit_conditions", {}),
            world=world,
            context=context,
        )
        if not result["matched"]:
            blockers.append(
                {
                    "code": "exit_condition_failed",
                    "message": f"当前场景〔{current.get('label') or current_ref}〕的离场条件尚未满足。",
                    "scene_ref": current_ref,
                    "diagnostic": result,
                }
            )
    result = evaluate_story_condition(
        target.get("entry_conditions", {}),
        world=world,
        context=context,
    )
    if not result["matched"]:
        blockers.append(
            {
                "code": "entry_condition_failed",
                "message": f"目标场景〔{target.get('label') or target_scene}〕的入场条件尚未满足。",
                "scene_ref": str(target_scene),
                "diagnostic": result,
            }
        )

    vote = runtime_data.get("active_vote")
    if isinstance(vote, Mapping) and str(vote.get("status") or "active") in {
        "active",
        "pending",
        "open",
    }:
        blockers.append(
            {
                "code": "active_vote_blocked",
                "message": "当前存在未完成的集体表决，请先完成或作废表决。",
            }
        )
    risks = _sequence(runtime_data.get("pending_risks"))
    if any(
        isinstance(item, Mapping)
        and str(item.get("severity") or "").lower() == "lethal"
        and not bool(item.get("resolved"))
        for item in risks
    ):
        blockers.append(
            {
                "code": "lethal_risk_blocked",
                "message": "当前存在尚未结算的致命风险，不能直接跳过。",
            }
        )
    session_data = _mapping(session)
    if (
        bool(session_data.get("card_review_blocked"))
        or bool(runtime_data.get("card_review_blocked"))
    ) or any(
        str(item.get("review_status") or item.get("card_status") or "").lower()
        in {"pending", "submitted", "needs_review"}
        for item in (squad or [])
        if isinstance(item, Mapping)
    ):
        blockers.append(
            {
                "code": "card_review_blocked",
                "message": "队伍仍有角色卡待审核，不能绕过开演阻塞。",
            }
        )
    return blockers


def recommended_transition(
    current_scene: Mapping[str, Any] | None,
    target_scene: str,
) -> bool:
    """Whether the target is declared as a recommended next scene."""

    if not isinstance(current_scene, Mapping):
        return False
    return any(
        isinstance(item, Mapping)
        and str(item.get("scene_ref") or item.get("target") or "") == target_scene
        for item in _sequence(current_scene.get("recommended_transitions"))
    )


def recommend_opening_scenarios(
    world: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Rank declared openings from structured card fields without exposing secrets.

    排序只能改变五个声明开局的推荐次序，不得把未命中
    画像的开局从可选集合中删除。
    """

    graph = _mapping(_mapping(world.get("rules")).get("scene_graph"))
    nodes = _scene_nodes(world)
    profiles: list[dict[str, Any]] = []
    for member in squad:
        if not isinstance(member, Mapping):
            continue
        raw = member.get("card_profile") or member.get("profile") or {}
        if isinstance(raw, Mapping):
            profiles.append(dict(raw))
    ranked: list[dict[str, Any]] = []
    for order, raw in enumerate(_sequence(graph.get("opening_scenarios"))):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        scene_ref = str(item.get("scene_ref") or "").strip()
        if scene_ref not in nodes:
            continue
        fields = [
            str(field)
            for field in _sequence(item.get("background_fields"))
            if str(field).strip()
        ]
        regions = {
            str(value)
            for value in _sequence(item.get("region_refs"))
            if str(value).strip()
        }
        matched_fields: set[str] = set()
        region_matches = 0
        for profile in profiles:
            for field in fields:
                if profile.get(field) not in (None, "", [], {}):
                    matched_fields.add(field)
            origin = profile.get("origin_region")
            origin_values = (
                {str(value) for value in origin}
                if isinstance(origin, Sequence)
                and not isinstance(origin, (str, bytes))
                else {str(origin)}
            )
            if regions & origin_values:
                region_matches += 1
        score = len(matched_fields) * 10 + region_matches * 25
        ranked.append(
            {
                "opening_id": str(item.get("id") or ""),
                "scene_ref": scene_ref,
                "scene_label": str(nodes[scene_ref].get("label") or ""),
                "score": score,
                "matched_background_fields": sorted(matched_fields),
            }
        )
    return sorted(
        ranked,
        key=lambda item: (-int(item["score"]), item["opening_id"]),
    )


def select_opening_scenario(
    world: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]],
    *,
    seed: int = 0,
) -> dict[str, Any]:
    """确定性选择开场。

    开场在创建副本时选择一次并冻结；重启、重放、导出导入均使用同一开场，
    禁止每次加载重新随机。评分基于已选候选的稳定引用；同分用副本创建种子
    稳定打散；无建卡数据时按种子在候选间稳定轮换。
    """

    seed = max(0, int(seed or 0))
    ranked = recommend_opening_scenarios(world, squad)
    graph = _mapping(_mapping(world.get("rules")).get("scene_graph"))
    declared = [
        dict(item)
        for item in _sequence(graph.get("opening_scenarios"))
        if isinstance(item, Mapping)
    ]
    if not ranked and not declared:
        return {
            "selected": False,
            "opening_scenario_id": "",
            "opening_scene_ref": "",
            "opening_selection_version": 1,
            "opening_selection_reasons": ["没有声明任何开场候选"],
        }
    if not ranked:
        # 无建卡数据：按种子在候选间稳定轮换。
        chosen = declared[seed % len(declared)]
        scene_ref = str(chosen.get("scene_ref") or "").strip()
        return {
            "selected": True,
            "opening_scenario_id": str(chosen.get("id") or scene_ref),
            "opening_scene_ref": scene_ref,
            "opening_selection_version": 1,
            "opening_selection_reasons": ["无建卡数据，按副本种子稳定轮换"],
        }
    top_score = int(ranked[0]["score"])
    tied = [item for item in ranked if int(item["score"]) == top_score]
    chosen = tied[seed % len(tied)]
    return {
        "selected": True,
        "opening_scenario_id": str(chosen.get("opening_id") or ""),
        "opening_scene_ref": str(chosen.get("scene_ref") or ""),
        "opening_selection_version": 1,
        "opening_selection_reasons": [
            f"队伍相关字段命中：{', '.join(chosen.get('matched_background_fields') or []) or '无'}"
        ],
    }


def _opening_scene_ref(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> str:
    """Resolve the opening scene reference without hardcoding any world."""

    runtime_data = _mapping(runtime)
    graph = _mapping(_mapping(world.get("rules")).get("scene_graph"))
    initial = _mapping(graph.get("runtime_initial_state"))
    return str(
        runtime_data.get("current_scene")
        or world.get("opening_scene_ref")
        or graph.get("entry")
        or initial.get("current_scene")
        or ""
    )


def _npc_labels(world: Mapping[str, Any]) -> dict[str, str]:
    rules = _mapping(world.get("rules"))
    module = _mapping(rules.get("npc_lifecycle"))
    npcs = module.get("npcs", module.get("npc_definitions", []))
    result: dict[str, str] = {}
    for item in _sequence(npcs):
        if not isinstance(item, Mapping) or not item.get("id"):
            continue
        result[str(item["id"])] = str(item.get("name") or item.get("label") or "")
    return result


def project_opening_scene(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """D1 开场场景语义的纯投影 helper。

    读取世界包声明的 opening 场景节点（opening_scene_ref / scene_graph.entry
    / runtime_initial_state），输出开局目标、入口 NPC、职业入口、首轮冲突、
    推荐转场与开场节奏门禁。所有字段都来自世界包声明，不硬编码具体实体。
    """

    nodes = _scene_nodes(world)
    scene_ref = _opening_scene_ref(world, runtime)
    node = nodes.get(scene_ref, {})
    npc_labels = _npc_labels(world)
    npc_refs = [
        str(item)
        for item in _sequence(node.get("npc_refs"))
        if str(item).strip()
    ]
    entry_npcs = [
        {
            "npc_ref": ref,
            "label": str(npc_labels.get(ref) or ref),
        }
        for ref in npc_refs
    ]
    declared_entries = _sequence(
        node.get("profession_entries", node.get("opening_profession_entries"))
    )
    profession_entries: list[dict[str, Any]] = []
    for item in declared_entries:
        if not isinstance(item, Mapping):
            continue
        profession_entries.append(
            {
                "profession_ref": str(item.get("profession_ref") or item.get("profession") or ""),
                "entry_ref": str(item.get("entry_ref") or item.get("action_ref") or ""),
                "label": str(item.get("label") or ""),
                "description": str(item.get("description") or ""),
                "limitations": str(item.get("limitations") or ""),
            }
        )
    if squad is not None:
        squad_professions = {
            str(member.get("profession") or member.get("profession_ref") or "")
            for member in squad
            if isinstance(member, Mapping)
        }
        for entry in profession_entries:
            entry["matched"] = bool(
                entry["profession_ref"] and entry["profession_ref"] in squad_professions
            )
    rhythm = _mapping(node.get("opening_rhythm"))
    return {
        "opening_scene_ref": scene_ref,
        "declared": bool(node),
        "label": str(node.get("label") or scene_ref),
        "summary": str(node.get("summary") or ""),
        "objectives": [
            (
                {
                    "label": str(item.get("label") or ""),
                    "description": str(item.get("description") or ""),
                }
                if isinstance(item, Mapping)
                else str(item)
            )
            for item in _sequence(node.get("objectives"))
        ],
        "entry_npcs": entry_npcs,
        "profession_entries": profession_entries,
        "first_round_conflict": bool(
            node.get("first_round_conflict")
            or rhythm.get("round_1")
        ),
        "opening_text": (
            str(_mapping(node.get("opening_text")).get("text") or "")
            if isinstance(node.get("opening_text"), Mapping)
            else str(node.get("opening_text") or world.get("opening_scene") or "")
        ),
        "action_hooks": [
            {
                "label": str(item.get("label") or ""),
                "description": str(item.get("description") or ""),
                "limitations": str(item.get("limitations") or ""),
            }
            for item in _sequence(node.get("action_hooks"))
            if isinstance(item, Mapping)
        ],
        "recommended_transitions": [
            dict(item)
            for item in _sequence(node.get("recommended_transitions"))
            if isinstance(item, Mapping)
        ],
        "stall_policy": dict(_mapping(node.get("stall_policy"))),
        "opening_rhythm": rhythm,
    }


def opening_rhythm_targets(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> dict[str, Any]:
    """D1 开场节奏门禁（第一轮冲突 / 第二轮职业入口 / 第三轮代价选择）。"""

    nodes = _scene_nodes(world)
    scene_ref = _opening_scene_ref(world, runtime)
    node = nodes.get(scene_ref, {})
    rhythm = _mapping(node.get("opening_rhythm"))
    return {
        "opening_scene_ref": scene_ref,
        "declared": bool(rhythm),
        "round_1": dict(_mapping(rhythm.get("round_1"))),
        "round_2": dict(_mapping(rhythm.get("round_2"))),
        "round_3": dict(_mapping(rhythm.get("round_3"))),
        "act_end": dict(_mapping(rhythm.get("act_end"))),
    }


def scene_entry_points(
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """当前场景为队伍提供的职业/身份相关行动入口（第二轮门禁投影）。"""

    projection = project_opening_scene(world, runtime, squad=squad)
    if projection["opening_scene_ref"] != str(runtime.get("current_scene") or ""):
        return []
    return list(projection["profession_entries"])


def validate_opening_contract(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the authoritative opening contract and scene graph."""

    from .opening_contract import opening_contract_issues

    strict = opening_contract_issues(world)
    if strict:
        return [item.export() for item in strict]

    graph = _mapping(_mapping(world.get("rules")).get("scene_graph"))
    nodes = _scene_nodes(world)
    problems: list[dict[str, Any]] = []
    for order, raw in enumerate(_sequence(graph.get("opening_scenarios"))):
        if not isinstance(raw, Mapping):
            continue
        scene_ref = str(raw.get("scene_ref") or "").strip()
        opening_id = str(raw.get("id") or f"opening:{scene_ref or order}")
        if scene_ref not in nodes:
            problems.append(
                {
                    "code": "opening_contract.missing_scene",
                    "opening_id": opening_id,
                    "message": f"开场战役 {opening_id} 引用了未注册场景：{scene_ref or '（空）'}",
                }
            )
            continue
        node = nodes[scene_ref]
        if not bool(node.get("first_round_conflict")) and not bool(
            _mapping(node.get("opening_rhythm")).get("round_1")
        ):
            problems.append(
                {
                    "code": "opening_contract.round1_missing",
                    "opening_id": opening_id,
                    "scene_ref": scene_ref,
                    "message": f"开场场景「{node.get('label') or scene_ref}」没有声明第一轮可执行冲突。",
                }
            )
        entries = _sequence(
            node.get("profession_entries", node.get("opening_profession_entries"))
        )
        if not entries:
            problems.append(
                {
                    "code": "opening_contract.profession_entries_missing",
                    "opening_id": opening_id,
                    "scene_ref": scene_ref,
                    "message": f"开场场景「{node.get('label') or scene_ref}」没有声明职业行动入口。",
                }
            )
    return problems


def opening_choices_projection(
    world: Mapping[str, Any],
    squad: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """开局选项投影：只暴露世界声明字段，不泄漏内部引用。"""

    rules = _mapping(world.get("rules"))
    choices = _sequence(rules.get("opening_choices"))
    squad_professions = {
        str(member.get("profession") or member.get("profession_ref") or "")
        for member in squad
        if isinstance(member, Mapping)
    }
    projected: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        item: dict[str, Any] = {
            "key": str(choice.get("key") or ""),
            "text": str(choice.get("text") or ""),
            "risk": str(choice.get("risk") or ""),
            "requires_check": bool(choice.get("requires_check", False)),
            "collective": bool(choice.get("collective", False)),
        }
        professions = _sequence(
            choice.get("profession_refs", choice.get("professions"))
        )
        if professions:
            item["profession_refs"] = [str(value) for value in professions]
            item["matched"] = bool(set(professions) & squad_professions)
        projected.append(item)
    return {
        "declared": bool(projected),
        "choices": projected,
    }


__all__ = [
    "build_story_condition_context",
    "evaluate_story_condition_detail",
    "evaluate_story_condition",
    "opening_choices_projection",
    "opening_rhythm_targets",
    "project_opening_scene",
    "recommended_transition",
    "recommend_opening_scenarios",
    "scene_entry_points",
    "scene_transition_blockers",
    "select_opening_scenario",
    "validate_opening_contract",
]
