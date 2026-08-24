from __future__ import annotations

from collections.abc import Mapping, Sequence
from itertools import product
from typing import Any

from .card_wizard import PRESET_REFS_KEY, preset_options
from .candidates import normalize_candidate_rules
from .entity_registry import split_ref


PRESET_STACK_MODE = "preset_stack"
STAT_GENERATION_SNAPSHOT_KEY = "stat_generation_snapshot"
MAX_PRESET_COMBINATIONS = 100_000

# D1-DATA-005：角色确认后的职业运行状态契约。
RUNTIME_STATE_KEYS = frozenset(
    {
        "profession",
        "resources",
        "abilities",
        "career_specialties",
        "runtime_states",
        "combination_unlocks",
        "grants",
    }
)
RUNTIME_UNLOCK_KINDS = frozenset(
    {"capability", "ability_track", "resource", "runtime_effect"}
)


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def uses_preset_stack_stats(template: Mapping[str, Any]) -> bool:
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    generation = stats.get("stat_generation")
    generation = generation if isinstance(generation, Mapping) else {}
    return str(stats.get("mode") or generation.get("mode") or "").lower() == PRESET_STACK_MODE


def stat_generation_config(template: Mapping[str, Any]) -> dict[str, Any]:
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    raw = stats.get("stat_generation")
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "mode": str(raw.get("mode") or stats.get("mode") or "").lower(),
        "base_stats": dict(raw.get("base_stats") or {}),
        "bonus_sources": [str(item) for item in _sequence(raw.get("bonus_sources"))],
        "bonus_source_rules": {
            str(key): dict(value)
            for key, value in (raw.get("bonus_source_rules") or {}).items()
            if isinstance(value, Mapping)
        } if isinstance(raw.get("bonus_source_rules"), Mapping) else {},
        "expected_total": raw.get("expected_total", stats.get("budget")),
        "min_per_stat": raw.get("min_per_stat"),
        "max_per_stat": raw.get("max_per_stat"),
        "allow_manual_edit": bool(raw.get("allow_manual_edit", False)),
    }


def _attribute_index(template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("key") or ""): dict(item)
        for item in template.get("stats", {}).get("attributes", [])
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }


def _field_index(template: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("key") or ""): dict(item)
        for item in template.get("fields", [])
        if isinstance(item, Mapping) and str(item.get("key") or "")
    }


def _int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} 必须是整数")
    return value


def _option_bonus(
    option: Mapping[str, Any],
    attribute_keys: set[str],
    *,
    path: str,
) -> dict[str, int]:
    source = option.get("source")
    source = source if isinstance(source, Mapping) else option
    raw = source.get("stat_bonus")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"{path}.stat_bonus 必须是非空对象")
    bonus: dict[str, int] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        if key not in attribute_keys:
            raise ValueError(f"{path}.stat_bonus 引用了未知属性：{key}")
        bonus[key] = _int(raw_value, f"{path}.stat_bonus.{key}")
    return bonus


