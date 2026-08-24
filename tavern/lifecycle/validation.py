from __future__ import annotations

from .world_time import *
from .character_creation import *

def uses_profession_preset_stats(
    template: Mapping[str, Any],
) -> bool:
    """Return True when the card template opts into the profession-preset
    stat mode (fixed 50 base + primary +7 / secondary +3 = 60)."""
    stats = template.get("stats")
    if not isinstance(stats, Mapping):
        return False
    return stats_mode(stats) == "preset"


def semantic_field_key(
    template: Mapping[str, Any],
    semantic_role: str,
) -> str:
    matches = [
        str(item.get("key") or "")
        for item in template.get("fields") or []
        if isinstance(item, Mapping)
        and str(item.get("semantic_role") or "") == semantic_role
    ]
    if len(matches) > 1:
        raise ValueError(f"语义角色 {semantic_role} 被重复声明")
    return matches[0] if matches else ""


def find_profession_preset(template: Mapping[str, Any], profession_ref: str) -> Mapping[str, Any]:
    reference = str(profession_ref or "").strip().casefold()
    for preset in template.get("profession_presets") or []:
        if not isinstance(preset, Mapping):
            continue
        aliases = preset.get("aliases")
        aliases = aliases if isinstance(aliases, Sequence) and not isinstance(aliases, (str, bytes)) else []
        candidates = {str(preset.get("id") or "").strip().casefold(), str(preset.get("key") or "").strip().casefold(), str(preset.get("name") or "").strip().casefold(), *(str(item).strip().casefold() for item in aliases)}
        if reference and reference in candidates:
            return preset
    raise ValueError(f"不存在预设“{profession_ref}”，请从提示中的可选项选择")


def attribute_maps(
    template: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    attributes = template.get("stats", {}).get("attributes", [])
    label_to_key: dict[str, str] = {}
    key_to_label: dict[str, str] = {}
    for attribute in attributes:
        if not isinstance(attribute, Mapping):
            continue
        key = str(attribute.get("key") or "")
        label = str(attribute.get("label") or key)
        if not key:
            continue
        label_to_key[label] = key
        label_to_key[key] = key
        key_to_label[key] = label
    return label_to_key, key_to_label


def resolve_profession_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """Compute final preset stats from the frozen world contract.

    The preset base total, choice bonuses and final total are authoritative
    world-package data. Preview, confirmation and audit paths reuse this
    resolver so persisted values cannot drift from that declaration.
    """
    stats_config = template.get("stats") or {}
    selector = stats_config.get("preset_selector") or {}
    selector_field = str(
        selector.get("field")
        or semantic_field_key(template, "actor.identity.profession")
    )
    if not selector_field:
        raise ValueError("属性生成策略缺少职业语义字段")
    profession_ref = str(fields.get(selector_field) or "")
    if not profession_ref:
        raise ValueError("请先选择预设")
    preset = find_profession_preset(template, profession_ref)
    profession_name = str(preset.get("name") or profession_ref)
    base_source = (
        preset.get("base_attributes")
        or preset.get("attributes")
        or {}
    )
    attribute_defs = template["stats"]["attributes"]
    attribute_keys = [str(item["key"]) for item in attribute_defs]
    base: dict[str, int] = {}
    for key in attribute_keys:
        if key not in base_source:
            raise ValueError(
                f"职业“{profession_name}”缺少属性：{key}"
            )
        base[key] = int(base_source[key])
    validation = stats_config.get("total_validation") or {}
    base_total = int(validation.get("base_total", stats_config.get("base_budget", 50)))
    final_total = int(validation.get("final_total", stats_config.get("budget", base_total)))
    bonus_choices = stats_config.get("bonus_choices") or []
    primary_field = semantic_field_key(template, "actor.stats.primary")
    secondary_field = semantic_field_key(template, "actor.stats.secondary")
    if not primary_field or not secondary_field:
        raise ValueError("属性生成策略缺少主属性或副属性语义字段")
    primary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == primary_field), {})
    secondary_cfg = next((x for x in bonus_choices if isinstance(x, Mapping) and x.get("field") == secondary_field), {})
    primary_bonus = int(primary_cfg.get("bonus", stats_config.get("primary_bonus", 7)))
    secondary_bonus = int(secondary_cfg.get("bonus", stats_config.get("secondary_bonus", 3)))
    if sum(base.values()) != base_total:
        raise ValueError(f"预设“{profession_name}”基础属性总和不是{base_total}")
    label_to_key, key_to_label = attribute_maps(template)
    primary_label = str(fields.get(primary_field) or "")
    secondary_label = str(fields.get(secondary_field) or "")
    if require_complete and not primary_label:
        raise ValueError("尚未选择主属性")
    if require_complete and not secondary_label:
        raise ValueError("尚未选择副属性")
    primary_key = (
        label_to_key.get(primary_label) if primary_label else None
    )
    secondary_key = (
        label_to_key.get(secondary_label) if secondary_label else None
    )
    if primary_label and primary_key is None:
        raise ValueError("主属性不在可选属性列表中")
    if secondary_label and secondary_key is None:
        raise ValueError("副属性不在可选属性列表中")
    if (
        primary_key is not None
        and secondary_key is not None
        and primary_key == secondary_key
    ):
        raise ValueError("主属性与副属性不能相同")
    effective = dict(base)
    if primary_key:
        effective[primary_key] += primary_bonus
    if secondary_key:
        effective[secondary_key] += secondary_bonus
    if require_complete and sum(effective.values()) != final_total:
        raise ValueError(f"最终属性总和必须为{final_total}")
    attribute_definitions = {
        str(item["key"]): item for item in attribute_defs
    }
    for key, value in effective.items():
        definition = attribute_definitions[key]
        minimum = int(definition["minimum"])
        maximum = int(definition["maximum"])
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{definition['label']}最终值{value}"
                f"超出允许范围{minimum}—{maximum}"
            )
    modifier_table = template["stats"].get("modifier_table", {})
    modifiers = {
        key: int(modifier_table.get(str(value), 0))
        for key, value in effective.items()
    }
    return {
        "mode": "preset",
        "profession_id": str(preset.get("id") or preset.get("key") or profession_ref),
        "profession": profession_name,
        "base": base,
        "raw": effective,
        "labels": key_to_label,
        "modifiers": modifiers,
        "primary": {
            "attribute": primary_key or "",
            "label": primary_label,
            "bonus": primary_bonus if primary_key else 0,
        },
        "secondary": {
            "attribute": secondary_key or "",
            "label": secondary_label,
            "bonus": secondary_bonus if secondary_key else 0,
        },
        "base_total": base_total,
        "bonus_total": ((primary_bonus if primary_key else 0) + (secondary_bonus if secondary_key else 0)),
        "effective_total": sum(effective.values()),
        "modifier_table": dict(modifier_table),
    }


