"""Session-scoped RC8 NarrativeDocument modes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

@dataclass(frozen=True, slots=True)
class NarrativeMode:
    mode: str
    label: str
    minimum: int
    maximum: int
    description: str
    instruction: str

    def bounds(self, kind: str = "turn") -> tuple[int, int]:
        del kind
        return self.minimum, self.maximum

    def public_view(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "label": self.label,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": self.description,
        }


NARRATIVE_MODES: Mapping[str, NarrativeMode] = {
    "minimal": NarrativeMode(
        mode="minimal",
        label="极简模式",
        minimum=350,
        maximum=600,
        description="保留行动、结果与下一步线索，省略非关键过程细节。",
        instruction=(
            "省略非关键环境、过程和修辞细节；必须保留已发生行动、"
            "可见结果、必要反应与下一步决策线索。"
        ),
    ),
    "balanced": NarrativeMode(
        mode="balanced",
        label="平衡模式",
        minimum=700,
        maximum=1200,
        description="兼顾推进速度、清晰细节与现场氛围。",
        instruction=(
            "使用清晰、具体的白描兼顾行动推进、可见细节和现场反应；"
            "不重复环境描写或心理描写来凑长度。"
        ),
    ),
    "epic": NarrativeMode(
        mode="epic",
        label="史诗模式",
        minimum=1400,
        maximum=2600,
        description="充分描绘现场变化、感官细节与角色可见反应。",
        instruction=(
            "充分描绘临场感、空间变化、感官细节、行动因果和角色可见反应；"
            "不得添加未裁定事实，也不得替玩家决定思想、对白或行动。"
        ),
    ),
}

DEFAULT_NARRATIVE_MODE = "balanced"


NARRATIVE_DIALOGUE_INSTRUCTION = (
    "场景中有能够自然发言的 NPC、AI 队友或其他人物，且说话符合当下局势时，"
    "让对白成为推进现场的重要部分；极简目标 0—3 个 dialogue block，"
    "平衡目标 2—6 个，史诗目标 4—10 个；"
    "每句直接对白必须是独立 dialogue block。"
    "在对白前后用说话者的神态、动作、停顿、视线或语气承接，以增强临场感，"
    "不要让连续环境描写挤掉人物互动。没有合适说话者或场景应当沉默时不得硬造对白；"
    "不得替玩家角色补写未经选择的对白、思想、决定或行动。"
)


def normalize_narrative_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in NARRATIVE_MODES else DEFAULT_NARRATIVE_MODE


def narrative_mode_from_session(session: Mapping[str, Any] | None) -> str:
    source = dict(session) if isinstance(session, Mapping) else {}
    direct = source.get("narrative_mode")
    if isinstance(direct, Mapping):
        direct = direct.get("mode")
    if direct:
        return normalize_narrative_mode(direct)
    phase = source.get("phase_meta")
    if isinstance(phase, Mapping):
        return normalize_narrative_mode(phase.get("narrative_mode"))
    return DEFAULT_NARRATIVE_MODE


def narrative_quality_policy(mode: object) -> NarrativeMode:
    """Return the canonical structured-document mode bounds."""

    return NARRATIVE_MODES[normalize_narrative_mode(mode)]


def narrative_length_instruction(mode: object) -> str:
    selected = NARRATIVE_MODES[normalize_narrative_mode(mode)]
    return (
        f"当前采用{selected.label}，故事正文必须为 "
        f"{selected.minimum}—{selected.maximum} 个中文可见字符；"
        f"{selected.instruction}{NARRATIVE_DIALOGUE_INSTRUCTION}"
    )


def narrative_length_instruction_for_session(
    session: Mapping[str, Any] | None,
) -> str:
    """Use the injected narrative mode while preserving direct prompt callers."""

    source = dict(session) if isinstance(session, Mapping) else {}
    phase = source.get("phase_meta")
    explicit = source.get("narrative_mode")
    if not explicit and isinstance(phase, Mapping):
        explicit = phase.get("narrative_mode")
    if explicit:
        return narrative_length_instruction(explicit)
    if source.get("opening_scene_projection"):
        return (
            "本回合是开场，采用当前模式上限的 1.25 倍开场余量；"
            + NARRATIVE_DIALOGUE_INSTRUCTION
        )
    return (
        "本回合故事正文必须遵守当前 NarrativeDocument 模式；"
        + NARRATIVE_DIALOGUE_INSTRUCTION
    )


def narrative_mode_view(
    mode: object,
    *,
    revision: int = 0,
    updated_at: str = "",
    can_manage: bool = False,
    replayed: bool = False,
) -> dict[str, Any]:
    selected = NARRATIVE_MODES[normalize_narrative_mode(mode)]
    return {
        **selected.public_view(),
        "revision": max(0, int(revision or 0)),
        "updated_at": str(updated_at or ""),
        "can_manage": bool(can_manage),
        "applies_to": "next_generation",
        "options": [item.public_view() for item in NARRATIVE_MODES.values()],
        "replayed": bool(replayed),
    }


__all__ = [
    "DEFAULT_NARRATIVE_MODE",
    "NARRATIVE_MODES",
    "NARRATIVE_DIALOGUE_INSTRUCTION",
    "NarrativeMode",
    "narrative_length_instruction",
    "narrative_length_instruction_for_session",
    "narrative_mode_from_session",
    "narrative_mode_view",
    "narrative_quality_policy",
    "normalize_narrative_mode",
]
