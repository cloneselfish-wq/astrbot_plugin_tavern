"""Generic candidate constraints for character-card authoring and runtime.

The module deliberately knows nothing about a concrete world's fields or
candidate IDs.  World packages declare all relationships through stable IDs;
labels and aliases remain presentation/input conveniences and never
participate in constraint evaluation.
"""


from __future__ import annotations


from collections.abc import Mapping, Sequence


import json


from typing import Any


from ..entity_registry import split_ref


PRESET_REFS_KEY = "_preset_refs"


CONSTRAINT_GROUPS = ("requires_all", "requires_any", "excludes")


UNRESOLVED_POLICIES = frozenset({"hide", "show", "generic_only"})


CANDIDATE_RULE_KEYS = frozenset(
    {
        "eligibility",
        "conflicts",
        "recommendations",
        "unlocks",
        "grants",
        "resource_modifiers",
        "ability_pool_add",
        "ability_pool_remove",
        "runtime_effect_refs",
        "visibility",
    }
)


CANDIDATE_VISIBILITIES = frozenset({"public", "private"})


GRANT_POLICIES = frozenset({"ignore", "refresh", "stack", "modify", "transition"})


RESOURCE_MODIFIER_OPS = frozenset({"set", "add", "subtract", "cap", "floor"})


UNLOCK_KINDS = frozenset(
    {"capability", "ability_track", "resource", "runtime_effect"}
)


COMMON_DECLARED_EXPORTS = [
    "CONSTRAINT_GROUPS",
    "UNRESOLVED_POLICIES",
    "CANDIDATE_RULE_KEYS",
    "CANDIDATE_VISIBILITIES",
    "GRANT_POLICIES",
    "RESOURCE_MODIFIER_OPS",
    "UNLOCK_KINDS",
    "candidate_match_details",
    "candidate_matches",
    "candidate_rule_apply_signature",
    "candidate_rule_field_refs",
    "candidate_rule_matches",
    "candidate_rule_status",
    "rank_candidates",
    "candidate_rule_view",
    "candidate_visibility",
    "constraint_field_refs",
    "dependency_graph",
    "dependent_fields",
    "normalize_candidate_rules",
    "normalize_candidate_constraints",
    "raw_candidate_options",
    "reachable_candidates",
    "selected_ids",
    "validate_constraint_graph",
]



__all__ = [name for name in globals() if not name.startswith('__')]

