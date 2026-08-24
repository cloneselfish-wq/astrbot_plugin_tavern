"""统一模板体检服务。

世界包导入、WebUI 保存、群聊开始建卡与自动测试调用同一体检入口。输出按
「阻断错误 / 警告 / 建议」分组，并给出覆盖矩阵、固定数量、引用完整性、
原型包展开完整性与私密泄露风险。
"""


from __future__ import annotations


import json


import re


from collections.abc import Mapping, Sequence


from typing import Any


from ...candidates import candidate_rule_apply_signature


from ...card_wizard import field_visible, preset_options


from ...condition_engine import validate_condition_tree


from ...entity_registry import EntityRegistry, module_value


from ...lifecycle import card_template


from ...operation_engine import OperationEngine


from ...presets import normalize_preset_libraries


FIXED_MULTI_TARGETS = {
    "abilities": 4,
    "specialties": 3,
    "weakness": 2,
    "languages": 2,
    "knowledge": 2,
}


FIXED_MULTI_MIN_CANDIDATES = {
    "abilities": 12,
    "specialties": 15,
    "weakness": 12,
    "languages": 8,
    "knowledge": 10,
}


FREE_FIELDS = frozenset({"name", "code", "appearance"})


MIN_SPECIALIZATIONS = 3


MIN_WEAPONS = 6


MIN_ARMORS = 5


OWNED_PROFESSIONS = frozenset(
    {
        "engineer",
        "rune_guard",
        "beastmaster",
        "astrologer",
        "medium",
        "sea_oath",
        "blood_hunter",
        "shadow_dancer",
    }
)


TENDENCY_DIMENSIONS = frozenset(
    {"risk", "cooperation", "mercy", "curiosity", "authority", "planning"}
)


NUMBERED_LABEL_PATTERN = re.compile(
    r"(?:专精|路径|路线)\s*[0-9一二三四五六七八九十]+$"
)


COMMON_DECLARED_EXPORTS = [
    "FIXED_MULTI_MIN_CANDIDATES",
    "FIXED_MULTI_TARGETS",
    "FREE_FIELDS",
    "MIN_ARMORS",
    "MIN_SPECIALIZATIONS",
    "MIN_WEAPONS",
    "check_preset_libraries",
    "check_template",
    "coverage_matrix",
    "preset_library_catalog",
]




__all__ = [name for name in globals() if not name.startswith('__')]

