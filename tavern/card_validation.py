"""Authoritative character-card field validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .security import clean_text


_NAME_ROLES = frozenset({"actor.identity.name", "actor.identity.alias"})
_TEXT_TYPES = frozenset({"text", "textarea"})
_SINGLE_PRESET_TYPES = frozenset({"select", "preset_select"})


def _option_identities(option: Mapping[str, Any]) -> set[str]:
    return {
        str(option.get(key) or "").casefold()
        for key in ("id", "value", "label", "name")
        if str(option.get(key) or "").strip()
    }


def _resolve_preset(
    raw: object,
    options: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    matches = [
        option
        for option in options
        if value.casefold() in _option_identities(option)
    ]
    if len(matches) != 1:
        raise ValueError(f"{label}不是当前可选项，系统已保留其他资料")
    return str(matches[0].get("id") or value)


def validate_card_field(
    definition: Mapping[str, Any],
    raw: object,
    options: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Validate and normalize one field according to its declared type.

    Text length applies only to player prose. Stable preset references are
    resolved against the current candidate set and never measured as prose.
    """

    field_type = str(definition.get("type") or "text").strip().lower()
    label = str(definition.get("label") or definition.get("key") or "该字段")
    required = bool(definition.get("required"))

    if field_type in _TEXT_TYPES:
        maximum = int(definition.get("max_chars", 0) or 0)
        if maximum <= 0:
            raise ValueError(f"{label}缺少有效的字数上限")
        text = clean_text(raw, max_chars=maximum)
        if str(definition.get("semantic_role") or "") in _NAME_ROLES and any(
            character.isspace() for character in str(raw or "")
        ):
            raise ValueError(f"{label}不能包含空格、全角空格、换行或制表符")
        if required and not text:
            raise ValueError(f"{label}不能为空")
        return text

    if field_type in _SINGLE_PRESET_TYPES:
        value = _resolve_preset(raw, options, label=label)
        if required and not value:
            raise ValueError(f"{label}不能为空")
        return value

    if field_type == "multi_select":
        values = (
            list(raw)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
            else []
        )
        resolved: list[str] = []
        for value in values:
            preset_id = _resolve_preset(value, options, label=label)
            if preset_id and preset_id not in resolved:
                resolved.append(preset_id)
        minimum = int(definition.get("min_choices", 0) or 0)
        maximum = int(definition.get("max_choices", 100) or 100)
        if not minimum <= len(resolved) <= maximum:
            expected = (
                f"{minimum} 项"
                if minimum == maximum
                else f"{minimum}—{maximum} 项"
            )
            raise ValueError(f"{label}必须选择 {expected}")
        return resolved

    if field_type == "integer":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须填写整数") from exc
        minimum = int(definition.get("minimum", -100))
        maximum = int(definition.get("maximum", 100))
        if not minimum <= value <= maximum:
            raise ValueError(f"{label}必须在 {minimum}—{maximum} 之间")
        return value

    raise ValueError(f"{label}使用了不支持的字段类型，无法继续建卡")


__all__ = ["validate_card_field"]