def next_fillable_card_step(
    template: Mapping[str, Any],
    fields_def: list[Mapping[str, Any]],
    start_step: int,
    values: Mapping[str, Any] | None = None,
    *,
    allow_stages: Sequence[str] | None = None,
) -> int:
    step = start_step
    while step < len(fields_def):
        definition = fields_def[step]
        if not isinstance(definition, Mapping):
            step += 1
            continue
        key = str(definition.get("key") or "")
        if (
            allow_stages is not None
            and field_stage(definition) not in set(allow_stages)
        ):
            step += 1
            continue
        if not field_visible(definition, values):
            step += 1
            continue
        if (
            (uses_profession_preset_stats(template) or uses_preset_stack_stats(template))
            and (
                key.startswith("stat_")
                or definition.get("skip_manual_prompt")
            )
        ):
            step += 1
            continue
        break
    return step


def repair_profession_preset_draft(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    current_step: int,
) -> tuple[dict[str, Any], int]:
    """Recompute profession-preset stat fields for a legacy/partial draft.

    Reused when an old draft already carries hand-filled ``stat_*`` fields so
    the values are overwritten with the formula-derived ones and the cursor is
    moved to the first non-attribute field.
    """
    if not uses_profession_preset_stats(template):
        return fields, current_step
    profession_field = semantic_field_key(
        template, "actor.identity.profession"
    )
    profession = fields.get(profession_field) if profession_field else None
    if not profession:
        return fields, current_step
    resolved = resolve_profession_stats(
        template, fields, require_complete=False
    )
    fields["profession_base_stats"] = resolved["base"]
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    fields_def = template["fields"]
    repaired_step = next_fillable_card_step(
        template, fields_def, current_step, fields
    )
    return fields, repaired_step


