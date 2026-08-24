from .common import *
from .normalization import *
from .dependencies import *
from .rules import *

def candidate_rule_field_refs(
    raw: Any,
    *,
    include_recommendations: bool = True,
) -> set[str]:
    """Return fields referenced by candidate rules.

    Recommendations remain reference-validated, but callers building hard
    dependency graphs must exclude them: they never hide a candidate, clear a
    downstream choice, or form a blocking cycle.
    """

    rules = normalize_candidate_rules(_rule_keys_view(raw))
    groups = ["eligibility", "conflicts"]
    if include_recommendations:
        groups.append("recommendations")
    return {
        str(field)
        for group in groups
        for field in rules[group]
    }


def candidate_visibility(raw: Any) -> str:
    rules = normalize_candidate_rules(_rule_keys_view(raw))
    return str(rules["visibility"])


def candidate_rule_status(
    raw: Any,
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a candidate's D1 rules against current draft values.

    Semantics:
    - eligibility: every declared field must be selected AND match at least
      one of its values; an unselected dependency field blocks the candidate
      (``eligibility_unresolved``) so staging supplements cannot be confirmed
      before their dependencies exist.
    - conflicts: only *selected* values count; unresolved fields never
      conflict.
    - recommendations: non-blocking; matched selections only add a
      recommendation flag.
    """

    rules = normalize_candidate_rules(_rule_keys_view(raw))
    reasons: list[str] = []
    unresolved: list[str] = []
    conflict_fields: list[str] = []

    for field, expected in rules["eligibility"].items():
        actual = set(selected_ids(values, field))
        if not actual:
            unresolved.append(field)
            continue
        if not actual & set(expected):
            reasons.append("eligibility_unmet")
    if unresolved:
        reasons.append("eligibility_unresolved")
    for field, expected in rules["conflicts"].items():
        actual = set(selected_ids(values, field))
        if actual & set(expected):
            reasons.append("conflict")
            conflict_fields.append(field)
    recommended = any(
        set(selected_ids(values, field)) & set(expected)
        for field, expected in rules["recommendations"].items()
    )
    eligible = not reasons
    conflicted = "conflict" in reasons
    return {
        "eligible": eligible,
        "conflicted": conflicted,
        "recommended": bool(recommended),
        "matches": eligible and not conflicted,
        "reasons": sorted(set(reasons)),
        "unresolved_fields": sorted(set(unresolved)),
        "conflict_fields": sorted(set(conflict_fields)),
    }


def rank_candidates(
    field: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    values: Mapping[str, Any],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Filter and deterministically rank author-declared candidates.

    Eligibility and conflicts remain hard rules.  Affinity is deliberately
    interpreted as the final author weight: the runtime never multiplies a
    dimension by a hidden coefficient.  Ranking metadata is private and is
    consumed by the candidate projection layer; it must not be persisted in a
    public character profile.
    """

    ranked: list[dict[str, Any]] = []
    selected_refs = {
        ref
        for key in values
        if not str(key).startswith("_")
        for ref in selected_ids(values, str(key))
    }
    for source_order, raw in enumerate(candidates):
        candidate = dict(raw)
        hard_rules = {
            key: candidate[key]
            for key in ("eligibility", "conflicts", "recommendations")
            if key in candidate
        }
        status = candidate_rule_status(hard_rules, values)
        if not status["matches"]:
            continue
        score = 0
        matched_refs: list[str] = []
        affinity = candidate.get("affinity")
        if isinstance(affinity, Mapping):
            for dimension, weights in affinity.items():
                if not isinstance(weights, Mapping):
                    continue
                dimension_refs = (
                    selected_refs
                    if str(dimension) == "anti"
                    else set(selected_ids(values, str(dimension)))
                )
                for ref, raw_weight in weights.items():
                    stable_ref = str(ref)
                    if stable_ref not in dimension_refs:
                        continue
                    try:
                        score += int(raw_weight)
                    except (TypeError, ValueError):
                        continue
                    matched_refs.append(stable_ref)
        reasons_by_ref = candidate.get("recommendation_reasons")
        reasons_by_ref = (
            reasons_by_ref if isinstance(reasons_by_ref, Mapping) else {}
        )
        reasons: list[str] = []
        for ref in matched_refs:
            reason = str(reasons_by_ref.get(ref) or "").strip()
            if reason and reason not in reasons:
                reasons.append(reason)
            if len(reasons) == 2:
                break
        candidate["_candidate_score"] = score
        candidate["_candidate_recommended"] = score > 0
        candidate["_candidate_recommendation_reasons"] = reasons
        candidate["_candidate_source_order"] = source_order
        ranked.append(candidate)

    def identity(item: Mapping[str, Any]) -> str:
        return str(
            item.get("id")
            or item.get("key")
            or item.get("value")
            or item.get("name")
            or item.get("label")
            or ""
        )

    ordered = sorted(
        ranked,
        key=lambda item: (
            -int(item.get("_candidate_score") or 0),
            int(item.get("_candidate_source_order") or 0),
            identity(item),
        ),
    )
    maximum = len(ordered) if limit is None else max(0, int(limit))
    if maximum >= len(ordered):
        return ordered

    # Give every declared diversity family one highest-scoring recommended
    # representative first.  Tag order follows author source order and is
    # therefore deterministic across processes.
    tags: list[str] = []
    for item in ranked:
        raw_tags = item.get("diversity_tags")
        if not isinstance(raw_tags, Sequence) or isinstance(raw_tags, (str, bytes)):
            continue
        for raw_tag in raw_tags:
            tag = str(raw_tag).strip()
            if tag and tag not in tags:
                tags.append(tag)
    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()
    for tag in tags:
        candidate = next(
            (
                item
                for item in ordered
                if int(item.get("_candidate_score") or 0) > 0
                and tag in [str(value) for value in item.get("diversity_tags") or []]
                and identity(item) not in chosen_ids
            ),
            None,
        )
        if candidate is not None:
            chosen.append(candidate)
            chosen_ids.add(identity(candidate))
            if len(chosen) == maximum:
                return chosen
    for item in ordered:
        if identity(item) in chosen_ids:
            continue
        chosen.append(item)
        chosen_ids.add(identity(item))
        if len(chosen) == maximum:
            break
    return chosen


def candidate_rule_matches(raw: Any, values: Mapping[str, Any]) -> bool:
    return bool(candidate_rule_status(raw, values)["matches"])


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        items = [_canonicalize(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    return value


def candidate_rule_apply_signature(raw: Any) -> str:
    """Stable fingerprint of a candidate's *effects* (excluding presentation).

    Two candidates with the same effects produce the same signature, which
    lets the runtime reject duplicate application of identical effects.
    Presentation-only fields (labels) never participate.
    """

    rules = normalize_candidate_rules(_rule_keys_view(raw))
    effects = {
        "unlocks": [
            {"ref": item["ref"], "kind": item["kind"]}
            for item in rules["unlocks"]
        ],
        "grants": [
            {
                "ref": item["ref"],
                "kind": item["kind"],
                "grant_ref": item["grant_ref"],
                "policy": item["policy"],
                "when": item["when"],
            }
            for item in rules["grants"]
        ],
        "resource_modifiers": [
            {
                "resource_ref": item["resource_ref"],
                "op": item["op"],
                "value": item["value"],
            }
            for item in rules["resource_modifiers"]
        ],
        "ability_pool_add": rules["ability_pool_add"],
        "ability_pool_remove": rules["ability_pool_remove"],
        "runtime_effect_refs": rules["runtime_effect_refs"],
    }
    return json.dumps(
        _canonicalize(effects),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [name for name in globals() if not name.startswith('__')]