def validate_stat_generation_config(
    template: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate preset_stack declarations and every legal preset combination."""

    if not uses_preset_stack_stats(template):
        return {"mode": str(template.get("stats", {}).get("mode") or "manual"), "combination_count": 0}
    config = stat_generation_config(template)
    if config["mode"] != PRESET_STACK_MODE:
        raise ValueError("stat_generation.mode 必须是 preset_stack")
    if config["allow_manual_edit"]:
        raise ValueError("preset_stack 必须设置 allow_manual_edit=false")

    attributes = _attribute_index(template)
    attribute_keys = set(attributes)
    base_raw = config["base_stats"]
    if set(base_raw) != attribute_keys:
        missing = sorted(attribute_keys - set(base_raw))
        extra = sorted(set(base_raw) - attribute_keys)
        detail = []
        if missing:
            detail.append("缺少 " + "、".join(missing))
        if extra:
            detail.append("未知 " + "、".join(extra))
        raise ValueError("stat_generation.base_stats 必须覆盖全部属性（" + "；".join(detail) + "）")
    base = {
        key: _int(base_raw[key], f"stat_generation.base_stats.{key}")
        for key in attributes
    }

    sources = config["bonus_sources"]
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("stat_generation.bonus_sources 必须是非空且不重复的字段列表")
    fields = _field_index(template)
    option_groups: list[list[dict[str, Any]]] = []
    source_rules = config["bonus_source_rules"]
    for source_id in sources:
        field = fields.get(source_id)
        if not field:
            raise ValueError(f"bonus_sources 引用了不存在的建卡字段：{source_id}")
        if str(field.get("type") or "") not in {"select", "preset_select"}:
            raise ValueError(f"加成来源 {source_id} 必须是单选预设字段")
        options = preset_options(template, field, {})
        if not options:
            raise ValueError(f"加成来源 {source_id} 没有有效选项")
        expected_source_total = source_rules.get(source_id, {}).get("expected_bonus_total")
        normalized: list[dict[str, Any]] = []
        for option in options:
            option_id = str(option.get("id") or "?")
            bonus = _option_bonus(
                option,
                attribute_keys,
                path=f"{source_id}.{option_id}",
            )
            if expected_source_total is not None:
                expected_value = _int(
                    expected_source_total,
                    f"bonus_source_rules.{source_id}.expected_bonus_total",
                )
                if sum(bonus.values()) != expected_value:
                    raise ValueError(
                        f"加成来源 {source_id} 的选项 {option_id} 合计必须为 {expected_value}"
                    )
            normalized.append({**option, "stat_bonus": bonus})
        option_groups.append(normalized)

    combination_count = 1
    for options in option_groups:
        combination_count *= len(options)
    if combination_count > MAX_PRESET_COMBINATIONS:
        raise ValueError(
            f"preset_stack 合法组合数 {combination_count} 超过发布体检上限 {MAX_PRESET_COMBINATIONS}"
        )
    expected_total = _int(config["expected_total"], "stat_generation.expected_total")
    configured_min = config.get("min_per_stat")
    configured_max = config.get("max_per_stat")
    for combination in product(*option_groups):
        generated = dict(base)
        option_ids: list[str] = []
        for option in combination:
            option_ids.append(str(option.get("id") or "?"))
            for key, bonus in option["stat_bonus"].items():
                generated[key] += bonus
        if sum(generated.values()) != expected_total:
            raise ValueError(
                "preset_stack 组合总和不符："
                + "+".join(option_ids)
                + f" 得到 {sum(generated.values())}，应为 {expected_total}"
            )
        for key, value in generated.items():
            minimum = int(configured_min) if configured_min is not None else int(attributes[key]["minimum"])
            maximum = int(configured_max) if configured_max is not None else int(attributes[key]["maximum"])
            if not minimum <= value <= maximum:
                raise ValueError(
                    "preset_stack 组合越界："
                    + "+".join(option_ids)
                    + f" 的 {attributes[key].get('label', key)}={value}，允许 {minimum}—{maximum}"
                )
    return {
        "mode": PRESET_STACK_MODE,
        "combination_count": combination_count,
        "expected_total": expected_total,
        "sources": list(sources),
    }


def _selected_option(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    source_id: str,
) -> dict[str, Any] | None:
    field = _field_index(template).get(source_id)
    if not field:
        return None
    refs = fields.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    ref = refs.get(source_id)
    ref = ref if isinstance(ref, Mapping) else {}
    options = preset_options(template, field, fields)
    current = str(fields.get(source_id) or "").casefold()
    ref_id = str(ref.get("id") or "").casefold()
    ref_values = {
        ref_id,
        str(ref.get("value") or "").casefold(),
        str(ref.get("label") or "").casefold(),
    }
    ref_values.discard("")
    if current and current in ref_values and ref_id:
        for option in options:
            if str(option.get("id") or "").casefold() == ref_id:
                return option
    if current:
        current_matches: list[dict[str, Any]] = []
        for option in options:
            identities = {
                str(option.get("id") or "").casefold(),
                str(option.get("value") or "").casefold(),
                str(option.get("label") or "").casefold(),
            }
            if current in identities:
                current_matches.append(option)
        if len(current_matches) == 1:
            return current_matches[0]
        if len(current_matches) > 1:
            raise ValueError(
                f"属性来源 {source_id} 的当前值对应多个同名预设，"
                "请返回该字段重新选择具体选项"
            )
    candidates = {
        str(ref.get("id") or "").casefold(),
        str(ref.get("value") or "").casefold(),
        str(ref.get("label") or "").casefold(),
    }
    candidates.discard("")
    for option in options:
        identities = {
            str(option.get("id") or "").casefold(),
            str(option.get("value") or "").casefold(),
            str(option.get("label") or "").casefold(),
        }
        if candidates & identities:
            return option
    return None


def calculate_preset_stack_stats(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> dict[str, Any] | None:
    """Calculate from the immutable base on every call; never add to stored stats."""

    if not uses_preset_stack_stats(template):
        raise ValueError("当前角色卡不是 preset_stack 属性模式")
    config = stat_generation_config(template)
    attributes = _attribute_index(template)
    keys = set(attributes)
    base = {key: int(config["base_stats"][key]) for key in attributes}
    generated = dict(base)
    sources: list[dict[str, Any]] = []
    for source_id in config["bonus_sources"]:
        option = _selected_option(template, fields, source_id)
        if option is None:
            if require_complete:
                label = _field_index(template).get(source_id, {}).get("label") or source_id
                raise ValueError(f"尚未选择：{label}")
            return None
        bonus = _option_bonus(option, keys, path=f"{source_id}.{option.get('id') or '?'}")
        for key, value in bonus.items():
            generated[key] += value
        sources.append(
            {
                "source_id": source_id,
                "source_label": str(_field_index(template).get(source_id, {}).get("label") or source_id),
                "option_id": str(option.get("id") or ""),
                "option_label": str(option.get("label") or option.get("value") or ""),
                "stat_bonus": bonus,
            }
        )
    expected_total = int(config["expected_total"])
    if sum(generated.values()) != expected_total:
        raise ValueError(f"自动生成属性总和为 {sum(generated.values())}，应为 {expected_total}")
    for key, value in generated.items():
        minimum = int(config["min_per_stat"]) if config.get("min_per_stat") is not None else int(attributes[key]["minimum"])
        maximum = int(config["max_per_stat"]) if config.get("max_per_stat") is not None else int(attributes[key]["maximum"])
        if not minimum <= value <= maximum:
            raise ValueError(f"{attributes[key].get('label', key)}自动生成值 {value} 超出 {minimum}—{maximum}")
    labels = {key: str(item.get("label") or key) for key, item in attributes.items()}
    table = template.get("stats", {}).get("modifier_table") or {}
    modifiers = {key: int(table.get(str(value), 0)) for key, value in generated.items()}
    snapshot = {
        "mode": PRESET_STACK_MODE,
        "base_stats": dict(base),
        "sources": sources,
    }
    return {
        "mode": PRESET_STACK_MODE,
        "base": base,
        "raw": generated,
        "labels": labels,
        "modifiers": modifiers,
        "sources": sources,
        "base_total": sum(base.values()),
        "bonus_total": sum(sum(item["stat_bonus"].values()) for item in sources),
        "effective_total": sum(generated.values()),
        "budget": expected_total,
        "modifier_table": dict(table),
        STAT_GENERATION_SNAPSHOT_KEY: snapshot,
    }


def clear_generated_stats(template: Mapping[str, Any], fields: dict[str, Any]) -> None:
    for key in _attribute_index(template):
        fields.pop(f"stat_{key}", None)
    fields.pop("resolved_stat_total", None)
    fields.pop(STAT_GENERATION_SNAPSHOT_KEY, None)


def sync_preset_stack_fields(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    *,
    require_complete: bool = False,
) -> dict[str, Any] | None:
    clear_generated_stats(template, fields)
    resolved = calculate_preset_stack_stats(
        template,
        fields,
        require_complete=require_complete,
    )
    if resolved is None:
        return None
    for key, value in resolved["raw"].items():
        fields[f"stat_{key}"] = value
    refs = fields.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    for source in resolved["sources"]:
        source_id = str(source["source_id"])
        option = _selected_option(template, fields, source_id)
        option_source = option.get("source") if isinstance(option, Mapping) else {}
        refs[source_id] = {
            "id": str(source["option_id"]),
            "value": str(option.get("value") or "") if isinstance(option, Mapping) else "",
            "label": str(source["option_label"]),
            "snapshot": dict(option_source) if isinstance(option_source, Mapping) else {},
        }
    fields[PRESET_REFS_KEY] = refs
    fields["resolved_stat_total"] = int(resolved["effective_total"])
    fields[STAT_GENERATION_SNAPSHOT_KEY] = dict(
        resolved[STAT_GENERATION_SNAPSHOT_KEY]
    )
    return resolved


def assess_preset_stack_migration(
    template: Mapping[str, Any],
    profile: Mapping[str, Any],
    stored_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a legacy card without mutating it or silently changing values."""

    resolved = calculate_preset_stack_stats(template, profile, require_complete=True)
    assert resolved is not None
    raw = stored_stats.get("raw")
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        matches = all(
            key in raw and int(raw[key]) == value
            for key, value in resolved["raw"].items()
        )
    except (TypeError, ValueError):
        matches = False
    return {
        "status": "snapshot_backfill_safe" if matches else "admin_confirmation_required",
        "matches": matches,
        "stored": dict(raw),
        "calculated": dict(resolved["raw"]),
        STAT_GENERATION_SNAPSHOT_KEY: dict(resolved[STAT_GENERATION_SNAPSHOT_KEY]),
    }


def format_preset_stack_result(resolved: Mapping[str, Any]) -> str:
    labels = resolved.get("labels") or {}
    raw = resolved.get("raw") or {}
    title = (
        "【角色五维已自动生成】"
        if len(raw) == 5
        else "【角色属性已自动生成】"
    )
    lines = [
        title,
        "｜".join(
            f"{labels.get(key, key)} {value}"
            for key, value in raw.items()
        ),
        "",
        "属性来源：",
    ]
    for source in resolved.get("sources") or []:
        bonus = source.get("stat_bonus") or {}
        lines.append(
            f"{source.get('option_label') or source.get('option_id')}："
            + "、".join(
                f"{labels.get(key, key)}{int(value):+d}"
                for key, value in bonus.items()
            )
        )
    lines.extend(
        [
            "",
            f"总和：{resolved.get('effective_total', 0)}",
            "属性已根据角色预设锁定，将继续填写后续资料。",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# D1-DATA-005 运行状态快照契约与资源修饰器纯应用
# ---------------------------------------------------------------------------


def _runtime_typed_ref(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{path} 不能为空")
    try:
        split_ref(text)
    except ValueError:
        raise ValueError(f"{path} 必须是稳定类型化引用（如 resource:contract_echo）") from None
    return text


def _runtime_ref_entry(value: Any, *, path: str, allow_kind: bool = False) -> dict[str, Any]:
    if isinstance(value, str):
        return {
            "ref": _runtime_typed_ref(value, path),
            "label": "",
        }
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} 必须是字符串或对象")
    allowed = {"ref", "label"}
    if allow_kind:
        allowed.add("kind")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(
            f"{path} 包含未知字段："
            + "、".join(sorted(str(item) for item in unknown))
        )
    ref = _runtime_typed_ref(value.get("ref"), f"{path}.ref")
    label = str(value.get("label") or "").strip()
    entry: dict[str, Any] = {"ref": ref, "label": label}
    if allow_kind:
        kind = str(value.get("kind") or "").strip()
        if kind and kind not in RUNTIME_UNLOCK_KINDS:
            raise ValueError(
                f"{path}.kind 必须是 " + "、".join(sorted(RUNTIME_UNLOCK_KINDS))
            )
        entry["kind"] = kind
    return entry


def normalize_runtime_state_snapshot(raw: Any) -> dict[str, Any]:
    """Return the canonical D1-DATA-005 runtime state snapshot.

    Unknown top-level keys, malformed resources and invalid refs raise
    immediately; an empty input yields the canonical empty state.
    """

    if raw is None or raw == "":
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("runtime state 必须是对象")
    unknown = set(raw) - RUNTIME_STATE_KEYS
    if unknown:
        raise ValueError(
            "runtime state 包含未知字段："
            + "、".join(sorted(str(item) for item in unknown))
        )
    normalized: dict[str, Any] = {
        "profession": None,
        "resources": {},
        "abilities": [],
        "career_specialties": [],
        "runtime_states": [],
        "combination_unlocks": [],
        "grants": [],
    }
    profession = raw.get("profession")
    if profession not in (None, ""):
        if not isinstance(profession, Mapping):
            raise ValueError("runtime state.profession 必须是对象")
        profession_unknown = set(profession) - {"id", "label", "specialization"}
        if profession_unknown:
            raise ValueError(
                "runtime state.profession 包含未知字段："
                + "、".join(sorted(str(item) for item in profession_unknown))
            )
        profession_id = str(profession.get("id") or "").strip()
        profession_label = str(profession.get("label") or "").strip()
        if not profession_id or not profession_label:
            raise ValueError("runtime state.profession 必须包含 id 与 label")
        specialization = profession.get("specialization")
        normalized_profession: dict[str, Any] = {
            "id": profession_id,
            "label": profession_label,
            "specialization": None,
        }
        if specialization not in (None, ""):
            if not isinstance(specialization, Mapping):
                raise ValueError("runtime state.profession.specialization 必须是对象")
            specialization_unknown = set(specialization) - {"id", "label"}
            if specialization_unknown:
                raise ValueError(
                    "runtime state.profession.specialization 包含未知字段："
                    + "、".join(sorted(str(item) for item in specialization_unknown))
                )
            specialization_id = str(specialization.get("id") or "").strip()
            specialization_label = str(specialization.get("label") or "").strip()
            if not specialization_id or not specialization_label:
                raise ValueError(
                    "runtime state.profession.specialization 必须包含 id 与 label"
                )
            normalized_profession["specialization"] = {
                "id": specialization_id,
                "label": specialization_label,
            }
        normalized["profession"] = normalized_profession

    resources = raw.get("resources")
    if resources not in (None, ""):
        if not isinstance(resources, Mapping):
            raise ValueError("runtime state.resources 必须是对象")
        for ref, entry in resources.items():
            resource_ref = _runtime_typed_ref(
                ref, f"runtime state.resources.{ref}"
            )
            if not isinstance(entry, Mapping):
                raise ValueError(f"runtime state.resources.{ref} 必须是对象")
            entry_unknown = set(entry) - {"label", "current", "maximum"}
            if entry_unknown:
                raise ValueError(
                    f"runtime state.resources.{ref} 包含未知字段："
                    + "、".join(sorted(str(item) for item in entry_unknown))
                )
            label = str(entry.get("label") or "").strip()
            if not label:
                raise ValueError(f"runtime state.resources.{ref}.label 不能为空")
            current = entry.get("current")
            maximum = entry.get("maximum")
            if isinstance(current, bool) or not isinstance(current, int):
                raise ValueError(f"runtime state.resources.{ref}.current 必须是整数")
            if isinstance(maximum, bool) or not isinstance(maximum, int):
                raise ValueError(f"runtime state.resources.{ref}.maximum 必须是整数")
            if current < 0 or maximum < 0:
                raise ValueError(
                    f"runtime state.resources.{ref} 的数值不能为负"
                )
            if current > maximum:
                raise ValueError(
                    f"runtime state.resources.{ref} 的当前值不能超过上限"
                )
            normalized["resources"][resource_ref] = {
                "label": label,
                "current": current,
                "maximum": maximum,
            }

    for key in ("abilities", "career_specialties", "runtime_states"):
        raw_list = raw.get(key)
        if raw_list in (None, ""):
            continue
        if not isinstance(raw_list, Sequence) or isinstance(raw_list, (str, bytes)):
            raise ValueError(f"runtime state.{key} 必须是数组")
        normalized[key] = [
            _runtime_ref_entry(item, path=f"runtime state.{key}[{index}]")
            for index, item in enumerate(raw_list)
        ]
    raw_unlocks = raw.get("combination_unlocks")
    if raw_unlocks not in (None, ""):
        if not isinstance(raw_unlocks, Sequence) or isinstance(
            raw_unlocks, (str, bytes)
        ):
            raise ValueError("runtime state.combination_unlocks 必须是数组")
        normalized["combination_unlocks"] = [
            _runtime_ref_entry(
                item,
                path=f"runtime state.combination_unlocks[{index}]",
                allow_kind=True,
            )
            for index, item in enumerate(raw_unlocks)
        ]
    raw_grants = raw.get("grants")
    if raw_grants not in (None, ""):
        if not isinstance(raw_grants, Sequence) or isinstance(
            raw_grants, (str, bytes)
        ):
            raise ValueError("runtime state.grants 必须是数组")
        grants: list[dict[str, Any]] = []
        for index, item in enumerate(raw_grants):
            if not isinstance(item, Mapping):
                raise ValueError(f"runtime state.grants[{index}] 必须是对象")
            grant_unknown = set(item) - {"ref", "kind", "label", "policy", "when"}
            if grant_unknown:
                raise ValueError(
                    f"runtime state.grants[{index}] 包含未知字段："
                    + "、".join(sorted(str(value) for value in grant_unknown))
                )
            grant_ref = _runtime_typed_ref(
                item.get("ref"), f"runtime state.grants[{index}].ref"
            )
            kind = str(item.get("kind") or "").strip()
            if kind and kind not in RUNTIME_UNLOCK_KINDS:
                raise ValueError(
                    f"runtime state.grants[{index}].kind 必须是 "
                    + "、".join(sorted(RUNTIME_UNLOCK_KINDS))
                )
            policy = str(item.get("policy") or "").strip()
            when = item.get("when")
            if when is not None and not isinstance(when, Mapping):
                raise ValueError(f"runtime state.grants[{index}].when 必须是对象")
            grants.append(
                {
                    "ref": grant_ref,
                    "kind": kind,
                    "label": str(item.get("label") or "").strip(),
                    "policy": policy,
                    "when": dict(when) if isinstance(when, Mapping) else None,
                }
            )
        normalized["grants"] = grants
    return normalized


def apply_resource_modifiers(
    resources: Mapping[str, Any],
    modifiers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply D1 resource modifiers purely and return a new resource table.

    ``modifiers`` may be raw D1 declarations or already normalized entries;
    they are normalized through the shared candidate-rule contract so every
    consumer sees identical strict validation.
    """

    rules = normalize_candidate_rules({"resource_modifiers": list(modifiers)})
    result = normalize_runtime_state_snapshot(
        {"resources": dict(resources)}
    )["resources"]
    for modifier in rules["resource_modifiers"]:
        resource_ref = str(modifier["resource_ref"])
        if resource_ref not in result:
            raise ValueError(
                f"资源 {resource_ref} 尚未声明，不能应用修饰（请先由世界包声明初始资源）"
            )
        op = str(modifier["op"])
        value = int(modifier["value"])
        entry = result[resource_ref]
        current = int(entry["current"])
        maximum = int(entry["maximum"])
        if op == "set":
            current = value
        elif op == "add":
            current = current + value
        elif op == "subtract":
            current = current - value
        elif op == "cap":
            current = min(current, value)
        elif op == "floor":
            current = max(current, value)
        else:
            raise ValueError(f"不支持的资源修饰操作：{op}")
        if current < 0:
            raise ValueError(f"资源 {resource_ref} 不足，无法完成本次消耗")
        if current > maximum:
            raise ValueError(
                f"资源 {resource_ref} 超过上限 {maximum}，无法应用修饰"
            )
        entry["current"] = current
    return result


__all__ = [
    "MAX_PRESET_COMBINATIONS",
    "PRESET_STACK_MODE",
    "RUNTIME_STATE_KEYS",
    "RUNTIME_UNLOCK_KINDS",
    "STAT_GENERATION_SNAPSHOT_KEY",
    "apply_resource_modifiers",
    "assess_preset_stack_migration",
    "calculate_preset_stack_stats",
    "clear_generated_stats",
    "format_preset_stack_result",
    "normalize_runtime_state_snapshot",
    "stat_generation_config",
    "sync_preset_stack_fields",
    "uses_preset_stack_stats",
    "validate_stat_generation_config",
]
