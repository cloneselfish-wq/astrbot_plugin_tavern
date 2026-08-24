from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .candidates import (
    GRANT_POLICIES,
    candidate_rule_apply_signature,
    candidate_rule_view,
    normalize_candidate_rules,
)
from .condition_engine import ConditionEngine
from .entity_registry import EntityRegistry, module_value, split_ref
from .stat_generation import apply_resource_modifiers, normalize_runtime_state_snapshot


class CapabilityService:
    def __init__(self, world: Mapping[str, Any], registry: EntityRegistry) -> None:
        self.world = dict(world)
        self.registry = registry
        self.conditions = ConditionEngine(registry)

    @staticmethod
    def _instances(context: Mapping[str, Any]) -> list[dict[str, Any]]:
        actor = context.get("actor", {})
        actor = actor if isinstance(actor, Mapping) else {}
        values = actor.get("capabilities", [])
        return [dict(item) for item in values if isinstance(item, Mapping)] if isinstance(values, Sequence) else []

    def list_available(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for instance in self._instances(context):
            ref = str(instance.get("capability_ref") or instance.get("ref") or "")
            if not self.registry.contains(ref) or not bool(instance.get("available", True)):
                continue
            definition = self.registry.resolve(ref, "capability").definition
            constraints = definition.get("usage_constraints", [])
            allowed = True
            if isinstance(constraints, Sequence) and not isinstance(constraints, (str, bytes)):
                for constraint in constraints:
                    if isinstance(constraint, Mapping):
                        condition = constraint.get("when", constraint.get("condition", constraint))
                        if not self.conditions.evaluate(condition, context).matched:
                            allowed = False
                            break
            if allowed:
                result.append({**instance, "definition": dict(definition)})
        return result

    def validate_intent(self, intent: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
        ref = str(intent.get("capability_ref") or "").strip()
        if not ref:
            return None
        available = {str(item.get("capability_ref") or item.get("ref")): item for item in self.list_available(context)}
        if ref not in available:
            raise ValueError(f"行动使用了未解锁或当前不可用的能力：{ref}")
        definition = available[ref]["definition"]
        self._validate_targets(definition, intent, context)
        resources = context.get("actor", {})
        resources = resources.get("refs", {}) if isinstance(resources, Mapping) else {}
        costs = definition.get("costs", [])
        if isinstance(costs, Sequence) and not isinstance(costs, (str, bytes)):
            for cost in costs:
                if not isinstance(cost, Mapping): continue
                resource_ref = str(cost.get("resource_ref") or "")
                required = float(cost.get("value", 0) or 0)
                if float(resources.get(resource_ref, 0) or 0) < required:
                    raise ValueError(f"资源不足：{resource_ref}")
        return dict(definition)

    def _validate_targets(
        self,
        definition: Mapping[str, Any],
        intent: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        targeting = definition.get("targeting")
        if not isinstance(targeting, Mapping):
            return
        raw_targets = intent.get("targets", [])
        if not isinstance(raw_targets, Sequence) or isinstance(raw_targets, (str, bytes)):
            raise TypeError("行动目标必须是数组")
        targets = list(raw_targets)
        minimum = int(targeting.get("min_targets", 0) or 0)
        maximum = int(targeting.get("max_targets", minimum or len(targets)) or 0)
        if len(targets) < minimum or (maximum >= 0 and len(targets) > maximum):
            raise ValueError(f"目标数量必须在 {minimum}—{maximum} 之间")
        raw_types = targeting.get("entity_types", [])
        allowed_types = {
            str(value) for value in raw_types
        } if isinstance(raw_types, Sequence) and not isinstance(raw_types, (str, bytes)) else set()
        selector = targeting.get("selector")
        target_contexts = context.get("targets", {})
        target_contexts = target_contexts if isinstance(target_contexts, Mapping) else {}
        for raw in targets:
            target_ref = str(
                raw.get("ref") if isinstance(raw, Mapping) else raw
            ).strip()
            entity_type, _ = split_ref(target_ref)
            if allowed_types and entity_type not in allowed_types:
                raise ValueError(f"目标类型不允许：{entity_type}")
            if selector:
                selected_context = target_contexts.get(target_ref, {})
                selected_context = (
                    selected_context
                    if isinstance(selected_context, Mapping)
                    else {}
                )
                evaluation_context = {
                    **context,
                    "target": selected_context,
                }
                if not self.conditions.evaluate(selector, evaluation_context).matched:
                    raise ValueError(f"目标不满足能力选择条件：{target_ref}")

    def initial_grants(self, preset_refs: Mapping[str, Any]) -> list[dict[str, Any]]:
        # D1 编译后的能力作者数据属于 capability_effects 模块。
        # 不再读取已经废弃的 rules.capabilities 入口。
        module = module_value(self.world, "capability_effects", {})
        module = module if isinstance(module, Mapping) else {}
        grants = module.get("initial_grants", [])
        result: list[dict[str, Any]] = []
        if not isinstance(grants, Sequence) or isinstance(grants, (str, bytes)):
            return result
        context = {"actor": {"refs": dict(preset_refs)}}
        for grant in grants:
            if not isinstance(grant, Mapping): continue
            evaluation = self.conditions.evaluate(grant.get("when", {}), context)
            if evaluation.matched:
                ref = str(grant.get("capability_ref") or grant.get("target_ref") or "")
                self.registry.resolve(ref, "capability")
                # 记录命中的预设维度（支持多选集合），供授予来源与去重。
                preset_keys = sorted({
                    str(read.get("ref") or "")
                    for read in evaluation.reads
                    if str(read.get("ref") or "").startswith("custom:preset.")
                })
                result.append({**dict(grant), "preset_keys": preset_keys})
        return result

    def progression_cycles(self) -> list[list[str]]:
        graph: dict[str, set[str]] = {}
        module = module_value(self.world, "capability_effects", {})
        module = module if isinstance(module, Mapping) else {}
        transitions = module.get("transitions", [])
        if not isinstance(transitions, Sequence) or isinstance(transitions, (str, bytes)):
            return []
        for transition in transitions:
            if not isinstance(transition, Mapping): continue
            sources = transition.get("from", [])
            operations = transition.get("operations", [])
            targets = [
                str(op.get("target_ref")) for op in operations
                if isinstance(op, Mapping) and op.get("op") == "grant_reference"
            ] if isinstance(operations, Sequence) else []
            for source in sources if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)) else []:
                graph.setdefault(str(source), set()).update(targets)
        cycles: list[list[str]] = []
        visiting: list[str] = []
        visited: set[str] = set()
        def visit(node: str) -> None:
            if node in visiting:
                cycles.append(visiting[visiting.index(node):] + [node]); return
            if node in visited: return
            visiting.append(node)
            for target in graph.get(node, set()): visit(target)
            visiting.pop(); visited.add(node)
        for node in graph: visit(node)
        return cycles

    def validate_candidate_rules(self, raw: Any) -> dict[str, Any]:
        """Registry-aware validation of one candidate's D1 rules.

        Capability/resource/runtime-effect references must resolve in the
        active world snapshot; ability tracks are syntax-checked only (they
        are declared by the author source, not the runtime registry).
        """

        rules = normalize_candidate_rules(candidate_rule_view(raw))

        def require_registered(ref: str, expected_type: str) -> None:
            if not self.registry.contains(ref):
                raise ValueError(f"候选规则引用了未注册的 {expected_type}：{ref}")
            item = self.registry.resolve(ref, expected_type)
            if item.entity_type != expected_type:
                raise ValueError(
                    f"候选规则引用类型不匹配：{ref} 期望 {expected_type}"
                )

        for ref, _label in (*rules["ability_pool_add"], *rules["ability_pool_remove"]):
            require_registered(ref, "capability")
        for grant in rules["grants"]:
            kind = str(grant.get("kind") or "")
            if kind in {"capability", "resource", "runtime_effect"}:
                require_registered(str(grant.get("ref") or ""), kind)
            elif kind == "ability_track":
                split_ref(str(grant.get("ref") or ""))
        for unlock in rules["unlocks"]:
            kind = str(unlock.get("kind") or "")
            if kind in {"capability", "resource", "runtime_effect"}:
                require_registered(str(unlock.get("ref") or ""), kind)
            elif kind == "ability_track":
                split_ref(str(unlock.get("ref") or ""))
        for modifier in rules["resource_modifiers"]:
            require_registered(str(modifier.get("resource_ref") or ""), "resource")
        for ref, _label in rules["runtime_effect_refs"]:
            require_registered(ref, "runtime_effect")
        return rules


def apply_candidate_rules(
    raw: Any,
    runtime_state: Mapping[str, Any],
    *,
    applied: set[str] | None = None,
) -> dict[str, Any]:
    """Apply one candidate's D1 rules onto a runtime snapshot, purely.

    Returns a new snapshot; the input is never mutated.  When ``applied`` is
    provided, re-applying a candidate with an identical effect signature
    raises instead of double-counting resources or duplicating grants.
    """

    if applied is not None:
        signature = candidate_rule_apply_signature(raw)
        if signature in applied:
            raise ValueError("该候选的效果已经应用过，不能重复应用")
        applied.add(signature)
    rules = normalize_candidate_rules(candidate_rule_view(raw))
    state = normalize_runtime_state_snapshot(runtime_state)

    abilities = [dict(item) for item in state["abilities"]]
    for ref, label in rules["ability_pool_add"]:
        if not any(str(item.get("ref")) == ref for item in abilities):
            abilities.append({"ref": ref, "label": label})
    for ref, _label in rules["ability_pool_remove"]:
        abilities = [
            item for item in abilities if str(item.get("ref")) != ref
        ]
    state["abilities"] = abilities

    state["resources"] = apply_resource_modifiers(
        state["resources"], rules["resource_modifiers"]
    )

    grants = [dict(item) for item in state["grants"]]
    for grant in rules["grants"]:
        identity = (str(grant.get("kind") or ""), str(grant.get("ref") or ""))
        if not any(
            (str(item.get("kind") or ""), str(item.get("ref") or "")) == identity
            for item in grants
        ):
            grants.append(dict(grant))
    state["grants"] = grants

    unlocks = [dict(item) for item in state["combination_unlocks"]]
    for unlock in rules["unlocks"]:
        ref = str(unlock.get("ref") or "")
        if not any(str(item.get("ref")) == ref for item in unlocks):
            unlocks.append(dict(unlock))
    state["combination_unlocks"] = unlocks

    runtime_states = [dict(item) for item in state["runtime_states"]]
    for ref, label in rules["runtime_effect_refs"]:
        if not any(str(item.get("ref")) == ref for item in runtime_states):
            runtime_states.append({"ref": ref, "label": label})
    state["runtime_states"] = runtime_states
    return state


__all__ = [
    "CapabilityService",
    "GRANT_POLICIES",
    "apply_candidate_rules",
]
