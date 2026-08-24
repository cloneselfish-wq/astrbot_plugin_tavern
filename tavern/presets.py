from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any


KNOWLEDGE_LEVELS = (
    "unknown",
    "rumor",
    "basic",
    "familiar",
    "expert",
    "insider",
)
SELECTION_MODES = {
    "single",
    "multiple",
    "ranked",
    "conditional",
    "system_assigned",
    "author_fixed",
}


class PresetLibraryContractError(ValueError):
    def __init__(self, code: str, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _unique(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _preset_candidates(
    card: Mapping[str, Any],
    source: str,
) -> list[dict[str, Any]]:
    sets = card.get("preset_sets")
    sets = sets if isinstance(sets, Mapping) else {}
    raw = sets.get(source)
    if source == "profession_presets" and not isinstance(raw, Sequence):
        raw = card.get("profession_presets")
    return [
        dict(item)
        for item in _sequence(raw)
        if isinstance(item, Mapping)
    ]


def normalize_preset_libraries(
    card: Mapping[str, Any] | None,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Build the authoritative preset-library DTO and validate source coverage."""

    card = card if isinstance(card, Mapping) else {}
    fields = [
        dict(item)
        for item in _sequence(card.get("fields"))
        if isinstance(item, Mapping)
    ]
    preset_sets = card.get("preset_sets")
    preset_sets = preset_sets if isinstance(preset_sets, Mapping) else {}
    problems: list[dict[str, Any]] = []
    references: dict[str, list[dict[str, Any]]] = {}
    for index, field in enumerate(fields):
        if "preset_set" in field:
            problems.append(
                {
                    "code": "actor.preset_library.legacy_source",
                    "message": "字段仍使用已废弃的 preset_set，请改为 preset_source",
                    "path": f"rules.actor.fields[{index}].preset_set",
                    "severity": "error",
                }
            )
        source = str(field.get("preset_source") or "").strip()
        source_exists = source in preset_sets or (
            source == "profession_presets"
            and bool(_sequence(card.get("profession_presets")))
        )
        if source and source_exists:
            references.setdefault(source, []).append(field)
        elif source:
            problems.append(
                {
                    "code": "actor.preset_library.source_missing",
                    "message": f"字段引用了不存在的候选来源：{source}",
                    "path": f"rules.actor.fields[{index}].preset_source",
                    "severity": "error",
                }
            )

    raw_libraries = card.get("preset_libraries")
    raw_libraries = (
        list(raw_libraries)
        if isinstance(raw_libraries, Sequence)
        and not isinstance(raw_libraries, (str, bytes))
        else []
    )
    declared: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_libraries):
        if not isinstance(raw, Mapping):
            problems.append(
                {
                    "code": "actor.preset_library.invalid",
                    "message": "preset_libraries 每项必须是对象",
                    "path": f"rules.actor.preset_libraries[{index}]",
                    "severity": "error",
                }
            )
            continue
        item = dict(raw)
        library_id = str(item.get("id") or "").strip()
        if not library_id:
            problems.append(
                {
                    "code": "actor.preset_library.id_missing",
                    "message": "预设库缺少稳定 id",
                    "path": f"rules.actor.preset_libraries[{index}].id",
                    "severity": "error",
                }
            )
            continue
        if library_id in declared:
            problems.append(
                {
                    "code": "actor.preset_library.duplicate",
                    "message": f"预设库重复：{library_id}",
                    "path": f"rules.actor.preset_libraries[{index}].id",
                    "severity": "error",
                }
            )
            continue
        declared[library_id] = item

    for source in sorted(references):
        if source not in declared:
            problems.append(
                {
                    "code": "actor.preset_library.missing",
                    "message": f"字段引用了未声明元数据的预设库：{source}",
                    "path": "rules.actor.preset_libraries",
                    "severity": "error",
                }
            )
    for library_id in sorted(declared):
        if not _preset_candidates(card, library_id):
            problems.append(
                {
                    "code": "actor.preset_library.orphan",
                    "message": f"预设库没有对应候选集合：{library_id}",
                    "path": f"rules.actor.preset_libraries.{library_id}",
                    "severity": "error",
                }
            )

    items: list[dict[str, Any]] = []
    for library_id, raw in declared.items():
        fields_using = references.get(library_id, [])
        actual_field_ids = [
            str(field.get("key") or "").strip()
            for field in fields_using
            if str(field.get("key") or "").strip()
        ]
        declared_field_ids = _unique(
            _sequence(raw.get("source_field_ids"))
        )
        unknown_declared = [
            field_id
            for field_id in declared_field_ids
            if field_id not in {
                str(field.get("key") or "").strip() for field in fields
            }
        ]
        if unknown_declared:
            problems.append(
                {
                    "code": "actor.preset_library.field_missing",
                    "message": (
                        f"预设库 {library_id} 引用了未知字段："
                        + "、".join(unknown_declared)
                    ),
                    "path": (
                        f"rules.actor.preset_libraries.{library_id}."
                        "source_field_ids"
                    ),
                    "severity": "error",
                }
            )
        if set(declared_field_ids) != set(actual_field_ids):
            problems.append(
                {
                    "code": "actor.preset_library.field_mismatch",
                    "message": f"预设库 {library_id} 的字段引用目录与实际字段不一致",
                    "path": (
                        f"rules.actor.preset_libraries.{library_id}."
                        "source_field_ids"
                    ),
                    "severity": "error",
                }
            )
        label = str(raw.get("label") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not label:
            problems.append(
                {
                    "code": "actor.preset_library.label_missing",
                    "message": f"预设库 {library_id} 缺少玩家可读中文标题",
                    "path": f"rules.actor.preset_libraries.{library_id}.label",
                    "severity": "error",
                }
            )
        if not description:
            problems.append(
                {
                    "code": "actor.preset_library.description_missing",
                    "message": f"预设库 {library_id} 缺少用途说明",
                    "path": (
                        f"rules.actor.preset_libraries.{library_id}.description"
                    ),
                    "severity": "error",
                }
            )
        visibility = str(raw.get("visibility") or "public").strip()
        private_reference = any(
            bool(field.get("private"))
            or str(field.get("visibility") or "").strip()
            in {"dm", "host", "private"}
            for field in fields_using
        )
        if private_reference and visibility == "public":
            problems.append(
                {
                    "code": "actor.preset_library.visibility_conflict",
                    "message": f"私密字段引用了公开预设库：{library_id}",
                    "path": (
                        f"rules.actor.preset_libraries.{library_id}.visibility"
                    ),
                    "severity": "error",
                }
            )
        if not fields_using:
            problems.append(
                {
                    "code": "actor.preset_library.unused",
                    "message": f"预设库未被任何角色字段引用：{library_id}",
                    "path": f"rules.actor.preset_libraries.{library_id}",
                    "severity": "warning",
                }
            )
        item_problems = [
            dict(problem)
            for problem in problems
            if library_id in str(problem.get("path") or "")
            or library_id in str(problem.get("message") or "")
        ]
        items.append(
            {
                **raw,
                "id": library_id,
                "source_module": "actor",
                "source_field_ids": actual_field_ids,
                "candidate_contract": (
                    raw.get("candidate_contract")
                    or card.get("candidate_contract")
                    or "twp-actor-candidate/1.0.0-rc10"
                ),
                "candidate_count": len(
                    _preset_candidates(card, library_id)
                ),
                "visibility": visibility,
                "editable": bool(raw.get("editable", True)),
                "metadata_source": "declared",
                "metadata_complete": not any(
                    problem.get("severity") == "error"
                    for problem in item_problems
                )
                and bool(label and description),
                "problems": item_problems,
            }
        )
    items.sort(
        key=lambda item: (
            int(item.get("sort_order") or 0),
            str(item.get("id") or ""),
        )
    )
    errors = [
        problem
        for problem in problems
        if problem.get("severity") == "error"
    ]
    if strict and errors:
        first = errors[0]
        raise PresetLibraryContractError(
            str(first["code"]),
            str(first["message"]),
            str(first["path"]),
        )
    return {
        "items": items,
        "count": len(items),
        "referenced_library_ids": sorted(references),
        "metadata_complete": bool(references)
        and not errors
        and set(references) == set(declared),
        "problems": problems,
    }


def normalize_preset_dimensions(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    card = card if isinstance(card, Mapping) else {}
    preset_sets = card.get("preset_sets")
    preset_sets = preset_sets if isinstance(preset_sets, Mapping) else {}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(_sequence(card.get("fields"))):
        if not isinstance(raw, Mapping):
            continue
        source = str(raw.get("preset_source") or "").strip()
        raw_options = preset_sets.get(source)
        if not source or not isinstance(raw_options, Sequence) or isinstance(
            raw_options,
            (str, bytes),
        ):
            continue
        dimension_id = str(raw.get("key") or "").strip()
        if not dimension_id or dimension_id in seen:
            continue
        seen.add(dimension_id)
        multi = str(raw.get("type") or "") == "multi_select"
        mode = "multiple" if multi else "single"
        required = bool(raw.get("required", True))
        minimum = max(
            0,
            int(raw.get("min_choices", 1 if required else 0) or 0),
        )
        maximum = max(
            minimum,
            int(raw.get("max_choices", minimum or 1) or minimum or 1),
        )
        if not multi:
            minimum = min(minimum, 1)
            maximum = 1
        options: list[dict[str, Any]] = []
        option_ids: set[str] = set()
        for option_raw in raw_options:
            if not isinstance(option_raw, Mapping):
                continue
            option = deepcopy(dict(option_raw))
            option_id = str(option.get("id") or option.get("value") or "").strip()
            label = str(option.get("label") or option.get("name") or option_id).strip()
            if not option_id or not label or option_id in option_ids:
                continue
            option_ids.add(option_id)
            option["id"] = option_id
            option.setdefault("value", label)
            option.setdefault("label", label)
            option.setdefault("enabled", True)
            options.append(option)
        result.append(
            {
                "id": dimension_id,
                "label": str(raw.get("label") or dimension_id).strip(),
                "description": str(raw.get("description") or "").strip(),
                "selection": {"mode": mode, "min": minimum, "max": maximum},
                "required": required,
                "allow_custom": False,
                "randomizable": bool(raw.get("randomizable", False)),
                "player_editable": bool(raw.get("player_editable", True)),
                "visibility": str(
                    raw.get("visibility")
                    or ("private" if raw.get("private") else "player")
                ),
                "display_order": int(raw.get("display_order", (index + 1) * 10)),
                "page_size": max(1, min(10, int(raw.get("page_size", 5) or 5))),
                "options": options,
            }
        )
    return sorted(result, key=lambda item: (item["display_order"], item["id"]))


def validate_preset_dimensions(card: Mapping[str, Any] | None) -> dict[str, Any]:
    card = card if isinstance(card, Mapping) else {}
    dimensions = normalize_preset_dimensions(card)
    declared = [
        field
        for field in _sequence(card.get("fields"))
        if isinstance(field, Mapping) and field.get("preset_source")
    ]
    if len(dimensions) != len(declared):
        raise ValueError("actor.fields 存在空 key、重复 key 或无效 preset_source")
    known = {item["id"] for item in dimensions}
    option_count = 0
    for dimension in dimensions:
        if dimension["required"] and not dimension["options"]:
            raise ValueError(f"必选预设维度 {dimension['id']} 没有有效选项")
        option_count += len(dimension["options"])
        ids = [str(item["id"]) for item in dimension["options"]]
        if len(ids) != len(set(ids)):
            raise ValueError(f"预设维度 {dimension['id']} 存在重复选项 ID")
        for option in dimension["options"]:
            for rule_name in ("requirements", "conflicts"):
                for rule in _sequence(option.get(rule_name)):
                    if not isinstance(rule, Mapping):
                        raise ValueError(
                            f"{dimension['id']}.{option['id']}.{rule_name} 必须是对象数组"
                        )
                    dependency = str(rule.get("dimension") or "")
                    if dependency and dependency not in known:
                        raise ValueError(
                            f"{dimension['id']}.{option['id']} 引用了不存在的维度 {dependency}"
                        )
    return {"dimension_count": len(dimensions), "option_count": option_count}


def validate_preset_selection(
    card: Mapping[str, Any] | None,
    fields: Mapping[str, Any],
    *,
    require_complete: bool = False,
    required_dimension_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate selected stable IDs against v4 requirements and conflicts.

    ``required_dimension_ids`` narrows only the completeness gate.  It is used
    by staged character creation so the opening transaction can require every
    A-group preset without pretending that deferred B/C dimensions are already
    due.  Existing callers that omit it retain the full-card behavior.
    """
    dimensions = normalize_preset_dimensions(card)
    selected_snapshots = _selected_snapshots(fields)
    selected: dict[str, set[str]] = {
        key: {
            str(item.get("id") or item.get("value") or "")
            for item in values
            if str(item.get("id") or item.get("value") or "")
        }
        for key, values in selected_snapshots.items()
    }
    if require_complete:
        required_ids = (
            {str(item) for item in required_dimension_ids}
            if required_dimension_ids is not None
            else None
        )
        for dimension in dimensions:
            if (
                required_ids is not None
                and dimension["id"] not in required_ids
            ):
                continue
            count = len(selected.get(dimension["id"], set()))
            minimum = int(dimension["selection"]["min"])
            maximum = int(dimension["selection"]["max"])
            if not minimum <= count <= maximum:
                expected = (
                    f"{minimum} 项"
                    if minimum == maximum
                    else f"{minimum}—{maximum} 项"
                )
                raise ValueError(
                    f"{dimension['label']}必须选择 {expected}"
                )
    for dimension_id, snapshots in selected_snapshots.items():
        for option in snapshots:
            label = str(option.get("label") or option.get("id") or "该预设")
            for rule in _sequence(option.get("requirements")):
                if not isinstance(rule, Mapping):
                    continue
                dependency = str(rule.get("dimension") or "")
                expected = set(_unique(_sequence(rule.get("values"))))
                actual = selected.get(dependency, set())
                if expected and not (actual & expected):
                    if actual or require_complete:
                        raise ValueError(
                            f"{label} 的前置条件未满足：{dependency}"
                        )
            for rule in _sequence(option.get("conflicts")):
                if not isinstance(rule, Mapping):
                    continue
                dependency = str(rule.get("dimension") or "")
                blocked = set(_unique(_sequence(rule.get("values"))))
                if blocked & selected.get(dependency, set()):
                    raise ValueError(
                        f"{label} 与已选择的 {dependency} 互斥"
                    )
    return {"valid": True, "selected": {k: sorted(v) for k, v in selected.items()}}


def dimension_fields(card: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for dimension in normalize_preset_dimensions(card):
        mode = dimension["selection"]["mode"]
        if mode in {"system_assigned", "author_fixed"}:
            continue
        multi = mode in {"multiple", "ranked"}
        result.append(
            {
                "key": dimension["id"],
                "label": dimension["label"],
                "required": dimension["required"],
                "private": dimension["visibility"] in {"host", "private"},
                "max_chars": 4000,
                "type": "multi_select" if multi else "preset_select",
                "options": deepcopy(dimension["options"]),
                "page_size": dimension["page_size"],
                "min_choices": dimension["selection"]["min"],
                "max_choices": dimension["selection"]["max"],
                "display_order": dimension["display_order"],
                "preset_dimension": True,
            }
        )
    return result


def _boundary_sources(world: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    card = rules.get("actor")
    card = card if isinstance(card, Mapping) else {}
    return rules, card


def _selected_snapshots(fields: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    refs = fields.get("_preset_refs")
    refs = refs if isinstance(refs, Mapping) else {}
    result: dict[str, list[Mapping[str, Any]]] = {}
    for key, value in refs.items():
        entries = value if isinstance(value, list) else [value]
        snapshots: list[Mapping[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            snapshot = entry.get("snapshot")
            if isinstance(snapshot, Mapping):
                snapshots.append(snapshot)
        result[str(key)] = snapshots
    return result


def resolve_character_presets(
    world: Mapping[str, Any], fields: Mapping[str, Any]
) -> dict[str, Any]:
    rules, card = _boundary_sources(world)
    knowledge = rules.get("knowledge_boundary")
    knowledge = knowledge if isinstance(knowledge, Mapping) else {}
    content = rules.get("content_boundary")
    content = content if isinstance(content, Mapping) else {}
    knowledge_profiles = rules.get("knowledge_profiles") or card.get("knowledge_profiles")
    knowledge_profiles = knowledge_profiles if isinstance(knowledge_profiles, Mapping) else {}
    content_profiles = rules.get("content_profiles") or card.get("content_profiles")
    content_profiles = content_profiles if isinstance(content_profiles, Mapping) else {}

    knowledge_result: dict[str, Any] = {
        "policy": str(knowledge.get("policy") or "strict"),
        "global_rules": _unique(_sequence(knowledge.get("global_rules"))),
        "forbidden_domains": _unique(_sequence(knowledge.get("forbidden_domains"))),
        "metagame_policy": str(knowledge.get("metagame_policy") or "deny"),
        "unknown_fact_behavior": str(
            knowledge.get("unknown_fact_behavior")
            or "表现为不知道、误解或请求检定"
        ),
        "domains": {},
        "known_facts": [],
        "unknown_facts": [],
        "sources": ["world"],
    }
    content_result: dict[str, Any] = {
        "rating": str(content.get("rating") or "general"),
        "hard_denials": _unique(_sequence(content.get("hard_denials"))),
        "fade_to_black": _unique(_sequence(content.get("fade_to_black"))),
        "allowed_with_limits": dict(content.get("allowed_with_limits") or {}),
        "narrative_rules": _unique(_sequence(content.get("narrative_rules"))),
        "player_may_tighten": bool(content.get("player_may_tighten", True)),
        "player_may_relax": False,
        "sources": ["world"],
    }

    explicit_unknown: set[str] = set()
    domain_levels: dict[str, list[str]] = {}
    for dimension_id, snapshots in _selected_snapshots(fields).items():
        for snapshot in snapshots:
            option_id = str(snapshot.get("id") or snapshot.get("value") or "")
            source_label = f"{dimension_id}:{option_id}".rstrip(":")
            knowledge_ref = str(snapshot.get("knowledge_profile_ref") or "")
            content_ref = str(snapshot.get("content_profile_ref") or "")
            profile = knowledge_profiles.get(knowledge_ref.removeprefix("knowledge."))
            if not isinstance(profile, Mapping):
                profile = knowledge_profiles.get(knowledge_ref)
            if isinstance(profile, Mapping):
                knowledge_result["sources"].append(source_label)
                for domain, raw_level in dict(profile.get("domains") or {}).items():
                    level = str(raw_level or "unknown").lower()
                    if level not in KNOWLEDGE_LEVELS:
                        level = "unknown"
                    domain_levels.setdefault(str(domain), []).append(level)
                    if level == "unknown":
                        explicit_unknown.add(str(domain))
                knowledge_result["known_facts"].extend(
                    _sequence(profile.get("known_facts"))
                )
                knowledge_result["unknown_facts"].extend(
                    _sequence(profile.get("unknown_facts"))
                )
            profile = content_profiles.get(content_ref.removeprefix("content."))
            if not isinstance(profile, Mapping):
                profile = content_profiles.get(content_ref)
            if isinstance(profile, Mapping):
                content_result["sources"].append(source_label)
                content_result["hard_denials"].extend(
                    _sequence(profile.get("additional_denials"))
                )
                content_result["fade_to_black"].extend(
                    _sequence(profile.get("fade_to_black"))
                )
                content_result["narrative_rules"].extend(
                    _sequence(profile.get("narrative_rules"))
                )
    for domain, levels in domain_levels.items():
        if domain in explicit_unknown:
            knowledge_result["domains"][domain] = "unknown"
        else:
            knowledge_result["domains"][domain] = max(
                levels, key=KNOWLEDGE_LEVELS.index
            )
    knowledge_result["known_facts"] = _unique(knowledge_result["known_facts"])
    knowledge_result["unknown_facts"] = _unique(knowledge_result["unknown_facts"])
    unknown = set(knowledge_result["unknown_facts"])
    knowledge_result["known_facts"] = [
        item for item in knowledge_result["known_facts"] if item not in unknown
    ]
    for key in ("hard_denials", "fade_to_black", "narrative_rules", "sources"):
        content_result[key] = _unique(content_result[key])
    knowledge_result["sources"] = _unique(knowledge_result["sources"])
    return {"knowledge": knowledge_result, "content": content_result}


def explain_boundary_sources(
    world: Mapping[str, Any], fields: Mapping[str, Any]
) -> dict[str, Any]:
    return resolve_character_presets(world, fields)


def check_character_knowledge(
    resolved: Mapping[str, Any], domain: str
) -> dict[str, Any]:
    knowledge = resolved.get("knowledge")
    knowledge = knowledge if isinstance(knowledge, Mapping) else {}
    domain = str(domain or "").strip()
    if domain in set(_sequence(knowledge.get("forbidden_domains"))):
        return {"allowed": False, "level": "unknown", "reason": "world_forbidden"}
    level = str((knowledge.get("domains") or {}).get(domain) or "unknown")
    return {"allowed": level != "unknown", "level": level, "reason": "resolved"}


def check_content_permission(
    resolved: Mapping[str, Any], content_tags: Sequence[str]
) -> dict[str, Any]:
    content = resolved.get("content")
    content = content if isinstance(content, Mapping) else {}
    denied = set(_sequence(content.get("hard_denials")))
    blocked = [str(tag) for tag in content_tags if str(tag) in denied]
    return {"allowed": not blocked, "blocked": blocked}


__all__ = [
    "KNOWLEDGE_LEVELS",
    "PresetLibraryContractError",
    "check_character_knowledge",
    "check_content_permission",
    "dimension_fields",
    "explain_boundary_sources",
    "normalize_preset_dimensions",
    "normalize_preset_libraries",
    "resolve_character_presets",
    "validate_preset_dimensions",
    "validate_preset_selection",
]
