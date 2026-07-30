from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .constants import DEFAULT_WORLD_SLUG
from .lifecycle import normalize_time_rules


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _trigger_prefix(value: Any) -> str:
    text = str(value or "jg").strip()
    if (
        not text
        or len(text) > 16
        or any(char.isspace() or ord(char) < 33 for char in text)
    ):
        return "jg"
    return text


@dataclass(frozen=True, slots=True)
class TavernConfig:
    admin_ids: frozenset[str] = field(default_factory=frozenset)
    allowed_group_ids: frozenset[str] = field(default_factory=frozenset)
    require_group_whitelist: bool = True
    unauthorized_command_behavior: str = "silent"
    public_status: bool = True

    provider_id: str = ""
    fallback_provider_ids: tuple[str, ...] = ()
    image_caption_provider_id: str = ""
    image_caption_prompt: str = (
        "请用简洁、客观的中文描述图片中与角色行动、场景、物品、"
        "人物姿态和可见文字有关的信息。不要猜测看不见的事实。"
    )
    max_images_per_turn: int = 4
    temperature: float = 0.7
    max_tokens: int = 1800
    request_timeout_seconds: int = 120
    json_repair_attempts: int = 1

    default_world_slug: str = DEFAULT_WORLD_SLUG
    trigger_prefix: str = "jg"
    two_phase_checks: bool = True
    max_input_chars: int = 2000
    max_output_chars: int = 5000
    recent_turns: int = 12
    memory_limit: int = 10
    user_cooldown_seconds: float = 1.5
    auto_snapshot_interval: int = 5
    ooc_prefixes: tuple[str, ...] = ("【OOC】", "[OOC]", "OOC:")
    time_rules: Mapping[str, Any] = field(
        default_factory=normalize_time_rules
    )

    audit_retention_days: int = 90
    store_model_payloads: bool = False
    debug: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TavernConfig":
        root = _mapping(raw)
        security = _mapping(root.get("security"))
        model = _mapping(root.get("model"))
        runtime = _mapping(root.get("runtime"))
        advanced = _mapping(root.get("advanced"))

        behavior = str(
            security.get("unauthorized_command_behavior", "silent")
        ).strip()
        if behavior not in {"silent", "deny"}:
            behavior = "silent"

        prefixes = _strings(runtime.get("ooc_prefixes"))
        if not prefixes:
            prefixes = ("【OOC】", "[OOC]", "OOC:")

        provider_id = str(model.get("provider_id", "")).strip()
        fixed_fallbacks = [
            str(model.get(f"fallback_provider_{index}_id", "") or "").strip()
            for index in range(1, 5)
        ]
        fallback_provider_ids = tuple(
            item
            for item in _strings(
                [
                    *fixed_fallbacks,
                    *_strings(model.get("fallback_provider_ids")),
                ]
            )[:8]
            if item != provider_id
        )
        image_caption_prompt = str(
            model.get(
                "image_caption_prompt",
                (
                    "请用简洁、客观的中文描述图片中与角色行动、场景、"
                    "物品、人物姿态和可见文字有关的信息。"
                    "不要猜测看不见的事实。"
                ),
            )
            or ""
        ).strip()
        if not image_caption_prompt:
            image_caption_prompt = (
                "请用简洁、客观的中文描述图片中与角色行动、场景、"
                "物品、人物姿态和可见文字有关的信息。"
                "不要猜测看不见的事实。"
            )
        image_caption_prompt = image_caption_prompt[:2000]

        return cls(
            admin_ids=frozenset(_strings(security.get("admin_ids"))),
            allowed_group_ids=frozenset(
                _strings(security.get("allowed_group_ids"))
            ),
            require_group_whitelist=bool(
                security.get("require_group_whitelist", True)
            ),
            unauthorized_command_behavior=behavior,
            public_status=bool(security.get("public_status", True)),
            provider_id=provider_id,
            fallback_provider_ids=fallback_provider_ids,
            image_caption_provider_id=str(
                model.get("image_caption_provider_id", "")
            ).strip(),
            image_caption_prompt=image_caption_prompt,
            max_images_per_turn=_bounded_int(
                model.get("max_images_per_turn"), 4, 1, 8
            ),
            temperature=_bounded_float(
                model.get("temperature"), 0.7, 0.0, 2.0
            ),
            max_tokens=_bounded_int(
                model.get("max_tokens"), 1800, 256, 16000
            ),
            request_timeout_seconds=_bounded_int(
                model.get("request_timeout_seconds"), 120, 15, 600
            ),
            json_repair_attempts=_bounded_int(
                model.get("json_repair_attempts"), 1, 0, 2
            ),
            default_world_slug=(
                str(
                    runtime.get("default_world_slug", DEFAULT_WORLD_SLUG)
                ).strip()
                or DEFAULT_WORLD_SLUG
            ),
            trigger_prefix=_trigger_prefix(
                runtime.get("trigger_prefix", "jg")
            ),
            two_phase_checks=bool(
                runtime.get("two_phase_checks", True)
            ),
            max_input_chars=_bounded_int(
                runtime.get("max_input_chars"), 2000, 100, 12000
            ),
            max_output_chars=_bounded_int(
                runtime.get("max_output_chars"), 5000, 500, 20000
            ),
            recent_turns=_bounded_int(
                runtime.get("recent_turns"), 12, 2, 50
            ),
            memory_limit=_bounded_int(
                runtime.get("memory_limit"), 10, 0, 40
            ),
            user_cooldown_seconds=_bounded_float(
                runtime.get("user_cooldown_seconds"), 1.5, 0.0, 60.0
            ),
            auto_snapshot_interval=_bounded_int(
                runtime.get("auto_snapshot_interval"), 5, 0, 100
            ),
            ooc_prefixes=prefixes,
            time_rules=normalize_time_rules(runtime.get("time_rules")),
            audit_retention_days=_bounded_int(
                advanced.get("audit_retention_days"), 90, 1, 3650
            ),
            store_model_payloads=bool(
                advanced.get("store_model_payloads", False)
            ),
            debug=bool(advanced.get("debug", False)),
        )

    def is_admin(self, sender_id: str) -> bool:
        return str(sender_id) in self.admin_ids

    def is_group_allowed(self, group_id: str) -> bool:
        normalized = str(group_id)
        if not self.require_group_whitelist:
            return True
        return normalized in self.allowed_group_ids

    def to_mapping(self) -> dict[str, Any]:
        return {
            "security": {
                "admin_ids": sorted(self.admin_ids),
                "allowed_group_ids": sorted(self.allowed_group_ids),
                "require_group_whitelist": self.require_group_whitelist,
                "unauthorized_command_behavior": (
                    self.unauthorized_command_behavior
                ),
                "public_status": self.public_status,
            },
            "model": {
                "provider_id": self.provider_id,
                **{
                    f"fallback_provider_{index}_id": (
                        self.fallback_provider_ids[index - 1]
                        if len(self.fallback_provider_ids) >= index
                        else ""
                    )
                    for index in range(1, 5)
                },
                # Keep the aggregate field for the built-in console and for
                # upgrading configurations written by 0.4.x.
                "fallback_provider_ids": list(
                    self.fallback_provider_ids
                ),
                "image_caption_provider_id": (
                    self.image_caption_provider_id
                ),
                "image_caption_prompt": self.image_caption_prompt,
                "max_images_per_turn": self.max_images_per_turn,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "request_timeout_seconds": self.request_timeout_seconds,
                "json_repair_attempts": self.json_repair_attempts,
            },
            "runtime": {
                "default_world_slug": self.default_world_slug,
                "trigger_prefix": self.trigger_prefix,
                "two_phase_checks": self.two_phase_checks,
                "max_input_chars": self.max_input_chars,
                "max_output_chars": self.max_output_chars,
                "recent_turns": self.recent_turns,
                "memory_limit": self.memory_limit,
                "user_cooldown_seconds": self.user_cooldown_seconds,
                "auto_snapshot_interval": self.auto_snapshot_interval,
                "ooc_prefixes": list(self.ooc_prefixes),
                "time_rules": dict(self.time_rules),
            },
            "advanced": {
                "audit_retention_days": self.audit_retention_days,
                "store_model_payloads": self.store_model_payloads,
                "debug": self.debug,
            },
        }
