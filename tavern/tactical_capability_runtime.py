"""Strict frozen item, capability, resource, effect, and elemental helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
from typing import Any

from .elemental import parse as parse_elemental
from .elemental import resolve as resolve_elemental
from .tactical_support import (
    ACTION_KINDS,
    _clone,
    _find,
    _sequence,
    _state_refs,
)

def _item_use_effect(item: Mapping[str, Any], action_kind: str) -> dict[str, Any]:
    effect = next(
        (
            dict(raw)
            for raw in _sequence(item.get("use_effects"))
            if isinstance(raw, Mapping)
            and str(raw.get("kind") or "").strip() == action_kind
        ),
        {},
    )
    if not effect:
        raise ValueError("所选装备不支持当前战术行动")
    return effect

def _item_resource_cost(effect: Mapping[str, Any]) -> tuple[int, int]:
    cost = effect.get("cost") or {}
    if not isinstance(cost, Mapping):
        raise ValueError("所选装备的资源消耗定义无效")
    values: list[int] = []
    for field in ("durability", "charges"):
        raw = cost.get(field, 0)
        if isinstance(raw, bool):
            raise ValueError("所选装备的资源消耗定义无效")
        try:
            value = int(raw or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("所选装备的资源消耗定义无效") from exc
        if value < 0 or value > 10000:
            raise ValueError("所选装备的资源消耗定义无效")
        values.append(value)
    return values[0], values[1]

def _validate_frozen_item_use(
    item: Mapping[str, Any],
    action_kind: str,
) -> tuple[dict[str, Any], int, int]:
    limits = {
        "instance_id": 160,
        "instance_owner_type": 40,
        "instance_owner_ref": 128,
        "item_id": 128,
    }
    frozen: dict[str, str] = {}
    for field, limit in limits.items():
        token = str(item.get(field) or "").strip()
        if not token or len(token) > limit:
            raise ValueError("所选装备缺少冻结实例信息，请刷新战况后重新选择")
        frozen[field] = token
    if frozen["instance_owner_type"] not in {"character", "party", "actor"}:
        raise ValueError("所选装备的冻结所有者无效，请刷新战况后重新选择")
    quantity = item.get("quantity")
    if (
        isinstance(quantity, bool) or not isinstance(quantity, int)
        or not 1 <= quantity <= 1_000_000
    ):
        raise ValueError("所选装备的冻结数量无效，请刷新战况后重新选择")
    effect = _item_use_effect(item, action_kind)
    durability_cost, charges_cost = _item_resource_cost(effect)
    resources: list[int] = []
    for field in ("durability", "charges"):
        raw = item.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 10000:
            raise ValueError("所选装备的冻结耐久或充能无效，请刷新战况后重新选择")
        resources.append(raw)
    durability, charges = resources
    if durability < durability_cost:
        raise ValueError("锁定的装备耐久不足")
    if charges < charges_cost:
        raise ValueError("锁定的装备充能不足")
    return effect, durability_cost, charges_cost

def _capability_targets(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
) -> list[dict[str, str]]:
    refs = _state_refs(state)
    participants = state.get("participants") or {}
    targets: list[dict[str, str]] = []
    for ref in draft.get("target_refs") or ():
        token = str(ref or "").strip()
        if token in refs["participants"]:
            participant = dict(participants.get(token) or {})
            participant_ref = str(participant.get("participant_ref") or "").strip()
            if not participant_ref:
                raise ValueError("能力目标缺少冻结角色身份")
            targets.append({"ref": f"character:{participant_ref}", "type": "actor"})
        elif token in refs["threats"]:
            targets.append({"ref": token, "type": "threat"})
        else:
            raise ValueError("能力目标已失效或类型不可证明")
    zone_ref = str(draft.get("zone_ref") or "").strip()
    if zone_ref:
        targets.append({"ref": zone_ref, "type": "zone"})
    objective_ref = str(draft.get("objective_ref") or "").strip()
    if objective_ref:
        targets.append({"ref": objective_ref, "type": "objective"})
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        identity = (target["ref"], target["type"])
        if identity not in seen:
            seen.add(identity)
            result.append(target)
    return result

def _positive_int(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(message)
    if value > 1_000_000:
        raise ValueError(message)
    return value

def _qualified_ref(value: Any, *, label: str) -> str:
    token = str(value or "").strip()
    if (
        not token or len(token) > 200 or ":" not in token
        or any(ord(char) < 32 for char in token)
    ):
        raise ValueError(f"能力{label}引用无效")
    return token

def _capability_element_contract(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
    capability: Mapping[str, Any],
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    element_ref = str(definition.get("element_ref") or "").strip()
    element_fields_present = any(
        field in definition
        for field in (
            "element_target", "exposure_layers", "failure_exposure_layers"
        )
    )
    if not element_ref:
        if element_fields_present:
            raise ValueError("能力元素合同缺少 element_ref")
        return {}
    elemental_source = state.get("elemental_table") or state.get("elemental")
    if not isinstance(elemental_source, Mapping):
        raise ValueError("战术状态缺少冻结 elemental table")
    table = parse_elemental({"elemental": elemental_source})
    if element_ref not in set(table.get("elements") or ()):
        raise ValueError("能力 element_ref 未在冻结元素表中声明")
    target_kind = str(definition.get("element_target") or "").strip()
    if target_kind not in {"self", "selected_target", "environment"}:
        raise ValueError("能力 element_target 无效")
    success_layers = definition.get("exposure_layers")
    failure_layers = definition.get("failure_exposure_layers")
    if (
        isinstance(success_layers, bool) or not isinstance(success_layers, int)
        or not 1 <= success_layers <= 5
        or isinstance(failure_layers, bool) or not isinstance(failure_layers, int)
        or not 0 <= failure_layers <= 5
    ):
        raise ValueError("能力元素暴露层数无效")

    actor_key = str(draft.get("actor_key") or "").strip()
    participants = state.get("participants") or {}
    if target_kind == "self":
        participant = dict(participants.get(actor_key) or {})
        participant_ref = str(participant.get("participant_ref") or "").strip()
        if not participant_ref:
            raise ValueError("能力元素自身目标缺少冻结角色身份")
        binding = {
            "bucket": "participants",
            "key": actor_key,
            "target_ref": f"character:{participant_ref}",
            "target_label": str(participant.get("label") or "行动者").strip()[:120],
        }
    elif target_kind == "environment":
        binding = {
            "bucket": "environment",
            "key": "environment",
            "target_ref": "environment:tactical",
            "target_label": "当前环境",
        }
    else:
        selected = [str(value).strip() for value in draft.get("target_refs") or () if str(value).strip()]
        if len(selected) != 1:
            raise ValueError("能力元素 selected_target 必须唯一选择角色或威胁")
        selected_ref = selected[0]
        if selected_ref in participants:
            participant = dict(participants.get(selected_ref) or {})
            participant_ref = str(participant.get("participant_ref") or "").strip()
            if not participant_ref:
                raise ValueError("能力元素角色目标缺少冻结身份")
            binding = {
                "bucket": "participants",
                "key": selected_ref,
                "target_ref": f"character:{participant_ref}",
                "target_label": str(participant.get("label") or "队伍成员").strip()[:120],
            }
        else:
            threats = [
                dict(item) for item in _sequence(
                    state.get("known_threats") or state.get("threats")
                ) if isinstance(item, Mapping)
            ]
            threat = _find(threats, selected_ref, "threat_id", "id", "ref")
            if threat is None:
                raise ValueError("能力元素 selected_target 不是冻结角色或威胁")
            binding = {
                "bucket": "threats",
                "key": selected_ref,
                "target_ref": selected_ref,
                "target_label": str(threat.get("label") or "已知威胁").strip()[:120],
            }
    return {
        **binding,
        "element_ref": element_ref,
        "success_layers": success_layers,
        "failure_layers": failure_layers,
        "table": table,
    }

def _apply_capability_elemental(
    current: dict[str, Any],
    contract: Mapping[str, Any],
    *,
    success: bool,
) -> dict[str, Any]:
    if not contract:
        return {}
    exposures = current.get("elemental_exposures") or {}
    if not isinstance(exposures, Mapping):
        raise ValueError("战术元素暴露状态无效")
    exposures = _clone(exposures)
    bucket = str(contract["bucket"])
    key = str(contract["key"])
    if bucket == "environment":
        raw_layers = exposures.get("environment") or {}
        if not isinstance(raw_layers, Mapping):
            raise ValueError("战术环境元素暴露状态无效")
        layer_map = dict(raw_layers)
    else:
        raw_bucket = exposures.get(bucket) or {}
        if not isinstance(raw_bucket, Mapping):
            raise ValueError("战术目标元素暴露状态无效")
        target_bucket = dict(raw_bucket)
        raw_layers = target_bucket.get(key) or {}
        if not isinstance(raw_layers, Mapping):
            raise ValueError("战术目标元素暴露层无效")
        layer_map = dict(raw_layers)
    normalized_layers: dict[str, int] = {}
    for element, raw in layer_map.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 5:
            raise ValueError("战术元素暴露层必须是 0..5 整数")
        normalized_layers[str(element)] = raw
    target_element = next(
        (
            element for element, layers in sorted(
                normalized_layers.items(), key=lambda item: (-item[1], item[0])
            ) if layers > 0
        ),
        "",
    )
    element_ref = str(contract["element_ref"])
    before = int(normalized_layers.get(element_ref, 0))
    added = int(
        contract["success_layers"] if success else contract["failure_layers"]
    )
    after = min(5, before + added)
    normalized_layers[element_ref] = after
    if bucket == "environment":
        exposures["environment"] = normalized_layers
    else:
        target_bucket = dict(exposures.get(bucket) or {})
        target_bucket[key] = normalized_layers
        exposures[bucket] = target_bucket
    current["elemental_exposures"] = exposures
    resolved = resolve_elemental(
        contract["table"],
        element_ref,
        str(contract["target_ref"]),
        target_element or None,
        context={"success": success, "layers_before": before, "layers_after": after},
    ) or {}
    receipt = resolved.get("receipt") or {}
    return {
        "element_ref": element_ref,
        "target_label": str(contract["target_label"]),
        "layers_before": before,
        "layers_after": after,
        "matched_reaction": str(receipt.get("matched_reaction") or ""),
        "matched_interaction": str(receipt.get("matched_interaction") or ""),
        "public_copy": str(receipt.get("public_copy") or "").strip()[:300],
        "settlement_order": [
            str(item)[:80] for item in resolved.get("settlement_order") or ()
        ][:16],
    }

def _capability_execution(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
    capability: Mapping[str, Any],
    *,
    success: bool,
    lock_key: str,
) -> dict[str, Any]:
    definition = capability.get("definition")
    instance_state = capability.get("instance_state")
    if not isinstance(definition, Mapping) or not definition:
        raise ValueError("所选能力缺少冻结作者定义，请刷新战况后重新选择")
    if not isinstance(instance_state, Mapping):
        raise ValueError("所选能力缺少冻结实例状态，请刷新战况后重新选择")
    instance_id = str(capability.get("instance_id") or "").strip()
    actor_ref = str(capability.get("actor_ref") or "").strip()
    participant_ref = str(capability.get("participant_ref") or "").strip()
    if (
        not instance_id or len(instance_id) > 200
        or not participant_ref or len(participant_ref) > 160
        or actor_ref != f"character:{participant_ref}"
    ):
        raise ValueError("所选能力的冻结实例或角色绑定无效")
    if instance_state.get("available") is False:
        raise ValueError("所选能力实例当前不可用")
    elemental_contract = _capability_element_contract(
        state, draft, capability, definition,
    )
    narrative_only = bool(definition.get("narrative_only"))
    tactical_actions = definition.get("tactical_action_kinds")
    if tactical_actions is None and narrative_only:
        tactical_actions = []
    else:
        if (
            not isinstance(tactical_actions, list)
            or not 1 <= len(tactical_actions) <= len(ACTION_KINDS)
            or len(set(tactical_actions)) != len(tactical_actions)
            or any(str(value) not in ACTION_KINDS for value in tactical_actions)
        ):
            raise ValueError("所选能力缺少有效 tactical_action_kinds 合同")
        if str(draft.get("action_kind") or "") not in tactical_actions:
            raise ValueError("所选能力不支持当前战术行动")
    constraints = definition.get("usage_constraints") or []
    if constraints:
        raise ValueError("所选能力含当前战术上下文无法证明的使用条件")

    targets = _capability_targets(state, draft)
    targeting = definition.get("targeting")
    if targeting not in (None, {}) and not isinstance(targeting, Mapping):
        raise ValueError("所选能力的目标合同无效")
    targeting = dict(targeting or {})
    if targeting.get("selector"):
        raise ValueError("所选能力含当前战术上下文无法证明的目标条件")
    if set(targeting) - {"min_targets", "max_targets", "entity_types"}:
        raise ValueError("所选能力含当前战术运行时不支持的目标条件")
    minimum = int(targeting.get("min_targets", 0) or 0)
    maximum = int(
        targeting.get("max_targets", minimum or len(targets))
        if targeting else 0
    )
    if not 0 <= minimum <= maximum <= 8:
        raise ValueError("所选能力的目标数量合同无效")
    if targets and not targeting:
        raise ValueError("所选能力未声明可验证的目标合同")
    if not minimum <= len(targets) <= maximum:
        raise ValueError("所选能力的目标数量不符合冻结作者定义")
    raw_types = targeting.get("entity_types") or []
    if raw_types and (
        not isinstance(raw_types, Sequence)
        or isinstance(raw_types, (str, bytes))
    ):
        raise ValueError("所选能力的目标类型合同无效")
    allowed_types = {str(value).strip() for value in raw_types}
    if any(target["type"] not in allowed_types for target in targets):
        raise ValueError("所选能力的目标类型不符合冻结作者定义")

    label = str(definition.get("label") or "能力").strip()[:120]
    costs = definition.get("costs") or []
    effects = definition.get("effects") or []
    if (
        not isinstance(costs, Sequence) or isinstance(costs, (str, bytes))
        or not isinstance(effects, Sequence) or isinstance(effects, (str, bytes))
        or len(costs) > 16 or len(effects) > 32
    ):
        raise ValueError("所选能力的成本或效果合同无效")

    deltas: dict[str, int] = {}
    narrative: list[str] = []
    effect_ops: list[dict[str, Any]] = []
    has_mechanical = False
    if elemental_contract:
        has_mechanical = True

    def add_delta(resource_ref: str, delta: int) -> None:
        nonlocal has_mechanical
        deltas[resource_ref] = deltas.get(resource_ref, 0) + delta
        has_mechanical = True

    for raw in costs:
        if not isinstance(raw, Mapping):
            raise ValueError("所选能力含不可执行的文字成本")
        resource_ref = _qualified_ref(raw.get("resource_ref"), label="资源")
        amount = _positive_int(raw.get("value", raw.get("amount")), "能力资源成本必须是正整数")
        if str(raw.get("operation") or "subtract") not in {"subtract", "cost"}:
            raise ValueError("能力 costs 只允许明确扣减资源")
        add_delta(resource_ref, -amount)

    known_narrative_kinds = {"narrative", "description", "limitation", "target"}
    for index, raw in enumerate(effects):
        if not isinstance(raw, Mapping):
            raise ValueError("所选能力效果必须是对象列表")
        kind = str(raw.get("kind") or "").strip()
        op = str(raw.get("op") or "").strip()
        description = str(raw.get("description") or "").strip()[:240]
        if kind in {"resource_cost", "resource_gain"}:
            resource_ref = _qualified_ref(
                raw.get("resource_ref") or raw.get("ref"), label="资源",
            )
            amount = _positive_int(
                raw.get("amount", raw.get("value")),
                "能力资源变化必须是正整数",
            )
            if kind == "resource_cost" or success:
                add_delta(resource_ref, -amount if kind == "resource_cost" else amount)
            else:
                has_mechanical = True
            if description and (kind == "resource_cost" or success):
                narrative.append(description)
            continue
        if op in {"create_instance", "end_instance"}:
            has_mechanical = True
            effect_ref = _qualified_ref(raw.get("target_ref") or raw.get("ref"), label="效果")
            if not effect_ref.startswith("runtime_effect:"):
                raise ValueError("能力运行时效果必须引用 runtime_effect")
            definition_ref = _qualified_ref(
                definition.get("id") or capability.get("id"), label="来源",
            )
            supplied_source = str(raw.get("source_ref") or "").strip()
            if supplied_source and supplied_source != definition_ref:
                raise ValueError("能力运行时效果来源与冻结定义不一致")
            scope = str(raw.get("persistence_scope") or "session").strip()
            if scope not in {
                "global_character", "world_character", "campaign",
                "session", "scene", "temporary",
            }:
                raise ValueError("能力运行时效果作用域无效")
            recipient = raw.get("recipient") or {}
            if not isinstance(recipient, Mapping):
                raise ValueError("能力运行时效果目标声明无效")
            recipient_scope = str(recipient.get("scope") or "").strip()
            effect_targets = (
                [{"ref": actor_ref, "type": "actor"}]
                if recipient_scope == "actor"
                else targets
                if recipient_scope == "target" and targets
                else []
            )
            if not effect_targets:
                raise ValueError("能力运行时效果缺少可证明的目标")
            if op == "create_instance":
                value = raw.get("value")
                duration = raw.get("duration") or {}
                if not isinstance(value, Mapping) or not isinstance(duration, Mapping):
                    raise ValueError("创建运行时效果缺少明确状态或持续期")
                for target_index, target in enumerate(effect_targets):
                    deterministic_id = "tactical-effect:" + hashlib.sha256(
                        f"{lock_key}\0{draft['actor_key']}\0{index}\0{target_index}\0{effect_ref}".encode()
                    ).hexdigest()[:32]
                    effect_ops.append({
                        "operation": "create",
                        "instance_id": deterministic_id,
                        "target_ref": target["ref"],
                        "effect_ref": effect_ref,
                        "source_ref": definition_ref,
                        "persistence_scope": scope,
                        "state": dict(value),
                        "duration": dict(duration),
                    })
            else:
                frozen_effects = instance_state.get("runtime_effects") or []
                if not isinstance(frozen_effects, Sequence) or isinstance(frozen_effects, (str, bytes)):
                    raise ValueError("结束运行时效果缺少冻结实例清单")
                for target in effect_targets:
                    matches = [
                        dict(item) for item in frozen_effects
                        if isinstance(item, Mapping)
                        and str(item.get("target_ref") or "") == target["ref"]
                        and str(item.get("effect_ref") or "") == effect_ref
                        and str(item.get("source_ref") or "") == definition_ref
                        and str(item.get("status") or "") == "active"
                    ]
                    if len(matches) != 1:
                        raise ValueError("结束运行时效果无法唯一匹配冻结实例")
                    frozen = matches[0]
                    effect_ops.append({
                        "operation": "end",
                        "instance_id": str(frozen.get("instance_id") or ""),
                        "target_ref": target["ref"],
                        "effect_ref": effect_ref,
                        "source_ref": definition_ref,
                        "persistence_scope": scope,
                        "status_before": "active",
                        "status_after": "ended",
                    })
            if description:
                narrative.append(description)
            continue
        if kind in known_narrative_kinds or (narrative_only and not kind and not op):
            if description:
                narrative.append(description)
            continue
        raise ValueError("所选能力含当前战术运行时不支持的机械效果")

    if narrative_only and has_mechanical:
        raise ValueError("纯叙事能力不能声明资源成本或状态变化")
    if not narrative_only and not has_mechanical:
        raise ValueError("所选能力没有可安全执行的作者明示机械合同")

    resources = instance_state.get("resources") or {}
    if deltas and not isinstance(resources, Mapping):
        raise ValueError("所选能力缺少冻结角色资源 preimage")
    resource_updates: list[dict[str, Any]] = []
    for resource_ref, delta in sorted(deltas.items()):
        frozen = resources.get(resource_ref)
        if not isinstance(frozen, Mapping):
            raise ValueError("所选能力缺少冻结角色资源 preimage")
        before = frozen.get("current")
        maximum = frozen.get("maximum")
        if (
            isinstance(before, bool) or not isinstance(before, int)
            or isinstance(maximum, bool) or not isinstance(maximum, int)
            or not 0 <= before <= maximum <= 1_000_000
        ):
            raise ValueError("所选能力的冻结角色资源无效")
        after = before + delta
        if not 0 <= after <= maximum:
            raise ValueError("所选能力的角色资源不足或已达上限")
        if after != before:
            resource_updates.append({
                "participant_ref": participant_ref,
                "actor_ref": actor_ref,
                "resource_ref": resource_ref,
                "current_before": before,
                "current_after": after,
                "maximum_before": maximum,
            })
    return {
        "label": label,
        "mode": "narrative_only" if narrative_only else "mechanical",
        "narrative": narrative[:8],
        "failure_forward": str(definition.get("failure_forward") or "").strip()[:300],
        "resource_updates": resource_updates,
        "effect_updates": effect_ops if success else [],
        "mechanical": bool(
            resource_updates
            or (success and effect_ops)
            or (
                elemental_contract
                and int(
                    elemental_contract["success_layers"]
                    if success else elemental_contract["failure_layers"]
                ) > 0
            )
        ),
        "elemental_contract": elemental_contract,
    }
