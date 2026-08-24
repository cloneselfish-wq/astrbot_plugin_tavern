from __future__ import annotations


import hashlib


import json


import re


import stat


import zipfile


from collections.abc import Mapping, Sequence


from copy import deepcopy


from dataclasses import replace


from pathlib import Path, PurePosixPath


from typing import Any


from ..field_accounting import (
    FieldAccountingError,
    field_account,
    validate_field_count_declarations,
)


from ..interaction_prompt import INTERACTION_MODES


from ..messaging.registry import (
    MessageSectionDefinition,
    get_message,
)


from ..opening_contract import opening_contract_issues


from ..world_contract import (
    FEATURE_VERSIONS,
    WORLD_SCHEMA_VERSION,
    validate_world_contract,
)


from ..presets import (
    PresetLibraryContractError,
    normalize_preset_libraries,
)


from ..twp.runtime import initialize_runtime


from ..rules_digest import (
    RULES_DIGEST_PATH,
    RULES_DIGEST_SCHEMA,
    structural_issues,
)


from .commands import command_catalog


from .constants import (
    TWP_ARTIFACT_SCHEMA,
    TWP_COMPILER_ABI,
    TWP_COMPILED_WORLD_SCHEMA,
    TWP_CORE_VERSION,
    TWP_FORMAT,
    TWP_MATURITY,
    TWP_PACKAGE_FORMAT,
    TWP_RUNTIME_SCHEMA,
    TWP_VERSION,
)


from .errors import TwpPackageError, TwpValidationIssue


from .manifest import parse_module_descriptor, validate_manifest


from .localization import compile_localization


from .extensions import (
    compile_summary_metrics,
    validate_ai_companions,
    validate_extensions,
)


from .models import EntityRef, Problem, SourceLocation, WorldArtifact


MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


MAX_UNCOMPRESSED_BYTES = 192 * 1024 * 1024


MAX_MEMBER_BYTES = 32 * 1024 * 1024


MAX_FILES = 1024


SAFE_SUFFIXES = {
    ".json",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
    ".mp3",
    ".ogg",
    ".wav",
    ".pdf",
}


REFERENCE_TYPES = frozenset(
    {
        "act",
        "region",
        "scene",
        "quest",
        "objective",
        "npc",
        "faction",
        "clock",
        "fact",
        "recipe",
        "item",
        "progression_track",
        "milestone",
        "handout",
        "map",
        "ending",
        "fate_state",
        "terminal",
        "capability",
        "resource",
        "runtime_effect",
        "event",
        "command",
        "extension_entity",
        "actor",
        "relationship",
        "currency",
        "shop",
    }
)


SCENE_CONTRACT_FIELDS = (
    "chapter_id",
    "entry_conditions",
    "objectives",
    "required_clues",
    "optional_clues",
    "npc_refs",
    "shop_refs",
    "exit_conditions",
    "recommended_transitions",
    "skippable_processes",
    "stall_policy",
)


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


COMMON_DECLARED_EXPORTS = ["inspect_twp_archive"]



__all__ = [name for name in globals() if not name.startswith('__')]

