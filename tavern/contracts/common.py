"""共享的玩家外显安全工具（纯函数）。

所有视图模块都经由这里清洗文本、判断泄漏标记与规范化数值，保证
BOT 与 WebUI 两条渲染链面对同一套安全边界。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..copy.candidate import player_copy_text


# D1 玩家可见命令前缀（14_BOT_MESSAGE_DESIGN_SYSTEM.md：模板统一由
# command_display_prefix 填充，不允许一条消息内混用两个前缀）。
DEFAULT_COMMAND_PREFIX = "/团"


# 玩家正文中禁止出现的内部技术标记。渲染器与测试都使用同一清单；
# 命中即视为“内部信息泄漏”，渲染层应拒绝输出或降级为错误文案。
LEAKAGE_MARKERS = (
    "UMO",
    "unified_msg_origin",
    "unified_origin",
    "revision",
    "stable_key",
    "stable_id",
    "compiler_abi",
    "source_artifact_hash",
    "meta_json",
    "rules_json",
    "state_json",
    "twp-",
    "preset_sets.",
    "text_id",
    "source_refs",
)


_STABLE_REF_RE = re.compile(r"\b[a-z_][a-z0-9_]*:[a-z0-9_\-]+\b")
_REVISION_RE = re.compile(r"\br\d+\b")


def safe_int(value: Any, default: int = 0) -> int:
    """规范化整数；数组、对象与非法文本一律按 ``default`` 处理，绝不产生 NaN。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value and abs(value) < 1 << 53 else default
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return default


def clean_label(value: Any, fallback: str = "") -> str:
    """清洗单条玩家可见文本；容器与内部标记内容返回 ``fallback``。"""

    text = player_copy_text(value)
    if not text:
        return fallback
    return text


def _contains_internal_reference(text: str) -> bool:
    """是否包含形如 ``quest:last-train`` 的稳定引用。"""

    return bool(_STABLE_REF_RE.search(text))


def contains_leakage(text: Any) -> bool:
    """判断文本是否包含任何玩家不可见的技术标记。"""

    value = str(text or "")
    lowered = value.casefold()
    if "{" in value or "}" in value:
        return True
    if _REVISION_RE.search(value):
        return True
    if _contains_internal_reference(value):
        return True
    return any(marker.casefold() in lowered for marker in LEAKAGE_MARKERS)


def _text_list(value: Any) -> list[str]:
    """把任意输入规范为安全文本列表；对象与标量直接忽略。"""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [clean_label(item) for item in value if clean_label(item)]
    return []


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "LEAKAGE_MARKERS",
    "clean_label",
    "contains_leakage",
    "safe_int",
]
