"""统一交互提示契约。

四种模式由数据库、引擎、BOT、API、WebUI、AI 队友、计时器和投票共享：
``choices`` 创建稳定候选；``free_text`` 和 ``dialogue`` 接受自然语言；
``notice`` 只播报、不建立等待输入状态。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from .lifecycle import normalize_model_choices


INTERACTION_MODES = frozenset({"choices", "free_text", "dialogue", "notice"})


@dataclass(frozen=True, slots=True)
class InteractionPrompt:
    mode: str
    revision: int
    prompt_text_ref: str = ""
    choices: tuple[dict[str, Any], ...] = ()
    input_hint_ref: str = ""
    deadline: str = ""
    vote_policy: dict[str, Any] = field(default_factory=dict)
    resume_policy: dict[str, Any] = field(default_factory=dict)

    def export(self) -> dict[str, Any]:
        value = asdict(self)
        value["choices"] = [dict(item) for item in self.choices]
        return value


def normalize_interaction_prompt(
    value: Mapping[str, Any] | None,
    *,
    world: Mapping[str, Any] | None = None,
    revision: int = 1,
) -> InteractionPrompt:
    raw = dict(value or {})
    mode = str(raw.get("mode") or "choices").strip().lower()
    if mode not in INTERACTION_MODES:
        raise ValueError(
            "交互模式必须是 choices、free_text、dialogue 或 notice"
        )
    prompt_revision = max(1, int(raw.get("revision") or revision or 1))
    deadline = str(raw.get("deadline") or "").strip()
    if deadline:
        try:
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("交互提示 deadline 必须是 ISO-8601 时间") from exc
    raw_choices = raw.get("choices")
    if mode == "choices":
        if not isinstance(raw_choices, Sequence) or isinstance(
            raw_choices, (str, bytes)
        ):
            raise ValueError("choices 模式必须提供候选数组")
        choices = tuple(normalize_model_choices(raw_choices, world))
    else:
        if raw_choices not in (None, (), []):
            raise ValueError(f"{mode} 模式不能携带候选数组")
        choices = ()
    vote_policy = raw.get("vote_policy")
    vote_policy = dict(vote_policy) if isinstance(vote_policy, Mapping) else {}
    if mode != "choices" and vote_policy:
        raise ValueError(f"{mode} 模式不能启动投票")
    resume_policy = raw.get("resume_policy")
    resume_policy = (
        dict(resume_policy) if isinstance(resume_policy, Mapping) else {}
    )
    return InteractionPrompt(
        mode=mode,
        revision=prompt_revision,
        prompt_text_ref=str(raw.get("prompt_text_ref") or "").strip(),
        choices=choices,
        input_hint_ref=str(raw.get("input_hint_ref") or "").strip(),
        deadline=deadline,
        vote_policy=vote_policy,
        resume_policy=resume_policy,
    )


def interaction_policy(world: Mapping[str, Any] | None) -> dict[str, Any]:
    world = world if isinstance(world, Mapping) else {}
    rules = world.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    chat = rules.get("chat_experience")
    chat = chat if isinstance(chat, Mapping) else {}
    policy = chat.get("interaction_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    default_mode = str(policy.get("default_mode") or "choices").strip().lower()
    if default_mode not in INTERACTION_MODES:
        default_mode = "choices"
    return {
        "default_mode": default_mode,
        "opening_mode": str(
            policy.get("opening_mode") or default_mode
        ).strip().lower(),
        "resume_policy": dict(policy.get("resume_policy") or {}),
    }


__all__ = [
    "INTERACTION_MODES",
    "InteractionPrompt",
    "interaction_policy",
    "normalize_interaction_prompt",
]

