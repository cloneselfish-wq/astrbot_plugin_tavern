"""角色构筑设计器服务。

提供候选解析、构筑模拟、机械效果归约、模板/角色卡差异与因果预览，
供 WebUI 设计器与群聊建卡共用同一后端权威逻辑。
"""
from __future__ import annotations

# TWP actor authoring helpers.
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..card_wizard import PRESET_REFS_KEY, field_visible, preset_options
from ..lifecycle import (
    CARD_STAGE_A,
    card_template,
    field_stage,
    resolve_profession_stats,
    stage_field_projection,
    staged_creation,
)
from ..presets import validate_preset_selection
from .commands import preview_command


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip()[:maximum]


def _actor_template(world: Mapping[str, Any]) -> dict[str, Any]:
    """Return the sole authoring source for character creation."""
    return _mapping(_mapping(world.get("rules")).get("actor"))


def _replace_actor_template(
    world: Mapping[str, Any],
    actor: Mapping[str, Any],
) -> dict[str, Any]:
    working = deepcopy(dict(world))
    rules = dict(working.get("rules") or {})
    rules["actor"] = dict(actor)
    rules.pop("character_card", None)
    working["rules"] = rules
    return working


def _simulation_issue(
    category: str,
    message: str,
    *,
    severity: str = "error",
    code: str = "",
    field: str = "",
    reference: str = "",
) -> dict[str, Any]:
    return {
        "category": category,
        "severity": severity,
        "code": code or category,
        "message": str(message),
        "field": field,
        "reference": reference,
    }


def candidate_resolution(
    template: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """按当前选择解析每个字段的权威候选、禁用原因与数量（§27.4）。"""
    values = values if isinstance(values, Mapping) else {}
    result: list[dict[str, Any]] = []
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        visible = field_visible(field, values)
        options: list[dict[str, Any]] = []
        disabled: list[str] = []
        if visible:
            try:
                options = preset_options(template, field, values)
            except Exception as exc:  # noqa: BLE001
                disabled.append(f"候选解析失败：{exc}")
        result.append(
            {
                "key": key,
                "label": str(field.get("label") or key),
                "type": str(field.get("type") or ""),
                "visible": visible,
                "required": bool(field.get("required", True)),
                "private": bool(field.get("private", False)),
                "min_choices": int(field.get("min_choices", 0) or 0),
                "max_choices": int(field.get("max_choices", 100) or 100),
                "candidates": len(options),
                "options": options,
                "disabled_reasons": disabled,
                "clear_on_change": _sequence(field.get("clear_on_change")),
            }
        )
    return result


def _selected_refs(fields: Mapping[str, Any]) -> dict[str, list[str]]:
    refs = fields.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    result: dict[str, list[str]] = {}
    for dimension, selected in refs.items():
        if isinstance(selected, Mapping):
            result[dimension] = [
                str(selected.get("id") or selected.get("snapshot", {}).get("id") or "")
            ]
        elif isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)):
            result[dimension] = [
                str(item.get("id") or item.get("snapshot", {}).get("id") or "")
                for item in selected
                if isinstance(item, Mapping)
            ]
    return result


