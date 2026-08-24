"""Platform-neutral, mobile-first text delivery helpers for C3.

The story engine only emits text.  Adapter differences are represented as
capabilities and delivery results instead of platform-specific branches in
the game rules.  Unknown adapters receive conservative defaults: event
replies remain available, while proactive delivery is attempted only when a
caller explicitly asks for it and its outcome is reported honestly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .copy.pagination import paginate_text
from .copy.render import mobile_format_text


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    platform: str
    text_reply: bool = True
    group_conversation: bool = True
    private_conversation: bool = True
    # Unified origins may start with a user-defined platform instance id.
    # Unknown ids optimistically attempt AstrBot's standard text API; callers
    # still persist any unconfirmed or failed send instead of claiming success.
    proactive_send: bool = True
    mentions: bool = False
    threads: bool = False
    markdown_text: bool = False
    max_text_length: int = 3500
    # Per-burst cap on proactive text messages.  Delivery chains use it to
    # bound how many physical parts one logical batch may expand into.
    max_messages: int = 6
    identity_scope: str = "adapter_instance"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# The table describes transport behavior only.  Business features never
# branch on these names; they ask for a capability instead.
_CAPABILITIES: dict[str, PlatformCapabilities] = {
    "aiocqhttp": PlatformCapabilities(
        "aiocqhttp",
        proactive_send=True,
        mentions=True,
        max_messages=8,
    ),
    "qq_official": PlatformCapabilities(
        "qq_official",
        mentions=True,
        markdown_text=True,
        max_text_length=1800,
        max_messages=8,
    ),
    "qq_official_webhook": PlatformCapabilities(
        "qq_official_webhook",
        mentions=True,
        markdown_text=True,
        max_text_length=1800,
        max_messages=8,
    ),
    "telegram": PlatformCapabilities(
        "telegram",
        proactive_send=True,
        mentions=True,
        threads=True,
        max_text_length=3900,
        max_messages=6,
    ),
    "lark": PlatformCapabilities("lark", proactive_send=True, mentions=True, threads=True),
    "slack": PlatformCapabilities("slack", proactive_send=True, mentions=True, threads=True),
    "discord": PlatformCapabilities(
        "discord",
        proactive_send=True,
        mentions=True,
        threads=True,
        max_text_length=1900,
        max_messages=8,
    ),
    "misskey": PlatformCapabilities("misskey", proactive_send=True, max_text_length=2800),
    "satori": PlatformCapabilities("satori", proactive_send=True, mentions=True),
    "dingtalk": PlatformCapabilities("dingtalk", mentions=True),
    "kook": PlatformCapabilities("kook", mentions=True, threads=True),
    "wecom": PlatformCapabilities("wecom", mentions=True),
    "wecom_ai_bot": PlatformCapabilities("wecom_ai_bot", mentions=True),
    "weixin_official_account": PlatformCapabilities(
        "weixin_official_account",
        group_conversation=False,
        max_text_length=1900,
    ),
    "weixin_oc": PlatformCapabilities(
        "weixin_oc",
        group_conversation=False,
        max_text_length=1900,
    ),
    "line": PlatformCapabilities("line", max_text_length=4500),
    "matrix": PlatformCapabilities("matrix", mentions=True, threads=True),
    "mattermost": PlatformCapabilities("mattermost", mentions=True, threads=True),
    "vocechat": PlatformCapabilities("vocechat", mentions=True),
    "webchat": PlatformCapabilities("webchat", proactive_send=True, mentions=False),
}


def normalize_platform(value: Any) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        # Older AstrBot OneBot unified origins commonly used the short
        # ``qq`` prefix.  Keep that identity mapping while removing the
        # retired qq_restapi transport itself.
        "qq": "aiocqhttp",
        "qqofficial": "qq_official",
        "qqofficial_webhook": "qq_official_webhook",
        "onebot": "aiocqhttp",
        "onebot_v11": "aiocqhttp",
    }
    return aliases.get(text, text or "unknown")


def platform_from_origin(origin: Any) -> str:
    return normalize_platform(str(origin or "").split(":", 1)[0])


def capabilities_for(platform_or_origin: Any) -> PlatformCapabilities:
    raw = str(platform_or_origin or "")
    platform = platform_from_origin(raw) if ":" in raw else normalize_platform(raw)
    return _CAPABILITIES.get(platform, PlatformCapabilities(platform))


def markdown_supported(platform_or_origin: Any) -> bool:
    """Whether the known adapter has a native Markdown transport."""

    return bool(capabilities_for(platform_or_origin).markdown_text)


def capability_matrix() -> list[dict[str, Any]]:
    return [item.to_dict() for item in sorted(_CAPABILITIES.values(), key=lambda row: row.platform)]


def split_text(text: Any, maximum: int) -> list[str]:
    """Split by logical blocks and add stable page headers."""

    return paginate_text(
        mobile_format_text(text),
        maximum,
    )


__all__ = [
    "PlatformCapabilities",
    "capabilities_for",
    "capability_matrix",
    "markdown_supported",
    "normalize_platform",
    "platform_from_origin",
    "split_text",
]
