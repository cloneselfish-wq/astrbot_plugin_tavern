"""D1-UX-011：PlayerMessageView 纯投影。

BOT 消息与 WebUI 时间线卡共用同一视图。视图只携带已清洗的文案与实体
类型标记；实体装饰（「」〔〕『』〈〉《》〖〗）由渲染器统一添加，
字段 key 与稳定 ID 永不进入普通字段。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...copy.entities import EntityToken

from ..common import DEFAULT_COMMAND_PREFIX, clean_label


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _section_view(raw: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(raw)
    items = [
        clean_label(item)
        for item in (raw.get("items") or ())
        if clean_label(item)
    ]
    return {
        "kind": str(raw.get("kind") or "text"),
        "body": clean_label(raw.get("body")),
        "items": items,
    }


def _entity_view(raw: Any) -> dict[str, Any]:
    if isinstance(raw, EntityToken):
        return {
            "entity_type": raw.entity_type,
            "label": clean_label(raw.label),
            "visibility": raw.visibility or "public",
        }
    raw = _mapping(raw)
    return {
        "entity_type": str(raw.get("entity_type") or ""),
        "label": clean_label(raw.get("label")),
        "visibility": str(raw.get("visibility") or "public"),
    }


def project_player_message_view(
    message_type: str,
    *,
    title: str = "",
    summary: str = "",
    sections: Sequence[Mapping[str, Any]] | None = None,
    entities: Sequence[Any] | None = None,
    actions: Sequence[str] | None = None,
    audience: str = "public",
    privacy: str = "public",
    delivery_policy: str = "group",
    fallback_message_type: str = "delivery.failed",
    prefix: str = DEFAULT_COMMAND_PREFIX,
    snapshot_test_id: str = "",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把消息定义与领域数据规范化为 BOT / WebUI 共用消息视图。

    ``prefix`` 用于把 ``{prefix}`` 占位符统一替换为当前命令显示前缀，
    保证一条消息内不会混用两个前缀。
    """

    command_prefix = clean_label(prefix, DEFAULT_COMMAND_PREFIX) or DEFAULT_COMMAND_PREFIX
    sections_view = [
        _section_view(raw)
        for raw in (sections or ())
        if isinstance(raw, Mapping)
    ]
    entities_view = [
        _entity_view(raw)
        for raw in (entities or ())
        if isinstance(raw, (EntityToken, Mapping))
    ]
    actions_view = [
        clean_label(str(item).replace("{prefix}", command_prefix))
        for item in (actions or ())
        if clean_label(str(item).replace("{prefix}", command_prefix))
    ]
    view: dict[str, Any] = {
        "schema": "tavern-player-message/1.0.0-rc10",
        "message_type": str(message_type or ""),
        "audience": str(audience or "public"),
        "title": clean_label(title, "系统通知"),
        "summary": clean_label(summary),
        "sections": sections_view,
        "entities": entities_view,
        "actions": actions_view,
        "privacy": str(privacy or "public"),
        "delivery_policy": str(delivery_policy or "group"),
        "fallback_message_type": str(fallback_message_type or "delivery.failed"),
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = {
            "message_type": str(message_type or ""),
            "snapshot_test_id": str(snapshot_test_id or ""),
        }
    return view


__all__ = ["project_player_message_view"]