def build_simulation(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    world: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """机械构筑模拟：属性、资源、能力、专长、弱点、装备、语言、知识、关系、时钟、任务。

    结果全部来自后端，前端不得自行猜测效果（§26.1 标签五）。
    """
    result: dict[str, Any] = {"ok": True, "issues": []}
    staged = staged_creation(template)
    validation_fields = (
        stage_field_projection(
            template,
            fields,
            stages=(CARD_STAGE_A,),
        )
        if staged
        else dict(fields)
    )
    required_preset_ids = {
        str(field.get("key") or "")
        for field in _sequence(template.get("fields"))
        if isinstance(field, Mapping)
        and field.get("preset_source")
        and (not staged or field_stage(field) == CARD_STAGE_A)
    }
    if staged:
        result["deferred_fields"] = [
            {
                "key": str(field.get("key") or ""),
                "label": str(field.get("label") or field.get("key") or ""),
                "stage": field_stage(field),
            }
            for field in _sequence(template.get("fields"))
            if isinstance(field, Mapping)
            and field_stage(field) != CARD_STAGE_A
        ]
    # 1. 固定数量检查
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        if staged and field_stage(field) != CARD_STAGE_A:
            continue
        if str(field.get("type") or "") != "multi_select":
            continue
        key = str(field.get("key") or "")
        minimum = int(field.get("min_choices", 0) or 0)
        maximum = int(field.get("max_choices", 100) or 100)
        chosen = validation_fields.get(key)
        count = len(chosen) if isinstance(chosen, Sequence) and not isinstance(chosen, (str, bytes)) else 0
        if not minimum <= count <= maximum:
            result["ok"] = False
            result["issues"].append(
                _simulation_issue(
                    "input_incomplete",
                    f"本项必须选择 {minimum if minimum == maximum else f'{minimum}—{maximum}'} 个预设选项，当前选择 {count} 个",
                    code="selection.count_invalid",
                    field=key,
                )
            )
    # 2. 预设选择合法性
    try:
        validate_preset_selection(
            template,
            validation_fields,
            require_complete=True,
            required_dimension_ids=required_preset_ids,
        )
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["issues"].append(
            _simulation_issue(
                "selection_invalid",
                str(exc),
                code="selection.invalid",
            )
        )
    # 3. 属性
    try:
        if template.get("preset_dimensions"):
            from ..lifecycle import resolve_profession_stats as _rps

            resolved = _rps(template, fields, require_complete=True)
            result["attributes"] = resolved.get("raw", {})
            result["attribute_total"] = int(resolved.get("effective_total", 0))
            result["attribute_labels"] = resolved.get("labels", {})
    except Exception as exc:  # noqa: BLE001
        result["issues"].append(
            _simulation_issue(
                "warning",
                str(exc),
                severity="warning",
                code="attributes.resolve_failed",
            )
        )
    # 4. 能力/专长/弱点/语言/知识实例
    refs = _selected_refs(fields)
    result["abilities"] = [{"ref": ref} for ref in refs.get("abilities", [])]
    result["specialties"] = [{"ref": ref} for ref in refs.get("specialties", [])]
    result["weakness"] = [{"ref": ref} for ref in refs.get("weakness", [])]
    result["languages"] = [{"ref": ref} for ref in refs.get("languages", [])]
    result["knowledge"] = [{"ref": ref} for ref in refs.get("knowledge", [])]
    # 5. 装备与物品实例（稳定 ID）
    if isinstance(world, Mapping):
        from ..item_catalog import card_item_grants

        grants = card_item_grants(world, fields)
        result["item_instances"] = [
            {"item_id": item_id, "quantity": quantity}
            for item_id, quantity in sorted(grants["items"].items())
        ]
        result["item_sources"] = grants["sources"]
    else:
        result["item_instances"] = []
    # 6. 关系与个人线
    for key in ("faction_affiliation", "personal_goal", "debt", "contact", "rival", "private_secret", "camp_duty", "loadout"):
        refs_list = refs.get(key, [])
        if refs_list:
            result[key] = refs_list[0]
    return result


def effect_reducer(
    world: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """白名单效果归约（§27.3）：只输出已注册效果，世界包不能携带脚本。"""
    from ..capability_service import CapabilityService
    from ..entity_registry import EntityRegistry
    from ..item_catalog import card_item_grants

    preview: dict[str, Any] = {
        "capability_grants": [],
        "item_instances": [],
        "resources": {},
        "wallet": [],
        "knowledge": {},
        "content_boundary": {},
        "faction_reputation": {},
        "relations": [],
        "personal_clocks": [],
        "personal_quests": [],
        "runtime_limits": [],
    }
    issues: list[dict[str, Any]] = []
    try:
        registry = EntityRegistry(world)
        service = CapabilityService(world, registry)
        preset_values: dict[str, Any] = {}
        refs = fields.get(PRESET_REFS_KEY)
        refs = refs if isinstance(refs, Mapping) else {}
        for dimension, selected in refs.items():
            if isinstance(selected, Mapping):
                preset_values[f"custom:preset.{dimension}"] = str(
                    selected.get("id") or selected.get("snapshot", {}).get("id") or ""
                )
            elif isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)):
                ids = [
                    str(item.get("id") or item.get("snapshot", {}).get("id") or "")
                    for item in selected if isinstance(item, Mapping)
                ]
                ids = [item for item in ids if item]
                if ids:
                    preset_values[f"custom:preset.{dimension}"] = ids
        for grant in service.initial_grants(preset_values):
            preview["capability_grants"].append(
                {
                    "capability_ref": grant.get("capability_ref") or grant.get("target_ref"),
                    "source_ref": grant.get("source_ref") or "actor",
                    "preset_keys": grant.get("preset_keys", []),
                }
            )
        items = card_item_grants(world, fields)
        preview["item_instances"] = [
            {"item_id": item_id, "quantity": quantity}
            for item_id, quantity in sorted(items["items"].items())
        ]
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        category = (
            "reference_missing"
            if any(token in message for token in ("不存在", "缺失", "未找到", "引用"))
            else "unsupported_effect"
            if any(token in message for token in ("不支持", "未注册", "脚本"))
            else "mechanical_conflict"
        )
        issues.append(
            _simulation_issue(
                category,
                message,
                code=f"effect.{category}",
            )
        )
    return {
        "ok": not issues,
        "dry_run": bool(dry_run),
        "preview": preview,
        "issues": issues,
    }


