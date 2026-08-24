"""Audience-safe, component-specific RC10 surface DTO projectors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import integer, mapping, text
from .keys import OpaqueKeyFactory
from .surface_registry import registry_entry


class UnsupportedSurfaceError(ValueError):
    """Raised when a compiled manifest references no installed renderer."""


_PHASE_LABELS = {
    "setup": "准备冲突",
    "declare": "声明行动",
    "locked": "行动已锁定",
    "resolve_players": "结算玩家行动",
    "resolve_opposition": "结算敌方行动",
    "environment": "推进环境",
    "settle_round": "回合收束",
    "victory": "目标达成",
    "partial_success": "带代价达成",
    "retreat": "已安全撤退",
    "negotiated": "已达成停战",
    "defeat_forward": "失败后继续推进",
    "aborted_by_host": "主持人已中止",
}
_CHALLENGE_MODE_LABELS = {
    "investigation": "调查", "social": "交涉", "chase": "追逐",
    "rescue": "救援", "hazard": "环境风险", "infiltration": "潜入",
    "ritual": "仪式", "choice": "抉择", "tactical": "战术冲突",
}
_CHALLENGE_PHASE_LABELS = {
    "inactive": "尚未开始", "setup": "准备挑战", "declare": "声明行动",
    "locked": "行动已锁定", "resolve": "结算行动", "settle": "阶段收束",
    "ended": "挑战已结束",
}
_CHALLENGE_OUTCOME_LABELS = {
    "active": "挑战继续", "success": "目标达成", "partial": "带代价达成",
    "failure_forward": "失败后推进", "retreat": "已撤出挑战",
    "negotiated": "已达成协商", "aborted": "主持人已中止",
}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _visible(raw: Mapping[str, Any], role: str) -> bool:
    audience = raw.get("audience") or raw.get("visibility") or raw.get("disclosure_scope")
    if isinstance(audience, Mapping):
        if role in {"dm", "admin"}:
            return bool(audience.get("public", True) or audience.get("host") or audience.get("dm") or audience.get("admin"))
        return bool(audience.get("public", True) or audience.get("player"))
    scopes = {str(item).lower() for item in _sequence(audience)}
    if not scopes and audience not in (None, ""):
        scopes = {str(audience).lower()}
    if not scopes:
        return True
    if role in {"dm", "admin"}:
        return bool(scopes & {"public", "party", "player", "host", "dm", "admin", "readonly"})
    return bool(scopes & {"public", "party", "player", "readonly", "spectator"})


def _label(raw: Mapping[str, Any], fallback: str = "") -> str:
    return text(raw.get("label") or raw.get("name") or raw.get("title"), limit=120, default=fallback)


def _summary(raw: Mapping[str, Any]) -> str:
    return text(
        raw.get("summary") or raw.get("description") or raw.get("public_summary")
        or raw.get("effect") or raw.get("objective"),
        limit=260,
    )


def _public_rows(value: Any, *, role: str, keys: OpaqueKeyFactory, kind: str, limit: int = 40) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, value_item in enumerate(_sequence(value)):
        raw = mapping(value_item)
        if not raw or not _visible(raw, role):
            continue
        label = _label(raw)
        if not label:
            continue
        item: dict[str, Any] = {
            "key": keys.key(kind, f"{index}:{label}"),
            "label": label,
            "summary": _summary(raw),
        }
        state = text(raw.get("state") or raw.get("status") or raw.get("phase"), limit=40)
        if state:
            item["state"] = state
        for source, target in (
            ("current", "current"), ("progress", "progress"), ("total", "total"),
            ("deadline", "deadline"), ("risk", "risk"), ("limitation", "limitation"),
            ("failure_forward", "failure_forward"), ("recent_change", "recent_change"),
        ):
            value = raw.get(source)
            if isinstance(value, (str, int, float)) and str(value).strip():
                item[target] = value if isinstance(value, (int, float)) else text(value, limit=220)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _module_definition(world: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    return mapping(world.get(module_id)) or mapping(mapping(world.get("rules")).get(module_id))


def _module_state(runtime: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    return mapping(mapping(mapping(runtime.get("modules")).get(module_id)).get("state"))


def _gameplay_state(gameplay: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    items = [mapping(item) for item in gameplay.get("items") or ()]
    active = next((item for item in items if text(item.get("state_key")) == "active"), items[0] if items else {})
    return mapping(active.get("state")), integer(active.get("revision"), 0)


def _definition_items(definition: Mapping[str, Any]) -> Any:
    for key in (
        "templates", "quests", "objectives", "facts", "npcs", "states", "items",
        "recipes", "assemblies", "accords", "rumors", "elements", "endings",
        "conditions", "tracks", "regions", "nodes", "definitions",
    ):
        if definition.get(key):
            return definition.get(key)
    return []


def _tactical_data(state: Mapping[str, Any], *, role: str, keys: OpaqueKeyFactory, principal_ref: str) -> dict[str, Any]:
    zone_rows = _sequence(state.get("zones"))
    zones = _public_rows(zone_rows, role=role, keys=keys, kind="tactical-zone")
    objectives = _public_rows(state.get("objectives"), role=role, keys=keys, kind="tactical-objective")
    actors: list[dict[str, Any]] = []
    participant_source = state.get("participants")
    participant_rows = (
        [(str(key), value) for key, value in participant_source.items()]
        if isinstance(participant_source, Mapping)
        else [("", value) for value in _sequence(participant_source)]
    )
    for index, (source_ref, raw_value) in enumerate(participant_rows):
        raw = mapping(raw_value)
        label = _label(raw, "队伍成员")
        own = principal_ref and principal_ref in {
            source_ref, text(raw.get("actor_key")), text(raw.get("user_id")), text(raw.get("group_user_id"))
        }
        if not own and role not in {"dm", "admin"} and not _visible(raw, role):
            continue
        budget = mapping(raw.get("action_budget")) if own or role in {"dm", "admin"} else {}
        actors.append({
            "key": keys.key("tactical-actor", f"{index}:{label}"),
            "label": label,
            "zone_label": text(raw.get("zone_label"), limit=100),
            "fate_label": text(raw.get("fate_label") or raw.get("fate"), limit=80),
            "guard_label": text(raw.get("guard_label") or raw.get("guard"), limit=80),
            "conditions": [text(item, limit=80) for item in raw.get("conditions") or () if text(item, limit=80)][:8],
            "action_budget": {
                key: max(0, integer(budget.get(key), 0))
                for key in ("major", "maneuver", "reaction")
            } if budget else {},
            "is_self": bool(own),
        })
    threats = _public_rows(
        state.get("known_threats") or state.get("threats"),
        role=role, keys=keys, kind="tactical-threat",
    )
    capabilities = _public_rows(
        [
            item for item in _sequence(state.get("available_capabilities"))
            if role in {"dm", "admin"} or text(mapping(item).get("owner_ref")) == principal_ref
        ],
        role=role, keys=keys, kind="tactical-capability", limit=32,
    )
    items = _public_rows(
        [
            item for item in _sequence(state.get("available_items"))
            if role in {"dm", "admin"} or text(mapping(item).get("owner_ref")) == principal_ref
        ],
        role=role, keys=keys, kind="tactical-item", limit=32,
    )
    public_refs: dict[str, str] = {}
    for source, names in (
        (state.get("zones"), ("zone_id", "zone_ref", "id", "ref")),
        (state.get("objectives"), ("id", "objective_id", "objective_ref", "ref")),
        (state.get("known_threats") or state.get("threats"), ("threat_id", "id", "ref")),
        (state.get("available_capabilities"), ("id", "capability_id", "ref")),
        (state.get("available_items"), ("id", "item_id", "ref")),
    ):
        for raw_value in _sequence(source):
            raw = mapping(raw_value)
            ref = next((text(raw.get(name), limit=160) for name in names if text(raw.get(name), limit=160)), "")
            if ref:
                public_refs[ref] = _label(raw, "已选择对象")
    if isinstance(state.get("participants"), Mapping):
        for ref, raw_value in state["participants"].items():
            public_refs[str(ref)] = _label(mapping(raw_value), "队伍成员")

    def public_change(value: Any) -> str:
        result = text(value, limit=240)
        for ref, label in sorted(public_refs.items(), key=lambda item: len(item[0]), reverse=True):
            result = result.replace(ref, label)
        return result
    receipts = []
    receipt_source = list(state.get("resolved_receipts") or ())[-6:] + list(state.get("locked_receipts") or ())[-4:]
    for index, raw_value in enumerate(receipt_source[-8:]):
        raw = mapping(raw_value)
        band = text(raw.get("result_band"))
        receipts.append({
            "key": keys.key("tactical-receipt", f"{index}:{text(raw.get('receipt_id'))}"),
            "actor_label": text(raw.get("actor_label"), limit=100, default="队伍成员"),
            "action_label": {
                "strike": "攻击", "guard": "防守", "maneuver": "移动", "cast": "施展能力",
                "interact": "处理目标", "aid": "援助", "retreat": "撤退", "parley": "谈判",
            }.get(text(raw.get("action_kind")), "执行行动"),
            "result_label": {
                "pending": "等待主持人锁定", "critical": "关键成功", "success": "成功",
                "success_with_cost": "带代价成功", "failure_forward": "失败后推进",
            }.get(band, "状态已更新"),
            "outcome_label": _PHASE_LABELS.get(text(raw.get("outcome")), "冲突继续"),
            "roll_summary": (
                f"d20 {integer(raw.get('roll'), 0)} + {integer(raw.get('modifier'), 0)}，难度 {integer(raw.get('difficulty'), 0)}"
                if raw.get("roll") is not None else ""
            ),
            "changes": [public_change(item) for item in raw.get("effects") or () if public_change(item)][:6],
        })
    return {
        "phase": {"key": text(state.get("phase"), limit=40), "label": _PHASE_LABELS.get(text(state.get("phase")), "等待开始")},
        "round": max(1, integer(state.get("round"), 1)),
        "objective_summary": text(state.get("objective"), limit=220),
        "objectives": objectives,
        "zones": zones,
        "actors": actors,
        "known_threats": threats,
        "telegraphs": [text(item, limit=180) for item in state.get("telegraphs") or () if text(item, limit=180)][:8],
        "environment": _public_rows(state.get("environment"), role=role, keys=keys, kind="tactical-environment", limit=12),
        "escape_routes": _public_rows(state.get("escape_routes"), role=role, keys=keys, kind="tactical-escape", limit=8),
        "negotiation_options": _public_rows(state.get("negotiation_options"), role=role, keys=keys, kind="tactical-parley", limit=8),
        "available_capabilities": capabilities,
        "available_items": items,
        "pending_intent": mapping(mapping(state.get("pending_intents")).get(principal_ref)).get("action_kind") if principal_ref else "",
        "intensity": {
            "label": _label(mapping(state.get("intensity")), ""),
            "summary": _summary(mapping(state.get("intensity"))),
        },
        "recent_receipts": receipts,
    }


def _challenge_data(state: Mapping[str, Any], *, role: str, keys: OpaqueKeyFactory) -> dict[str, Any]:
    mode = text(state.get("mode"), limit=40)
    phase = text(state.get("phase"), limit=40)
    objectives_source = state.get("objectives")
    if not objectives_source and text(state.get("objective")):
        objectives_source = [{
            "label": text(state.get("objective"), limit=160),
            "summary": text(state.get("objective_summary"), limit=260),
            "progress": integer(state.get("progress"), 0),
            "total": integer(state.get("target"), 0),
            "failure_forward": text(state.get("failure_forward"), limit=220),
        }]
    receipts: list[dict[str, Any]] = []
    for index, raw_value in enumerate(list(state.get("receipts") or ())[-8:]):
        raw = mapping(raw_value)
        receipts.append({
            "key": keys.key("challenge-receipt", f"{index}:{text(raw.get('receipt_id'))}"),
            "label": {
                "success": "行动成功", "partial": "带代价推进",
                "failure_forward": "失败后推进",
            }.get(text(raw.get("result_band")), "挑战状态已更新"),
            "summary": text(raw.get("reason"), limit=220),
            "progress": integer(raw.get("progress_after"), 0),
            "state": _CHALLENGE_OUTCOME_LABELS.get(text(raw.get("outcome"), limit=40), "挑战状态已更新"),
        })
    detail_sources = {
        "investigation": state.get("evidence") or state.get("hypotheses"),
        "social": state.get("positions") or state.get("stakes"),
        "chase": state.get("routes") or state.get("hazards"),
        "rescue": state.get("rescue_windows") or state.get("targets"),
        "hazard": state.get("hazards") or state.get("mitigations"),
        "infiltration": state.get("zones") or state.get("entry_points"),
        "ritual": state.get("steps") or state.get("materials"),
        "choice": state.get("options") or state.get("choices"),
        "tactical": state.get("objectives"),
    }
    return {
        "mode": {"key": mode, "label": _CHALLENGE_MODE_LABELS.get(mode, "当前挑战")},
        "phase": {"key": phase, "label": _CHALLENGE_PHASE_LABELS.get(phase, "等待开始")},
        "round": max(1, integer(state.get("round"), 1)),
        "objective_summary": text(state.get("objective"), limit=240),
        "risk_summary": text(state.get("risk") or state.get("risk_summary"), limit=240),
        "failure_forward": text(state.get("failure_forward"), limit=240),
        "progress": max(0, integer(state.get("progress"), 0)),
        "target": max(0, integer(state.get("target"), 0)),
        "objectives": _public_rows(objectives_source, role=role, keys=keys, kind="challenge-objective", limit=12),
        "options": _public_rows(state.get("options") or state.get("choices"), role=role, keys=keys, kind="challenge-option", limit=16),
        "mode_details": _public_rows(detail_sources.get(mode), role=role, keys=keys, kind=f"challenge-{mode or 'detail'}", limit=20),
        "telegraphs": [text(item, limit=180) for item in state.get("telegraphs") or () if text(item, limit=180)][:8],
        "recent_receipts": receipts,
    }


def _surface_action(
    intent: str,
    label: str,
    revision: int,
    *,
    fields: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    action = {
        "action_id": "surface-" + intent.replace(".", "-"),
        "intent": intent,
        "label": label,
        "endpoint": "sessions/gameplay",
        "target_kind": "session",
        "expected_revision": revision,
        "transportReady": True,
        "focus_return": "opener",
    }
    if fields:
        action["fields"] = [dict(item) for item in fields]
    return action


def project_surface(
    surface: Mapping[str, Any],
    *,
    world: Mapping[str, Any],
    runtime: Mapping[str, Any],
    gameplay: Mapping[str, Any] | None,
    role: str,
    keys: OpaqueKeyFactory,
    principal_ref: str = "",
) -> dict[str, Any]:
    component = text(surface.get("component_kind"), limit=60)
    registered = registry_entry(component)
    if registered is None or registered.get("data_kind") != surface.get("data_kind"):
        raise UnsupportedSurfaceError("世界声明了当前插件未注册的可视化组件")
    module_id = text(surface.get("module_id"), limit=80)
    definition = _module_definition(world, module_id)
    state = _module_state(runtime, module_id)
    gameplay_state, gameplay_revision = _gameplay_state(mapping(gameplay))
    if gameplay_state:
        state = gameplay_state
    # Gameplay rows have an independent CAS counter.  Mixing it with the
    # world/runtime revision makes a freshly projected action stale whenever
    # the world state happens to have advanced further.
    revision = gameplay_revision if gameplay_state else integer(runtime.get("revision"), 0)

    if component == "tactical_board":
        data = _tactical_data(state, role=role, keys=keys, principal_ref=principal_ref)
    elif component == "challenge_board" and state.get("mode"):
        data = _challenge_data(state, role=role, keys=keys)
    elif component == "world_overview":
        data = {
            "title": text(world.get("name"), limit=120),
            "summary": text(world.get("summary") or world.get("description"), limit=500),
            "players": text(world.get("recommended_players"), limit=60),
            "tags": [text(item, limit=50) for item in world.get("display_tags") or () if text(item, limit=50)][:12],
        }
    elif component == "content_inventory":
        content_labels = {
            "acts": "故事幕", "scenes": "场景", "quests": "任务",
            "main_quests": "主线任务", "side_quests": "支线任务",
            "challenges": "挑战", "characters": "人物", "npcs": "人物",
            "endings": "顶层结局", "ending_variants": "结局变体",
            "evidence_chains": "证据链", "quick_builds": "快速建卡",
            "factions": "阵营", "adventure_sites": "冒险站点",
            "dynamic_crises": "动态危机", "interludes": "幕间事件",
        }
        data = {"items": [
            {"label": content_labels.get(str(key), "其他内容"), "count": integer(value, 0)}
            for key, value in sorted(mapping(world.get("content_stats")).items())
            if isinstance(value, (int, float))
        ]}
    elif component == "character_build_catalog":
        data = {"items": _public_rows(
            definition.get("quick_builds") or definition.get("presets") or definition.get("characters"),
            role=role, keys=keys, kind="character-build",
        )}
    elif component == "readme_reader":
        index = mapping(world.get("readme_index"))
        data = {"sections": _public_rows(index.get("sections"), role=role, keys=keys, kind="readme-section")}
    else:
        runtime_items = (
            state.get("items") or state.get("entries") or state.get("objectives")
            or state.get("actors") or state.get("states") or state.get("rumors")
            or state.get("accords") or state.get("assemblies") or state.get("elements")
            or state.get("conditions") or state.get("endings")
        )
        source_items = runtime_items or _definition_items(definition)
        data = {"items": _public_rows(source_items, role=role, keys=keys, kind=component)}
        if component == "element_matrix":
            data["interactions"] = _public_rows(
                definition.get("interactions") or definition.get("reactions"),
                role=role, keys=keys, kind="element-interaction", limit=24,
            )
        if component == "environment_board":
            data["mitigations"] = _public_rows(
                state.get("mitigations") or definition.get("mitigations"),
                role=role, keys=keys, kind="environment-mitigation", limit=12,
            )
    actions: list[dict[str, Any]] = []
    challenge_terminal = text(state.get("phase")) == "ended"
    tactical_terminal = text(state.get("phase")) in {"victory", "partial_success", "retreat", "negotiated", "defeat_forward", "aborted_by_host"}
    if component == "challenge_board" and (not state.get("mode") or challenge_terminal) and role in {"dm", "admin"}:
        templates = [mapping(item) for item in _sequence(definition.get("templates")) if mapping(item)]
        template_options = [
            {
                "value": keys.key("challenge-template", f"{index}:{_label(item)}"),
                "label": _label(item, "世界挑战"),
            }
            for index, item in enumerate(templates)
            if text(item.get("mode"), limit=40)
        ]
        actions.append(_surface_action(
            "challenge.start", "开始世界挑战", revision,
            fields=(
                {"name": "template_key", "type": "select", "label": "世界挑战模板", "required": True, "options": template_options},
            ),
        ))
    if component == "tactical_board" and (not state or tactical_terminal) and role in {"dm", "admin"}:
        conflicts = [mapping(item) for item in _sequence(definition.get("conflicts") or definition.get("templates")) if mapping(item)]
        intensity_block = mapping(world.get("adventure_intensity"))
        intensity_options = [
            {"value": text(item.get("id")), "label": _label(item, "冒险强度")}
            for item in (mapping(value) for value in _sequence(intensity_block.get("profiles")))
            if text(item.get("id"))
        ]
        start_fields: list[dict[str, Any]] = [
            {"name": "template_key", "type": "select", "label": "战术冲突模板", "required": True, "options": [
                {"value": keys.key("tactical-template", f"{index}:{_label(item)}"), "label": _label(item, "战术冲突")}
                for index, item in enumerate(conflicts)
            ]},
        ]
        if intensity_options:
            start_fields.append({
                "name": "intensity", "type": "select", "label": "冒险强度", "required": True,
                "default": text(intensity_block.get("default"), limit=40, default="balanced"),
                "options": intensity_options,
            })
        actions.append(_surface_action(
            "tactical.conflict.start", "开始战术冲突", revision,
            fields=start_fields,
        ))
    if (
        component == "tactical_board"
        and text(state.get("phase"), limit=40) == "declare"
        and (role in {"dm", "admin"} or bool(principal_ref))
    ):
        tactical_fields = (
            {"name": "action_kind", "type": "select", "label": "行动方式", "required": True, "options": [
                {"value": value, "label": label} for value, label in (
                    ("strike", "攻击"), ("guard", "防守"), ("maneuver", "移动"), ("cast", "施展能力或物品"),
                    ("interact", "处理目标"), ("aid", "援助"), ("retreat", "撤退"), ("parley", "谈判"),
                )
            ]},
            {"name": "description", "type": "textarea", "label": "行动说明", "required": True, "maxLength": 500},
            {"name": "target_key", "type": "select", "label": "人物或威胁目标", "required": False, "options": [
                {"value": item["key"], "label": item["label"]} for item in list(data.get("known_threats") or ()) + list(data.get("actors") or ())
            ]},
            {"name": "zone_key", "type": "select", "label": "目标区域", "required": False, "options": [
                {"value": item["key"], "label": item["label"]} for item in data.get("zones") or ()
            ]},
            {"name": "objective_key", "type": "select", "label": "公开目标", "required": False, "options": [
                {"value": item["key"], "label": item["label"]} for item in data.get("objectives") or ()
            ]},
            {"name": "capability_or_item_key", "type": "select", "label": "能力或装备", "required": False, "options": [
                {"value": item["key"], "label": item["label"]} for item in list(data.get("available_capabilities") or ()) + list(data.get("available_items") or ())
            ]},
        )
        actions.extend(_surface_action(intent, label, revision, fields=tactical_fields) for intent, label in (
                ("tactical.action.draft", "整理行动草稿"),
                ("tactical.action.preview", "预览已知影响"),
                ("tactical.action.commit", "确认提交行动"),
                ("tactical.withdraw.commit", "确认撤退"),
                ("tactical.negotiate.commit", "提出谈判"),
            ))
    if component == "tactical_board" and role in {"dm", "admin"} and state and not tactical_terminal:
        actions.extend((
            _surface_action("tactical.phase.advance", "推进战术阶段", revision),
            _surface_action("tactical.correction.apply", "记录战术纠错", revision),
            _surface_action("tactical.conflict.end", "结束战术冲突", revision),
        ))
    if component == "challenge_board" and state.get("mode") and text(state.get("phase")) == "declare":
        actions.extend(_surface_action(intent, label, revision) for intent, label in (
            ("challenge.action.draft", "整理挑战草稿"),
            ("challenge.action.preview", "预览已知影响"),
            ("challenge.action.commit", "确认挑战行动"),
            ("challenge.withdraw.commit", "退出当前挑战"),
            ("challenge.negotiate.commit", "提出协商方案"),
        ))
    if component == "challenge_board" and state.get("mode") and role in {"dm", "admin"} and text(state.get("phase")) != "ended":
        actions.extend((
            _surface_action("challenge.phase.advance", "推进挑战阶段", revision),
            _surface_action("challenge.end", "结束当前挑战", revision),
        ))
    return {
        "surface_key": text(surface.get("surface_key"), limit=96),
        "component_kind": component,
        "data_kind": text(surface.get("data_kind"), limit=80),
        "contract": {
            "world_revision": text(surface.get("_world_revision") or mapping(mapping(world.get("ui_profile")).get("ui_surface_manifest")).get("world_revision"), limit=100),
            "surface_manifest_revision": text(surface.get("_manifest_revision") or mapping(mapping(world.get("ui_profile")).get("ui_surface_manifest")).get("manifest_revision"), limit=100),
            "state_revision": revision,
            "audience_scope": "host" if role in {"dm", "admin"} else "player",
        },
        "copy": mapping(surface.get("copy")),
        "visual_recipe": text(surface.get("visual_recipe"), limit=80),
        "mobile_presentation": text(surface.get("mobile_presentation"), limit=64),
        "data": data,
        "actions": actions,
    }


__all__ = ["UnsupportedSurfaceError", "project_surface"]
