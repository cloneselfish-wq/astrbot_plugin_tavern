"""RC9 world and session narrative-style contract.

The style axis changes expression only.  It cannot change facts, rulings,
permissions, player agency, the output schema, or safety boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

NARRATIVE_STYLE_SCHEMA = "tavern-narrative-style/1.0.0-rc10"
DEFAULT_NARRATIVE_STYLE = "balanced"
MAX_CUSTOM_VISIBLE_CHARS = 600
MAX_COMPILED_STYLE_TOKENS = 256


@dataclass(frozen=True, slots=True)
class NarrativeStylePreset:
    preset_id: str
    label: str
    dialogue_weight: float
    description_weight: float
    public_summary: str
    directive: str

    def public_view(self) -> dict[str, Any]:
        return {
            "preset_id": self.preset_id,
            "label": self.label,
            "dialogue_weight": self.dialogue_weight,
            "description_weight": self.description_weight,
            "public_summary": self.public_summary,
        }


NARRATIVE_STYLE_PRESETS: Mapping[str, NarrativeStylePreset] = {
    "dialogue_high": NarrativeStylePreset(
        "dialogue_high", "多对白", 0.80, 0.20,
        "让自然对白主要推进现场，同时保留必要动作、环境与结果。",
        "自然对白约占可见故事文字八成；没有合适发言者时不得硬造对白。",
    ),
    "dialogue_soft": NarrativeStylePreset(
        "dialogue_soft", "偏对白", 0.65, 0.35,
        "对话略多于描写，以动作和神态承接人物交流。",
        "优先用自然交流推进关系与信息，描写负责承接动作、空间和后果。",
    ),
    "balanced": NarrativeStylePreset(
        "balanced", "均衡", 0.50, 0.50,
        "对白与描写随场景自然平衡。",
        "让对白、动作、空间、感官与后果按当前场景需要自然平衡。",
    ),
    "description_soft": NarrativeStylePreset(
        "description_soft", "偏描写", 0.35, 0.65,
        "细节与行动因果略多，对话仍承担信息和关系变化。",
        "优先写清空间、动作因果和可见反应，同时保留必要人物交流。",
    ),
    "description_high": NarrativeStylePreset(
        "description_high", "多描写", 0.20, 0.80,
        "充分描绘空间、感官、动作与后果，仍保留自然交流。",
        "描写约占可见故事文字八成；不得重复修辞，也不得删除必要对白。",
    ),
}

_FORBIDDEN_CUSTOM_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"忽略.{0,12}(系统|规则|指令)",
        r"(泄露|输出|显示).{0,12}(提示词|system prompt|schema|密钥)",
        r"替.{0,8}玩家.{0,8}(决定|行动|说话|思考)",
        r"绕过.{0,12}(权限|安全|裁定|同意)",
        r"<\s*(script|iframe|style)\b",
    )
)


def normalize_style_id(value: object) -> str:
    style_id = str(value or "").strip().lower()
    return style_id if style_id in NARRATIVE_STYLE_PRESETS else DEFAULT_NARRATIVE_STYLE


def validate_custom_expectation(value: object) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) > MAX_CUSTOM_VISIBLE_CHARS:
        raise ValueError(f"自定义文风最多 {MAX_CUSTOM_VISIBLE_CHARS} 个可见字符")
    if any(pattern.search(text) for pattern in _FORBIDDEN_CUSTOM_PATTERNS):
        raise ValueError("自定义文风试图覆盖系统规则、权限或玩家决定")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError("自定义文风包含不支持的控制字符")
    return text


def normalize_world_narrative_style(value: object) -> dict[str, Any]:
    """Return the author-declared world style in the canonical RC9 shape.

    World packages own the default voice while the runtime owns the five
    stable controls.  Keeping those concerns separate lets an installed world
    supply useful editor text without allowing it to redefine permissions or
    the meaning of the session-level preset IDs.
    """

    source = dict(value) if isinstance(value, Mapping) else {}
    default_preset_id = normalize_style_id(
        source.get("default_preset_id") or source.get("default_preset")
    )
    world_voice = validate_custom_expectation(source.get("world_voice") or "")
    default_expectation = validate_custom_expectation(
        source.get("default_expectation") or world_voice
    )
    canonical = {
        "schema": NARRATIVE_STYLE_SCHEMA,
        "version": "1.0.0-rc10",
        "default_preset_id": default_preset_id,
        "default_expectation": default_expectation,
        "world_voice": world_voice or default_expectation,
        "presets": [
            preset.public_view()
            for preset in NARRATIVE_STYLE_PRESETS.values()
        ],
    }
    canonical["sha256"] = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return canonical


def compile_style_directive(
    preset_id: object,
    custom_expectation: object = "",
    *,
    world_voice: str = "",
) -> dict[str, Any]:
    selected = NARRATIVE_STYLE_PRESETS[normalize_style_id(preset_id)]
    custom = validate_custom_expectation(custom_expectation)
    parts = [
        "叙事文风只改变表达侧重，不改变事实、裁定、权限、安全边界或玩家决定。",
        str(world_voice or "").strip(),
        selected.directive,
        f"会话文风期望：{custom}" if custom else "",
    ]
    text = "\n".join(part for part in parts if part)
    # Conservative no-tokenizer bound: at most three visible characters/token.
    max_chars = MAX_COMPILED_STYLE_TOKENS * 3
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return {
        "schema": NARRATIVE_STYLE_SCHEMA,
        "preset_id": selected.preset_id,
        "directive": text,
        "estimated_tokens": (len(text) + 2) // 3,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "custom_truncated": bool(custom and custom not in text),
    }


def narrative_style_view(
    preset_id: object,
    *,
    custom_expectation: str = "",
    revision: int = 0,
    updated_at: str = "",
    source_world_style_sha: str = "",
    can_manage: bool = False,
    include_private: bool = False,
    replayed: bool = False,
    world_style: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = NARRATIVE_STYLE_PRESETS[normalize_style_id(preset_id)]
    author_style = normalize_world_narrative_style(world_style)
    return {
        **selected.public_view(),
        "has_custom_expectation": bool(custom_expectation),
        "custom_expectation": custom_expectation if include_private and can_manage else "",
        "revision": max(0, int(revision or 0)),
        "updated_at": str(updated_at or ""),
        "source_world_style_sha": str(source_world_style_sha or ""),
        "world_style_sha": str(author_style.get("sha256") or ""),
        "world_default_preset_id": str(
            author_style.get("default_preset_id") or DEFAULT_NARRATIVE_STYLE
        ),
        "world_default_expectation": str(
            author_style.get("default_expectation") or ""
        ),
        "world_voice": str(author_style.get("world_voice") or ""),
        "can_manage": bool(can_manage),
        "applies_to": "next_generation",
        "options": [item.public_view() for item in NARRATIVE_STYLE_PRESETS.values()],
        "replayed": bool(replayed),
    }


def narrative_style_instruction_for_session(session: Mapping[str, Any] | None) -> str:
    source = dict(session) if isinstance(session, Mapping) else {}
    state = source.get("narrative_style")
    state = dict(state) if isinstance(state, Mapping) else {}
    compiled = compile_style_directive(
        state.get("preset_id") or DEFAULT_NARRATIVE_STYLE,
        state.get("custom_expectation") or "",
        world_voice=str(state.get("world_voice") or ""),
    )
    return "\n<session_narrative_style>\n" + compiled["directive"] + "\n</session_narrative_style>"


__all__ = [
    "DEFAULT_NARRATIVE_STYLE",
    "MAX_COMPILED_STYLE_TOKENS",
    "MAX_CUSTOM_VISIBLE_CHARS",
    "NARRATIVE_STYLE_PRESETS",
    "NARRATIVE_STYLE_SCHEMA",
    "compile_style_directive",
    "narrative_style_view",
    "narrative_style_instruction_for_session",
    "normalize_style_id",
    "normalize_world_narrative_style",
    "validate_custom_expectation",
]
