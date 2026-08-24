"""Strict RC10 challenge/tactical start-state freezing and builders."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from ...visualization.keys import OpaqueKeyFactory
from . import WebRouteError, mapping, text
from .gameplay_freeze import (
    _bind_effect_revisions,
    _freeze_action_checks,
    _freeze_elemental_contract,
    _freeze_error,
    _freeze_intensity_profile,
    _freeze_site_environment,
    _freeze_tactical_choices,
    _freeze_tactical_site,
    _frozen_participants,
    _resolve_intensity_profile,
    _safe_frozen_value,
    _world_module,
)


def _template_label(value: Mapping[str, Any], fallback: str) -> str:
    return text(value.get("label") or value.get("name") or value.get("title"), fallback)


def _template_revision(value: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


async def _resolve_frozen_template(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
    role: str,
    *,
    module_id: str,
    template_key: str,
    expected_world_revision: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    reader = getattr(database, "get_instance_config", None)
    instance = mapping(await reader(session_id)) if callable(reader) else {}
    world = mapping(instance.get("world_snapshot"))
    profile = mapping(instance.get("ui_profile"))
    manifest = mapping(profile.get("ui_surface_manifest"))
    frozen_revision = text(manifest.get("world_revision") or instance.get("world_revision"))
    if not world or not frozen_revision:
        raise WebRouteError(
            409, "gameplay.frozen_world_missing", "当前副本缺少冻结世界定义。",
            "系统没有开始挑战；请由管理员检查副本世界快照后重试。",
        )
    if not expected_world_revision or expected_world_revision != frozen_revision:
        raise WebRouteError(
            409, "gameplay.world_revision_conflict", "当前副本的冻结世界版本已与页面不一致。",
            "系统没有拼接新旧模板；请刷新跑团现场后重新选择。",
        )
    definition = mapping(world.get(module_id)) or mapping(mapping(world.get("rules")).get(module_id))
    collection_name = "conflicts" if module_id == "tactical_conflict" else "templates"
    templates = [mapping(item) for item in definition.get(collection_name) or definition.get("templates") or () if isinstance(item, Mapping)]
    kind = "tactical-template" if module_id == "tactical_conflict" else "challenge-template"
    keys = OpaqueKeyFactory(scope=f"console:{role}:{text(principal.get('username'))}:{session_id}")
    for index, template in enumerate(templates):
        label = _template_label(template, "世界模板")
        if keys.key(kind, f"{index}:{label}") == template_key:
            return template, world, frozen_revision
    raise WebRouteError(
        409, "gameplay.template_key_invalid", "所选世界模板已失效或不属于当前副本。",
        "系统没有使用客户端文本补造模板；请刷新后重新选择。",
    )


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


async def build_challenge_start_state(
    database: Any,
    session_id: str,
    template: Mapping[str, Any],
    world_revision: str,
    *,
    request_key: str,
    world: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    frozen_world = mapping(world)
    if not frozen_world:
        reader = getattr(database, "get_instance_config", None)
        instance = mapping(await reader(session_id)) if callable(reader) else {}
        frozen_world = mapping(instance.get("world_snapshot"))
    objectives = [mapping(item) for item in template.get("objectives") or () if isinstance(item, Mapping)]
    objective = text(template.get("objective") or (objectives[0].get("label") if objectives else "") or template.get("label"))
    target = max(1, int((objectives[0].get("target") or objectives[0].get("success_threshold") or 3) if objectives else 3))
    telegraphs = [text(item) for item in template.get("telegraphs") or () if text(item)]
    participants, _roster = await _frozen_participants(database, session_id, frozen_world)
    frozen_effects, effect_revision_fingerprint = await _bind_effect_revisions(
        database,
        session_id,
        [("challenge.outcome_effects", template.get("outcome_effects") or {})],
    )
    elemental_contract, _element_keys = _freeze_elemental_contract(frozen_world)
    if template.get("actor_fate_policy") not in (None, {}) and not isinstance(template.get("actor_fate_policy"), Mapping):
        raise _freeze_error("gameplay.actor_fate_policy_invalid", "角色命运专用策略定义无效。")
    template_ref = text(template.get("id") or template.get("challenge_id"))
    template_revision = _template_revision(template)
    return {
        "visibility": "party",
        "status": "active",
        "phase": "setup",
        "round": 1,
        "mode": text(template.get("mode")),
        "objective": objective,
        "objectives": objectives,
        "risk": text(telegraphs[0] if telegraphs else template.get("kind") or "行动前会公开风险预告"),
        "telegraphs": telegraphs,
        "failure_forward": text(template.get("failure_forward") or "失败会打开代价更高的替代路径。"),
        "options": [mapping(item) for item in template.get("options") or () if isinstance(item, Mapping)],
        "participants": participants,
        "progress": 0,
        "target": target,
        "template_ref": template_ref,
        "template_revision": template_revision,
        "world_revision": world_revision,
        "outcome_effects": frozen_effects["challenge.outcome_effects"],
        "actor_fate_policy": _safe_frozen_value(mapping(template.get("actor_fate_policy"))),
        "elemental": elemental_contract,
        "elemental_contract": elemental_contract,
        "elemental_exposures": {"participants": {}, "threats": {}, "environment": {}},
        "start_receipt": {
            "intent": "challenge.start",
            "idempotency_key": request_key,
            "template_revision": template_revision,
            "world_revision": world_revision,
            "effect_revision_fingerprint": effect_revision_fingerprint,
        },
        "_semantic_events": [{
            "kind": "challenge_started",
            "label": "世界挑战已开始",
            "summary": f"挑战目标“{objective}”已冻结到当前副本。",
            "visibility": "party",
            "details": {"template_ref": template_ref, "mode": text(template.get("mode"))},
        }],
    }


async def build_tactical_start_state(
    database: Any,
    session_id: str,
    template: Mapping[str, Any],
    world: Mapping[str, Any],
    world_revision: str,
    *,
    intensity_id: str,
    request_key: str,
) -> dict[str, Any]:
    site, zone_rows, zone_labels, zone_edges, starting_zone = _freeze_tactical_site(world, template)
    elemental_contract, element_keys = _freeze_elemental_contract(world)
    environment = _freeze_site_environment(site, world, element_keys)
    site_ref = text(site.get("site_id"))
    participants, roster = await _frozen_participants(database, session_id, world)
    for participant in participants.values():
        participant.update({
            "zone_ref": starting_zone,
            "zone_label": zone_labels.get(starting_zone, "起始区域"),
            "action_budget": {"major": 1, "maneuver": 1, "reaction": 1},
        })
    profile, selected_intensity = _resolve_intensity_profile(world, intensity_id)
    site_ids = {
        text(item.get("site_id"))
        for item in mapping(world.get("adventure_sites")).get("sites") or ()
        if isinstance(item, Mapping) and text(item.get("site_id"))
    }
    conflict_ids = {
        text(item.get("conflict_template_id"))
        for item in _world_module(world, "tactical_conflict").get("conflicts") or ()
        if isinstance(item, Mapping) and text(item.get("conflict_template_id"))
    }
    frozen_intensity = _freeze_intensity_profile(
        profile,
        site_ids=site_ids,
        conflict_ids=conflict_ids,
    )
    action_checks = _freeze_action_checks(template, world)

    scaling = mapping(template.get("scaling"))
    tiers = sorted((int(str(key)), mapping(value)) for key, value in scaling.items() if str(key).isdigit())
    party_size = max(1, len(participants))
    tier_size, tier = next(((size, value) for size, value in tiers if size >= party_size), tiers[-1] if tiers else (party_size, {}))
    objectives = [_copy(mapping(item)) for item in template.get("objectives") or () if isinstance(item, Mapping)]
    for index, objective in enumerate(objectives):
        objective.setdefault("id", f"objective:{text(template.get('conflict_template_id'))}:{index + 1}")
        if tier.get("objective_budget"):
            objective["success_threshold"] = max(1, int(tier["objective_budget"]))
        objective.setdefault("progress", 0)
    guard_delta = int(profile.get("threat_guard_delta") or 0)
    all_threats = [_copy(mapping(item)) for item in template.get("threats") or () if isinstance(item, Mapping)]
    front_count = max(1, int(tier.get("fronts") or len(all_threats) or 1))
    threats = all_threats[:front_count]
    for index, threat in enumerate(threats):
        threat.setdefault("threat_id", f"threat:{text(template.get('conflict_template_id'))}:{index + 1}")
        threat["guard"] = max(0, int(threat.get("guard") or 0) + guard_delta)
        threat.setdefault("resolve", max(1, int(threat.get("rank") or 1)))

    available_items, available_capabilities = await _freeze_tactical_choices(
        database,
        session_id,
        roster,
        world,
        request_key=request_key,
        element_keys=element_keys,
    )

    dynamic = mapping(world.get("dynamic_crises"))
    site_crises = [
        _copy(mapping(item)) for item in dynamic.get("crises") or ()
        if isinstance(item, Mapping)
        and (not item.get("site_ref") or text(item.get("site_ref")) == site_ref)
    ]
    crisis_count = max(0, int(profile.get("crises_per_site") or 0))
    active_crises = site_crises[:crisis_count]
    effect_sources: list[tuple[str, Any]] = [
        ("tactical.outcome_effects", template.get("outcome_effects") or {}),
    ]
    crisis_scopes: list[str] = []
    for index, crisis in enumerate(active_crises):
        crisis_scope = f"crisis:{text(crisis.get('id')) or index + 1}.environment_effects"
        crisis_scopes.append(crisis_scope)
        effect_sources.append((crisis_scope, crisis.get("environment_effects") or {}))
    frozen_effects, effect_revision_fingerprint = await _bind_effect_revisions(
        database,
        session_id,
        effect_sources,
    )
    if template.get("actor_fate_policy") not in (None, {}) and not isinstance(template.get("actor_fate_policy"), Mapping):
        raise _freeze_error("gameplay.actor_fate_policy_invalid", "角色命运专用策略定义无效。")
    for crisis, scope in zip(active_crises, crisis_scopes, strict=True):
        crisis["environment_effects"] = frozen_effects[scope]
    interlude_block = mapping(world.get("interludes"))
    interludes = [_copy(mapping(item)) for item in interlude_block.get("interludes") or () if isinstance(item, Mapping)][:2]
    template_ref = text(template.get("conflict_template_id"))
    template_revision = _template_revision(template)
    return {
        "visibility": "party",
        "status": "active",
        "phase": "setup",
        "round": 1,
        "objective": text((objectives[0].get("label") if objectives else "") or template.get("label")),
        "objectives": objectives,
        "zones": zone_rows,
        "zone_edges": zone_edges,
        "participants": participants,
        "threats": threats,
        "known_threats": threats,
        "telegraphs": [text(item.get("telegraph")) for item in threats if text(item.get("telegraph"))],
        "environment": environment,
        "elemental": elemental_contract,
        "elemental_contract": elemental_contract,
        "elemental_exposures": {"participants": {}, "threats": {}, "environment": {}},
        "escape_routes": [{"label": zone_labels.get(text(item), "撤退出口"), "zone_ref": text(item)} for item in site.get("exit_points") or ()],
        "negotiation_options": [{"label": text(value)} for item in threats for value in item.get("nonviolent_offramps") or () if text(value)],
        "available_items": available_items,
        "available_capabilities": available_capabilities,
        "action_checks": action_checks,
        "active_crisis": active_crises[0] if active_crises else {},
        "available_crises": active_crises,
        "available_interludes": interludes,
        "intensity": frozen_intensity,
        "scaling_applied": {
            "party_size": party_size,
            "tier": tier_size,
            "fronts": front_count,
            "objective_budget": int(tier.get("objective_budget") or 3),
            "support": max(0, int(tier.get("support") or 0) + int(profile.get("resource_delta") or 0)),
        },
        "difficulty": max(5, min(25, 8 + max((int(item.get("rank") or 1) for item in threats), default=1) + int(profile.get("difficulty_delta") or 0))),
        "failure_forward": _copy(template.get("failure_forward") or []),
        "template_ref": template_ref,
        "template_revision": template_revision,
        "world_revision": world_revision,
        "outcome_effects": frozen_effects["tactical.outcome_effects"],
        "actor_fate_policy": _safe_frozen_value(mapping(template.get("actor_fate_policy"))),
        "start_receipt": {
            "intent": "tactical.conflict.start",
            "idempotency_key": request_key,
            "template_revision": template_revision,
            "world_revision": world_revision,
            "intensity": text(profile.get("id") or selected_intensity),
            "effect_revision_fingerprint": effect_revision_fingerprint,
        },
        "_semantic_events": [{
            "kind": "conflict_started",
            "label": "战术冲突已开始",
            "summary": f"冲突“{_template_label(template, '战术冲突')}”已按{_template_label(profile, '均衡')}强度冻结。",
            "visibility": "party",
            "details": {"template_ref": template_ref, "party_size": party_size, "intensity": text(profile.get("id") or selected_intensity)},
        }],
    }


__all__ = ["build_challenge_start_state", "build_tactical_start_state"]
