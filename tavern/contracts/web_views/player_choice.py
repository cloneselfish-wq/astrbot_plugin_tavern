"""D1-UX-001～004：PlayerChoiceView 纯投影。

候选对象既可来自 ``tavern.card_wizard.preset_options`` 的规范化选项
（Mapping，含 ``id/value/label/description/source``），也可直接使用
``tavern.copy.candidate.CandidatePlayerCopy``。文本清洗全部复用
``copy.candidate`` 的既有预算与隐藏标记规则。

``choice_ref`` 只服务内部动作提交，普通渲染链禁止把它拼进玩家文本；
``value_preview`` 的 ``before/bonus/after`` 由服务端一次性计算，渲染器
不得自行相加。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...copy.candidate import CandidatePlayerCopy, candidate_player_copy

from ..common import clean_label, safe_int


CHOICE_COMPATIBILITY_LABELS = {
    "recommended": "推荐",
    "allowed": "可选",
    "locked": "锁定",
    "conflict": "冲突",
}

_SELECTABLE_STATES = frozenset({"recommended", "allowed"})


def _compatibility_view(
    compatibility: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = dict(compatibility) if isinstance(compatibility, Mapping) else {}
    state = str(raw.get("state") or "allowed").strip().lower()
    if state not in CHOICE_COMPATIBILITY_LABELS:
        state = "allowed"
    reasons: list[str] = []
    raw_reasons = raw.get("reasons") or raw.get("reason") or []
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    for item in raw_reasons:
        text = clean_label(item)
        if text and text not in reasons:
            reasons.append(text)
    return {
        "state": state,
        "label": CHOICE_COMPATIBILITY_LABELS[state],
        "reasons": reasons,
    }


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = clean_label(value)
        return [text] if text else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [clean_label(item) for item in value if clean_label(item)]
    return []


def _option_source(option: Any) -> Mapping[str, Any]:
    if not isinstance(option, Mapping):
        return {}
    source = option.get("source")
    return source if isinstance(source, Mapping) else option


def _rule_compatibility(
    option: Any,
    values: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(values, Mapping):
        return None
    source = _option_source(option)
    if not source:
        return None
    if isinstance(option, Mapping) and bool(option.get("recommended")):
        return {
            "state": "recommended",
            "reasons": _text_list(option.get("recommendation_reasons"))[:2],
        }
    from ...candidates import candidate_rule_status

    executable_rules: dict[str, Any] = {
        key: source[key]
        for key in ("eligibility", "conflicts")
        if key in source
    }
    recommendations = source.get("recommendations")
    if isinstance(recommendations, Mapping):
        executable_rules["recommendations"] = recommendations
    elif (
        isinstance(recommendations, Sequence)
        and not isinstance(recommendations, (str, bytes))
        and all(
            isinstance(item, Mapping)
            and set(item).issubset({"field", "values"})
            for item in recommendations
        )
    ):
        executable_rules["recommendations"] = recommendations
    if not executable_rules:
        return None
    try:
        status = candidate_rule_status(executable_rules, values)
    except ValueError:
        return None
    if status["conflicted"]:
        return {
            "state": "conflict",
            "reasons": ["与当前已经确认的角色设定冲突。"],
        }
    if not status["eligible"]:
        return {
            "state": "locked",
            "reasons": ["当前角色设定尚未满足该选项的资格条件。"],
        }
    if status["recommended"]:
        return {
            "state": "recommended",
            "reasons": ["与当前职业、出身或背景设定相互呼应。"],
        }
    return None


def _profession_mechanical_preview(
    option: Any,
    *,
    template: Mapping[str, Any] | None,
    field: Mapping[str, Any] | None,
) -> list[str]:
    """Project the complete profession stat contract for BOT and WebUI.

    D2 keeps the world author source authoritative: renderers no longer inspect
    ``base_attributes`` independently, so proactive candidate delivery cannot
    silently lose the values that the ordinary card prompt still shows.
    """

    if not isinstance(template, Mapping) or not isinstance(field, Mapping):
        return []
    if str(field.get("semantic_role") or "") != "actor.identity.profession":
        return []
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    if str(stats.get("mode") or "").strip().lower() not in {
        "preset",
        "profession_preset",
    }:
        return []
    source = _option_source(option)
    base = source.get("base_attributes") or source.get("attributes")
    if not isinstance(base, Mapping) or not base:
        return []
    attributes = [
        item
        for item in stats.get("attributes") or []
        if isinstance(item, Mapping) and str(item.get("key") or "")
    ]
    pairs = [
        (
            str(item.get("label") or item.get("key") or ""),
            int(base[str(item["key"])]),
        )
        for item in attributes
        if str(item["key"]) in base
    ]
    if not pairs:
        return []
    midpoint = min(5, len(pairs))
    first = "｜".join(f"{label} {value}" for label, value in pairs[:midpoint])
    second = "｜".join(f"{label} {value}" for label, value in pairs[midpoint:])
    lines = [f"基础属性：{first}"]
    if second:
        lines.append(f"　　　　　{second}")
    base_total = int(source.get("base_total") or sum(value for _, value in pairs))
    lines.append(f"基础总和：{base_total}")
    resource = source.get("core_resource")
    if isinstance(resource, Mapping):
        resource_label = clean_label(resource.get("label"))
        initial = resource.get("initial")
        maximum = resource.get("maximum")
        if resource_label and initial is not None and maximum is not None:
            lines.append(
                f"核心资源：{resource_label} {safe_int(initial, 0)}/"
                f"{safe_int(maximum, 0)}"
            )
    bonus_rule = source.get("bonus_rule")
    bonus_rule = bonus_rule if isinstance(bonus_rule, Mapping) else {}
    primary = safe_int(
        bonus_rule.get("primary_attribute_bonus")
        or stats.get("primary_bonus"),
        7,
    )
    secondary = safe_int(
        bonus_rule.get("secondary_attribute_bonus")
        or stats.get("secondary_bonus"),
        3,
    )
    final_total = safe_int(
        source.get("final_total")
        or (stats.get("total_validation") or {}).get("final_total")
        or stats.get("budget"),
        base_total + primary + secondary,
    )
    lines.append(
        f"后续加点：主属性 +{primary}｜副属性 +{secondary}"
        f"｜最终总和 {final_total}"
    )
    return lines


def _attribute_value_preview(
    option: Any,
    *,
    template: Mapping[str, Any] | None,
    field: Mapping[str, Any] | None,
    values: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any] | None]:
    if (
        not isinstance(template, Mapping)
        or not isinstance(field, Mapping)
        or not isinstance(values, Mapping)
    ):
        return None, [], None
    role = str(field.get("semantic_role") or "")
    if role not in {"actor.stats.primary", "actor.stats.secondary"}:
        return None, [], None
    from ...lifecycle import (
        attribute_maps,
        resolve_profession_stats,
        semantic_field_key,
    )

    try:
        resolved = resolve_profession_stats(
            template,
            values,
            require_complete=False,
        )
    except ValueError:
        return None, [], None
    label = (
        str(option.get("label") or option.get("value") or "")
        if isinstance(option, Mapping)
        else str(option or "")
    ).strip()
    label_to_key, _key_to_label = attribute_maps(template)
    key = label_to_key.get(label)
    if not key or key not in resolved["base"]:
        return None, [], None
    primary_key = semantic_field_key(template, "actor.stats.primary")
    chosen_primary = str(values.get(primary_key) or "")
    if role == "actor.stats.secondary" and chosen_primary == label:
        compatibility = {
            "state": "conflict",
            "reasons": ["副属性不能与已经选择的主属性相同。"],
        }
        return None, ["与当前主属性冲突。"], compatibility
    stats = template.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    bonus = safe_int(
        stats.get(
            "primary_bonus"
            if role == "actor.stats.primary"
            else "secondary_bonus"
        ),
        7 if role == "actor.stats.primary" else 3,
    )
    before = int(resolved["base"][key])
    after = before + bonus
    preview = {"before": before, "bonus": bonus, "after": after}
    return (
        preview,
        [f"当前：{before}", f"选择后：{after}（+{bonus}）"],
        None,
    )


def project_player_choice_view(
    option: Any,
    *,
    field: Mapping[str, Any] | None = None,
    template: Mapping[str, Any] | None = None,
    values: Mapping[str, Any] | None = None,
    choice_ref: str = "",
    compatibility: Mapping[str, Any] | None = None,
    costs: Sequence[str] | None = None,
    mechanical_preview: Sequence[str] | None = None,
    result_preview: Sequence[str] | None = None,
    value_preview: Mapping[str, Any] | None = None,
    locale: str = "zh-CN",
) -> dict[str, Any]:
    """把一个候选规范化为 PlayerChoiceView。

    ``option`` 支持三种真实形态：

    - ``CandidatePlayerCopy``（``copy.candidate`` 的已清洗 DTO）；
    - ``card_wizard.preset_options`` 的规范化选项 Mapping；
    - 世界包的原始候选 Mapping（含 ``source`` 子映射）。

    内容字段缺失时按 ``copy.candidate`` 既有规则抛出
    :class:`CandidateCopyError`，由调用方决定降级策略。
    """

    if isinstance(option, CandidatePlayerCopy):
        copy: CandidatePlayerCopy = option
        ref = choice_ref or copy.candidate_id or copy.value
        label = copy.label
        summary = copy.summary
        advantages = list(copy.advantages)
        limitations = list(copy.limitations)
        story_hooks = list(copy.story_hooks)
        entity_type = copy.entity_type
    else:
        copy = candidate_player_copy(option, field=field, locale=locale)
        ref = choice_ref or copy.candidate_id or (
            str(option.get("value") or option.get("id") or "")
            if isinstance(option, Mapping)
            else ""
        )
        label = copy.label
        summary = copy.summary
        advantages = list(copy.advantages)
        limitations = list(copy.limitations)
        story_hooks = list(copy.story_hooks)
        entity_type = copy.entity_type

    generated_preview = _profession_mechanical_preview(
        option,
        template=template,
        field=field,
    )
    generated_value, generated_attribute_preview, generated_compatibility = (
        _attribute_value_preview(
            option,
            template=template,
            field=field,
            values=values,
        )
    )
    if generated_compatibility is None:
        source = _option_source(option)
        belief_affinity = source.get("belief_affinity")
        generated_compatibility = (
            belief_affinity.get("compatibility")
            if isinstance(belief_affinity, Mapping)
            else _rule_compatibility(option, values)
        )
    if mechanical_preview is None:
        mechanical_preview = [
            *generated_preview,
            *generated_attribute_preview,
        ]
    if value_preview is None and generated_value is not None:
        value_preview = generated_value
    if compatibility is None and generated_compatibility is not None:
        compatibility = generated_compatibility
    compatibility_view = _compatibility_view(compatibility)
    selectable = compatibility_view["state"] in _SELECTABLE_STATES
    preview = dict(value_preview) if isinstance(value_preview, Mapping) else {}
    value_view: dict[str, Any] | None = None
    if preview:
        before = preview.get("before")
        bonus = preview.get("bonus")
        after = preview.get("after")
        if after is None and before is not None and bonus is not None:
            after = safe_int(before, 0) + safe_int(bonus, 0)
        if after is not None:
            value_view = {
                "before": before,
                "bonus": bonus,
                "after": after,
            }
    return {
        "label": label,
        "entity_type": entity_type or "",
        "summary": summary,
        "advantages": advantages,
        "limitations": limitations,
        "costs": _text_list(costs),
        "mechanical_preview": _text_list(mechanical_preview),
        "compatibility": compatibility_view,
        "value_preview": value_view,
        "result_preview": _text_list(result_preview),
        "story_hooks": story_hooks,
        "selectable": selectable,
    }


def project_player_choice_views(
    options: Sequence[Any],
    *,
    field: Mapping[str, Any] | None = None,
    template: Mapping[str, Any] | None = None,
    values: Mapping[str, Any] | None = None,
    locale: str = "zh-CN",
) -> list[dict[str, Any]]:
    """批量投影；任一候选无法生成合法文案时按 ``copy.candidate`` 规则抛出。"""

    return [
        project_player_choice_view(
            option,
            field=field,
            template=template,
            values=values,
            locale=locale,
        )
        for option in options
    ]


__all__ = [
    "CHOICE_COMPATIBILITY_LABELS",
    "project_player_choice_view",
    "project_player_choice_views",
]
