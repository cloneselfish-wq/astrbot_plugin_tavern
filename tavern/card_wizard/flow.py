from .common import *
from .state import *
from .options import *

def archetype_packs(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    flow = creation_flow(template)
    return [item for item in _sequence(flow.get("archetype_packs")) if isinstance(item, Mapping)]


def current_creation_mode(fields: Mapping[str, Any]) -> str:
    return str(fields.get("_creation_mode") or "").strip()


def mode_auto_filled_keys(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> set[str]:
    """当前模式下应自动代填、玩家无需逐项选择的字段集合（§6.1/6.2）。"""
    mode = current_creation_mode(fields)
    if not mode or mode == "deep":
        return set()
    flow = creation_flow(template)
    modes = {str(item.get("id") or ""): item for item in _sequence(flow.get("modes")) if isinstance(item, Mapping)}
    config = modes.get(mode, {})
    user_fields = config.get("user_fields")
    if user_fields is None or not _sequence(user_fields):
        return set()
    user_set = {str(item) for item in _sequence(user_fields)}
    auto: set[str] = set()
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        if not key or key.startswith("_") or key in user_set:
            continue
        auto.add(key)
    return auto


def mode_step(template: Mapping[str, Any]) -> dict[str, Any]:
    """B1：建卡模式选择的合成步骤（不写入最终角色卡）。"""
    modes = creation_modes(template)
    return {
        "key": "_creation_mode",
        "label": "建卡模式",
        "required": True,
        "private": False,
        "max_chars": 40,
        "type": "preset_select",
        "options": [
            {
                "id": str(mode.get("id") or ""),
                "value": str(mode.get("label") or mode.get("id") or ""),
                "label": str(mode.get("label") or mode.get("id") or ""),
                "description": str(mode.get("description") or ""),
                "source": dict(mode),
            }
            for mode in modes
            if mode.get("id")
        ],
        "page_size": 3,
        "persist": False,
    }


def archetype_step(template: Mapping[str, Any]) -> dict[str, Any]:
    """B1：快速模式原型包选择的合成步骤（不写入最终角色卡）。"""
    packs = archetype_packs(template)
    return {
        "key": "_archetype_id",
        "label": "角色原型",
        "required": True,
        "private": False,
        "max_chars": 60,
        "type": "preset_select",
        "options": [
            {
                "id": str(pack.get("id") or ""),
                "value": str(pack.get("label") or pack.get("id") or ""),
                "label": str(pack.get("label") or pack.get("id") or ""),
                "description": str(
                    pack.get("summary")
                    or pack.get("description")
                    or "套用一组可在确认前逐项修改的角色预设。"
                ),
                "source": dict(pack),
            }
            for pack in packs
            if pack.get("id")
        ],
        "page_size": 4,
        "persist": False,
    }


def next_wizard_step(
    template: Mapping[str, Any],
    fields_def: Sequence[Mapping[str, Any]],
    step: int,
    fields: Mapping[str, Any],
    *,
    allow_stages: Sequence[str] | None = None,
) -> int:
    """B1：跳过自动代填与不可见字段的下一步（替代 next_fillable_card_step 的调用点）。

    D1：``allow_stages`` 非空时只推进到属于这些建卡阶段（A/B/C）的字段，
    用于开演前只呈现 A 组、B/C 组留待剧情补充。
    """
    from ..lifecycle import field_stage

    auto = mode_auto_filled_keys(template, fields)
    index = max(0, int(step))
    definitions = [item for item in fields_def if isinstance(item, Mapping)]
    while index < len(definitions):
        field = definitions[index]
        key = str(field.get("key") or "")
        if (
            allow_stages is not None
            and field_stage(field) not in set(allow_stages)
        ):
            index += 1
            continue
        if key in auto:
            index += 1
            continue
        if not field_visible(field, fields):
            index += 1
            continue
        value = fields.get(key)
        if value is not None and value != "" and value != []:
            index += 1
            continue
        break
    return index


def next_player_fillable_step(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    start: int = 0,
    *,
    allow_stages: Sequence[str] | None = None,
) -> int:
    """返回当前模式中下一个缺失且应由玩家填写的真实字段。"""

    definitions = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    return next_wizard_step(
        template,
        definitions,
        start,
        fields,
        allow_stages=allow_stages,
    )


def _wizard_step_from_definition(
    definition: Mapping[str, Any],
    *,
    kind: str,
    source_index: int | None,
    auto_filled: bool,
    stage: str = "A",
) -> WizardStep:
    if kind not in {"synthetic", "field"}:
        raise ValueError("WizardStep kind 必须为 synthetic 或 field")
    item = dict(definition)
    return WizardStep(
        step_key=str(item.get("key") or ""),
        kind=kind,
        label=str(item.get("label") or item.get("key") or "建卡步骤"),
        field_type=str(item.get("type") or "text"),
        required=bool(item.get("required")),
        persist_to_profile=bool(item.get("persist", kind == "field")),
        source_index=source_index,
        stage=str(stage or "A"),
        user_fillable=not auto_filled,
        auto_filled=bool(auto_filled),
        options=tuple(
            dict(option)
            for option in _sequence(item.get("options"))
            if isinstance(option, Mapping)
        ),
        page_size=page_size(item),
        definition=item,
    )


def resolve_current_wizard_step(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    current_step: int,
    *,
    allow_stages: Sequence[str] | None = None,
) -> WizardStep | None:
    """通过单一 DTO 解析合成步骤和真实字段。"""

    flow = creation_flow(template)
    mode = current_creation_mode(fields)
    if flow.get("modes") and not mode:
        return _wizard_step_from_definition(
            mode_step(template),
            kind="synthetic",
            source_index=None,
            auto_filled=False,
        )
    if mode == "quick" and not fields.get("_archetype_id"):
        return _wizard_step_from_definition(
            archetype_step(template),
            kind="synthetic",
            source_index=None,
            auto_filled=False,
        )

    definitions = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    index = next_wizard_step(
        template,
        definitions,
        current_step,
        fields,
        allow_stages=allow_stages,
    )
    if not 0 <= index < len(definitions):
        return None
    definition = dict(definitions[index])
    from ..lifecycle import field_stage

    return _wizard_step_from_definition(
        definition,
        kind="field",
        source_index=index,
        auto_filled=str(definition.get("key") or "")
        in mode_auto_filled_keys(template, fields),
        stage=field_stage(definition),
    )


def _mode_configuration(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> dict[str, Any]:
    mode = current_creation_mode(fields)
    for item in _sequence(creation_flow(template).get("modes")):
        if isinstance(item, Mapping) and str(item.get("id") or "") == mode:
            return dict(item)
    return {}


def auto_fill_for_phase(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    phase: str,
) -> list[str]:
    """只填充当前阶段拥有的快速模式字段。"""

    phase = str(phase or "").strip()
    if phase not in AUTO_FILL_PHASES:
        raise ValueError("自动填充阶段无效")
    automatic = mode_auto_filled_keys(template, fields)
    config = _mode_configuration(template, fields)
    if phase == "pre_archetype":
        safe = {
            str(item)
            for item in _sequence(config.get("pre_archetype_safe_fields"))
        }
        targets = automatic & safe
    else:
        targets = automatic

    filled: list[str] = []
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        if (
            key not in targets
            or key in fields
            or not field_visible(field, fields)
        ):
            continue
        options = preset_options(template, field, fields)
        if not options:
            continue
        if str(field.get("type") or "") == "multi_select":
            minimum = max(1, int(field.get("min_choices", 1) or 1))
            selected = options[:minimum]
            if not selected:
                continue
            fields[key] = [
                str(option.get("id") or "") for option in selected
            ]
            store_preset_snapshots(fields, key, selected)
        else:
            selected = options[0]
            fields[key] = str(selected.get("id") or "")
            store_preset_snapshot(fields, key, selected)
        filled.append(key)
    return filled


def apply_archetype_pack_atomic(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    pack: Mapping[str, Any],
) -> dict[str, Any]:
    """在深复制草稿上严格应用原型；失败时不返回部分结果。"""

    archetype_id = str(pack.get("id") or "").strip()
    if not archetype_id:
        return {
            "ok": False,
            "error_code": "CARD_ARCHETYPE_NOT_FOUND",
            "reason": "所选角色原型不存在或已经被世界作者移除。",
            "recovery": "系统未修改建卡资料。请重新查看角色原型后再选择。",
        }
    declared = pack.get("fields")
    if not isinstance(declared, Mapping) or not declared:
        return {
            "ok": False,
            "error_code": "CARD_ARCHETYPE_EMPTY",
            "reason": "该角色原型没有可套用的世界设定。",
            "recovery": "系统未修改建卡资料。请重新选择角色原型，并通知管理员检查世界内容。",
        }

    working = deepcopy(dict(fields))
    automatic = mode_auto_filled_keys(template, working)
    owned = automatic | {str(key) for key in declared}
    cleared: list[str] = []
    for key in sorted(owned):
        if key in working:
            clear_field_and_dependents(template, working, key)
            cleared.append(key)
    working["_archetype_id"] = archetype_id
    working.pop(WIZARD_DELIVERY_KEY, None)

    field_map = {
        str(item.get("key") or ""): item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    }
    pending = [(str(key), value) for key, value in declared.items()]
    applied: list[str] = []
    last_failure: tuple[str, str] | None = None
    while pending:
        progressed = False
        remaining: list[tuple[str, Any]] = []
        for field_key, raw_value in pending:
            field = field_map.get(field_key)
            if field is None:
                last_failure = (
                    field_key,
                    "原型引用了世界中不存在的角色字段",
                )
                remaining.append((field_key, raw_value))
                continue
            options = preset_options(template, field, working)
            if not options:
                last_failure = (
                    str(field.get("label") or field_key),
                    "当前依赖条件下没有可用候选",
                )
                remaining.append((field_key, raw_value))
                continue
            wanted = (
                [str(item).strip() for item in _sequence(raw_value)]
                if str(field.get("type") or "") == "multi_select"
                else [str(raw_value).strip()]
            )
            selected: list[dict[str, Any]] = []
            for expected in wanted:
                match = next(
                    (
                        option
                        for option in options
                        if expected
                        in {
                            str(option.get("id") or ""),
                            str(option.get("value") or ""),
                            str(option.get("label") or ""),
                        }
                    ),
                    None,
                )
                if match is None:
                    selected = []
                    break
                selected.append(dict(match))
            if not selected:
                last_failure = (
                    str(field.get("label") or field_key),
                    "原型声明的内容不属于当前可选范围",
                )
                remaining.append((field_key, raw_value))
                continue
            if str(field.get("type") or "") == "multi_select":
                working[field_key] = [
                    str(option.get("id") or "") for option in selected
                ]
                store_preset_snapshots(working, field_key, selected)
            else:
                working[field_key] = str(selected[0].get("id") or "")
                store_preset_snapshot(working, field_key, selected[0])
            applied.append(field_key)
            progressed = True
        if not remaining:
            break
        if not progressed:
            label, reason = last_failure or (
                "角色原型",
                "原型依赖无法解析",
            )
            return {
                "ok": False,
                "error_code": "CARD_ARCHETYPE_CONFLICT",
                "field_label": label,
                "reason": f"{label}：{reason}。",
                "recovery": "系统未套用该原型，其他建卡资料保持不变。请重新选择角色原型。",
            }
        pending = remaining

    auto_filled = auto_fill_for_phase(template, working, "post_archetype")
    dependency = revalidate_dependent_selections(template, working)
    if dependency.get("cleared") or dependency.get("needs_revision"):
        issue = (
            dependency.get("cleared") or dependency.get("needs_revision")
        )[0]
        label = str(
            issue.get("field_label")
            or issue.get("field")
            or "角色资料"
        )
        return {
            "ok": False,
            "error_code": "CARD_ARCHETYPE_CONFLICT",
            "field_label": label,
            "reason": f"{label}在应用原型后不再符合当前世界候选。",
            "recovery": "系统未套用该原型，其他建卡资料保持不变。请重新选择角色原型。",
        }
    return {
        "ok": True,
        "archetype_id": archetype_id,
        "fields": working,
        "applied_fields": applied,
        "auto_filled_fields": auto_filled,
        "cleared_fields": cleared,
        "warnings": [],
    }


__all__ = [name for name in globals() if not name.startswith('__')]