def validate_card_template_config(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("玩家角色卡模板必须是 JSON 对象")
    try:
        version = int(value.get("version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数") from exc
    if version < 1:
        raise ValueError("角色卡模板 version 必须是大于 0 的整数")
    fields = value.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ValueError("角色卡模板必须包含 fields 数组")
    keys: list[str] = []
    semantic_roles: dict[str, str] = {}
    for item in fields:
        if not isinstance(item, Mapping):
            raise ValueError("角色卡 fields 的每一项都必须是对象")
        key = str(item.get("key") or "").strip()
        if not key:
            raise ValueError("角色卡字段 key 不能为空")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"角色卡字段 key 非法：{key}")
        field_type = str(item.get("type") or "text").lower()
        if field_type not in SUPPORTED_CARD_FIELD_TYPES:
            raise ValueError(f"角色字段 {key} 使用了未注册类型：{field_type}")
        stage = str(item.get("stage") or "").strip().upper()
        if stage and stage not in CARD_STAGES:
            raise ValueError(
                f"角色字段 {key} 声明了无效建卡阶段：{stage}（仅允许 A/B/C）"
            )
        semantic_role = str(item.get("semantic_role") or "").strip()
        if semantic_role:
            if semantic_role in semantic_roles:
                raise ValueError(
                    f"语义角色 {semantic_role} 被字段 "
                    f"{semantic_roles[semantic_role]} 与 {key} 重复声明"
                )
            semantic_roles[semantic_role] = key
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise ValueError("角色卡字段 key 不能重复")
    for required_role in ("actor.identity.name", "actor.identity.alias"):
        if required_role not in semantic_roles:
            raise ValueError(f"角色卡缺少必需语义角色 {required_role}")
    for item in fields:
        semantic_role = str(item.get("semantic_role") or "").strip()
        if semantic_role not in {
            "actor.identity.name",
            "actor.identity.alias",
        }:
            continue
        try:
            max_chars = int(item.get("max_chars", 12))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "角色姓名与副本代号 max_chars 必须是整数"
            ) from exc
        if max_chars > 12:
            raise ValueError(
                "角色姓名与副本代号最多只能设置为 12 个字符"
            )

    normalized_template = card_template(
        {"rules": {"actor": dict(value)}}
    )
    normalized_fields = normalized_template.get("fields") or []
    field_key_set = {
        str(item.get("key") or "")
        for item in normalized_fields
        if isinstance(item, Mapping)
    }
    dependency_graph: dict[str, set[str]] = {key: set() for key in field_key_set}
    for field in normalized_fields:
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        condition = field.get("visible_when")
        if isinstance(condition, Mapping):
            for dependency in condition:
                dependency_key = str(dependency)
                if dependency_key not in field_key_set:
                    raise ValueError(
                        f"字段 {key} 的 visible_when 引用了不存在的字段："
                        f"{dependency_key}"
                    )
                dependency_graph[key].add(dependency_key)
        different = str(field.get("must_differ_from") or "")
        if different and different not in field_key_set:
            raise ValueError(
                f"字段 {key} 的 must_differ_from 引用了不存在的字段："
                f"{different}"
            )
        for target in field.get("clear_on_change") or []:
            target_key = str(target)
            if target_key not in field_key_set:
                raise ValueError(
                    f"字段 {key} 的 clear_on_change 引用了不存在的字段："
                    f"{target_key}"
                )
        if str(field.get("type") or "") in {"select", "preset_select"}:
            # Validate the declared candidate pool, not only candidates visible
            # before any earlier field has been selected.  Dependent fields such
            # as species_culture intentionally hide every option while species
            # is unresolved; treating that valid state as an empty library makes
            # a compiled flagship world impossible to install.
            declared_options = [
                option
                for option in field.get("options") or []
                if isinstance(option, Mapping)
                and str(option.get("id") or option.get("value") or "").strip()
            ]
            source_field = dict(field)
            source_field.pop("visible_when", None)
            resolved_options = preset_options(
                normalized_template,
                source_field,
                {},
            )
            options = declared_options or resolved_options
            if field.get("required") and not options:
                raise ValueError(f"必填预设字段 {key} 没有任何有效选项")
            ids = [
                str(item.get("id") or item.get("value") or "").casefold()
                for item in options
            ]
            if len(ids) != len(set(ids)):
                raise ValueError(f"预设字段 {key} 存在重复的稳定 ID")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"角色卡条件字段形成循环依赖：{node}")
        if node in visited:
            return
        visiting.add(node)
        for dependency in dependency_graph.get(node, set()):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for field_key in dependency_graph:
        visit(field_key)

    constraint_issues = validate_constraint_graph(normalized_template)
    constraint_errors = [
        item
        for item in constraint_issues
        if str(item.get("level") or "error") == "error"
    ]
    if constraint_errors:
        details = "；".join(
            f"{item.get('path') or 'actor'}：{item.get('message') or '候选约束无效'}"
            for item in constraint_errors[:12]
        )
        raise ValueError(details)

    stats = value.get("stats")
    stats = stats if isinstance(stats, Mapping) else {"mode": "none"}
    mode = stats_mode(stats)
    if uses_preset_stack_stats(normalized_template):
        mode = "preset_stack"
    attributes = stats.get("attributes")
    if not isinstance(attributes, Sequence) or isinstance(
        attributes,
        (str, bytes),
    ) or (mode != "none" and not attributes):
        raise ValueError("启用数值时必须包含 stats.attributes")
    if mode == "none":
        if attributes:
            raise ValueError("stats.mode=none 时不得声明角色属性")
        return
    attribute_keys: set[str] = set()
    minimum_budget = 0
    maximum_budget = 0
    for item in attributes:
        if not isinstance(item, Mapping):
            raise ValueError("属性定义必须是 JSON 对象")
        key = str(item.get("key") or "").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", key):
            raise ValueError(f"属性 key 非法：{key or '空'}")
        if key in attribute_keys:
            raise ValueError("属性 key 不能重复")
        attribute_keys.add(key)
        try:
            minimum = int(item.get("minimum"))
            maximum = int(item.get("maximum"))
            default = int(item.get("default"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"属性 {key} 的范围必须是整数") from exc
        if minimum > maximum or not minimum <= default <= maximum:
            raise ValueError(f"属性 {key} 的默认值不在合法范围内")
        minimum_budget += minimum
        maximum_budget += maximum
    try:
        budget = int(stats.get("budget"))
    except (TypeError, ValueError) as exc:
        raise ValueError("属性预算必须是整数") from exc
    if mode == "manual" and not minimum_budget <= budget <= maximum_budget:
        raise ValueError(
            f"属性预算必须介于 {minimum_budget} 与 {maximum_budget} 之间"
        )

    # Preset stat mode. Totals and bonuses come from the world package.
    if mode == "preset_stack":
        validate_stat_generation_config(normalized_template)

    if mode == "preset":
        required_strategy_roles = (
            "actor.identity.profession",
            "actor.stats.primary",
            "actor.stats.secondary",
        )
        for required_role in required_strategy_roles:
            if not semantic_field_key(normalized_template, required_role):
                raise ValueError(
                    f"预设属性策略必须声明语义角色 {required_role}"
                )
        presets = normalized_template.get("profession_presets")
        presets = presets if isinstance(presets, list) else []
        if not presets:
            raise ValueError("职业预设模式至少需要一个职业预设")
        attr_index = {
            str(item.get("key") or ""): item
            for item in attributes
            if isinstance(item, Mapping)
        }
        for preset in presets:
            if not isinstance(preset, Mapping):
                raise ValueError("职业预设必须是 JSON 对象")
            name = str(preset.get("name") or "")
            if not name:
                raise ValueError("职业预设缺少名称")
            base_source = (
                preset.get("base_attributes")
                or preset.get("attributes")
                or {}
            )
            if not isinstance(base_source, Mapping):
                raise ValueError(f"职业“{name}”缺少基础属性")
            for key, definition in attr_index.items():
                if key not in base_source:
                    raise ValueError(
                        f"职业“{name}”缺少属性：{key}"
                    )
                try:
                    value_int = int(base_source[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 必须是整数"
                    ) from exc
                amin = int(definition["minimum"])
                amax = int(definition["maximum"])
                if not amin <= value_int <= amax:
                    raise ValueError(
                        f"职业“{name}”属性 {key} 超出允许范围"
                        f"{amin}—{amax}"
                    )
            if sum(int(v) for v in base_source.values()) != int((stats.get("total_validation") or {}).get("base_total", stats.get("base_budget", 50))):
                raise ValueError(
                    f"职业“{name}”基础属性总和不符合 total_validation.base_total"
                )
        configured_bonus_ceiling = max(
            (
                max(0, int(item.get("bonus", 0)))
                for item in (stats.get("bonus_choices") or [])
                if isinstance(item, Mapping)
            ),
            default=0,
        )
        if not configured_bonus_ceiling:
            configured_bonus_ceiling = max(
                0,
                int(stats.get("primary_bonus", 7)),
            )
        for key, definition in attr_index.items():
            amax = int(definition["maximum"])
            maximum_base = max(
                int(
                    (preset.get("base_attributes") or {}).get(key, 0)
                )
                for preset in presets
                if isinstance(preset, Mapping)
            )
            if maximum_base + configured_bonus_ceiling > amax:
                raise ValueError(
                    f"属性 {definition.get('label', key)} 最大值 {amax}"
                    "不足以容纳世界包声明的预设加成"
                )

__all__ = [name for name in globals() if not name.startswith('__')]
