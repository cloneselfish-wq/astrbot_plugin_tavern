from .common import *

def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, maximum: int = 200) -> str:
    return str(value or "").strip()[:maximum]


def _issue(
    level: str,
    code: str,
    message: str,
    path: str = "",
    hint: str = "",
    *,
    entity: str = "",
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        "path": path,
        "hint": hint,
        "entity": entity,
        "blocking": level == "error",
    }


def _multi_fields(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = [item for item in _sequence(template.get("fields")) if isinstance(item, Mapping)]
    return [
        item for item in fields
        if str(item.get("type") or "") == "multi_select"
    ]


def _single_fields(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    fields = [item for item in _sequence(template.get("fields")) if isinstance(item, Mapping)]
    return [
        item for item in fields
        if str(item.get("type") or "") in {"preset_select", "select"}
    ]


def _uses_strict_creation_contract(template: Mapping[str, Any]) -> bool:
    """Whether the template declares the B1/B2 full character-creation contract."""
    flow = template.get("creation_flow")
    return isinstance(flow, Mapping) and bool(flow)


def check_free_fields(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    fields = [item for item in _sequence(template.get("fields")) if isinstance(item, Mapping)]
    free = {
        str(item.get("key") or "")
        for item in fields
        if str(item.get("type") or "") in {"text", "textarea"}
    }
    wrong_free = free - FREE_FIELDS
    for key in sorted(wrong_free):
        issues.append(
            _issue(
                "error",
                "free_field.not_allowed",
                f"字段 {key} 是文本输入，但不在允许自由编写的四字段内",
                f"actor.fields.{key}",
                "姓名/昵称/外貌/性格之外必须使用预设",
            )
        )
    missing_free = FREE_FIELDS - free
    for key in sorted(missing_free):
        issues.append(
            _issue(
                "error",
                "free_field.missing",
                f"缺少允许自由编写的字段：{key}",
                "actor.fields",
            )
        )
    return issues


def check_fixed_counts(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    fields_by_key = {
        str(item.get("key") or ""): item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    }
    for key, target in FIXED_MULTI_TARGETS.items():
        field = fields_by_key.get(key)
        if field is None:
            # 允许通过 preset_dimensions 声明（维度 id 与 key 相同）
            dimension = next(
                (
                    item for item in _sequence(template.get("preset_dimensions"))
                    if isinstance(item, Mapping) and str(item.get("id")) == key
                ),
                None,
            )
            if dimension is None:
                issues.append(
                    _issue(
                        "error",
                        "fixed_multi.missing",
                        f"缺少固定多选字段：{key}（要求恰选 {target} 项）",
                        "actor.fields",
                    )
                )
                continue
            minimum = int(dimension.get("selection", {}).get("min", 0) or 0)
            maximum = int(dimension.get("selection", {}).get("max", 0) or 0)
        else:
            minimum = int(field.get("min_choices", 0) or 0)
            maximum = int(field.get("max_choices", 0) or 0)
        if minimum != target or maximum != target:
            issues.append(
                _issue(
                    "error",
                    "fixed_multi.count",
                    f"字段 {key} 固定数量错误：要求 {target}，实际 min={minimum} max={maximum}",
                    f"actor.fields.{key}",
                    "固定多选必须 min=max=目标数量",
                )
            )
        candidates = len(_sequence(fields_by_key.get(key, {}).get("options")))
        if candidates and candidates < FIXED_MULTI_MIN_CANDIDATES.get(key, 1):
            issues.append(
                _issue(
                    "error",
                    "fixed_multi.candidates",
                    f"字段 {key} 候选数量不足：{candidates} < {FIXED_MULTI_MIN_CANDIDATES.get(key)}",
                    f"actor.fields.{key}",
                )
            )
    return issues


def _preset_set(template: Mapping[str, Any], source: str) -> list[dict[str, Any]]:
    sets = template.get("preset_sets")
    sets = sets if isinstance(sets, Mapping) else {}
    raw = sets.get(source)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


def check_references(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """检查预设来源、过滤来源、能力/对象/资源/知识/任务/NPC/阵营引用可解析。"""
    issues: list[dict[str, Any]] = []
    rules = template.get("_world_rules") if isinstance(template, Mapping) else None
    world = template.get("_world")
    known_sets = set(_mapping(template.get("preset_sets")).keys())
    known_sets.update({"species_presets", "profession_presets", "origin_region_presets", "social_identity_presets"})
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        source = str(field.get("preset_source") or field.get("options_source") or "")
        if source and source not in known_sets:
            issues.append(
                _issue(
                    "error",
                    "reference.preset_source",
                    f"字段 {field.get('key')} 引用未知预设来源 {source}",
                    f"actor.fields.{field.get('key')}",
                )
            )
    # 效果引用（能力/对象/资源）由效果归约器另行检查；这里做基础扫描
    if isinstance(world, Mapping):
        from ...item_catalog import item_definitions

        item_ids = {item["item_id"] for item in item_definitions(world)}
        objects = _mapping(_mapping(world.get("rules")).get("objects"))
        for raw in _sequence(objects.get("definitions")):
            if isinstance(raw, Mapping) and raw.get("object_id"):
                item_ids.add(str(raw["object_id"]))
        for field in _sequence(template.get("fields")):
            if not isinstance(field, Mapping):
                continue
            for option in _sequence(field.get("options")):
                if not isinstance(option, Mapping):
                    continue
                grants = option.get("item_grants") or option.get("grants")
                if isinstance(grants, Mapping):
                    for ref in grants:
                        if str(ref).startswith("item:") and str(ref).removeprefix("item:") not in item_ids:
                            issues.append(
                                _issue(
                                    "warning",
                                    "reference.item",
                                    f"预设 {option.get('id')} 引用未知物品 {ref}",
                                    f"actor.fields.{field.get('key')}",
                                )
                            )
    return issues


def check_dependency_cycles(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    fields = [item for item in _sequence(template.get("fields")) if isinstance(item, Mapping)]
    by_key = {str(item.get("key") or ""): item for item in fields}
    for field in fields:
        key = str(field.get("key") or "")
        if not key:
            continue
        visible_when = field.get("visible_when")
        if not isinstance(visible_when, Mapping):
            continue
        for dependency in visible_when:
            if dependency == key:
                issues.append(
                    _issue(
                        "error",
                        "dependency.self",
                        f"字段 {key} 依赖自身",
                        f"actor.fields.{key}.visible_when",
                    )
                )
    return issues


def check_archetype_packs(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """原型包展开后必须完整合法（§6.4/§31.1-10）。"""
    issues: list[dict[str, Any]] = []
    flow = template.get("creation_flow")
    flow = flow if isinstance(flow, Mapping) else {}
    packs = _sequence(flow.get("archetype_packs"))
    field_keys = {str(item.get("key") or "") for item in _sequence(template.get("fields"))}
    seen: set[str] = set()
    for pack in packs:
        if not isinstance(pack, Mapping):
            continue
        pack_id = _text(pack.get("id"), 120)
        if not pack_id:
            issues.append(_issue("error", "archetype.missing_id", "原型包缺少稳定 id", "creation_flow.archetype_packs"))
            continue
        if pack_id in seen:
            issues.append(_issue("error", "archetype.duplicate", f"原型包 ID 重复：{pack_id}", "creation_flow.archetype_packs"))
        seen.add(pack_id)
        values = pack.get("fields")
        if not isinstance(values, Mapping):
            issues.append(
                _issue("error", "archetype.fields", f"原型包 {pack_id} 缺少 fields 映射", f"creation_flow.archetype_packs.{pack_id}")
            )
            continue
        for field_key in values:
            if field_key not in field_keys:
                issues.append(
                    _issue(
                        "error",
                        "archetype.unknown_field",
                        f"原型包 {pack_id} 代填了未知字段 {field_key}",
                        f"creation_flow.archetype_packs.{pack_id}.fields.{field_key}",
                    )
                )
    return issues


__all__ = [name for name in globals() if not name.startswith('__')]

