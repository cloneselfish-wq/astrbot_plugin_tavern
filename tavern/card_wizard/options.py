from .common import *
from .state import *

def page_size(field: Mapping[str, Any]) -> int:
    try:
        value = int(field.get("page_size", 5))
    except (TypeError, ValueError):
        value = 5
    return min(10, max(1, value))


def choose_option(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    submitted: str,
) -> dict[str, Any]:
    options = preset_options(template, field, values)
    text = str(submitted or "").strip()
    if text.casefold() in NAV_NEXT | NAV_PREVIOUS:
        raise ValueError(
            "当前候选会按全局序号自动分段全部发送，无需翻页。"
            "请直接回复全局序号或完整名称；如需重看，请发送 /团 当前"
        )
    if text.isdigit():
        ordinal = int(text)
        if 1 <= ordinal <= len(options):
            return dict(options[ordinal - 1])
        raise ValueError("序号超出可选范围，请按列表中的全局序号选择")
    reference = text.casefold()
    matches = []
    for option in options:
        candidates = {
            str(option.get("id") or "").casefold(),
            str(option.get("value") or "").casefold(),
            str(option.get("label") or "").casefold(),
            *(str(item).casefold() for item in option.get("aliases", [])),
        }
        if reference and reference in candidates:
            matches.append(option)
    if len(matches) == 1:
        return dict(matches[0])
    if len(matches) > 1:
        raise ValueError("该名称对应多个预设，请回复全局序号或输入更完整的名称")
    label = str(field.get("label") or field.get("key") or "当前字段")
    available = "、".join(
        str(option.get("label") or option.get("value") or "")
        for option in options[: page_size(field)]
        if str(option.get("label") or option.get("value") or "").strip()
    )
    automatic = (
        f"系统已保留其他建卡资料；可选项示例：{available}。"
        if available
        else "系统已保留其他建卡资料，但当前没有可用候选。"
    )
    raise ValueError(
        f"填写“{label}”失败：该选项不属于当前可选范围。"
        f"{automatic}请重新查看本步骤候选后再选择"
    )


def choose_options(
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    values: Mapping[str, Any],
    submitted: str,
) -> list[dict[str, Any]]:
    """Resolve a multi-select answer while preserving the player's order."""

    options = preset_options(template, field, values)
    tokens = [
        token.strip()
        for token in re.split(r"[,，、;；\s]+", str(submitted or "").strip())
        if token.strip()
    ]
    if not tokens:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in tokens:
        if token.isdigit():
            ordinal = int(token)
            if not 1 <= ordinal <= len(options):
                raise ValueError("序号超出可选范围，请按列表中的全局序号选择")
            option = dict(options[ordinal - 1])
        else:
            option = choose_option(template, field, values, token)
        identity = str(option.get("id") or "").casefold()
        if identity and identity not in seen:
            result.append(option)
            seen.add(identity)
    minimum = max(0, int(field.get("min_choices", 0) or 0))
    maximum = max(minimum, int(field.get("max_choices", 100) or 100))
    if len(result) < minimum or len(result) > maximum:
        if minimum == maximum:
            raise ValueError(
                f"本项必须选择 {minimum} 个预设选项；"
                "请用逗号或空格分隔序号"
            )
        raise ValueError(
            f"本项必须选择 {minimum}—{maximum} 个预设选项；"
            "请用逗号或空格分隔序号"
        )
    return result


def store_preset_snapshot(
    values: dict[str, Any], field_key: str, option: Mapping[str, Any]
) -> None:
    refs = values.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    source = option.get("source")
    refs[field_key] = {
        "id": str(option.get("id") or ""),
        "value": str(option.get("value") or ""),
        "label": str(option.get("label") or ""),
        "snapshot": deepcopy(dict(source)) if isinstance(source, Mapping) else {},
    }
    values[PRESET_REFS_KEY] = refs


