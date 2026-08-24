from .common import *
from .sessions import *

def _profession_preset_line(
    preset: Mapping[str, Any],
    key_to_label: Mapping[str, str],
) -> str:
    """Render one profession preset as a single readable line."""
    display = str(preset.get("display_text") or "").strip()
    if display:
        return display
    name = str(preset.get("name") or "").strip() or "?"
    base = preset.get("base_attributes")
    if not isinstance(base, Mapping):
        base = preset.get("attributes")
    base = base if isinstance(base, Mapping) else {}
    numbers = "｜".join(str(int(value)) for value in base.values())
    total = sum(int(value) for value in base.values()) if base else 0
    role = str(preset.get("role") or "").strip()
    text = f"{name}：{numbers}"
    if base:
        text += f"（合计{total}）"
    if role:
        text += f" — {role}"
    return text


def _format_profession_step_prompt(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    field: Mapping[str, Any],
    step: int,
    total_fields: int,
) -> str:
    """Prompts for the profession-preset stat mode (fixed 50 base +7/+3)."""
    field_key = str(field.get("key") or "")
    profession_key = semantic_field_key(
        template, "actor.identity.profession"
    )
    primary_key = semantic_field_key(template, "actor.stats.primary")
    secondary_key = semantic_field_key(template, "actor.stats.secondary")
    if field_key not in {profession_key, primary_key, secondary_key}:
        return ""
    _label_to_key, key_to_label = attribute_maps(template)
    attribute_options = "、".join(key_to_label.values())
    if field_key == profession_key:
        lines = [
            _stage_header(
                template, values, step, total_fields, "选择职业"
            ),
            "选择职业后会自动载入固定 50 点基础属性。",
            "属性顺序：" + "｜".join(key_to_label.values()),
            "",
        ]
        for preset in template.get("profession_presets") or []:
            if not isinstance(preset, Mapping):
                continue
            line = _profession_preset_line(preset, key_to_label)
            if line:
                lines.append(f"· {line}")
        first_name = ""
        for preset in template.get("profession_presets") or []:
            if isinstance(preset, Mapping) and preset.get("name"):
                first_name = str(preset["name"])
                break
        example = first_name or "骑士"
        lines.extend(
            [
                "",
                f"直接回复职业名称，例如：{example}",
                f"或发送：/团 填写 {example}",
            ]
        )
        return "\n".join(lines)
    try:
        resolved = resolve_profession_stats(
            template,
            values,
            require_complete=False,
        )
    except ValueError as exc:
        return f"【无法继续】{exc}\n请先重新选择职业：/团 填写 <职业名称>"
    if field_key == primary_key:
        lines = [
            "【选择主属性｜固定+7】",
            f"当前职业：{resolved['profession']}",
            "职业基础属性：",
        ]
        for key, value in resolved["base"].items():
            lines.append(f"· {resolved['labels'][key]}：{value}")
        lines.extend(
            [
                "",
                f"可选：{attribute_options}",
                "直接回复属性名称，例如："
                + next(iter(key_to_label.values()), "力量"),
            ]
        )
        return "\n".join(lines)
    return (
        "【选择副属性｜固定+3】\n"
        f"职业：{resolved['profession']}\n"
        f"已选主属性：{values.get(primary_key) or '（未选）'}（+7）\n"
        "副属性不能与主属性相同。\n"
        f"可选：{attribute_options}\n"
        "直接回复属性名称"
    )


def _append_candidate_copy(
    lines: list[str],
    option: Mapping[str, Any],
    *,
    field: Mapping[str, Any] | None = None,
) -> None:
    """Append one candidate's summary/details from the shared player DTO."""
    dto = candidate_player_copy(option, field=field)
    if dto.summary:
        lines.append(f"   {dto.summary}")
    if dto.advantages:
        lines.append(f"   优势：{'；'.join(dto.advantages)}")
    if dto.limitations:
        lines.append(f"   限制：{'；'.join(dto.limitations)}")


