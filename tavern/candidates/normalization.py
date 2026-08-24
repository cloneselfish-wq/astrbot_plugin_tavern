from .common import *

def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _mapping_path(root: Mapping[str, Any], path: str) -> Any:
    current: Any = root
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _normalize_condition(raw: Any, *, group: str, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"constraints.{group}[{index}] 必须是对象")
    field = str(raw.get("field") or "").strip()
    if not field:
        raise ValueError(f"constraints.{group}[{index}].field 不能为空")
    raw_values = raw.get("values")
    if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
        raise ValueError(f"constraints.{group}[{index}].values 必须是数组")
    values: list[str] = []
    for raw_value in raw_values:
        value = str(raw_value or "").strip()
        if not value:
            raise ValueError(f"constraints.{group}[{index}].values 不能包含空值")
        if value not in values:
            values.append(value)
    if not values:
        raise ValueError(f"constraints.{group}[{index}].values 不能为空")
    return {"field": field, "values": values}


def normalize_candidate_constraints(raw: Any) -> dict[str, Any]:
    """Return the canonical all/any/excludes constraint object.

    Missing constraints are represented explicitly so every consumer sees the
    same contract.  Malformed declarations raise immediately instead of being
    silently ignored by one surface and accepted by another.
    """

    if raw in (None, ""):
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("candidate constraints 必须是对象")
    unknown = set(raw) - {*CONSTRAINT_GROUPS, "when_unresolved"}
    if unknown:
        raise ValueError(
            "candidate constraints 包含未知字段：" + "、".join(sorted(str(item) for item in unknown))
        )
    policy = str(raw.get("when_unresolved") or "hide").strip().lower()
    if policy not in UNRESOLVED_POLICIES:
        raise ValueError(
            "constraints.when_unresolved 必须是 hide、show 或 generic_only"
        )
    normalized: dict[str, Any] = {"when_unresolved": policy}
    for group in CONSTRAINT_GROUPS:
        conditions = raw.get(group, [])
        if not isinstance(conditions, Sequence) or isinstance(conditions, (str, bytes)):
            raise ValueError(f"constraints.{group} 必须是数组")
        normalized[group] = [
            _normalize_condition(item, group=group, index=index)
            for index, item in enumerate(conditions)
        ]
    return normalized


def constraint_field_refs(raw: Any) -> set[str]:
    constraints = normalize_candidate_constraints(raw)
    return {
        str(condition["field"])
        for group in CONSTRAINT_GROUPS
        for condition in constraints[group]
    }


def selected_ids(values: Mapping[str, Any], field_key: str) -> tuple[str, ...]:
    """Resolve stable selected IDs without consulting labels or aliases."""

    field_key = str(field_key or "")
    refs = values.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    selected = refs.get(field_key)
    entries = (
        list(selected)
        if isinstance(selected, Sequence)
        and not isinstance(selected, (str, bytes, Mapping))
        else [selected]
    )
    result: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        snapshot = entry.get("snapshot")
        snapshot = snapshot if isinstance(snapshot, Mapping) else {}
        candidate_id = str(entry.get("id") or snapshot.get("id") or "").strip()
        if candidate_id and candidate_id not in result:
            result.append(candidate_id)
    if result:
        return tuple(result)

    raw = values.get(field_key)
    raw_values = (
        list(raw)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, Mapping))
        else [raw]
    )
    for item in raw_values:
        candidate_id = str(item or "").strip()
        if candidate_id and candidate_id not in result:
            result.append(candidate_id)
    return tuple(result)


def candidate_match_details(
    candidate: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate one candidate and expose author-facing rejection details."""

    constraints = normalize_candidate_constraints(candidate.get("constraints"))
    def condition_matches(condition: Mapping[str, Any]) -> bool:
        actual = set(selected_ids(values, str(condition.get("field") or "")))
        expected = {str(item) for item in _sequence(condition.get("values"))}
        return bool(actual & expected)

    reasons: list[str] = []
    unresolved: set[str] = set()
    failed_all = [
        condition
        for condition in constraints["requires_all"]
        if selected_ids(values, str(condition.get("field") or ""))
        and not condition_matches(condition)
    ]
    unresolved.update(
        str(condition.get("field") or "")
        for condition in constraints["requires_all"]
        if not selected_ids(values, str(condition.get("field") or ""))
    )
    if failed_all:
        reasons.append("requires_all")
    requires_any = constraints["requires_any"]
    if requires_any and not any(condition_matches(item) for item in requires_any):
        unresolved_any = [
            str(item.get("field") or "")
            for item in requires_any
            if not selected_ids(values, str(item.get("field") or ""))
        ]
        if unresolved_any:
            unresolved.update(unresolved_any)
        else:
            reasons.append("requires_any")
    if any(condition_matches(item) for item in constraints["excludes"]):
        reasons.append("excluded")
    unresolved.update(
        str(condition.get("field") or "")
        for condition in constraints["excludes"]
        if not selected_ids(values, str(condition.get("field") or ""))
    )
    policy = constraints["when_unresolved"]
    if unresolved and not reasons:
        if policy == "show":
            return {
                "matches": True,
                "unresolved_fields": sorted(unresolved),
                "reasons": [],
            }
        if policy == "generic_only" and bool(candidate.get("generic", False)):
            return {
                "matches": True,
                "unresolved_fields": sorted(unresolved),
                "reasons": [],
            }
        reasons.append(
            "unresolved_generic_only"
            if policy == "generic_only"
            else "unresolved_hidden"
        )
    return {
        "matches": not reasons,
        "unresolved_fields": sorted(unresolved),
        "reasons": reasons,
    }


def candidate_matches(
    candidate: Mapping[str, Any],
    values: Mapping[str, Any],
) -> bool:
    return bool(candidate_match_details(candidate, values)["matches"])


def _source_value(template: Mapping[str, Any], field: Mapping[str, Any]) -> Any:
    source = str(field.get("preset_source") or field.get("options_source") or "").strip()
    if not source:
        return field.get("options")
    candidates = [source]
    if source.startswith("rules.actor."):
        candidates.append(source.removeprefix("rules.actor."))
    if source.startswith("actor."):
        candidates.append(source.removeprefix("actor."))
    candidates.extend((f"preset_sets.{source}", source.removesuffix("_presets")))
    for candidate in dict.fromkeys(candidates):
        value = _mapping_path(template, candidate)
        if _sequence(value):
            return value
    return field.get("options")


def raw_candidate_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
) -> list[Any]:
    """Return the candidate union consumed by a field, before filtering."""

    source = _sequence(_source_value(template, field))
    option_key = str(field.get("option_key") or "").strip()
    filter_by = str(field.get("filter_by") or "").strip()
    if not option_key or not filter_by:
        return source
    result: list[Any] = []
    seen: set[str] = set()
    for parent in source:
        if not isinstance(parent, Mapping):
            continue
        for option in _sequence(parent.get(option_key)):
            if isinstance(option, Mapping):
                identity = str(
                    option.get("id")
                    or option.get("key")
                    or option.get("value")
                    or option.get("name")
                    or ""
                ).strip()
            else:
                identity = str(option or "").strip()
            token = identity.casefold()
            if token and token not in seen:
                result.append(option)
                seen.add(token)
    return result


__all__ = [name for name in globals() if not name.startswith('__')]

