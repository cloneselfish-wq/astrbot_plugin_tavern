from .common import *
from .state import *
from .options import *
from .flow import *

def wizard_completion_state(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
    current_step: int,
    *,
    allow_stages: Sequence[str] | None = None,
) -> dict[str, Any]:
    """返回建卡完成性、缺失项、无效项和下一玩家步骤。"""

    pending = resolve_current_wizard_step(
        template,
        fields,
        current_step,
        allow_stages=allow_stages,
    )
    definitions = [
        item
        for item in _sequence(template.get("fields"))
        if isinstance(item, Mapping)
    ]
    automatic = mode_auto_filled_keys(template, fields)
    allowed = set(allow_stages) if allow_stages is not None else None
    from ..lifecycle import (
        field_stage,
        resolve_profession_stats,
        uses_profession_preset_stats,
    )
    from ..stat_generation import (
        calculate_preset_stack_stats,
        uses_preset_stack_stats,
    )

    missing: list[dict[str, str]] = []
    for definition in definitions:
        key = str(definition.get("key") or "")
        if not key or key.startswith("_") or key in automatic:
            continue
        if allowed is not None and field_stage(definition) not in allowed:
            continue
        if not field_visible(definition, fields):
            continue
        value = fields.get(key)
        if bool(definition.get("required")) and (
            value is None or value == "" or value == []
        ):
            missing.append(
                {
                    "key": key,
                    "label": str(definition.get("label") or key),
                    "owner": "player",
                }
            )

    checked = deepcopy(dict(fields))
    dependency = revalidate_dependent_selections(template, checked)
    invalid = [
        {
            "key": str(item.get("field") or ""),
            "label": str(
                item.get("field_label")
                or item.get("field")
                or "角色资料"
            ),
            "reason": str(item.get("reason") or "当前选择已经失效"),
        }
        for item in (
            list(dependency.get("cleared") or [])
            + list(dependency.get("needs_revision") or [])
        )
        if isinstance(item, Mapping)
    ]
    stat_error = ""
    if not missing and not invalid:
        try:
            if uses_preset_stack_stats(template):
                calculate_preset_stack_stats(
                    template,
                    fields,
                    require_complete=True,
                )
            elif uses_profession_preset_stats(template):
                resolve_profession_stats(
                    template,
                    fields,
                    require_complete=True,
                )
        except ValueError as exc:
            stat_error = str(exc)
            missing.append(
                {
                    "key": "",
                    "label": "角色数值",
                    "owner": "player",
                }
            )

    next_step = next_player_fillable_step(
        template,
        fields,
        0,
        allow_stages=allow_stages,
    )
    next_step_key = (
        str(definitions[next_step].get("key") or "")
        if 0 <= next_step < len(definitions)
        else ""
    )
    synthetic_pending = pending is not None and pending.kind == "synthetic"
    complete = (
        not synthetic_pending
        and not missing
        and not invalid
        and not stat_error
        and next_step >= len(definitions)
    )
    return {
        "complete": complete,
        "missing": missing,
        "invalid": invalid,
        "next_step": next_step,
        "next_step_key": (
            pending.step_key if synthetic_pending and pending else next_step_key
        ),
        "stat_error": stat_error,
        "needs_revision": bool(invalid),
    }


def apply_archetype_pack(
    template: Mapping[str, Any],
    fields: dict[str, Any],
    pack: Mapping[str, Any],
) -> list[str]:
    """兼容入口：只有原子应用成功后才修改传入草稿。"""

    result = apply_archetype_pack_atomic(template, fields, pack)
    if not result.get("ok"):
        raise ValueError(
            "套用角色原型失败："
            + str(result.get("reason") or "角色原型与当前世界内容冲突")
            + " "
            + str(result.get("recovery") or "系统未修改其他建卡资料。")
        )
    fields.clear()
    fields.update(dict(result["fields"]))
    return [str(item) for item in result.get("applied_fields", [])]


def auto_fill_remaining(
    template: Mapping[str, Any],
    fields: dict[str, Any],
) -> list[str]:
    """兼容入口：默认执行原型确定后的自动填充阶段。"""

    return auto_fill_for_phase(template, fields, "post_archetype")


def preset_only_guard(
    template: Mapping[str, Any],
    fields: Mapping[str, Any],
) -> None:
    """B2：确认建卡前校验——非自由字段的值必须能解析为预设候选。"""
    # B2：strict 仅对声明了建卡流程（B2 预设-only 世界）生效；
    # 旧世界包（无 creation_flow）只校验预设字段值，不强制拒绝其自由文本字段。
    strict = bool(template.get("creation_flow"))
    for field in _sequence(template.get("fields")):
        if not isinstance(field, Mapping):
            continue
        key = str(field.get("key") or "")
        if not key:
            continue
        field_type = str(field.get("type") or "text").lower()
        if key.startswith("_"):
            continue
        if key not in fields:
            continue
        if field_type in {"text", "textarea"}:
            # 自由文本由字段声明和统一校验器约束；旗舰世界是否包含某字段
            # 属于作者模板契约，不能在运行时按字段名硬编码。
            continue
        options = preset_options(template, field, fields)
        if not options:
            raise ValueError(f"预设字段 {key} 没有可选预设，无法确认")
        allowed: set[str] = set()
        for option in options:
            allowed.add(str(option.get("id") or ""))
            # 输入边界允许唯一名称；持久化前由
            # revalidate_dependent_selections() 统一转换为稳定 ID。
            allowed.add(str(option.get("value") or ""))
            allowed.add(str(option.get("label") or ""))
        allowed_casefold = {item.casefold() for item in allowed if item}
        raw = fields.get(key)
        if field_type == "multi_select":
            values = [str(item) for item in _sequence(raw)]
            if not values:
                if field.get("required"):
                    raise ValueError(f"预设多选字段 {key} 不能为空")
                continue
            for value in values:
                if value.casefold() not in allowed_casefold:
                    raise ValueError(
                        f"字段“{field.get('label') or key}”包含不是合法预设的值，"
                        "或该预设不属于当前依赖范围"
                    )
        else:
            value = str(raw or "")
            if not value:
                continue
            if value.casefold() not in allowed_casefold:
                raise ValueError(
                    f"字段“{field.get('label') or key}”的值不是合法预设，"
                    "或不属于当前依赖范围"
                )


def preset_source_exists(template: Mapping[str, Any], field: Mapping[str, Any]) -> bool:
    return bool(preset_options(template, field, {}))


__all__ = [name for name in globals() if not name.startswith('__')]