def template_diff(
    base: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """两个模板修订的字段与预设差异（§27.4）。"""
    changed_fields: list[dict[str, Any]] = []
    removed_fields: list[dict[str, Any]] = []
    added_fields: list[dict[str, Any]] = []
    base_fields = {str(item.get("key") or ""): item for item in _sequence(base.get("fields")) if isinstance(item, Mapping)}
    candidate_fields = {str(item.get("key") or ""): item for item in _sequence(candidate.get("fields")) if isinstance(item, Mapping)}
    for key, item in candidate_fields.items():
        if key not in base_fields:
            added_fields.append({"key": key, "label": str(item.get("label") or key)})
        elif item != base_fields[key]:
            changed_fields.append({"key": key, "label": str(item.get("label") or key)})
    for key, item in base_fields.items():
        if key not in candidate_fields:
            removed_fields.append({"key": key, "label": str(item.get("label") or key)})
    return {
        "added_fields": added_fields,
        "changed_fields": changed_fields,
        "removed_fields": removed_fields,
        "base_version": str(base.get("version") or ""),
        "candidate_version": str(candidate.get("version") or ""),
    }


def card_diff(
    current: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """当前角色卡与候选卡的机械差异（§27.4/§26.4）。"""
    current_refs = _selected_refs(current)
    candidate_refs = _selected_refs(candidate)
    added: list[str] = []
    removed: list[str] = []
    preserved: list[str] = []
    for dimension in sorted(set(current_refs) | set(candidate_refs)):
        before = set(current_refs.get(dimension, []))
        after = set(candidate_refs.get(dimension, []))
        added.extend(f"{dimension}:{ref}" for ref in sorted(after - before))
        removed.extend(f"{dimension}:{ref}" for ref in sorted(before - after))
        preserved.extend(f"{dimension}:{ref}" for ref in sorted(after & before))
    return {
        "added": added,
        "removed": removed,
        "preserved": preserved,
        "summary": {"added": len(added), "removed": len(removed), "preserved": len(preserved)},
    }


def _preset_set_items(world: Mapping[str, Any], set_key: str) -> list[dict[str, Any]]:
    """B2（A6）：按 preset_sets 键取出预设列表。"""
    card = _actor_template(world)
    sets = card.get("preset_sets")
    sets = sets if isinstance(sets, Mapping) else {}
    raw = sets.get(set_key)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def preset_references(
    world: Mapping[str, Any],
    set_key: str,
    preset_id: str,
) -> list[dict[str, Any]]:
    """B2（A6）：列出引用某预设的所有位置；有引用则禁止物理删除。"""
    preset_id = str(preset_id or "").strip()
    set_key = str(set_key or "").strip()
    if not preset_id or not set_key:
        raise ValueError("需要 set_key 与 preset_id")
    refs: list[dict[str, Any]] = []
    card = _actor_template(world)
    fields = [item for item in _sequence(card.get("fields")) if isinstance(item, Mapping)]
    for field in fields:
        key = str(field.get("key") or "")
        source = str(field.get("preset_source") or field.get("options_source") or "")
        if source != set_key:
            continue
        options = _sequence(field.get("options"))
        inline = any(str(o.get("id") or "") == preset_id for o in options if isinstance(o, Mapping))
        # 字段把整个 preset_sets 作为候选来源时，集合内任一预设都被该字段引用。
        consumes_set = not options and str(field.get("type") or "") in {"preset_select", "multi_select"}
        if inline or consumes_set:
            refs.append({"location": f"actor.fields.{key}", "detail": f"字段「{field.get('label') or key}」参考了该预设"})
    professions = _sequence(card.get("profession_presets"))
    for profession in professions:
        prof_id = str(profession.get("id") or profession.get("name") or "?")
        for option_key in (
            "specialization_options", "starting_weapon_options", "starting_armor_options",
            "ability_options", "specialty_options", "weakness_options",
        ):
            for option in _sequence(profession.get(option_key)):
                if isinstance(option, Mapping) and str(option.get("id") or "") == preset_id:
                    refs.append({"location": f"profession_presets.{prof_id}.{option_key}", "detail": f"职业「{prof_id}」的 {option_key} 参考了该预设"})
    flow = card.get("creation_flow")
    flow = flow if isinstance(flow, Mapping) else {}
    for pack in _sequence(flow.get("archetype_packs")):
        if not isinstance(pack, Mapping):
            continue
        for field_key, raw_value in dict(pack.get("fields") or {}).items():
            wanted = [str(item).strip() for item in _sequence(raw_value)]
            if not wanted:
                wanted = [str(raw_value)]
            if any(preset_id in {str(w), str(w).casefold()} for w in wanted) or any(
                str(w).casefold() == preset_id.casefold() for w in wanted
            ):
                refs.append({"location": f"archetype_packs.{pack.get('id')}.fields.{field_key}", "detail": f"原型包「{pack.get('label') or pack.get('id')}」代填了该预设"})
    for other_set_key, other_set in (card.get("preset_sets") or {}).items():
        if not isinstance(other_set, (list, dict)):
            continue
        items = other_set.values() if isinstance(other_set, dict) else other_set
        for option in items:
            if not isinstance(option, Mapping):
                continue
            for rule_name in ("requirements", "conflicts"):
                for rule in _sequence(option.get(rule_name)):
                    if not isinstance(rule, Mapping):
                        continue
                    values = _sequence(rule.get("values"))
                    if any(str(v) == preset_id for v in values):
                        refs.append({"location": f"preset_sets.{other_set_key}.{option.get('id')}.{rule_name}", "detail": f"预设「{option.get('label') or option.get('id')}」的前置/互斥参考了该预设"})
    # 去重
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for ref in refs:
        key = f"{ref.get('location')}|{ref.get('detail')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return unique


def delete_preset(
    world: Mapping[str, Any],
    set_key: str,
    preset_id: str,
) -> dict[str, Any]:
    """B2（A6）：引用检查通过后物理删除预设，返回新世界。

    不考虑旧存档/旧世界包兼容：预设可被物理删除；引用它的模板、原型包、
    其他预设会被列出并阻止删除。
    """
    refs = preset_references(world, set_key, preset_id)
    if refs:
        raise ValueError(
            f"预设 {preset_id} 被以下位置引用，禁止删除："
            + "；".join(f"{r['location']}（{r['detail']}）" for r in refs[:10])
        )
    card = _actor_template(world)
    sets = dict(card.get("preset_sets") or {})
    raw = sets.get(set_key)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        remaining = [
            item for item in raw
            if not (isinstance(item, Mapping) and str(item.get("id") or "") == preset_id)
        ]
        if len(remaining) == len(raw):
            raise ValueError(f"预设 {preset_id} 不存在于 {set_key}")
        sets[set_key] = remaining
        card["preset_sets"] = sets
        return _replace_actor_template(world, card)
    raise ValueError(f"预设集 {set_key} 不是可编辑列表")


_CARD_GROUPS: list[tuple[str, str]] = [
    ("identity", "基础形象"),
    ("background", "身份与出身"),
    ("profession", "职业与属性"),
    ("ability", "能力与专长"),
    ("relation", "世界关系"),
    ("equipment", "装备与物资"),
    ("personal", "弱点与个人线"),
    ("private", "私密信息"),
    ("source", "机械来源与版本"),
]

_CARD_GROUP_MEMBERS: dict[str, tuple[str, ...]] = {
    "identity": ("name", "code", "appearance"),
    "background": ("species", "species_culture", "origin_region", "hometown", "social_identity"),
    "profession": ("profession", "specialization", "primary_attribute", "secondary_attribute"),
    "ability": ("abilities", "specialties"),
    "relation": ("belief", "faith_oath", "faction_affiliation", "org_status", "bloodline_mark"),
    "equipment": ("starting_weapon", "starting_armor", "loadout"),
    "personal": ("weakness", "camp_duty", "signature_item", "personal_goal", "debt", "contact", "rival"),
    "private": ("private_secret",),
    "source": (),
}


def card_groups(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """B2（A4）：把角色卡字段按九组分组渲染，输出稳定 ID + 中文名 + 值。"""
    fields_by_key = {
        str(item.get("key") or ""): item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    }
    refs = fields.get("_preset_refs")
    refs = refs if isinstance(refs, Mapping) else {}
    groups: list[dict[str, Any]] = []
    for group_id, group_label in _CARD_GROUPS:
        members = _CARD_GROUP_MEMBERS.get(group_id, ())
        rows: list[dict[str, Any]] = []
        for key in members:
            field = fields_by_key.get(key)
            if field is None:
                continue
            raw = fields.get(key)
            if raw in (None, ""):
                continue
            # 解析预设 ID（来自 _preset_refs）
            preset_ref = refs.get(key)
            preset_ids: list[str] = []
            if isinstance(preset_ref, Mapping):
                preset_ids = [str(preset_ref.get("id") or "")]
            elif isinstance(preset_ref, (list, tuple)):
                preset_ids = [str(item.get("id") or "") for item in preset_ref if isinstance(item, Mapping)]
            label = str(field.get("label") or key)
            if isinstance(raw, (list, tuple)):
                value = "、".join(str(item) for item in raw)
                display = value
            else:
                display = str(raw)
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "value": display,
                    "preset_ids": [item for item in preset_ids if item],
                    "private": bool(field.get("private", False)),
                }
            )
        groups.append({"id": group_id, "label": group_label, "rows": rows})
    return groups


def distribution_summary(world: Mapping[str, Any]) -> dict[str, Any]:
    """B2（A10）：分发信息体检——版本、许可、更新通道与签名要求。"""
    rules = _mapping(world.get("rules"))
    distribution = _mapping(rules.get("distribution"))
    return {
        "publisher": str(distribution.get("publisher") or ""),
        "update_channel": str(distribution.get("update_channel") or ""),
        "license": str(distribution.get("license") or ""),
        "homepage": str(distribution.get("homepage") or ""),
        "signature_required": bool(distribution.get("signature_required", False)),
        "content_version": str(world.get("world_content_version") or rules.get("world_content_version") or ""),
        "package_id": str(world.get("package_id") or ""),
        "namespace": str(world.get("namespace") or ""),
        "protocol_name": str(_mapping(world.get("protocol")).get("name") or "twp"),
        "protocol_version": str(
            _mapping(world.get("protocol")).get("version")
            or "1.0.0-rc10"
        ),
        "package_format": int(world.get("package_format") or 2),
        "module_count": len(_mapping(_mapping(rules.get("protocol")).get("features"))),
    }


def upsert_preset(
    world: Mapping[str, Any],
    set_key: str,
    preset: Mapping[str, Any],
) -> dict[str, Any]:
    """B2（A3）：新建或更新 preset_sets 中的预设（id 唯一校验）。"""
    set_key = str(set_key or "").strip()
    preset = dict(preset or {})
    preset_id = str(preset.get("id") or "").strip()
    if not set_key or not preset_id:
        raise ValueError("需要 set_key 与预设 id")
    if not preset.get("label"):
        raise ValueError("预设必须包含中文名 label")
    card = _actor_template(world)
    sets = dict(card.get("preset_sets") or {})
    raw = sets.get(set_key)
    items = [item for item in _sequence(raw) if isinstance(item, Mapping)] if not isinstance(raw, Mapping) or isinstance(raw, list) else [dict(item) for item in raw.values() if isinstance(item, Mapping)]
    existing_ids = {str(item.get("id") or "") for item in items}
    if preset_id not in existing_ids:
        items.append(preset)
    else:
        items = [dict(preset) if str(item.get("id") or "") == preset_id else dict(item) for item in items]
    sets[set_key] = items
    card["preset_sets"] = sets
    return _replace_actor_template(world, card)


def upsert_field(
    world: Mapping[str, Any],
    field: Mapping[str, Any],
) -> dict[str, Any]:
    """B2（A3）：新建或更新角色卡字段（key 唯一、类型合法）。"""
    field = dict(field or {})
    key = str(field.get("key") or "").strip()
    if not key:
        raise ValueError("字段缺少 key")
    allowed_types = {"text", "textarea", "integer", "select", "preset_select", "multi_select", "boolean", "derived"}
    field_type = str(field.get("type") or "text").lower()
    if field_type not in allowed_types:
        raise ValueError(f"字段类型不合法：{field_type}")
    card = _actor_template(world)
    fields = [dict(item) for item in _sequence(card.get("fields")) if isinstance(item, Mapping)]
    keys = [str(item.get("key") or "") for item in fields]
    if key in keys:
        fields = [dict(field) if str(item.get("key") or "") == key else dict(item) for item in fields]
    else:
        fields.append(field)
    card["fields"] = fields
    return _replace_actor_template(world, card)


def reorder_presets(
    world: Mapping[str, Any],
    set_key: str,
    order: Sequence[str],
) -> dict[str, Any]:
    """B2（A3）：按稳定 ID 顺序重排预设列表。"""
    set_key = str(set_key or "").strip()
    order = [str(item) for item in _sequence(order)]
    card = _actor_template(world)
    sets = dict(card.get("preset_sets") or {})
    raw = sets.get(set_key)
    items = [item for item in _sequence(raw) if isinstance(item, Mapping)]
    by_id = {str(item.get("id") or ""): item for item in items}
    ordered = [by_id[item] for item in order if item in by_id]
    ordered += [item for item in items if str(item.get("id") or "") not in set(order)]
    sets[set_key] = ordered
    card["preset_sets"] = sets
    return _replace_actor_template(world, card)


def world_causality_preview(
    world: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    command: Mapping[str, Any],
) -> dict[str, Any]:
    """拟提交世界命令的因果预览（§26.7/§27.4）。"""
    return preview_command(world, state, command)


__all__ = [
    "build_simulation",
    "candidate_resolution",
    "card_groups",
    "delete_preset",
    "distribution_summary",
    "preset_references",
    "reorder_presets",
    "upsert_field",
    "upsert_preset",
    "card_diff",
    "effect_reducer",
    "template_diff",
    "world_causality_preview",
]
