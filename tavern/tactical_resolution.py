"""Authoritative check-context projection helpers for tactical resolution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .tactical_support import _sequence

def _bounded_check_modifier(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not -100 <= value <= 100:
        raise ValueError("战术检定 modifier 必须是 -100..100 的整数")
    return value

def _action_check_context(
    state: Mapping[str, Any],
    actor: Mapping[str, Any],
    action_kind: str,
    choice_kind: str,
    choice: Mapping[str, Any],
    choice_state: Mapping[str, Any],
) -> dict[str, Any]:
    source = state.get("action_checks") or {}
    if isinstance(source, Mapping):
        check = source.get(action_kind)
    else:
        matches = [
            raw for raw in _sequence(source)
            if isinstance(raw, Mapping)
            and str(raw.get("action_kind") or "") == action_kind
        ]
        if len(matches) > 1:
            raise ValueError("冻结 action_checks 含重复 action_kind")
        check = matches[0] if matches else None
    if isinstance(check, Mapping):
        stat_ref = str(check.get("stat_ref") or "").strip()
        if not stat_ref:
            raise ValueError("生产战术检定缺少冻结 stat_ref")
        stat_label = str(check.get("label") or "检定").strip()[:120]
        context = actor.get("check_context") or {}
        if not isinstance(context, Mapping):
            raise ValueError("行动者缺少冻结 check_context")
        modifiers = context.get("stat_modifiers") or {}
        if not isinstance(modifiers, Mapping):
            raise ValueError("行动者冻结 stat_modifiers 无效")
        raw_modifier = modifiers.get(stat_ref, 0)
        modifier = _bounded_check_modifier(raw_modifier)

        definition_modifier = 0
        if choice_kind == "capability":
            definition = choice.get("definition") or {}
            if not isinstance(definition, Mapping):
                raise ValueError("所选能力缺少冻结作者定义")
            raw_definition_modifier = definition.get("check_modifier", 0)
            if isinstance(raw_definition_modifier, Mapping):
                raw_definition_modifier = raw_definition_modifier.get(stat_ref, 0)
            definition_modifier = _bounded_check_modifier(raw_definition_modifier)
        elif choice_kind == "item" and "check_modifier" in choice_state:
            definition_modifier = _bounded_check_modifier(
                choice_state.get("check_modifier")
            )

        advantage_sources: list[str] = []
        for raw in _sequence(context.get("advantage_sources")):
            if isinstance(raw, Mapping):
                if not bool(raw.get("active", True)):
                    continue
                label = str(raw.get("label") or raw.get("name") or "已验证优势")
            else:
                label = str(raw or "")
            label = label.strip()[:120]
            if label:
                advantage_sources.append(label)

        disadvantage_sources: list[str] = []
        for raw in _sequence(context.get("statuses")):
            if not isinstance(raw, Mapping) or not bool(raw.get("active", True)):
                continue
            status = str(raw.get("status") or raw.get("state") or "active").strip()
            if status in {"inactive", "ended", "expired", "removed"}:
                continue
            affects = {
                str(value).strip()
                for value in _sequence(raw.get("affects"))
                if str(value).strip()
            }
            if stat_ref not in affects:
                continue
            label = str(raw.get("label") or raw.get("name") or "有效状态").strip()[:120]
            if label:
                disadvantage_sources.append(label)
        return {
            "stat_ref": stat_ref,
            "stat_label": stat_label,
            "modifier": modifier + definition_modifier,
            "advantage_sources": advantage_sources[:8],
            "disadvantage_sources": disadvantage_sources[:8],
            "legacy": False,
        }

    return {
        "stat_ref": "",
        "stat_label": "",
        "modifier": _bounded_check_modifier(int(actor.get("check_modifier") or 0))
        + _bounded_check_modifier(int(choice_state.get("check_modifier") or 0)),
        "advantage_sources": [
            str(item)[:120]
            for item in actor.get("advantages") or ()
            if str(item).strip()
        ][:8],
        "disadvantage_sources": [
            str(item)[:120]
            for item in actor.get("disadvantages") or ()
            if str(item).strip()
        ][:8],
        "legacy": True,
    }

