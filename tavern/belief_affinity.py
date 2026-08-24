from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


WEIGHTS = {
    "specialization": 8,
    "profession": 6,
    "species_culture": 5,
    "species": 4,
    "hometown": 4,
    "origin_region": 3,
    "social_identity": 3,
    "faction_affiliation": 3,
}

AFFINITY_KEYS = {
    "specialization": "specialization_refs",
    "profession": "profession_refs",
    "species_culture": "culture_refs",
    "species": "species_refs",
    "hometown": "hometown_refs",
    "origin_region": "origin_refs",
    "social_identity": "social_identity_refs",
    "faction_affiliation": "faction_refs",
}

FIELD_LABELS = {
    "specialization": "专精训练",
    "profession": "职业经历",
    "species_culture": "文化背景",
    "species": "族群经历",
    "hometown": "故乡",
    "origin_region": "出身地",
    "social_identity": "社会身份",
    "faction_affiliation": "阵营经历",
}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _profile_ref(profile: Mapping[str, Any], field: str) -> str:
    value = profile.get(field)
    if isinstance(value, Mapping):
        return str(value.get("id") or value.get("value") or "").strip()
    refs = profile.get("_preset_refs")
    ref = refs.get(field) if isinstance(refs, Mapping) else None
    if isinstance(ref, Mapping):
        stable = str(ref.get("id") or "").strip()
        if stable:
            return stable
    return str(value or "").strip()


def _rule_matches(rule: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    requirements = [
        item
        for item in _sequence(rule.get("requires_all"))
        if isinstance(item, Mapping)
    ]
    if not requirements:
        return False
    return all(
        _profile_ref(profile, str(item.get("field") or ""))
        in {str(value) for value in _sequence(item.get("values"))}
        for item in requirements
    )


def order_belief_candidates(
    beliefs: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if len(beliefs) != 10:
        raise ValueError("核心信念作者源必须恰好包含 10 项")
    rows: list[dict[str, Any]] = []
    for author_order, belief in enumerate(beliefs):
        item = dict(belief)
        affinity = item.get("affinity")
        if not isinstance(affinity, Mapping):
            raise ValueError(f"{item.get('id') or 'belief'} 缺少 affinity")
        dimension_hits: list[tuple[int, str, str]] = []
        score = 0
        for field, weight in WEIGHTS.items():
            selected = _profile_ref(profile, field)
            allowed = {
                str(value)
                for value in _sequence(affinity.get(AFFINITY_KEYS[field]))
            }
            if selected and selected in allowed:
                score += weight
                dimension_hits.append((weight, field, selected))
        matched_rules = [
            rule
            for rule in _sequence(affinity.get("synergy_rules"))
            if isinstance(rule, Mapping) and _rule_matches(rule, profile)
        ]
        synergy_count = len(matched_rules)
        score += synergy_count * 10
        if matched_rules:
            reasons = [
                str(rule.get("why") or "").strip()
                for rule in matched_rules
                if str(rule.get("why") or "").strip()
            ]
            state = "recommended"
            group = "与你的经历强相关"
        elif score >= 8:
            top = sorted(dimension_hits, reverse=True)[:2]
            reasons = [
                "与你的"
                + "和".join(FIELD_LABELS[field] for _, field, _ in top)
                + "相呼应。"
            ]
            state = "recommended"
            group = "与你的经历有关联"
        else:
            reasons = [str(affinity.get("counterpoint") or "").strip()]
            state = "allowed"
            group = "反典型但可选择"
        item["belief_affinity"] = {
            "score": score,
            "synergy_count": synergy_count,
            "group": group,
            "compatibility": {
                "state": state,
                "reasons": [reason for reason in reasons if reason],
            },
        }
        rows.append(item)
    return sorted(
        rows,
        key=lambda item: (
            -int(item["belief_affinity"]["synergy_count"]),
            -int(item["belief_affinity"]["score"]),
            next(
                index
                for index, original in enumerate(beliefs)
                if str(original.get("id")) == str(item.get("id"))
            ),
            str(item.get("id") or ""),
        ),
    )


__all__ = ["AFFINITY_KEYS", "WEIGHTS", "order_belief_candidates"]
