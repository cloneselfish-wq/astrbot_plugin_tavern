from .common import *

@dataclass(frozen=True, slots=True)
class WizardStep:
    """统一表示合成步骤与真实角色字段。"""

    step_key: str
    kind: str
    label: str
    field_type: str
    required: bool
    persist_to_profile: bool
    source_index: int | None
    stage: str
    user_fillable: bool
    auto_filled: bool
    options: tuple[dict[str, Any], ...]
    page_size: int
    definition: dict[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        # ``definition`` carries the executable candidate contract
        # (preset_source/options_source, filter_by/option_key, choice limits,
        # constraints, presentation metadata, and so on).  Starting from the
        # dataclass alone drops those keys and makes every externally-backed
        # preset field look as if it has no candidates.  Keep the original
        # definition at the top level, then overlay the resolver's normalized
        # wizard metadata.
        payload = deepcopy(self.definition)
        normalized = asdict(self)
        normalized.pop("options", None)
        payload.update(normalized)
        payload["key"] = self.step_key
        payload["type"] = self.field_type
        payload["persist"] = self.persist_to_profile
        if self.options:
            payload["options"] = [dict(item) for item in self.options]
        elif "options" not in payload:
            payload["options"] = []
        return payload


def candidate_input_fingerprint(
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> str:
    """Hash only ranking inputs and author rules that can change order."""

    public_values = {
        str(key): value
        for key, value in values.items()
        if not str(key).startswith("_")
    }
    catalog = [
        {
            "id": str(
                item.get("id")
                or item.get("key")
                or item.get("value")
                or item.get("name")
                or item.get("label")
                or ""
            ),
            "eligibility": item.get("eligibility"),
            "conflicts": item.get("conflicts"),
            "recommendations": item.get("recommendations"),
            "affinity": item.get("affinity"),
            "diversity_tags": item.get("diversity_tags"),
        }
        for item in candidates
    ]
    payload = json.dumps(
        {
            "field": str(field.get("key") or ""),
            "values": public_values,
            "catalog": catalog,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_ordered_candidates(
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshots = values.get(CANDIDATE_SNAPSHOTS_KEY)
    snapshots = snapshots if isinstance(snapshots, Mapping) else {}
    snapshot = snapshots.get(str(field.get("key") or ""))
    if not isinstance(snapshot, Mapping):
        return candidates
    if str(snapshot.get("input_fingerprint") or "") != candidate_input_fingerprint(
        field, values, candidates
    ):
        return candidates
    candidate_ids = snapshot.get("candidate_ids")
    if not isinstance(candidate_ids, Sequence) or isinstance(
        candidate_ids, (str, bytes)
    ):
        return candidates
    by_id = {
        str(
            item.get("id")
            or item.get("key")
            or item.get("value")
            or item.get("name")
            or item.get("label")
            or ""
        ): item
        for item in candidates
    }
    ordered = [
        by_id[str(candidate_id)]
        for candidate_id in candidate_ids
        if str(candidate_id) in by_id
    ]
    return ordered if len(ordered) == len(candidates) else candidates


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


def field_visible(
    field: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> bool:
    values = values if isinstance(values, Mapping) else {}
    field_constraints = field.get("constraints")
    if isinstance(field_constraints, Mapping) and not candidate_matches(
        field, values
    ):
        return False
    condition = field.get("visible_when")
    if not isinstance(condition, Mapping) or not condition:
        return True
    for dependency, expected in condition.items():
        actual = values.get(str(dependency))
        refs = values.get(PRESET_REFS_KEY)
        ref = (
            refs.get(str(dependency), {})
            if isinstance(refs, Mapping)
            else {}
        )
        actual_candidates = {
            str(actual or ""),
            str(ref.get("id") or "") if isinstance(ref, Mapping) else "",
            str(ref.get("value") or "") if isinstance(ref, Mapping) else "",
            str(ref.get("label") or "") if isinstance(ref, Mapping) else "",
        }
        allowed = _sequence(expected)
        if allowed:
            if not actual_candidates & {str(item) for item in allowed}:
                return False
        elif actual != expected:
            return False
    return True


def _preset_source_value(
    template: Mapping[str, Any], field: Mapping[str, Any]
) -> Any:
    source = str(
        field.get("preset_source")
        or field.get("preset_set")
        or field.get("options_source")
        or ""
    ).strip()
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


def _dependent_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """1.0.0-A7：按已选字段（filter_by）从预设集中取选项子集（option_key）。

    例如起始武器字段：options_source=profession_presets、filter_by=profession、
    option_key=starting_weapon_options —— 选项随所选职业变化。
    values 为空时返回全部职业选项的并集（供模板校验与占位）。
    """
    filter_by = str(field.get("filter_by") or "").strip()
    option_key = str(field.get("option_key") or "").strip()
    if not filter_by or not option_key:
        return None
    source = str(
        field.get("preset_source")
        or field.get("preset_set")
        or field.get("options_source")
        or ""
    ).strip()
    if not source:
        return None
    presets = _sequence(_preset_source_value(template, field))
    if not presets:
        return []
    values = values if isinstance(values, Mapping) else {}
    selected = values.get(filter_by)
    refs = values.get(PRESET_REFS_KEY)
    ref = (
        refs.get(filter_by, {})
        if isinstance(refs, Mapping)
        else {}
    )
    candidates: set[str] = set()
    if selected is not None:
        candidates.add(str(selected))
    if isinstance(ref, Mapping):
        for key in ("id", "value", "label", "name"):
            if ref.get(key):
                candidates.add(str(ref[key]))
    union: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for preset in presets:
        if not isinstance(preset, Mapping):
            continue
        option_list = _sequence(preset.get(option_key))
        if not candidates:
            # 未选择：收集并集用于模板校验/占位展示
            for option in option_list:
                if not isinstance(option, Mapping):
                    continue
                identity = str(
                    option.get("id")
                    or option.get("value")
                    or option.get("label")
                    or option.get("name")
                    or ""
                ).strip().casefold()
                if identity and identity not in seen_ids:
                    union.append(dict(option))
                    seen_ids.add(identity)
            continue
        preset_ids = {
            str(preset.get("id") or ""),
            str(preset.get("value") or ""),
            str(preset.get("name") or ""),
            str(preset.get("label") or ""),
            str(preset.get("selection_value") or ""),
        }
        if candidates & preset_ids:
            return [dict(item) for item in option_list if isinstance(item, Mapping)]
    if not candidates:
        return union
    return []


def preset_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not field_visible(field, values):
        return []
    value_field = str(field.get("value_field") or "value")
    label_field = str(field.get("label_field") or "label")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    dependent = _dependent_options(template, field, values)
    raw_options = _sequence(
        dependent if dependent is not None else _preset_source_value(template, field)
    )
    if str(field.get("semantic_role") or "") == "actor.identity.belief":
        from ..belief_affinity import order_belief_candidates

        raw_options = order_belief_candidates(
            [item for item in raw_options if isinstance(item, Mapping)],
            values if isinstance(values, Mapping) else {},
        )
    from ..candidates import rank_candidates

    mapped_options = [item for item in raw_options if isinstance(item, Mapping)]
    if mapped_options and len(mapped_options) == len(raw_options):
        limit = (
            10
            if str(field.get("semantic_role") or "")
            == "actor.capability.specialty"
            else None
        )
        raw_options = rank_candidates(
            field,
            mapped_options,
            values if isinstance(values, Mapping) else {},
            limit,
        )
        raw_options = _snapshot_ordered_candidates(
            field,
            values if isinstance(values, Mapping) else {},
            [dict(item) for item in raw_options],
        )
    for index, raw in enumerate(raw_options):
        if isinstance(raw, Mapping):
            source = dict(raw)
            # B2（A6）：声明 replacement 的预设视为已停用，新卡不可再选。
            if str(source.get("replacement") or "").strip():
                continue
            if not candidate_matches(source, values or {}):
                continue
            executable_rules = {
                key: source[key]
                for key in ("eligibility", "conflicts")
                if key in source
            }
            if executable_rules and not candidate_rule_matches(
                executable_rules, values or {}
            ):
                continue
            preset_id = str(
                source.get("id")
                or source.get("key")
                or source.get(value_field)
                or source.get("name")
                or source.get(label_field)
                or index + 1
            ).strip()
            value = str(
                source.get(value_field)
                or source.get("selection_value")
                or source.get("value")
                or source.get("name")
                or source.get(label_field)
                or preset_id
            ).strip()
            label = str(
                source.get(label_field)
                or source.get("label")
                or source.get("name")
                or value
            ).strip()
            aliases = [str(item).strip() for item in _sequence(source.get("aliases"))]
            description = str(
                source.get("description")
                or source.get("role")
                or source.get("summary")
                or ""
            ).strip()
        else:
            value = label = preset_id = str(raw or "").strip()
            aliases = []
            description = ""
            source = {"value": value, "label": label}
        different_from = str(field.get("must_differ_from") or "").strip()
        if (
            different_from
            and isinstance(values, Mapping)
            and str(values.get(different_from) or "").strip() == value
        ):
            continue
        identity = preset_id.casefold()
        if not value or not label or not identity:
            continue
        if identity in seen:
            raise ValueError(
                f"预设字段 {field.get('label') or field.get('key') or '?'} "
                "存在重复的内部编号，请联系管理员修复世界包"
            )
        seen.add(identity)
        result.append(
            {
                "id": preset_id,
                "value": value,
                "label": label,
                "description": description,
                "aliases": aliases,
                "source": source,
                "recommended": bool(source.get("_candidate_recommended")),
                "recommendation_reasons": list(
                    source.get("_candidate_recommendation_reasons") or []
                )[:2],
            }
        )
    return result


__all__ = [name for name in globals() if not name.startswith('__')]