def store_preset_snapshots(
    values: dict[str, Any], field_key: str, options: Sequence[Mapping[str, Any]]
) -> None:
    refs = values.get(PRESET_REFS_KEY)
    refs = dict(refs) if isinstance(refs, Mapping) else {}
    refs[field_key] = [
        {
            "id": str(option.get("id") or ""),
            "value": str(option.get("value") or ""),
            "label": str(option.get("label") or ""),
            "snapshot": deepcopy(dict(option.get("source") or {})),
        }
        for option in options
    ]
    values[PRESET_REFS_KEY] = refs


def clear_field_and_dependents(
    template: Mapping[str, Any], values: dict[str, Any], field_key: str
) -> dict[str, Any]:
    """Clear the edited field, then retain every still-valid downstream value."""

    field_key = str(field_key or "")
    values.pop(field_key, None)
    refs = values.get(PRESET_REFS_KEY)
    if isinstance(refs, dict):
        refs.pop(field_key, None)
    report = revalidate_dependent_selections(template, values)
    report["edited_field"] = field_key
    report["affected_fields"] = list(dependent_fields(template, field_key))
    snapshots = values.get(CANDIDATE_SNAPSHOTS_KEY)
    if isinstance(snapshots, dict):
        for key in {field_key, *report["affected_fields"]}:
            snapshots.pop(str(key), None)
        if not snapshots:
            values.pop(CANDIDATE_SNAPSHOTS_KEY, None)
    values.pop(WIZARD_DELIVERY_KEY, None)
    return report


def selected_preset_ids(
    values: Mapping[str, Any],
    field_key: str,
) -> list[str]:
    """Return the authoritative stable IDs selected for one draft field."""

    refs = values.get(PRESET_REFS_KEY)
    refs = refs if isinstance(refs, Mapping) else {}
    selected = refs.get(str(field_key))
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
        preset_id = str(
            entry.get("id")
            or (
                entry.get("snapshot", {}).get("id")
                if isinstance(entry.get("snapshot"), Mapping)
                else ""
            )
            or ""
        ).strip()
        if preset_id and preset_id not in result:
            result.append(preset_id)
    if result:
        return result
    raw = values.get(str(field_key))
    raw_values = (
        list(raw)
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else [raw]
    )
    return [str(item).strip() for item in raw_values if str(item or "").strip()]