def _format_preset_step_prompt(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    field: Mapping[str, Any],
    step: int,
    total_fields: int,
) -> str:
    options = preset_options(template, field, values)
    if not options:
        return ""
    label = str(field.get("label") or field.get("key") or "预设")
    lines = [
        _stage_header(template, values, step, total_fields, label),
        f"共 {len(options)} 个候选，系统将按全局序号自动分段发送全部选项。",
        "请等待候选发送完成后，直接回复全局序号或完整名称。",
    ]
    if str(field.get("type") or "") == "multi_select":
        minimum = max(0, int(field.get("min_choices", 0) or 0))
        maximum = max(
            minimum,
            int(field.get("max_choices", minimum or 100) or (minimum or 100)),
        )
        if minimum == maximum:
            lines.extend(
                [
                    f"本项必须选择 {minimum} 个预设选项。",
                    "请用逗号或空格分隔全局序号，例如：1，3，5，8。",
                ]
            )
        else:
            lines.extend(
                [
                    f"本项可选择 {minimum}—{maximum} 个预设选项。",
                    "请用逗号或空格分隔全局序号。",
                ]
            )
    return "\n".join(lines)


def _pending_creation_step(draft: Mapping[str, Any]) -> dict[str, Any] | None:
    """返回尚未完成的合成步骤；真实字段由同一 resolver 处理。"""

    template = draft.get("template") or {}
    values = draft.get("fields") or {}
    step = resolve_current_wizard_step(
        template,
        values,
        int(draft.get("current_step", draft.get("draft_step", 0)) or 0),
    )
    if step is None or step.kind != "synthetic":
        return None
    return step.to_mapping()


def _stage_header(
    template: Mapping[str, Any],
    values: Mapping[str, Any],
    step: int,
    total_fields: int,
    label: str,
) -> str:
    """D1：建卡步骤标题。分阶段世界显示 A 组进度，不显示内部字段数。"""
    if not staged_creation(template):
        return f"【角色卡 {step + 1}/{total_fields}】{label}"
    all_fields = [item for item in template.get("fields") or []]
    a_fields = [
        item
        for item in all_fields
        if isinstance(item, Mapping)
        and field_stage(item) == CARD_STAGE_A
        and field_visible(item, values)
    ]
    a_position = (
        sum(
            1
            for item in all_fields[: step]
            if isinstance(item, Mapping)
            and field_stage(item) == CARD_STAGE_A
        )
        + 1
    )
    return f"【角色卡 A 组 {a_position}/{len(a_fields)}】{label}"


def _append_stage_summary(
    lines: list[str],
    template: Mapping[str, Any],
    values: Mapping[str, Any],
) -> None:
    summary = format_card_stage_summary(template, values)
    if summary:
        lines.append(summary)


def format_card_stage_summary(
    template: Mapping[str, Any],
    fields: Mapping[str, Any] | None,
) -> str:
    """D1：玩家可见的阶段摘要（规格 16 §6/§10.1）。

    只显示“可开演”与待补充数量，不显示任何私密字段名称。
    """
    if not staged_creation(template):
        return ""
    state = card_stage_state(template, fields)
    lines = []
    if state["core_ready"]:
        lines.append("角色资料：可开演")
    else:
        lines.append(
            f"角色资料：待补充 A 组 {state['missing_a_count']} 项"
        )
    if state["pending_count"]:
        lines.append(f"待补充：{state['pending_count']} 项私密关系")
    return "\n".join(lines)


