"""Deterministic RC10 objective-zone tactical state machine and receipts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from .tactical_capability_runtime import (
    _apply_capability_elemental,
    _capability_execution,
    _validate_frozen_item_use,
)
from .tactical_choices import (
    draft_from_text,
    prepare_draft,
    validate_draft,
)
from .tactical_resolution import _action_check_context
from .tactical_support import (
    ACTION_KINDS,
    _choice,
    _clone,
    _find,
    _objective_rows,
    _sequence,
    _threat_rows,
    _zone_label,
)

MAJOR_ACTIONS = frozenset({"strike", "guard", "cast", "interact", "aid", "parley"})
MANEUVER_ACTIONS = frozenset({"maneuver", "retreat"})
TERMINAL_PHASES = frozenset(
    {
        "victory", "partial_success", "retreat", "negotiated",
        "defeat_forward", "aborted_by_host",
    }
)
PHASE_TRANSITIONS = {
    "setup": "declare",
    "declare": "locked",
    "locked": "resolve_players",
    "resolve_players": "resolve_opposition",
    "resolve_opposition": "environment",
    "environment": "settle_round",
    "settle_round": "declare",
}
RESULT_BANDS = frozenset({"critical", "success", "success_with_cost", "failure_forward"})

def _input_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

def _receipts(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in state.get("locked_receipts") or () if isinstance(item, Mapping)]

def _prior_receipt(state: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    return next((item for item in _receipts(state) if item.get("idempotency_key") == key), None)

def _restore_replay_side_effects(current: dict[str, Any], prior: Mapping[str, Any]) -> None:
    events = [dict(item) for item in prior.get("semantic_events") or () if isinstance(item, Mapping)]
    effects = [dict(item) for item in prior.get("effect_updates") or () if isinstance(item, Mapping)]
    item_updates = [
        dict(item)
        for item in prior.get("item_instance_updates") or ()
        if isinstance(item, Mapping)
    ]
    resource_updates = [
        dict(item)
        for item in prior.get("character_resource_updates") or ()
        if isinstance(item, Mapping)
    ]
    runtime_effect_updates = [
        dict(item)
        for item in prior.get("runtime_effect_instance_updates") or ()
        if isinstance(item, Mapping)
    ]
    if events:
        current["_semantic_events"] = events
    if effects:
        current["_effect_updates"] = effects
    if item_updates:
        current["_item_instance_updates"] = item_updates
    if resource_updates:
        current["_character_resource_updates"] = resource_updates
    if runtime_effect_updates:
        current["_runtime_effect_instance_updates"] = runtime_effect_updates

def _event(
    kind: str,
    label: str,
    summary: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "summary": summary,
        "visibility": "party",
        "details": dict(details or {}),
    }

def _outcome_effects(state: Mapping[str, Any], outcome: str) -> list[dict[str, Any]]:
    source = state.get("outcome_effects") or []
    if isinstance(source, Mapping):
        alias = {"victory": "success"}.get(outcome, outcome)
        source = source.get(outcome) or source.get(alias) or source.get("default") or []
    return [dict(item) for item in source if isinstance(item, Mapping)][:16]

def preview(state: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    current = dict(state)
    if str(current.get("phase") or "setup") != "declare":
        raise ValueError("当前阶段不能修改行动；系统保留草稿，请等待下一次声明阶段")
    normalized = prepare_draft(current, draft)
    actor = dict((current.get("participants") or {}).get(normalized["actor_key"]) or {})
    budget = dict(actor.get("action_budget") or {})
    cost_kind = "maneuver" if normalized["action_kind"] in MANEUVER_ACTIONS else "major"
    if int(budget.get(cost_kind) or 0) < 1:
        raise ValueError(f"本轮 {cost_kind} 行动额度已经用完")
    known = {
        "strike": "锁定后检定，成功时削减已知威胁的守势或意志",
        "guard": "锁定后提高本人或队友的一次公开守势",
        "maneuver": "锁定后移动到已选择且可达的区域",
        "cast": "锁定后按已授权能力或物品的 effect 结算",
        "interact": "锁定后推进已选择的公开目标",
        "aid": "锁定后为已选择的队友提供守势或救援",
        "retreat": "锁定后检定撤退；失败仍保留已取得成果并推进代价",
        "parley": "锁定后以证据或担保削减威胁意志，不自动说服",
    }[normalized["action_kind"]]
    return {
        "draft": normalized,
        "known_effects": [known, "当前步骤只保存 pending intent，不掷骰、不扣除最终额度"],
        "cost": {cost_kind: 1},
        "boundary": "预览不写状态；主持人锁定后才生成骰值、难度、效果和回执。",
    }

def commit(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("提交战术行动需要防重复凭证")
    normalized = prepare_draft(current, draft)
    digest = _input_hash(normalized)
    prior = _prior_receipt(current, key)
    if prior is not None:
        if prior.get("input_sha256") != digest or prior.get("intent") != "tactical.action.commit":
            raise ValueError("相同防重复凭证已用于另一项战术行动")
        _restore_replay_side_effects(current, prior)
        return current, {**prior, "replayed": True}

    checked = preview(current, normalized)
    normalized = dict(checked["draft"])

    pending = dict(current.get("pending_intents") or {})
    replaced = normalized["actor_key"] in pending
    pending[normalized["actor_key"]] = normalized
    current["pending_intents"] = pending
    kind = "intent_replaced" if replaced else "intent_submitted"
    events = [
        _event(
            kind,
            "玩家战术意图已更新" if replaced else "玩家战术意图已提交",
            "行动只进入待锁定区；尚未生成骰值或最终效果。",
            details={"actor": normalized["actor_key"], "action_kind": normalized["action_kind"]},
        )
    ]
    receipt = {
        "receipt_id": "tactical_" + hashlib.sha256(f"{key}:{digest}".encode()).hexdigest()[:20],
        "idempotency_key": key,
        "input_sha256": digest,
        "request_sha256": digest,
        "intent": "tactical.action.commit",
        "actor_key": normalized["actor_key"],
        "action_kind": normalized["action_kind"],
        "result_band": "pending",
        "outcome": "active",
        "effects": ["行动已保存，等待主持人锁定"],
        "replaced": replaced,
        "semantic_events": events,
        "replayed": False,
    }
    receipts = _receipts(current)
    receipts.append(receipt)
    current["locked_receipts"] = receipts[-200:]
    current["_semantic_events"] = events
    return current, receipt

def _resolve_intent(
    current: dict[str, Any],
    draft: Mapping[str, Any],
    *,
    lock_key: str,
) -> dict[str, Any]:
    normalized = prepare_draft(current, draft)
    digest = _input_hash(normalized)
    actor = dict((current.get("participants") or {}).get(normalized["actor_key"]) or {})
    choice_kind, choice = _choice(current, normalized["capability_or_item_ref"])
    choice_state = dict(choice.get("instance_state") or choice.get("state") or {})
    check_context = _action_check_context(
        current,
        actor,
        normalized["action_kind"],
        choice_kind,
        choice,
        choice_state,
    )
    modifier = int(check_context["modifier"])
    advantage_sources = list(check_context["advantage_sources"])
    disadvantage_sources = list(check_context["disadvantage_sources"])
    rolls = [
        int(hashlib.sha256(f"{lock_key}:{digest}:{index}".encode()).hexdigest()[:8], 16) % 20 + 1
        for index in (1, 2)
    ]
    advantage_mode = (
        "advantage" if advantage_sources and not disadvantage_sources
        else "disadvantage" if disadvantage_sources and not advantage_sources
        else "normal"
    )
    roll = max(rolls) if advantage_mode == "advantage" else min(rolls) if advantage_mode == "disadvantage" else rolls[0]
    environment_delta = sum(
        int(dict(item).get("difficulty_delta") or 0)
        for item in _sequence(current.get("environment"))
        if isinstance(item, Mapping)
    )
    difficulty = max(5, min(25, int(current.get("difficulty") or 10) + environment_delta))
    total = roll + modifier
    band = (
        "critical" if roll == 20
        else "success" if total >= difficulty + 3
        else "success_with_cost" if total >= difficulty
        else "failure_forward"
    )
    cost_kind = "maneuver" if normalized["action_kind"] in MANEUVER_ACTIONS else "major"
    budget = dict(actor.get("action_budget") or {})
    if int(budget.get(cost_kind) or 0) < 1:
        raise ValueError(f"锁定时 {normalized['actor_key']} 的 {cost_kind} 额度已经不足")
    budget[cost_kind] = int(budget.get(cost_kind) or 0) - 1
    actor["action_budget"] = budget
    participants = dict(current.get("participants") or {})
    participants[normalized["actor_key"]] = actor
    current["participants"] = participants

    success = band in {"critical", "success", "success_with_cost"}
    amount = 2 if band == "critical" else 1 if success else 0
    changes: list[str] = []
    action = normalized["action_kind"]
    objectives = _objective_rows(current)
    threats = _threat_rows(current)
    if action == "interact":
        objective = _find(objectives, normalized["objective_ref"], "id", "objective_id", "objective_ref", "ref")
        if objective is None:
            raise ValueError("锁定的公开目标已失效")
        before = max(0, int(objective.get("progress") or 0))
        threshold = max(1, int(objective.get("success_threshold") or objective.get("target") or 3))
        objective["progress"] = min(threshold, before + amount)
        changes.append(f"目标进度 {before} → {objective['progress']}")
    elif action in {"strike", "parley"}:
        target = _find(threats, normalized["target_refs"][0], "threat_id", "id", "ref")
        if target is None:
            raise ValueError("锁定的威胁目标已失效")
        field = "resolve" if action == "parley" else "guard"
        before = max(0, int(target.get(field) or 0))
        target[field] = max(0, before - amount)
        field_label = "意志" if field == "resolve" else "守势"
        changes.append(f"{target.get('label') or '威胁'}{field_label} {before} → {target[field]}")
    elif action in {"guard", "aid"}:
        target_key = normalized["target_refs"][0]
        target = dict(participants.get(target_key) or {})
        before = max(0, int(target.get("guard") or 0))
        target["guard"] = before + amount
        participants[target_key] = target
        current["participants"] = participants
        changes.append(f"{target.get('label') or '队伍成员'}守势 {before} → {target['guard']}")
    elif action in {"maneuver", "retreat"}:
        before = str(actor.get("zone_ref") or "")
        if success:
            actor["zone_ref"] = normalized["zone_ref"]
            participants[normalized["actor_key"]] = actor
            current["participants"] = participants
        after = str(actor.get("zone_ref") or before or "")
        changes.append(f"区域 {_zone_label(current, before)} → {_zone_label(current, after)}")
    elif action == "cast":
        objective = _find(objectives, normalized["objective_ref"], "id", "objective_id", "objective_ref", "ref") if normalized["objective_ref"] else None
        if objective and success:
            before = max(0, int(objective.get("progress") or 0))
            objective["progress"] = before + amount
            changes.append(f"能力推进目标 {before} → {objective['progress']}")
        else:
            changes.append("能力或物品按冻结定义结算，未创造未声明目标")
        if choice_kind == "capability":
            described = str(choice_state.get("effect") or choice_state.get("summary") or "").strip()
            if described:
                changes.append(described[:240])

    if choice_kind == "item":
        items = [
            dict(item)
            for item in _sequence(current.get("available_items"))
            if isinstance(item, Mapping)
        ]
        selected = _find(
            items,
            normalized["capability_or_item_ref"],
            "id",
            "item_id",
            "ref",
        )
        if selected is None:
            raise ValueError("锁定的装备已失效")
        use_effect, durability_cost, charges_cost = _validate_frozen_item_use(
            selected,
            action,
        )
        durability_before = max(0, int(selected.get("durability") or 0))
        charges_before = max(0, int(selected.get("charges") or 0))
        quantity_before = int(selected["quantity"])
        durability_after = durability_before - durability_cost
        charges_after = charges_before - charges_cost
        selected["durability"] = durability_after
        selected["charges"] = charges_after
        current["available_items"] = items
        label = str(selected.get("label") or "装备").strip()[:120]
        if durability_cost:
            changes.append(f"{label}耐久 {durability_before} → {durability_after}")
        if charges_cost:
            changes.append(f"{label}充能 {charges_before} → {charges_after}")
        if durability_cost or charges_cost:
            current.setdefault("_item_instance_updates", []).append(
                {
                    "instance_id": str(selected["instance_id"]),
                    "owner_type": str(selected["instance_owner_type"]),
                    "owner_ref": str(selected["instance_owner_ref"]),
                    "item_id": str(selected["item_id"]),
                    "quantity_before": quantity_before,
                    "quantity_after": quantity_before,
                    "durability_before": durability_before,
                    "durability_after": durability_after,
                    "charges_before": charges_before,
                    "charges_after": charges_after,
                }
            )
        described = str(
            use_effect.get("effect")
            if success
            else use_effect.get("failure_effect") or ""
        ).strip()
        if described:
            changes.append(described[:240])

    capability_execution: dict[str, Any] = {}
    elemental_result: dict[str, Any] = {}
    if choice_kind == "capability":
        capability_execution = _capability_execution(
            current,
            normalized,
            choice,
            success=success,
            lock_key=lock_key,
        )
        resource_updates = [
            dict(item) for item in capability_execution["resource_updates"]
        ]
        runtime_effect_updates = [
            dict(item) for item in capability_execution["effect_updates"]
        ]
        if resource_updates:
            current.setdefault("_character_resource_updates", []).extend(
                resource_updates
            )
        if runtime_effect_updates:
            current.setdefault("_runtime_effect_instance_updates", []).extend(
                runtime_effect_updates
            )
        elemental_result = _apply_capability_elemental(
            current,
            capability_execution["elemental_contract"],
            success=success,
        )
        capability_label = str(capability_execution["label"])
        if capability_execution["mode"] == "narrative_only":
            changes.append(
                f"〈{capability_label}〉仅记录叙事声明；能力本身未产生成本或额外状态变化"
            )
        else:
            if resource_updates:
                changes.append(
                    f"〈{capability_label}〉已按作者定义结算 {len(resource_updates)} 项角色资源变化"
                )
            if runtime_effect_updates:
                changes.append(
                    f"〈{capability_label}〉已按作者定义结算 {len(runtime_effect_updates)} 项状态效果"
                )
            if elemental_result:
                changes.append(
                    f"〈{capability_label}〉对「{elemental_result['target_label']}」施加"
                    f"{elemental_result['element_ref']}暴露 "
                    f"{elemental_result['layers_before']} → {elemental_result['layers_after']}"
                )
                if elemental_result["matched_reaction"]:
                    changes.append(
                        f"元素反应：{elemental_result['matched_reaction']}"
                    )
                if elemental_result["matched_interaction"]:
                    changes.append(
                        f"元素交互：{elemental_result['matched_interaction']}"
                    )
                if elemental_result["public_copy"]:
                    changes.append(elemental_result["public_copy"])
        if success:
            changes.extend(
                f"〈{capability_label}〉：{text}"
                for text in capability_execution["narrative"]
            )
        elif capability_execution["failure_forward"]:
            changes.append(
                f"〈{capability_label}〉失败推进：{capability_execution['failure_forward']}"
            )

    if not success:
        failure = current.setdefault("failure_events", [])
        forward = current.get("failure_forward") or "行动未直接成功；开放替代选择并推进公开风险。"
        if isinstance(forward, list):
            forward = "；".join(str(item) for item in forward)
        failure.append(str(forward)[:300])
        current["failure_events"] = failure[-24:]
        changes.append("已应用失败推进")

    outcome = "active"
    if action == "retreat" and success:
        outcome = "retreat"
    elif action == "parley" and success and all(int(item.get("resolve") or 0) <= 0 for item in threats):
        outcome = "negotiated"
    if outcome != "active":
        current["phase"] = outcome
        current["status"] = outcome
        current.setdefault("outcome_axes", {})["final"] = outcome
        current["_effect_updates"] = _outcome_effects(current, outcome)

    result = {
        "receipt_id": "tactical_roll_" + hashlib.sha256(f"{lock_key}:{digest}".encode()).hexdigest()[:20],
        "input_sha256": digest,
        "actor_key": normalized["actor_key"],
        "action_kind": action,
        "roll": roll,
        "rolls": rolls if advantage_mode != "normal" else [roll],
        "advantage": advantage_mode,
        "advantage_source_count": len(advantage_sources),
        "disadvantage_source_count": len(disadvantage_sources),
        "modifier": modifier,
        "stat_ref": str(check_context["stat_ref"]),
        "stat_label": str(check_context["stat_label"]),
        "difficulty": difficulty,
        "total": total,
        "result_band": band,
        "outcome": outcome,
        "effects": changes,
        "draft": normalized,
    }
    if capability_execution:
        result["capability_mode"] = capability_execution["mode"]
        result["capability_mechanical_change"] = bool(
            capability_execution["mechanical"]
        )
        result["capability_resource_change_count"] = len(
            capability_execution["resource_updates"]
        )
        result["capability_effect_change_count"] = len(
            capability_execution["effect_updates"]
        )
        if elemental_result:
            result.update({
                "element_ref": elemental_result["element_ref"],
                "element_target_label": elemental_result["target_label"],
                "element_layers_before": elemental_result["layers_before"],
                "element_layers_after": elemental_result["layers_after"],
                "element_matched_reaction": elemental_result["matched_reaction"],
                "element_matched_interaction": elemental_result["matched_interaction"],
                "element_public_copy": elemental_result["public_copy"],
                "element_settlement_order": elemental_result["settlement_order"],
            })
    return result

def _host_request_hash(intent: str, payload: Mapping[str, Any]) -> str:
    return _input_hash({"intent": intent, **dict(payload)})

def _host_replay(
    current: dict[str, Any],
    *,
    key: str,
    intent: str,
    request_hash: str,
) -> dict[str, Any] | None:
    prior = _prior_receipt(current, key)
    if prior is None:
        return None
    if prior.get("intent") != intent or prior.get("request_sha256") != request_hash:
        raise ValueError("相同防重复凭证已用于另一项主持战术操作")
    _restore_replay_side_effects(current, prior)
    return {**prior, "replayed": True}

def _append_host_receipt(
    current: dict[str, Any],
    *,
    key: str,
    intent: str,
    request_hash: str,
    changes: Mapping[str, Any],
    reason: str,
    events: list[dict[str, Any]],
    effects: list[dict[str, Any]] | None = None,
    item_instance_updates: list[dict[str, Any]] | None = None,
    character_resource_updates: list[dict[str, Any]] | None = None,
    runtime_effect_instance_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    receipt = {
        "receipt_id": "tactical_" + hashlib.sha256(f"{key}:{request_hash}".encode()).hexdigest()[:20],
        "idempotency_key": key,
        "input_sha256": request_hash,
        "request_sha256": request_hash,
        "intent": intent,
        "changes": dict(changes),
        "reason": str(reason or "").strip()[:500],
        "semantic_events": events,
        "effect_updates": list(effects or []),
        "replayed": False,
    }
    if item_instance_updates:
        receipt["item_instance_updates"] = [
            dict(item) for item in item_instance_updates
        ]
    if character_resource_updates:
        receipt["character_resource_updates"] = [
            dict(item) for item in character_resource_updates
        ]
    if runtime_effect_instance_updates:
        receipt["runtime_effect_instance_updates"] = [
            dict(item) for item in runtime_effect_instance_updates
        ]
    receipts = _receipts(current)
    receipts.append(receipt)
    current["locked_receipts"] = receipts[-200:]
    if events:
        current["_semantic_events"] = events
    if effects:
        current["_effect_updates"] = list(effects)
    return receipt

def _prepare_crisis_environment_effects(
    current: dict[str, Any],
) -> tuple[list[dict[str, Any]], str, list[str]]:
    crises = [
        dict(item)
        for item in _sequence(current.get("available_crises"))
        if isinstance(item, Mapping)
    ]
    if not crises:
        current["active_crisis"] = {}
        return [], "", []
    step_before = max(0, int(current.get("environment_steps") or 0))
    selected_index = step_before % len(crises)
    selected = crises[selected_index]
    label = str(selected.get("label") or "地区危机").strip()[:120]
    raw_effects = selected.get("environment_effects") or []
    if not isinstance(raw_effects, list) or len(raw_effects) > 16:
        raise ValueError("地区危机 environment_effects 合同无效")
    crisis_revision_policy = str(selected.get("revision_policy") or "").strip()
    if crisis_revision_policy and crisis_revision_policy != "bind_current_then_advance":
        raise ValueError("地区危机 revision 策略与执行合同冲突")
    intensity = current.get("intensity") or {}
    if not isinstance(intensity, Mapping):
        raise ValueError("冻结冒险强度合同无效")
    mainline_policy = str(intensity.get("mainline_policy") or "preserve")
    if mainline_policy != "preserve":
        raise ValueError("战术环境结算只支持 preserve 主线策略")
    raw_clock_delta = intensity.get("clock_delta", 0)
    if isinstance(raw_clock_delta, bool) or not isinstance(raw_clock_delta, int):
        raise ValueError("冻结冒险强度 clock_delta 无效")
    if not -100 <= raw_clock_delta <= 100:
        raise ValueError("冻结冒险强度 clock_delta 超出安全范围")

    plans: list[dict[str, Any]] = []
    next_effects: list[dict[str, Any]] = []
    affected_labels: list[str] = []
    for raw in raw_effects:
        if not isinstance(raw, Mapping):
            raise ValueError("地区危机 environment_effect 必须是对象")
        effect = _clone(raw)
        effect_revision_policy = str(effect.get("revision_policy") or "").strip()
        if (
            effect_revision_policy
            and crisis_revision_policy
            and effect_revision_policy != crisis_revision_policy
        ):
            raise ValueError("地区危机 effect revision 策略与危机合同冲突")
        resolved_revision_policy = effect_revision_policy or crisis_revision_policy
        if resolved_revision_policy != "bind_current_then_advance":
            raise ValueError("地区危机 effect 缺少 bind_current_then_advance revision 策略")
        expected = effect.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("地区危机 effect 缺少冻结 expected_revision")
        state = effect.get("state")
        if not isinstance(state, Mapping):
            raise ValueError("地区危机 effect 缺少明确目标状态")
        state = dict(state)
        if str(effect.get("module_id") or "") == "time_clock" and raw_clock_delta:
            raw_delta = state.get("delta", 0)
            if isinstance(raw_delta, bool) or not isinstance(raw_delta, int):
                raise ValueError("地区危机 time_clock delta 无效")
            state["delta"] = raw_delta + raw_clock_delta
        effect["state"] = state
        plans.append(effect)
        next_effect = _clone(raw)
        next_effect["expected_revision"] = expected + 1
        next_effects.append(next_effect)
        affected_labels.append(
            str(effect.get("label") or "关联状态已更新").strip()[:120]
        )
    selected["environment_effects"] = next_effects
    crises[selected_index] = selected
    current["available_crises"] = crises
    current["active_crisis"] = selected
    current["environment_steps"] = step_before + 1
    return plans, label, affected_labels

def advance_phase(
    state: Mapping[str, Any],
    *,
    idempotency_key: str,
    reason: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("主持战术推进需要防重复凭证")
    explanation = str(reason or "").strip()[:300]
    request_hash = _host_request_hash("tactical.phase.advance", {"reason": explanation})
    replay = _host_replay(
        current, key=key, intent="tactical.phase.advance", request_hash=request_hash,
    )
    if replay is not None:
        return current, replay

    phase_before = str(current.get("phase") or "setup")
    if phase_before in TERMINAL_PHASES:
        raise ValueError("战术冲突已经结束，不能继续推进阶段")
    phase_after = PHASE_TRANSITIONS.get(phase_before)
    if not phase_after:
        raise ValueError("当前战术阶段没有已注册的推进路径")
    round_before = max(1, int(current.get("round") or 1))
    round_after = round_before + 1 if phase_before == "settle_round" else round_before
    events = [
        _event(
            "phase_changed",
            "战术阶段已推进",
            f"阶段由 {phase_before} 推进到 {phase_after}。",
            details={"phase_before": phase_before, "phase_after": phase_after, "round": round_after},
        )
    ]
    effect_updates: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    environment_crisis_label = ""
    environment_affected_labels: list[str] = []

    if phase_before == "declare":
        pending = dict(current.get("pending_intents") or {})
        for actor_key in sorted((current.get("participants") or {})):
            if actor_key not in pending:
                pending[actor_key] = prepare_draft(
                    current,
                    {"actor_key": actor_key, "action_kind": "guard", "target_refs": [actor_key], "description": "未提交时使用预设防守"},
                )
        current["pending_intents"] = pending
        current["locked_intents"] = _clone(pending)
        events.append(_event("round_locked", "本轮行动已锁定", "玩家意图和未提交者的预设防守已冻结。", details={"intent_count": len(pending)}))
    elif phase_before == "locked":
        locked = dict(current.get("locked_intents") or {})
        for actor_key in sorted(locked):
            resolved.append(_resolve_intent(current, dict(locked[actor_key]), lock_key=f"{key}:{actor_key}"))
            if str(current.get("phase")) in TERMINAL_PHASES:
                break
        current.setdefault("resolved_receipts", []).extend(resolved)
        current["resolved_receipts"] = current["resolved_receipts"][-200:]
        events.append(_event("roll_locked", "玩家行动检定已锁定", "骰值、难度、结果带和实际变化已写入回执。", details={"resolved": len(resolved)}))
        events.extend(
            _event("effect_applied", "玩家行动效果已应用", "；".join(item.get("effects") or ())[:300], details={"actor": item.get("actor_key", ""), "result_band": item.get("result_band", "")})
            for item in resolved
        )
        if any(any("目标进度" in text for text in item.get("effects") or ()) for item in resolved):
            events.append(_event("objective_changed", "公开目标已更新", "至少一项行动改变了公开目标进度。"))
        if str(current.get("phase")) in TERMINAL_PHASES:
            phase_after = str(current["phase"])
            effect_updates = list(current.get("_effect_updates") or [])
    elif phase_before == "resolve_players":
        threats = _threat_rows(current)
        participants = dict(current.get("participants") or {})
        active_threats = [item for item in threats if int(item.get("guard") or 0) > 0 or int(item.get("resolve") or 0) > 0]
        if active_threats and participants:
            target_key = sorted(participants)[round_before % len(participants)]
            actor = dict(participants[target_key] or {})
            before = max(0, int(actor.get("guard") or 0))
            if before:
                actor["guard"] = before - 1
            else:
                conditions = [str(item) for item in actor.get("conditions") or []]
                if "受压" not in conditions:
                    conditions.append("受压")
                actor["conditions"] = conditions[-8:]
            participants[target_key] = actor
            current["participants"] = participants
            events.append(_event("reaction_triggered", "已预告威胁作出反应", "威胁只按公开 telegraph 施加守势损失或受压条件。", details={"actor": target_key}))
    elif phase_before == "resolve_opposition":
        (
            effect_updates,
            environment_crisis_label,
            environment_affected_labels,
        ) = _prepare_crisis_environment_effects(current)
        crisis = dict(current.get("active_crisis") or {})
        summary = str(crisis.get("failure_forward") or "环境与地区时钟按公开风险推进。")
        events.append(_event(
            "environment_advanced",
            "环境与危机已推进",
            summary,
            details={
                "crisis_label": environment_crisis_label,
                "affected_labels": "；".join(environment_affected_labels)[:300],
            },
        ))
    elif phase_before == "environment":
        objectives = _objective_rows(current)
        if objectives and all(
            int(item.get("progress") or 0) >= int(item.get("success_threshold") or item.get("target") or 1)
            for item in objectives
        ):
            phase_after = "victory"
            current["status"] = "victory"
            current.setdefault("outcome_axes", {})["final"] = "victory"
            effect_updates = _outcome_effects(current, "success") or _outcome_effects(current, "victory")
            events.append(_event("conflict_ended", "战术冲突目标已达成", "所有公开目标完成，终局 effects 将在同一事务中提交。", details={"outcome": "victory"}))
    elif phase_before == "settle_round":
        for actor_key, raw_actor in list((current.get("participants") or {}).items()):
            actor = dict(raw_actor or {})
            actor["action_budget"] = {"major": 1, "maneuver": 1, "reaction": 1}
            current["participants"][actor_key] = actor
        current["pending_intents"] = {}
        current["locked_intents"] = {}

    current["phase"] = phase_after
    current["round"] = round_after
    changes = {
        "phase_before": phase_before,
        "phase_after": phase_after,
        "round_before": round_before,
        "round_after": round_after,
        "resolved": len(resolved),
    }
    if environment_crisis_label:
        changes["crisis_label"] = environment_crisis_label
        changes["affected_labels"] = environment_affected_labels
    receipt = _append_host_receipt(
        current,
        key=key,
        intent="tactical.phase.advance",
        request_hash=request_hash,
        changes=changes,
        reason=explanation,
        events=events,
        effects=effect_updates,
        item_instance_updates=[
            dict(item)
            for item in current.get("_item_instance_updates") or ()
            if isinstance(item, Mapping)
        ],
        character_resource_updates=[
            dict(item)
            for item in current.get("_character_resource_updates") or ()
            if isinstance(item, Mapping)
        ],
        runtime_effect_instance_updates=[
            dict(item)
            for item in current.get("_runtime_effect_instance_updates") or ()
            if isinstance(item, Mapping)
        ],
    )
    receipt["resolved_receipts"] = resolved
    current["locked_receipts"][-1] = receipt
    return current, receipt

def apply_correction(
    state: Mapping[str, Any],
    correction: Mapping[str, Any],
    *,
    idempotency_key: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    explanation = str(reason or "").strip()[:500]
    key = str(idempotency_key or "").strip()
    if not key or not explanation:
        raise ValueError("战术纠错必须说明可审计原因并提供防重复凭证")
    field = str(correction.get("field") or "").strip()
    allowed = {"objective", "telegraphs", "environment", "escape_routes", "negotiation_options"}
    if field not in allowed:
        raise ValueError("该战术字段不能通过纠错入口修改")
    after = correction.get("value")
    request_hash = _host_request_hash(
        "tactical.correction.apply", {"field": field, "value": after, "reason": explanation},
    )
    replay = _host_replay(
        current, key=key, intent="tactical.correction.apply", request_hash=request_hash,
    )
    if replay is not None:
        return current, replay
    if str(current.get("phase") or "") in TERMINAL_PHASES:
        raise ValueError("战术冲突已经结束，不能再修改公开战况")
    before = current.get(field)
    current[field] = after
    events = [_event("correction_applied", "战术纠错已追加", "纠错保留原值、修订值和主持人原因。", details={"field": field})]
    receipt = _append_host_receipt(
        current,
        key=key,
        intent="tactical.correction.apply",
        request_hash=request_hash,
        changes={"field": field, "before": before, "after": after},
        reason=explanation,
        events=events,
    )
    return current, receipt

def end_conflict(
    state: Mapping[str, Any],
    *,
    outcome: str,
    idempotency_key: str,
    reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    terminal = str(outcome or "").strip()
    explanation = str(reason or "").strip()[:500]
    key = str(idempotency_key or "").strip()
    if terminal not in TERMINAL_PHASES:
        raise ValueError("战术结束结果未注册")
    if not explanation or not key:
        raise ValueError("结束战术冲突必须说明结果依据并提供防重复凭证")
    request_hash = _host_request_hash(
        "tactical.conflict.end", {"outcome": terminal, "reason": explanation},
    )
    replay = _host_replay(
        current, key=key, intent="tactical.conflict.end", request_hash=request_hash,
    )
    if replay is not None:
        return current, replay
    phase_before = str(current.get("phase") or "setup")
    if phase_before in TERMINAL_PHASES:
        raise ValueError("战术冲突已经结束，不能用新凭证改写结果")
    current["phase"] = terminal
    current["status"] = terminal
    current.setdefault("outcome_axes", {})["final"] = terminal
    effects = _outcome_effects(current, terminal)
    events = [_event("conflict_ended", "战术冲突已结束", explanation, details={"outcome": terminal, "phase_before": phase_before})]
    receipt = _append_host_receipt(
        current,
        key=key,
        intent="tactical.conflict.end",
        request_hash=request_hash,
        changes={"phase_before": phase_before, "outcome": terminal},
        reason=explanation,
        events=events,
        effects=effects,
    )
    receipt["outcome"] = terminal
    receipt["effects"] = [str(item.get("label") or "关联状态已更新")[:120] for item in effects]
    current["locked_receipts"][-1] = receipt
    return current, receipt

__all__ = [
    "ACTION_KINDS", "PHASE_TRANSITIONS", "RESULT_BANDS", "TERMINAL_PHASES",
    "advance_phase", "apply_correction", "commit", "draft_from_text",
    "end_conflict", "prepare_draft", "preview", "validate_draft",
]