def revalidate_dependent_selections(
    template: Mapping[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Canonicalize preset fields to stable IDs and clear stale dependencies.

    The pass is intentionally iterative: clearing one invalid selection can
    invalidate a later field whose candidate set depends on it.
    """

    definitions = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    cleared: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    needs_revision: list[dict[str, Any]] = []
    canonicalized: list[str] = []
    changed = True
    while changed:
        changed = False
        for field in definitions:
            key = str(field.get("key") or "")
            if not key or key not in values:
                continue
            field_type = str(field.get("type") or "").lower()
            is_preset = bool(
                field_type in {"select", "preset_select", "multi_select"}
                or field.get("options")
                or field.get("options_source")
                or field.get("preset_source")
                or field.get("preset_set")
            )
            if not is_preset:
                continue
            if not field_visible(field, values):
                previous = values.get(key)
                values.pop(key, None)
                refs = values.get(PRESET_REFS_KEY)
                if isinstance(refs, dict):
                    refs.pop(key, None)
                cleared.append(
                    {
                        "field": key,
                        "field_label": str(field.get("label") or key),
                        "values": _sequence(previous) or [previous],
                        "reason": "dependency_hidden",
                    }
                )
                changed = True
                continue
            options = preset_options(template, field, values)
            option_by_identity: dict[str, dict[str, Any]] = {}
            for option in options:
                for identity in (
                    option.get("id"),
                    option.get("value"),
                    option.get("label"),
                    *option.get("aliases", []),
                ):
                    text = str(identity or "").strip().casefold()
                    if text:
                        option_by_identity.setdefault(text, option)
            selected_ids = selected_preset_ids(values, key)
            resolved: list[dict[str, Any]] = []
            invalid: list[str] = []
            for selected in selected_ids:
                option = option_by_identity.get(str(selected).casefold())
                if option is None:
                    invalid.append(str(selected))
                elif str(option.get("id") or "") not in {
                    str(item.get("id") or "") for item in resolved
                }:
                    resolved.append(option)
            if field_type == "multi_select" and resolved:
                canonical = [str(item.get("id") or "") for item in resolved]
                if values.get(key) != canonical:
                    values[key] = canonical
                    canonicalized.append(key)
                store_preset_snapshots(values, key, resolved)
                if invalid:
                    retained.append(
                        {
                            "field": key,
                            "field_label": str(field.get("label") or key),
                            "retained": canonical,
                            "removed": invalid,
                            "reason": "selection_subset_retained",
                        }
                    )
                    changed = True
                minimum = max(0, int(field.get("min_choices", 0) or 0))
                if len(resolved) < minimum:
                    needs_revision.append(
                        {
                            "field": key,
                            "field_label": str(field.get("label") or key),
                            "retained": canonical,
                            "minimum": minimum,
                            "reason": "below_min_choices",
                        }
                    )
                continue
            if invalid or not resolved:
                previous = values.get(key)
                values.pop(key, None)
                refs = values.get(PRESET_REFS_KEY)
                if isinstance(refs, dict):
                    refs.pop(key, None)
                cleared.append(
                    {
                        "field": key,
                        "field_label": str(field.get("label") or key),
                        "values": invalid or (_sequence(previous) or [previous]),
                        "reason": "selection_invalid",
                    }
                )
                changed = True
                continue
            canonical = str(resolved[0].get("id") or "")
            if values.get(key) != canonical:
                values[key] = canonical
                canonicalized.append(key)
            store_preset_snapshot(values, key, resolved[0])
    return {
        "ok": not cleared and not needs_revision,
        "cleared": cleared,
        "retained": retained,
        "needs_revision": needs_revision,
        "canonicalized": sorted(set(canonicalized)),
    }


def creation_flow(template: Mapping[str, Any]) -> dict[str, Any]:
    """B1：建卡模式与原型包配置（§6）。"""
    flow = template.get("creation_flow")
    return dict(flow) if isinstance(flow, Mapping) else {}


def creation_modes(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    flow = creation_flow(template)
    return [
        creation_mode_plan(template, str(item.get("id") or ""))
        for item in _sequence(flow.get("modes"))
        if isinstance(item, Mapping) and item.get("id")
    ]


def creation_mode_plan(
    template: Mapping[str, Any],
    mode_id: str,
) -> dict[str, Any]:
    """Calculate the real player-facing steps for one creation mode."""

    raw_modes = [
        item
        for item in _sequence(creation_flow(template).get("modes"))
        if isinstance(item, Mapping)
    ]
    source = next(
        (
            dict(item)
            for item in raw_modes
            if str(item.get("id") or "") == str(mode_id)
        ),
        None,
    )
    if source is None:
        raise ValueError("建卡模式不存在")
    fields = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
        and str(item.get("key") or "")
        and not str(item.get("key") or "").startswith("_")
    ]
    declared = source.get("user_fields")
    if declared is None:
        player_keys = [str(item.get("key") or "") for item in fields]
    else:
        declared_keys = {str(item) for item in _sequence(declared)}
        player_keys = [
            str(item.get("key") or "")
            for item in fields
            if str(item.get("key") or "") in declared_keys
        ]
    auto_keys = [
        str(item.get("key") or "")
        for item in fields
        if str(item.get("key") or "") not in set(player_keys)
    ]
    synthetic_steps = 1 + (1 if str(mode_id) == "quick" else 0)
    source.pop("target_interactions", None)
    source.pop("estimated_messages", None)
    source.update(
        {
            "required_player_steps": len(player_keys),
            "auto_filled_steps": len(auto_keys),
            "logical_choice_batches": len(player_keys),
            "estimated_player_replies": len(player_keys) + synthetic_steps,
            "player_step_count": len(player_keys),
            "auto_fill_count": len(auto_keys),
        }
    )
    return source


__all__ = [name for name in globals() if not name.startswith('__')]