def format_card_prompt(draft: Mapping[str, Any]) -> str:
    if bool(draft.get("suspended")) or str(
        draft.get("session_state") or ""
    ) == SESSION_CLOSED:
        return str(
            draft.get("content_update_notice")
            or "【建卡已暂停】\n当前副本已关闭，系统已保留你的建卡资料。"
            "\n\n副本重新开放后发送：\n/团 当前步骤"
        )
    generated_notice = draft.get("stat_generation_result")
    if isinstance(generated_notice, Mapping):
        without_notice = dict(draft)
        without_notice.pop("stat_generation_result", None)
        return (
            format_preset_stack_result(generated_notice)
            + "\n\n"
            + format_card_prompt(without_notice)
        )
    template = draft.get("template") or {}
    fields = template.get("fields") or []
    values = draft.get("fields")
    values = values if isinstance(values, Mapping) else {}
    pending = _pending_creation_step(draft)
    if pending is not None:
        options = preset_options(template, pending, values)
        label = str(pending.get("label") or "建卡")
        return "\n".join(
            [
                f"【{label}】",
                f"共 {len(options)} 个候选，系统将按全局序号自动发送全部选项。",
                "请等待发送完成后，直接回复全局序号或完整名称。",
            ]
        )
    step = int(
        draft.get("current_step", draft.get("draft_step", 0)) or 0
    )
    world = draft.get("world") or {}
    preset_mode = uses_profession_preset_stats(template)
    preset_stack_mode = uses_preset_stack_stats(template)
    if step >= len(fields) and preset_stack_mode:
        try:
            resolved = calculate_preset_stack_stats(
                template,
                values,
                require_complete=True,
            )
        except ValueError as exc:
            return f"【角色卡数值尚未完成】{exc}"
        assert resolved is not None
        sources = "、".join(
            str(item) for item in stat_generation_config(template).get(
                "bonus_sources", []
            )
        )
        parts = [format_preset_stack_result(resolved)]
        _append_stage_summary(parts, template, values)
        parts.extend(
            [
                "",
                f"属性来源已锁定；如需调整可修改：{sources}。",
                "",
                "修改角色名：",
                "/团 修改角色名 <新名称>",
                "",
                "修改昵称：",
                "/团 修改昵称 <新昵称>",
                "",
                "修改其他字段：",
                "/团 修改 <字段名称>",
                "",
                "重新开始：",
                "/团 重新建卡",
                "",
                "先查看完整角色卡：",
                "/团 预览",
                "",
                "确认无误后：",
                "/团 确认建卡",
            ]
        )
        return "\n".join(parts)
    if step >= len(fields) and preset_mode:
        try:
            resolved = resolve_profession_stats(
                template,
                values,
                require_complete=True,
            )
        except ValueError as exc:
            return f"【角色卡数值尚未完成】{exc}"
        lines = ["【角色卡字段已填写完成】"]
        _append_stage_summary(lines, template, values)
        lines.extend(
            [
                f"职业：{resolved['profession']}",
                (
                    f"主属性：{resolved['primary']['label']}"
                    f" +{resolved['primary']['bonus']}"
                ),
                (
                    f"副属性：{resolved['secondary']['label']}"
                    f" +{resolved['secondary']['bonus']}"
                ),
                "最终属性：",
            ]
        )
        for key, value in resolved["raw"].items():
            lines.append(f"· {resolved['labels'][key]}：{value}")
        lines.extend(
            [
                f"基础总和：{resolved['base_total']}",
                f"专精加成：{resolved['bonus_total']}",
                f"最终总和：{resolved['effective_total']}",
                "",
                "重新选择主副属性：",
                "/团 重填数值",
                "",
                "修改角色名：",
                "/团 修改角色名 <新名称>",
                "",
                "修改昵称：",
                "/团 修改昵称 <新昵称>",
                "",
                "修改其他字段：",
                "/团 修改 <字段名称>",
                "",
                "重新开始：",
                "/团 重新建卡",
                "",
                "先查看完整角色卡：",
                "/团 预览",
                "",
                "确认无误后：",
                "/团 确认建卡",
            ]
        )
        return "\n".join(lines)
    if step >= len(fields):
        allocation = card_stat_allocation(template, values)
        lines = ["【角色卡字段已填写完成】"]
        _append_stage_summary(lines, template, values)
        if allocation["stat_fields"]:
            lines.append(
                f"角色数值：已使用 {allocation['used']}"
                f"/{allocation['budget']} 点"
                f" · 剩余 {allocation['remaining']} 点"
            )
            lines.append(
                "只重新分配数值：/团 重填数值"
            )
        profession_key = semantic_field_key(
            template, "actor.identity.profession"
        )
        if profession_key and values.get(profession_key):
            lines.append(
                "若已选预设职业，其基础数值已自动套用；"
                "剩余点数可经 /团 重填数值 自由分配。"
            )
        lines.extend(
            [
                "",
                "修改角色名：",
                "/团 修改角色名 <新名称>",
                "",
                "修改昵称：",
                "/团 修改昵称 <新昵称>",
                "",
                "修改其他字段：",
                "/团 修改 <字段名称>",
                "",
                "重新开始：",
                "/团 重新建卡",
                "",
                "先查看完整角色卡：",
                "/团 预览",
                "",
                "确认无误后：",
                "/团 确认建卡",
            ]
        )
        return "\n".join(lines)
    field = fields[step]
    if preset_mode and str(field.get("key") or "") in {
        semantic_field_key(template, "actor.stats.primary"),
        semantic_field_key(template, "actor.stats.secondary"),
    }:
        preset_prompt = _format_profession_step_prompt(
            template,
            values,
            field,
            step,
            len(fields),
        )
        if preset_prompt:
            return preset_prompt
    if (
        str(field.get("type") or "") in {"select", "preset_select"}
        or field.get("options")
        or field.get("options_source")
        or field.get("preset_source")
    ):
        preset_prompt = _format_preset_step_prompt(
            template,
            values,
            field,
            step,
            len(fields),
        )
        if preset_prompt:
            return preset_prompt
    allocation = card_stat_allocation(template, values, step)
    current_stat = allocation.get("current")
    if isinstance(current_stat, Mapping):
        lines = []
        if (
            int(current_stat["position"]) == 1
            and not allocation["values"]
        ):
            lines.extend(
                [
                    "【接下来开始填写角色数值】",
                    "前面的角色资料已经保存；"
                    "数值会按世界模板的总预算依次分配。",
                ]
            )
        lines.extend(
            [
                (
                    f"【角色数值 {current_stat['position']}"
                    f"/{current_stat['total']}】"
                    f"{current_stat['label']}"
                ),
                (
                    f"当前可填：{current_stat['minimum']}"
                    f"—{current_stat['effective_maximum']}"
                ),
                (
                    f"总预算：{allocation['budget']} 点"
                    f" · 已使用：{current_stat['used_before']} 点"
                    f" · 当前剩余："
                    f"{current_stat['remaining_before']} 点"
                ),
            ]
        )
        if int(current_stat["reserved_minimum"]) > 0:
            lines.append(
                f"已为后续属性预留最低 "
                f"{current_stat['reserved_minimum']} 点。"
            )
        lines.extend(
            [
                "后续属性的可填上限会随剩余预算自动递减。",
                "直接回复整数，或发送：/团 填写 <整数>",
                "重新分配全部数值：/团 重填数值",
            ]
        )
        return "\n".join(lines)
    required = "必填" if field.get("required") else "选填"
    header = _stage_header(
        template,
        values,
        step,
        len(fields),
        str(field.get("label") or ""),
    )
    if staged_creation(template):
        return (
            f"{header}（{required}，最多 {field.get('max_chars')} 字）\n"
            "此组为开演所需内容；其余部分将在剧情中补充。\n"
            "字段内容不得包含空格、全角空格、换行或制表符。\n"
            "直接回复内容，或发送：/团 填写 <内容>"
        )
    return (
        f"{header}（{required}，最多 {field.get('max_chars')} 字）\n"
        "字段内容不得包含空格、全角空格、换行或制表符。\n"
        "直接回复内容，或发送：/团 填写 <内容>"
    )


__all__ = [name for name in globals() if not name.startswith('__')]

