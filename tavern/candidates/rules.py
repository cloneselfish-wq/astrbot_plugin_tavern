from .common import *
from .normalization import *
from .dependencies import *

def _rule_typed_ref(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{path} 不能为空")
    try:
        split_ref(text)
    except ValueError:
        raise ValueError(
            f"{path} 必须是稳定类型化引用（如 capability:knight.wall_guard）"
        ) from None
    return text


def _rule_keys_view(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: raw[key] for key in CANDIDATE_RULE_KEYS if key in raw
    }


def candidate_rule_view(raw: Any) -> dict[str, Any]:
    """Extract only the D1 rule keys from a mapping.

    Accepts a full candidate mapping (which also carries id/label/constraints/
    presentation keys) and returns the rule-only view for the strict
    normalizer.  Non-mappings yield an empty rules object.
    """

    return _rule_keys_view(raw)


def _rule_optional_label(value: Any, path: str) -> str:
    if value is None or value == "":
        return ""
    text = str(value).strip()
    if not text:
        raise ValueError(f"{path} 不能为空字符串")
    return text


def _rule_field_groups(raw: Any, *, key: str) -> dict[str, tuple[str, ...]]:
    """Normalize ``{field: [values]}`` or a list of ``{field, values}`` objects."""

    if raw is None or raw == "":
        return {}
    entries: list[tuple[str, Sequence[Any]]] = []
    if isinstance(raw, Mapping):
        entries = [(str(field), values) for field, values in raw.items()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise ValueError(f"candidate rules.{key}[{index}] 必须是对象")
            allowed_keys = {"field", "values"}
            if key == "recommendations":
                # Recommendation reasons are player/author-facing explanation,
                # not part of matching semantics.  Preserve the richer TWP
                # declaration while normalizing only field + stable values.
                allowed_keys.add("reason")
            unknown = set(item) - allowed_keys
            if unknown:
                raise ValueError(
                    f"candidate rules.{key}[{index}] 包含未知字段："
                    + "、".join(sorted(str(value) for value in unknown))
                )
            if "reason" in item:
                _rule_optional_label(
                    item.get("reason"),
                    f"candidate rules.{key}[{index}].reason",
                )
            field = str(item.get("field") or "").strip()
            if not field:
                raise ValueError(f"candidate rules.{key}[{index}].field 不能为空")
            entries.append((field, item.get("values")))
    else:
        raise ValueError(f"candidate rules.{key} 必须是对象或数组")
    result: dict[str, tuple[str, ...]] = {}
    for field, raw_values in entries:
        if not field:
            continue
        if field in result:
            raise ValueError(f"candidate rules.{key} 重复声明字段：{field}")
        if not isinstance(raw_values, Sequence) or isinstance(
            raw_values, (str, bytes)
        ):
            raise ValueError(
                f"candidate rules.{key}.{field} 必须是数组"
            )
        values: list[str] = []
        for raw_value in raw_values:
            value = str(raw_value or "").strip()
            if not value:
                raise ValueError(
                    f"candidate rules.{key}.{field} 不能包含空值"
                )
            if value not in values:
                values.append(value)
        if not values:
            raise ValueError(f"candidate rules.{key}.{field} 不能为空数组")
        result[field] = tuple(values)
    return result


def _normalize_unlock(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError(f"candidate rules.unlocks[{index}] 必须是对象")
    unknown = set(item) - {"ref", "label", "kind"}
    if unknown:
        raise ValueError(
            f"candidate rules.unlocks[{index}] 包含未知字段："
            + "、".join(sorted(str(value) for value in unknown))
        )
    ref = _rule_typed_ref(item.get("ref"), f"candidate rules.unlocks[{index}].ref")
    kind = str(item.get("kind") or "").strip()
    if kind and kind not in UNLOCK_KINDS:
        raise ValueError(
            f"candidate rules.unlocks[{index}].kind 必须是 "
            + "、".join(sorted(UNLOCK_KINDS))
        )
    return {
        "ref": ref,
        "label": _rule_optional_label(
            item.get("label"), f"candidate rules.unlocks[{index}].label"
        ),
        "kind": kind,
    }


def _normalize_grant(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError(f"candidate rules.grants[{index}] 必须是对象")
    unknown = set(item) - {
        "grant_ref",
        "capability_ref",
        "resource_ref",
        "track_ref",
        "label",
        "policy",
        "when",
    }
    if unknown:
        raise ValueError(
            f"candidate rules.grants[{index}] 包含未知字段："
            + "、".join(sorted(str(value) for value in unknown))
        )
    ref_keys = [key for key in ("capability_ref", "resource_ref", "track_ref") if item.get(key)]
    if len(ref_keys) != 1:
        raise ValueError(
            f"candidate rules.grants[{index}] 必须且只能声明一个目标引用"
            "（capability_ref / resource_ref / track_ref）"
        )
    ref_key = ref_keys[0]
    kind = {
        "capability_ref": "capability",
        "resource_ref": "resource",
        "track_ref": "ability_track",
    }[ref_key]
    policy = str(item.get("policy") or "").strip()
    if policy and policy not in GRANT_POLICIES:
        raise ValueError(
            f"candidate rules.grants[{index}].policy 必须是 "
            + "、".join(sorted(GRANT_POLICIES))
        )
    when = item.get("when")
    if when is not None and not isinstance(when, Mapping):
        raise ValueError(f"candidate rules.grants[{index}].when 必须是对象")
    return {
        "ref": _rule_typed_ref(item.get(ref_key), f"candidate rules.grants[{index}].{ref_key}"),
        "kind": kind,
        "grant_ref": str(item.get("grant_ref") or "").strip(),
        "label": _rule_optional_label(
            item.get("label"), f"candidate rules.grants[{index}].label"
        ),
        "policy": policy,
        "when": dict(when) if isinstance(when, Mapping) else None,
    }


def _normalize_resource_modifier(item: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError(f"candidate rules.resource_modifiers[{index}] 必须是对象")
    unknown = set(item) - {"resource_ref", "op", "value", "label"}
    if unknown:
        raise ValueError(
            f"candidate rules.resource_modifiers[{index}] 包含未知字段："
            + "、".join(sorted(str(value) for value in unknown))
        )
    resource_ref = _rule_typed_ref(
        item.get("resource_ref"),
        f"candidate rules.resource_modifiers[{index}].resource_ref",
    )
    op = str(item.get("op") or "").strip()
    if op not in RESOURCE_MODIFIER_OPS:
        raise ValueError(
            f"candidate rules.resource_modifiers[{index}].op 必须是 "
            + "、".join(sorted(RESOURCE_MODIFIER_OPS))
        )
    raw_value = item.get("value")
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise ValueError(
            f"candidate rules.resource_modifiers[{index}].value 必须是整数"
        )
    return {
        "resource_ref": resource_ref,
        "op": op,
        "value": raw_value,
        "label": _rule_optional_label(
            item.get("label"),
            f"candidate rules.resource_modifiers[{index}].label",
        ),
    }


def _normalize_ref_list(raw: Any, *, key: str) -> tuple[tuple[str, str], ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError(f"candidate rules.{key} 必须是数组")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(raw):
        if isinstance(item, Mapping):
            unknown = set(item) - {"ref", "label"}
            if unknown:
                raise ValueError(
                    f"candidate rules.{key}[{index}] 包含未知字段："
                    + "、".join(sorted(str(value) for value in unknown))
                )
            ref = _rule_typed_ref(item.get("ref"), f"candidate rules.{key}[{index}].ref")
            label = _rule_optional_label(
                item.get("label"), f"candidate rules.{key}[{index}].label"
            )
        elif isinstance(item, str):
            ref = _rule_typed_ref(item, f"candidate rules.{key}[{index}]")
            label = ""
        else:
            raise ValueError(f"candidate rules.{key}[{index}] 必须是字符串或对象")
        if not any(existing[0] == ref for existing in result):
            result.append((ref, label))
    return tuple(result)


def normalize_candidate_rules(raw: Any) -> dict[str, Any]:
    """Return the canonical D1 candidate rule object.

    Missing rules are represented explicitly so every consumer sees the same
    contract.  Malformed declarations (unknown keys, invalid refs, wrong
    value types) raise immediately instead of being silently ignored.
    """

    if raw is None or raw == "":
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("candidate rules 必须是对象")
    unknown = set(raw) - CANDIDATE_RULE_KEYS
    if unknown:
        raise ValueError(
            "candidate rules 包含未知字段：" + "、".join(sorted(str(item) for item in unknown))
        )
    visibility = str(raw.get("visibility") or "public").strip().lower()
    if visibility not in CANDIDATE_VISIBILITIES:
        raise ValueError("candidate rules.visibility 必须是 public 或 private")
    unlocks = raw.get("unlocks")
    if unlocks is None or unlocks == "":
        normalized_unlocks: tuple[dict[str, Any], ...] = ()
    else:
        if not isinstance(unlocks, Sequence) or isinstance(unlocks, (str, bytes)):
            raise ValueError("candidate rules.unlocks 必须是数组")
        normalized_unlocks = tuple(
            _normalize_unlock(item, index=index) for index, item in enumerate(unlocks)
        )
    grants = raw.get("grants")
    if grants is None or grants == "":
        normalized_grants: tuple[dict[str, Any], ...] = ()
    else:
        if not isinstance(grants, Sequence) or isinstance(grants, (str, bytes)):
            raise ValueError("candidate rules.grants 必须是数组")
        normalized_grants = tuple(
            _normalize_grant(item, index=index) for index, item in enumerate(grants)
        )
    resource_modifiers = raw.get("resource_modifiers")
    if resource_modifiers is None or resource_modifiers == "":
        normalized_modifiers: tuple[dict[str, Any], ...] = ()
    else:
        if not isinstance(resource_modifiers, Sequence) or isinstance(
            resource_modifiers, (str, bytes)
        ):
            raise ValueError("candidate rules.resource_modifiers 必须是数组")
        normalized_modifiers = tuple(
            _normalize_resource_modifier(item, index=index)
            for index, item in enumerate(resource_modifiers)
        )
    return {
        "eligibility": _rule_field_groups(raw.get("eligibility"), key="eligibility"),
        "conflicts": _rule_field_groups(raw.get("conflicts"), key="conflicts"),
        "recommendations": _rule_field_groups(
            raw.get("recommendations"), key="recommendations"
        ),
        "unlocks": normalized_unlocks,
        "grants": normalized_grants,
        "resource_modifiers": normalized_modifiers,
        "ability_pool_add": _normalize_ref_list(
            raw.get("ability_pool_add"), key="ability_pool_add"
        ),
        "ability_pool_remove": _normalize_ref_list(
            raw.get("ability_pool_remove"), key="ability_pool_remove"
        ),
        "runtime_effect_refs": _normalize_ref_list(
            raw.get("runtime_effect_refs"), key="runtime_effect_refs"
        ),
        "visibility": visibility,
    }


__all__ = [name for name in globals() if not name.startswith('__')]

