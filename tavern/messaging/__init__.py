"""统一玩家消息注册表与最终渲染出口。

领域事实 → PlayerMessageView → 布局模板 → 平台分页/分片 → 投递结果，
消息模板只负责组织信息，不猜测数值、状态或内部 ID。
"""

from __future__ import annotations

from .projection import (
    project_message_view,
    project_message_view_from_definition,
)
from .registry import (
    DEFAULT_COMMAND_PREFIX,
    MessageDefinition,
    MessageSectionDefinition,
    get_message,
    message_categories,
    message_types,
    register_message,
    registry_snapshot,
)
from .render import (
    render_message_pages,
    render_message_type,
)
from .player import (
    PlayerMessage,
    PlayerOutput,
    prepare_player_output,
    render_player_markdown,
    render_player_message,
    render_player_text,
)
from .turn_bundle import (
    TurnMessageBundle,
    TurnMessagePart,
    deserialize_player_message,
    reply_message_parts,
    serialize_player_message,
    split_turn_bundle_for_delivery,
)
from .delivery_parts import send_ordered_parts

__all__ = [
    "DEFAULT_COMMAND_PREFIX",
    "MessageDefinition",
    "MessageSectionDefinition",
    "get_message",
    "message_categories",
    "message_types",
    "project_message_view",
    "project_message_view_from_definition",
    "PlayerMessage",
    "PlayerOutput",
    "prepare_player_output",
    "register_message",
    "registry_snapshot",
    "render_player_markdown",
    "render_player_message",
    "render_player_text",
    "render_message_pages",
    "render_message_type",
    "TurnMessageBundle",
    "TurnMessagePart",
    "deserialize_player_message",
    "reply_message_parts",
    "serialize_player_message",
    "split_turn_bundle_for_delivery",
    "send_ordered_parts",
]
