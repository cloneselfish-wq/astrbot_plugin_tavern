"""BOT 消息注册表：消息类型目录、定义与注册入口。

的玩家消息权威目录。每条定义至少包含：

``message_type / audience / title / summary / sections / entities /
actions / privacy / pagination_policy / delivery_policy /
fallback_message_type / snapshot_test_id``。

模板中的 ``{prefix}`` 占位符由 ``command_display_prefix`` 统一填充，
不允许在文案里硬编码多个命令前缀。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts.common import DEFAULT_COMMAND_PREFIX


@dataclass(frozen=True, slots=True)
class MessageSectionDefinition:
    """一个可按世界能力和运行数据裁剪的消息区块。

    ``source`` 只允许 ``core/world_text/runtime``。世界包只能向
    ``world_text`` 插槽提供冻结文本，不能覆盖权限、隐私、事务结果或恢复命令。
    """

    slot: str
    kind: str = "text"
    source: str = "core"
    body: str = ""
    copy_ref: str = ""
    required: bool = False
    requires_modules: tuple[str, ...] = ()
    requires_data: tuple[str, ...] = ()
    empty_policy: str = "omit"
    audience: str = "public"


SectionDefinition = tuple[str, str] | MessageSectionDefinition


@dataclass(frozen=True, slots=True)
class MessageDefinition:
    """一条已注册消息的完整契约。

    ``sections`` 同时兼容旧 ``(kind, body)`` 二元组和 的结构化
    :class:`MessageSectionDefinition`。``actions`` 是命令模板，渲染时统一
    替换 ``{prefix}``。``sensitive_fields`` 是需要受众裁剪的数据键；
    ``privacy="private"`` 的消息在非私密受众下必须裁剪。
    """

    message_type: str
    audience: str
    title: str
    summary: str = ""
    sections: tuple[SectionDefinition, ...] = ()
    entities: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    privacy: str = "public"
    pagination_policy: str = "logical_blocks"
    delivery_policy: str = "group"
    fallback_message_type: str = "delivery.failed"
    sensitive_fields: tuple[str, ...] = ()
    snapshot_test_id: str = ""


_REGISTRY: dict[str, MessageDefinition] = {}


def register_message(definition: MessageDefinition) -> None:
    """注册一条消息；重复的消息类型视为接线错误并拒绝。"""

    if not isinstance(definition, MessageDefinition):
        raise TypeError("消息定义必须是 MessageDefinition")
    message_type = str(definition.message_type or "").strip()
    if not message_type:
        raise ValueError("消息类型不能为空")
    if message_type in _REGISTRY:
        raise ValueError(f"消息类型重复注册：{message_type}")
    _REGISTRY[message_type] = definition


def get_message(message_type: str) -> MessageDefinition | None:
    """按消息类型取定义；未注册返回 ``None``。"""

    return _REGISTRY.get(str(message_type or "").strip())


def message_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def message_categories() -> dict[str, tuple[str, ...]]:
    """返回当前 18 类消息分组，供运行门禁与快照核对覆盖。"""

    groups: dict[str, tuple[str, ...]] = {}
    for message_type in sorted(_REGISTRY):
        category = str(message_type.split(".", 1)[0] or "other")
        groups.setdefault(category, ())
        groups[category] = groups[category] + (message_type,)
    return groups


def registry_snapshot() -> list[dict[str, Any]]:
    """导出注册表为纯 dict 列表，供测试与文档消费。"""

    return [
        {
            "message_type": item.message_type,
            "audience": item.audience,
            "title": item.title,
            "summary": item.summary,
            "sections": [
                (
                    {
                        "slot": section.slot,
                        "kind": section.kind,
                        "source": section.source,
                        "body": section.body,
                        "copy_ref": section.copy_ref,
                        "required": section.required,
                        "requires_modules": list(section.requires_modules),
                        "requires_data": list(section.requires_data),
                        "empty_policy": section.empty_policy,
                        "audience": section.audience,
                    }
                    if isinstance(section, MessageSectionDefinition)
                    else {"kind": section[0], "body": section[1]}
                )
                for section in item.sections
            ],
            "entities": list(item.entities),
            "actions": list(item.actions),
            "privacy": item.privacy,
            "pagination_policy": item.pagination_policy,
            "delivery_policy": item.delivery_policy,
            "fallback_message_type": item.fallback_message_type,
            "sensitive_fields": list(item.sensitive_fields),
            "snapshot_test_id": item.snapshot_test_id or item.message_type,
        }
        for item in sorted(_REGISTRY.values(), key=lambda d: d.message_type)
    ]


def _definition(
    message_type: str,
    audience: str,
    title: str,
    summary: str = "",
    sections: tuple[SectionDefinition, ...] = (),
    entities: tuple[str, ...] = (),
    actions: tuple[str, ...] = (),
    privacy: str = "public",
    pagination_policy: str = "logical_blocks",
    delivery_policy: str = "group",
    fallback_message_type: str = "delivery.failed",
    sensitive_fields: tuple[str, ...] = (),
) -> MessageDefinition:
    return MessageDefinition(
        message_type=message_type,
        audience=audience,
        title=title,
        summary=summary,
        sections=sections,
        entities=entities,
        actions=actions,
        privacy=privacy,
        pagination_policy=pagination_policy,
        delivery_policy=delivery_policy,
        fallback_message_type=fallback_message_type,
        sensitive_fields=sensitive_fields,
        snapshot_test_id=message_type,
    )


# Import catalogs after registry primitives are defined.
from . import catalog_gameplay as _catalog_gameplay
from . import catalog_system as _catalog_system
