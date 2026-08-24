from __future__ import annotations


import hashlib


import json


from collections.abc import Mapping, Sequence


from copy import deepcopy


from dataclasses import asdict, dataclass


import re


from typing import Any


from ..candidates import (
    candidate_matches,
    candidate_rule_matches,
    dependent_fields,
)


PRESET_REFS_KEY = "_preset_refs"


LAST_MESSAGE_KEY = "_last_card_message"


WIZARD_DELIVERY_KEY = "_wizard_delivery"


CANDIDATE_SNAPSHOTS_KEY = "_candidate_snapshots"


NAV_NEXT = {"下一页", "下页", "next"}


NAV_PREVIOUS = {"上一页", "上页", "prev", "previous"}


AUTO_FILL_PHASES = frozenset(
    {"pre_archetype", "post_archetype", "resume_repair"}
)


FREE_TEXT_FIELDS = frozenset({"name", "code", "appearance"})


COMMON_DECLARED_EXPORTS = [
    "AUTO_FILL_PHASES",
    "LAST_MESSAGE_KEY",
    "NAV_NEXT",
    "NAV_PREVIOUS",
    "WizardStep",
    "apply_archetype_pack",
    "apply_archetype_pack_atomic",
    "archetype_packs",
    "auto_fill_for_phase",
    "FREE_TEXT_FIELDS",
    "preset_only_guard",
    "revalidate_dependent_selections",
    "selected_preset_ids",
    "auto_fill_remaining",
    "creation_modes",
    "creation_mode_plan",
    "current_creation_mode",
    "mode_auto_filled_keys",
    "next_player_fillable_step",
    "next_wizard_step",
    "PRESET_REFS_KEY",
    "choose_option",
    "choose_options",
    "candidate_input_fingerprint",
    "CANDIDATE_SNAPSHOTS_KEY",
    "clear_field_and_dependents",
    "field_visible",
    "preset_options",
    "preset_source_exists",
    "resolve_current_wizard_step",
    "store_preset_snapshot",
    "store_preset_snapshots",
    "wizard_completion_state",
]



__all__ = [name for name in globals() if not name.startswith('__')]

