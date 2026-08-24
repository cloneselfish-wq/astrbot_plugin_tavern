"""Pure validation and snapshot helpers for gameplay start freezing."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from typing import Any

from ...gameplay_runtime import GAMEPLAY_RUNTIME_MODULES
from . import WebRouteError, mapping, text


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _freeze_label(value: Mapping[str, Any], fallback: str) -> str:
    return text(value.get("label") or value.get("name") or value.get("title"), fallback)


STANDARD_TACTICAL_ACTIONS = frozenset(
    {"strike", "guard", "maneuver", "cast", "interact", "aid", "retreat", "parley"}
)


def _freeze_error(code: str, message: str) -> WebRouteError:
    return WebRouteError(
        409,
        code,
        message,
        "系统没有开始玩法；请由作者或管理员修复冻结定义后重新选择。",
    )


def _world_module(world: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    return mapping(world.get(module_id)) or mapping(mapping(world.get("rules")).get(module_id))


def _safe_frozen_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:1000]
    if depth >= 5:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:96]:
            key = text(raw_key)[:80]
            if not key:
                continue
            frozen = _safe_frozen_value(raw_value, depth=depth + 1)
            if frozen is not None:
                result[key] = frozen
        return result
    if isinstance(value, (list, tuple)):
        return [
            frozen
            for raw in list(value)[:96]
            if (frozen := _safe_frozen_value(raw, depth=depth + 1)) is not None
        ]
    return None


def _active_advantage_sources(runtime: Mapping[str, Any]) -> list[str]:
    sources: list[str] = []
    for raw in runtime.get("advantage_sources") or ():
        if isinstance(raw, Mapping):
            if "active" in raw and not bool(raw.get("active")):
                continue
            status = text(raw.get("status") or raw.get("state"))
            if status and status not in {"active", "enabled", "current"}:
                continue
            source = text(raw.get("source") or raw.get("label") or raw.get("name") or raw.get("id"))
        else:
            source = text(raw)
        if source and source not in sources:
            sources.append(source[:120])
    return sources[:16]


def _sanitized_statuses(runtime: Mapping[str, Any]) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for raw in runtime.get("statuses") or ():
        if not isinstance(raw, Mapping):
            continue
        if "active" in raw and not bool(raw.get("active")):
            continue
        status = text(raw.get("status") or raw.get("state"))
        if status and status not in {"active", "enabled", "current"}:
            continue
        label = text(raw.get("label") or raw.get("name"))[:100]
        affects = [text(item)[:80] for item in raw.get("affects") or () if text(item)][:16]
        item: dict[str, Any] = {
            "label": label,
            "affects": affects,
            "severity": _safe_frozen_value(raw.get("severity")),
            "effect": _safe_frozen_value(raw.get("effect")),
        }
        statuses.append({key: value for key, value in item.items() if value not in (None, "", [])})
    return statuses[:32]


def _primary_stat_from_roster(world: Mapping[str, Any], roster_item: Mapping[str, Any]) -> str:
    actor = _world_module(world, "actor")
    primary_fields = [
        mapping(item)
        for item in actor.get("fields") or ()
        if isinstance(item, Mapping)
        and text(mapping(item).get("semantic_role")) == "actor.stats.primary"
    ]
    if not primary_fields:
        return ""
    if len(primary_fields) != 1 or not text(primary_fields[0].get("key")):
        raise _freeze_error("gameplay.participant_freeze_invalid", "角色主属性字段定义重复或缺失。")
    profile = mapping(roster_item.get("card_profile"))
    declared = text(profile.get(text(primary_fields[0].get("key"))))
    if not declared:
        return ""
    stats = mapping(roster_item.get("card_stats"))
    modifiers = mapping(stats.get("modifiers"))
    labels = mapping(stats.get("labels"))
    if declared in modifiers:
        return declared
    matches = [text(key) for key, label in labels.items() if text(label) == declared and text(key) in modifiers]
    if len(matches) != 1:
        raise _freeze_error("gameplay.participant_freeze_invalid", "角色主属性不能映射到已审核属性。")
    return matches[0]


async def _frozen_participants(
    database: Any,
    session_id: str,
    world: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    roster_reader = getattr(database, "list_roster", None)
    roster = await roster_reader(session_id) if callable(roster_reader) else []
    active_roster = [
        mapping(item)
        for item in roster or ()
        if text(mapping(item).get("group_user_id"))
        and text(mapping(item).get("participation_status")) == "active"
        and (
            "card_status" not in mapping(item)
            or text(mapping(item).get("card_status")) == "approved"
        )
    ]
    fate_reader = getattr(database, "list_actor_fate_states", None)
    fate_rows = await fate_reader(session_id) if callable(fate_reader) else []
    fate_by_participant: dict[str, dict[str, Any]] = {}
    for raw_fate in fate_rows or ():
        fate = mapping(raw_fate)
        participant_ref = text(fate.get("character_id") or fate.get("participant_ref"))
        if participant_ref in fate_by_participant:
            raise _freeze_error("gameplay.participant_freeze_invalid", "角色命运引用重复。")
        if participant_ref:
            fate_by_participant[participant_ref] = fate
    participants: dict[str, dict[str, Any]] = {}
    for item in active_roster:
        actor_key = text(item.get("group_user_id"))
        participant_ref = text(item.get("id"))
        if not actor_key or not participant_ref or actor_key in participants:
            raise _freeze_error("gameplay.participant_freeze_invalid", "有效参战者缺少唯一身份。")
        try:
            runtime_revision = int(item.get("runtime_revision") or 0)
        except (TypeError, ValueError, OverflowError):
            raise _freeze_error("gameplay.participant_freeze_invalid", "角色运行时版本无效。") from None
        if runtime_revision < 0:
            raise _freeze_error("gameplay.participant_freeze_invalid", "角色运行时版本无效。")
        stats = mapping(item.get("card_stats"))
        modifiers: dict[str, int] = {}
        for raw_key, raw_value in mapping(stats.get("modifiers")).items():
            key = text(raw_key)[:80]
            if not key:
                continue
            if isinstance(raw_value, bool):
                continue
            try:
                modifiers[key] = int(raw_value)
            except (TypeError, ValueError, OverflowError):
                raise _freeze_error("gameplay.participant_freeze_invalid", "角色属性修正值无效。") from None
            if not -100 <= modifiers[key] <= 100:
                raise _freeze_error("gameplay.participant_freeze_invalid", "角色属性修正值无效。")
        runtime = mapping(item.get("runtime_state"))
        fate = mapping(fate_by_participant.get(participant_ref))
        participants[actor_key] = {
            "label": text(item.get("character_name") or item.get("display_name"), "队伍成员"),
            "participant_ref": participant_ref,
            "actor_fate_ref": text(item.get("actor_fate_ref") or fate.get("character_id")),
            "runtime_revision": runtime_revision,
            "check_context": {
                "stat_modifiers": modifiers,
                "primary_stat": _primary_stat_from_roster(world, item),
                "advantage_sources": _active_advantage_sources(runtime),
                "statuses": _sanitized_statuses(runtime),
            },
            "visibility": "party",
        }
    if not participants:
        raise _freeze_error("gameplay.participants_unavailable", "当前没有已审核且正在参战的角色。")
    return participants, active_roster


def _freeze_tactical_site(
    world: Mapping[str, Any],
    template: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str], list[dict[str, Any]], str]:
    site_ref = text(template.get("site_ref"))
    sites = [
        mapping(item)
        for item in mapping(world.get("adventure_sites")).get("sites") or ()
        if isinstance(item, Mapping)
    ]
    site = next((item for item in sites if text(item.get("site_id")) == site_ref), {})
    if not site_ref or not site:
        raise _freeze_error("tactical.site_invalid", "战术模板引用的冒险站点不存在。")
    raw_zones = site.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise _freeze_error("tactical.site_invalid", "冒险站点缺少区域定义。")
    zone_rows: list[dict[str, Any]] = []
    zone_labels: dict[str, str] = {}
    for raw in raw_zones:
        if not isinstance(raw, Mapping):
            raise _freeze_error("tactical.site_invalid", "冒险站点区域定义无效。")
        zone = _copy(raw)
        zone_id = text(zone.get("zone_id"))
        if not zone_id or zone_id in zone_labels:
            raise _freeze_error("tactical.site_invalid", "冒险站点区域标识缺失或重复。")
        zone_labels[zone_id] = _freeze_label(zone, "区域")
        zone_rows.append(zone)
    if "edges" not in site or not isinstance(site.get("edges"), list):
        raise _freeze_error("tactical.site_invalid", "冒险站点缺少区域邻接定义。")
    edge_rows: list[dict[str, Any]] = []
    directed_edges: set[tuple[str, str]] = set()
    for raw in site.get("edges") or ():
        if not isinstance(raw, Mapping):
            raise _freeze_error("tactical.site_invalid", "冒险站点邻接定义无效。")
        source = text(raw.get("from") or raw.get("from_ref"))
        target = text(raw.get("to") or raw.get("to_ref"))
        if isinstance(raw.get("move_cost"), bool):
            raise _freeze_error("tactical.site_invalid", "区域邻接移动成本无效。")
        try:
            move_cost = int(raw.get("move_cost"))
        except (TypeError, ValueError, OverflowError):
            raise _freeze_error("tactical.site_invalid", "区域邻接移动成本无效。") from None
        bidirectional = bool(raw.get("bidirectional", True))
        if source not in zone_labels or target not in zone_labels or source == target or move_cost < 1:
            raise _freeze_error("tactical.site_invalid", "区域邻接引用、方向或移动成本无效。")
        occupied = {(source, target)}
        if bidirectional:
            occupied.add((target, source))
        if directed_edges & occupied:
            raise _freeze_error("tactical.site_invalid", "冒险站点包含重复区域邻接。")
        directed_edges.update(occupied)
        edge_rows.append({
            "from": source,
            "to": target,
            "move_cost": move_cost,
            "bidirectional": bidirectional,
        })
    if len(zone_rows) > 1 and not edge_rows:
        raise _freeze_error("tactical.site_invalid", "多区域冒险站点不能缺少邻接。")
    starting_zone = text(mapping(template.get("starting_zones")).get("party"))
    if starting_zone not in zone_labels:
        raise _freeze_error("tactical.site_invalid", "战术模板起始区域不属于冒险站点。")
    for exit_ref in site.get("exit_points") or ():
        if text(exit_ref) not in zone_labels:
            raise _freeze_error("tactical.site_invalid", "冒险站点撤退出口引用未知区域。")
    return site, zone_rows, zone_labels, edge_rows, starting_zone


def _resolve_intensity_profile(
    world: Mapping[str, Any],
    intensity_id: str,
) -> tuple[dict[str, Any], str]:
    intensity = mapping(world.get("adventure_intensity"))
    profiles = [mapping(item) for item in intensity.get("profiles") or () if isinstance(item, Mapping)]
    selected = text(intensity_id or intensity.get("default") or "balanced")
    profile = next((item for item in profiles if text(item.get("id")) == selected), {})
    if profiles and not profile:
        raise WebRouteError(
            409, "tactical.intensity_invalid", "所选冒险强度已失效。",
            "请刷新战术面板并重新选择世界声明的强度档位。",
        )
    return profile or {
        "id": selected,
        "label": "均衡",
        "threat_guard_delta": 0,
        "resource_delta": 0,
        "crises_per_site": 1,
    }, selected


def _freeze_intensity_profile(
    profile: Mapping[str, Any],
    *,
    site_ids: set[str],
    conflict_ids: set[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": text(profile.get("id")),
        "label": _freeze_label(profile, "均衡"),
        "summary": text(profile.get("summary")),
    }
    for field in (
        "threat_guard_delta", "resource_delta", "crises_per_site",
        "difficulty_delta", "clock_delta",
    ):
        if field in profile:
            try:
                result[field] = int(profile.get(field))
            except (TypeError, ValueError, OverflowError):
                raise _freeze_error("tactical.intensity_invalid", "冒险强度数值字段无效。") from None
    for field, allowed in (
        ("optional_site_refs", site_ids),
        ("optional_conflict_refs", conflict_ids),
    ):
        if field in profile:
            refs = [text(item) for item in profile.get(field) or () if text(item)]
            if len(refs) != len(set(refs)) or any(ref not in allowed for ref in refs):
                raise _freeze_error("tactical.intensity_invalid", "冒险强度包含未知或重复可选引用。")
            result[field] = refs
    if "mainline_policy" in profile:
        result["mainline_policy"] = text(profile.get("mainline_policy"))
        if result["mainline_policy"] != "preserve":
            raise _freeze_error("tactical.intensity_invalid", "冒险强度不得替换或过滤主线。")
    return {key: value for key, value in result.items() if value not in (None, "")}


def _freeze_elemental_contract(world: Mapping[str, Any]) -> tuple[dict[str, Any], set[str]]:
    raw = world.get("elemental")
    if raw in (None, {}):
        return {}, set()
    if not isinstance(raw, Mapping):
        raise _freeze_error("tactical.elemental_contract_invalid", "元素合同必须是对象。")
    elements = [text(item) for item in raw.get("elements") or () if text(item)]
    if not elements or len(elements) != len(set(elements)):
        raise _freeze_error("tactical.elemental_contract_invalid", "元素标识缺失或重复。")
    element_set = set(elements)
    exposure = mapping(raw.get("exposure"))
    if exposure:
        try:
            max_layers = int(exposure.get("max_layers"))
        except (TypeError, ValueError, OverflowError):
            raise _freeze_error("tactical.elemental_contract_invalid", "元素暴露层数无效。") from None
        if max_layers < 1 or not text(exposure.get("decay")):
            raise _freeze_error("tactical.elemental_contract_invalid", "元素暴露生命周期无效。")
    for collection, ref_fields in (
        (raw.get("interactions") or (), ("source_element", "target_selector")),
        (raw.get("reactions") or (), ("a", "b")),
    ):
        if not isinstance(collection, list):
            raise _freeze_error("tactical.elemental_contract_invalid", "元素交互或反应列表无效。")
        seen: set[str] = set()
        for item in collection:
            if not isinstance(item, Mapping):
                raise _freeze_error("tactical.elemental_contract_invalid", "元素交互或反应定义无效。")
            item_id = text(item.get("id"))
            refs = [text(item.get(field)) for field in ref_fields]
            if not item_id or item_id in seen or any(ref not in element_set for ref in refs):
                raise _freeze_error("tactical.elemental_contract_invalid", "元素交互或反应引用未知元素。")
            seen.add(item_id)
    affinities = raw.get("affinities") or {}
    if not isinstance(affinities, Mapping):
        raise _freeze_error("tactical.elemental_contract_invalid", "元素亲和定义无效。")
    for values in affinities.values():
        if not isinstance(values, Mapping):
            raise _freeze_error("tactical.elemental_contract_invalid", "元素亲和定义无效。")
        for element, value in values.items():
            if text(element) not in element_set or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise _freeze_error("tactical.elemental_contract_invalid", "元素亲和引用或数值无效。")
    return mapping(_safe_frozen_value(raw)), element_set


def _freeze_site_environment(
    site: Mapping[str, Any],
    world: Mapping[str, Any],
    element_keys: set[str],
) -> list[dict[str, Any]]:
    scene_environment = _world_module(world, "scene_environment")
    raw_states = scene_environment.get("states") or scene_environment.get("definitions") or ()
    if raw_states and (
        not isinstance(raw_states, list)
        or any(not isinstance(item, Mapping) for item in raw_states)
    ):
        raise _freeze_error("tactical.environment_invalid", "场景环境定义列表无效。")
    definitions: dict[str, dict[str, Any]] = {}
    for raw in raw_states:
        definition = mapping(raw)
        definition_id = text(definition.get("id"))
        if not definition_id or definition_id in definitions:
            raise _freeze_error("tactical.environment_invalid", "场景环境定义标识缺失或重复。")
        element_refs = [text(item) for item in definition.get("element_refs") or () if text(item)]
        if len(element_refs) != len(set(element_refs)) or any(item not in element_keys for item in element_refs):
            raise _freeze_error("tactical.environment_invalid", "场景环境引用未知或重复元素。")
        definitions[definition_id] = {**definition, "element_refs": element_refs}
    environment: list[dict[str, Any]] = []
    for raw in site.get("hazards") or ():
        if not isinstance(raw, Mapping):
            raise _freeze_error("tactical.environment_invalid", "站点危害定义无效。")
        hazard = _copy(raw)
        environment_ref = text(hazard.get("environment_ref"))
        if environment_ref:
            definition = definitions.get(environment_ref)
            if definition is None:
                raise _freeze_error("tactical.environment_invalid", "站点危害引用未知场景环境。")
            hazard["element_refs"] = list(definition["element_refs"])
            hazard["environment_definition"] = {
                key: frozen
                for key in ("id", "label", "summary", "element_refs", "operations", "visibility")
                if key in definition
                and (frozen := _safe_frozen_value(definition.get(key))) is not None
            }
        environment.append(hazard)
    return environment


def _world_stat_labels(world: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in mapping(mapping(world.get("rules")).get("entity_registry")).get("entities") or ():
        if not isinstance(raw, Mapping) or text(raw.get("entity_type")) != "stat":
            continue
        stat_id = text(raw.get("id"))
        label = text(raw.get("label"))
        if not stat_id or not label or stat_id in result:
            raise _freeze_error("tactical.action_checks_invalid", "冻结世界属性实体缺少唯一标识或标签。")
        result[stat_id] = label
    return result


def _freeze_action_checks(
    template: Mapping[str, Any],
    world: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_checks = template.get("action_checks")
    if not isinstance(raw_checks, list) or len(raw_checks) != len(STANDARD_TACTICAL_ACTIONS):
        raise _freeze_error("tactical.action_checks_invalid", "战术模板必须声明八种标准行动检定。")
    stat_labels = _world_stat_labels(world)
    if not stat_labels:
        raise _freeze_error("tactical.action_checks_invalid", "冻结世界缺少角色属性键。")
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_checks:
        if not isinstance(raw, Mapping):
            raise _freeze_error("tactical.action_checks_invalid", "标准行动检定定义无效。")
        action = text(raw.get("action_kind"))
        stat_ref = text(raw.get("stat_ref"))
        check_id = text(raw.get("id"))
        label = text(raw.get("label"))
        if (
            action not in STANDARD_TACTICAL_ACTIONS
            or action in seen
            or stat_ref not in stat_labels
            or not check_id
            or not label
            or stat_labels[stat_ref] not in label
        ):
            raise _freeze_error("tactical.action_checks_invalid", "标准行动检定重复、缺字段或引用未知属性。")
        item = {"id": check_id, "action_kind": action, "stat_ref": stat_ref, "label": label}
        if "difficulty_delta" in raw:
            try:
                item["difficulty_delta"] = int(raw.get("difficulty_delta"))
            except (TypeError, ValueError, OverflowError):
                raise _freeze_error("tactical.action_checks_invalid", "行动检定难度修正无效。") from None
        checks.append(item)
        seen.add(action)
    if seen != STANDARD_TACTICAL_ACTIONS:
        raise _freeze_error("tactical.action_checks_invalid", "标准行动检定种类不完整。")
    return sorted(checks, key=lambda item: item["action_kind"])


def _prepare_effect_source(
    source: Any,
    *,
    scope: str,
) -> tuple[Any, list[tuple[dict[str, Any], tuple[str, str], str, str]]]:
    references: list[tuple[dict[str, Any], tuple[str, str], str, str]] = []

    def freeze_branch(raw_effects: Any, branch: str) -> list[dict[str, Any]]:
        if not isinstance(raw_effects, list):
            raise _freeze_error("gameplay.effect_plan_invalid", "玩法效果分支必须是对象列表。")
        frozen: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_effects:
            if not isinstance(raw, Mapping):
                raise _freeze_error("gameplay.effect_plan_invalid", "玩法效果必须是对象。")
            effect = _copy(raw)
            target = (text(effect.get("module_id")), text(effect.get("state_key")))
            if (
                not all(target)
                or target[0] not in GAMEPLAY_RUNTIME_MODULES
                or target[0] == "actor_fate"
                or len(target[1]) > 160
                or target in seen
            ):
                raise _freeze_error("gameplay.effect_plan_invalid", "同一效果分支包含缺失或重复目标。")
            seen.add(target)
            effect.pop("expected_revision", None)
            frozen.append(effect)
            references.append((effect, target, scope, branch))
        return frozen

    if source in (None, {}, []):
        return _copy(source or {}), references
    if isinstance(source, Mapping):
        result = {
            text(branch): freeze_branch(raw_effects, text(branch))
            for branch, raw_effects in source.items()
            if text(branch)
        }
        if len(result) != len(source):
            raise _freeze_error("gameplay.effect_plan_invalid", "玩法效果包含空分支。")
        return result, references
    if isinstance(source, list):
        return freeze_branch(source, "default"), references
    raise _freeze_error("gameplay.effect_plan_invalid", "玩法效果定义格式无效。")


async def _bind_effect_revisions(
    database: Any,
    session_id: str,
    sources: list[tuple[str, Any]],
) -> tuple[dict[str, Any], str]:
    frozen_sources: dict[str, Any] = {}
    references: list[tuple[dict[str, Any], tuple[str, str], str, str]] = []
    for scope, source in sources:
        frozen, prepared = _prepare_effect_source(source, scope=scope)
        frozen_sources[scope] = frozen
        references.extend(prepared)
    targets = sorted({target for _effect, target, _scope, _branch in references})
    revisions: Mapping[Any, Any] = {}
    if targets:
        reader = getattr(database, "get_gameplay_state_revisions", None)
        if not callable(reader):
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本读取器不可用。")
        try:
            revisions = await reader(session_id, targets)
        except (TypeError, ValueError, OverflowError):
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本读取失败。") from None
        if not isinstance(revisions, Mapping) or any(target not in revisions for target in targets):
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本读取结果不完整。")
    fingerprint_rows: list[dict[str, Any]] = []
    for effect, target, scope, branch in references:
        raw_revision = revisions.get(target)
        if isinstance(raw_revision, bool):
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本无效。")
        try:
            revision = int(raw_revision)
        except (TypeError, ValueError, OverflowError):
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本无效。") from None
        if revision < 0:
            raise _freeze_error("gameplay.effect_revision_unavailable", "玩法效果目标版本无效。")
        effect["expected_revision"] = revision
        fingerprint_rows.append({
            "scope": scope,
            "branch": branch,
            "module_id": target[0],
            "state_key": target[1],
            "expected_revision": revision,
        })
    encoded = json.dumps(
        sorted(fingerprint_rows, key=lambda item: (item["scope"], item["branch"], item["module_id"], item["state_key"])),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return frozen_sources, "sha256:" + hashlib.sha256(encoded).hexdigest()


def _item_catalog(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    definition = _world_module(world, "items_inventory") or mapping(mapping(world.get("rules")).get("item_catalog"))
    raw_rows = definition.get("items") or definition.get("definitions") or ()
    if raw_rows == ():
        return {}
    if not isinstance(raw_rows, list) or any(not isinstance(item, Mapping) for item in raw_rows):
        raise _freeze_error("tactical.item_freeze_invalid", "物品定义列表无效。")
    rows = [mapping(item) for item in raw_rows]
    catalog: dict[str, dict[str, Any]] = {}
    for item in rows:
        item_id = text(item.get("item_id") or item.get("id"))
        if not item_id or item_id in catalog:
            raise _freeze_error("tactical.item_freeze_invalid", "物品定义标识缺失或重复。")
        catalog[item_id] = item
    return catalog


def _capability_catalog(world: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    definition = _world_module(world, "capability_effects")
    raw_rows = definition.get("definitions") or ()
    if raw_rows == ():
        return {}
    if not isinstance(raw_rows, list) or any(not isinstance(item, Mapping) for item in raw_rows):
        raise _freeze_error("tactical.capability_freeze_invalid", "能力定义列表无效。")
    rows = [mapping(item) for item in raw_rows]
    catalog: dict[str, dict[str, Any]] = {}
    for item in rows:
        capability_id = text(item.get("id") or item.get("capability_id"))
        if not capability_id or capability_id in catalog:
            raise _freeze_error("tactical.capability_freeze_invalid", "能力定义标识缺失或重复。")
        catalog[capability_id] = item
    return catalog


def _frozen_capability_definition(
    definition: Mapping[str, Any],
    *,
    element_keys: set[str],
) -> dict[str, Any]:
    allowed = (
        "id", "capability_id", "capability_type_ref", "label", "summary",
        "description", "effects", "costs", "targeting", "usage_constraints",
        "failure_forward", "narrative_only", "operations", "ops", "tags",
        "check_modifier", "limitation", "tactical_action_kinds",
        "element_ref", "target", "layers", "element_target",
        "exposure_layers", "failure_exposure_layers",
    )
    frozen_definition = {
        key: frozen
        for key in allowed
        if key in definition
        and (frozen := _safe_frozen_value(definition.get(key))) is not None
    }
    element_ref = text(frozen_definition.get("element_ref"))
    if element_ref and element_ref not in element_keys:
        raise _freeze_error("tactical.capability_freeze_invalid", "能力定义引用未知元素。")
    for field in ("layers", "exposure_layers", "failure_exposure_layers"):
        if field not in frozen_definition:
            continue
        value = frozen_definition[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _freeze_error("tactical.capability_freeze_invalid", "能力元素层数无效。")
    return frozen_definition


async def _freeze_tactical_choices(
    database: Any,
    session_id: str,
    roster: list[dict[str, Any]],
    world: Mapping[str, Any],
    *,
    request_key: str,
    element_keys: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    item_reader = getattr(database, "list_item_instances", None)
    capability_reader = getattr(database, "list_actor_capabilities", None)
    resource_reader = getattr(database, "list_character_resources", None)
    items_catalog = _item_catalog(world)
    capabilities_catalog = _capability_catalog(world)
    available_items: list[dict[str, Any]] = []
    available_capabilities: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    seen_capabilities: set[tuple[str, str]] = set()
    for roster_item in roster:
        owner_ref = text(roster_item.get("group_user_id"))
        participant_ref = text(roster_item.get("id"))
        if callable(item_reader):
            item_rows = await item_reader(session_id, participant_ref) or ()
            if not item_rows and participant_ref != owner_ref:
                item_rows = await item_reader(session_id, owner_ref) or ()
            for item in item_rows:
                raw = mapping(item)
                instance_id = text(raw.get("id"))
                item_id = text(raw.get("item_id"))
                instance_owner_type = text(raw.get("owner_type"))
                instance_owner_ref = text(raw.get("owner_ref"))
                if not instance_id or not item_id or not instance_owner_type or not instance_owner_ref:
                    raise _freeze_error("tactical.item_freeze_invalid", "真实物品实例缺少实例或所有者身份。")
                if instance_id in seen_instances or item_id not in items_catalog:
                    raise _freeze_error("tactical.item_freeze_invalid", "物品实例重复或缺少世界定义。")
                if instance_owner_ref not in {participant_ref, owner_ref}:
                    raise _freeze_error("tactical.item_freeze_invalid", "物品实例所有者与参战者不一致。")
                seen_instances.add(instance_id)
                try:
                    quantity = int(raw.get("quantity") or 0)
                    durability = int(raw.get("durability") or 0)
                    charges = int(raw.get("charges") or 0)
                except (TypeError, ValueError, OverflowError):
                    raise _freeze_error("tactical.item_freeze_invalid", "物品实例资源数值无效。") from None
                if min(quantity, durability, charges) < 0:
                    raise _freeze_error("tactical.item_freeze_invalid", "物品实例资源数值无效。")
                if quantity <= 0:
                    continue
                definition = items_catalog[item_id]
                use_effects = definition.get("use_effects") or definition.get("effects") or []
                if not isinstance(use_effects, list):
                    raise _freeze_error("tactical.item_freeze_invalid", "物品使用效果定义无效。")
                local_id = "tactical-item:" + hashlib.sha256(
                    f"{request_key}\0{instance_id}".encode("utf-8")
                ).hexdigest()[:24]
                available_items.append({
                    "id": local_id,
                    "item_id": item_id,
                    "instance_id": instance_id,
                    "instance_owner_type": instance_owner_type,
                    "instance_owner_ref": instance_owner_ref,
                    "label": _freeze_label(definition, "已持有装备"),
                    "owner_ref": owner_ref,
                    "quantity": quantity,
                    "durability": durability,
                    "charges": charges,
                    "use_effects": _safe_frozen_value(use_effects),
                })
        if callable(capability_reader):
            actor_ref = f"character:{participant_ref}"
            capability_rows = await capability_reader(session_id, actor_ref, available_only=True) or ()
            resources: dict[str, dict[str, int]] = {}
            if capability_rows:
                if not callable(resource_reader):
                    raise _freeze_error("tactical.capability_freeze_invalid", "能力资源读取器不可用。")
                for raw_resource in await resource_reader(session_id, participant_ref) or ():
                    resource = mapping(raw_resource)
                    resource_ref = text(resource.get("resource_ref"))
                    if not resource_ref or resource_ref in resources:
                        raise _freeze_error("tactical.capability_freeze_invalid", "能力资源标识缺失或重复。")
                    try:
                        current = int(resource.get("current") or 0)
                        maximum = int(resource.get("maximum") or 0)
                    except (TypeError, ValueError, OverflowError):
                        raise _freeze_error("tactical.capability_freeze_invalid", "能力资源数值无效。") from None
                    if current < 0 or maximum < 0 or current > maximum:
                        raise _freeze_error("tactical.capability_freeze_invalid", "能力资源数值无效。")
                    resources[resource_ref] = {"current": current, "maximum": maximum}
            for item in capability_rows:
                raw = mapping(item)
                if "available" in raw and not bool(raw.get("available")):
                    continue
                instance_id = text(raw.get("instance_id"))
                capability_ref = text(raw.get("capability_ref"))
                identity = (owner_ref, capability_ref)
                definition = mapping(capabilities_catalog.get(capability_ref))
                if not instance_id or not capability_ref or not definition or identity in seen_capabilities:
                    raise _freeze_error("tactical.capability_freeze_invalid", "能力实例重复、缺身份或缺少唯一世界定义。")
                seen_capabilities.add(identity)
                frozen_definition = _frozen_capability_definition(
                    definition,
                    element_keys=element_keys,
                )
                if not text(frozen_definition.get("id") or frozen_definition.get("capability_id")):
                    raise _freeze_error("tactical.capability_freeze_invalid", "能力定义缺少标识。")
                if frozen_definition.get("usage_constraints") not in (None, "", [], {}):
                    raise _freeze_error("tactical.capability_freeze_invalid", "能力使用条件尚不能由战术冻结上下文完整证明。")
                action_kinds = [
                    text(value)
                    for value in frozen_definition.get("tactical_action_kinds") or ()
                    if text(value)
                ]
                if action_kinds and (
                    len(action_kinds) != len(set(action_kinds))
                    or any(value not in STANDARD_TACTICAL_ACTIONS for value in action_kinds)
                ):
                    raise _freeze_error("tactical.capability_freeze_invalid", "能力适用战术行动定义重复或未知。")
                if not bool(frozen_definition.get("narrative_only")) and not action_kinds:
                    raise _freeze_error("tactical.capability_freeze_invalid", "机械能力缺少适用战术行动定义。")
                encoded_definition = json.dumps(frozen_definition, ensure_ascii=False, sort_keys=True)
                if "end_instance" in encoded_definition:
                    raise _freeze_error("tactical.capability_freeze_invalid", "结束既有效果实例的能力缺少冻结前像。")
                raw_instance_state = mapping(raw.get("state"))
                raw_instance_state.pop("resources", None)
                raw_instance_state.pop("runtime_effects", None)
                instance_state = {
                    **mapping(_safe_frozen_value(raw_instance_state)),
                    "available": True,
                    "resources": _copy(resources),
                    "runtime_effects": [],
                }
                limitation = text(frozen_definition.get("limitation"))
                if bool(frozen_definition.get("narrative_only")):
                    limitation = "仅用于叙事声明，不产生规则数值变化"
                available_capabilities.append({
                    "id": capability_ref,
                    "owner_ref": owner_ref,
                    "instance_id": instance_id,
                    "actor_ref": actor_ref,
                    "participant_ref": participant_ref,
                    "definition_version": int(raw.get("definition_version") or 1),
                    "source_ref": text(raw.get("source_ref")),
                    "persistence_scope": text(raw.get("persistence_scope")),
                    "label": _freeze_label(frozen_definition, capability_ref),
                    "summary": text(frozen_definition.get("summary") or frozen_definition.get("description")),
                    "limitation": limitation,
                    "definition": frozen_definition,
                    "instance_state": instance_state,
                })
    if len(available_items) > 64 or len(available_capabilities) > 64:
        raise _freeze_error("tactical.choice_freeze_invalid", "本场可用物品或能力超过安全上限。")
    return available_items, available_capabilities


__all__ = [name for name in globals() if name.startswith("_")] + ["STANDARD_TACTICAL_ACTIONS"]
