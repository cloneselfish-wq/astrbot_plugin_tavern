"""注册表消息 → PlayerMessageView 投影。

从注册表取出定义后，用领域数据填充占位符（``{prefix}`` 由
``command_display_prefix`` 统一填充），并按受众执行隐私裁剪：
``privacy="private"`` 的消息在非私密受众下只保留“仅限私聊”说明，
敏感字段数据绝不进入公开投影。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..contracts.common import DEFAULT_COMMAND_PREFIX, clean_label
from ..contracts.web_views.message import project_player_message_view

from .registry import (
    MessageDefinition,
    MessageSectionDefinition,
    get_message,
)


_PRIVATE_AUDIENCES = frozenset({"player", "character", "dm", "admin", "author"})
_LEFTOVER_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


def _substitute(template: str, data: Mapping[str, Any], prefix: str) -> str:
    text = str(template or "")
    text = text.replace("{prefix}", prefix)
    for key, value in (data or {}).items():
        placeholder = "{" + str(key) + "}"
        if placeholder not in text:
            continue
        cleaned = clean_label(value)
        if not cleaned:
            cleaned = "详情暂不可用，请稍后重试。"
        text = text.replace(placeholder, cleaned)
    text = _LEFTOVER_PLACEHOLDER.sub("详情暂不可用，请稍后重试。", text)
    return text.strip()


def _audience_allowed(section_audience: str, effective_audience: str) -> bool:
    expected = str(section_audience or "public").strip()
    actual = str(effective_audience or "public").strip()
    if expected == "public":
        return True
    if expected == actual:
        return True
    if actual in {"admin", "dm"}:
        return True
    return expected == "player" and actual in {"player", "character"}


def _world_copy_text(
    message_type: str,
    section: MessageSectionDefinition,
    *,
    bindings: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> str:
    message_bindings = bindings.get(message_type)
    message_bindings = (
        message_bindings if isinstance(message_bindings, Mapping) else {}
    )
    text_id = str(
        message_bindings.get(section.slot)
        or section.copy_ref
        or ""
    ).strip()
    if not text_id:
        return ""
    default_locale = str(
        catalog.get("_default_locale")
        if isinstance(catalog, Mapping)
        else ""
    ).strip()
    if default_locale and isinstance(catalog.get(default_locale), Mapping):
        catalog = catalog[default_locale]
    elif catalog and all(
        isinstance(item, Mapping) for item in catalog.values()
    ):
        first_locale = next(iter(sorted(catalog)), "")
        if first_locale:
            catalog = catalog[first_locale]
    raw = catalog.get(text_id)
    if raw is None:
        raw = catalog.get(f"{text_id}.text")
    if raw is None:
        raw = catalog.get(f"{text_id}.description")
    if isinstance(raw, Mapping):
        raw = raw.get("text") or raw.get("description")
    return clean_label(raw)


def _section_bodies(
    message_type: str,
    sections: tuple[Any, ...],
    data: Mapping[str, Any],
    prefix: str,
    effective_audience: str,
) -> list[dict[str, Any]]:
    enabled_modules = {
        str(item).strip()
        for item in (data.get("_enabled_modules") or ())
        if str(item).strip()
    }
    bindings = data.get("_message_copy_bindings")
    bindings = bindings if isinstance(bindings, Mapping) else {}
    catalog = data.get("_resolved_text_catalog")
    catalog = catalog if isinstance(catalog, Mapping) else {}
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(sections):
        if isinstance(raw, MessageSectionDefinition):
            section = raw
        else:
            kind, body = raw
            section = MessageSectionDefinition(
                slot=f"legacy_{index}",
                kind=str(kind or "text"),
                source="core",
                body=str(body or ""),
                empty_policy="fallback",
            )
        if not _audience_allowed(section.audience, effective_audience):
            continue
        if section.requires_modules and not set(section.requires_modules).issubset(
            enabled_modules
        ):
            continue
        if section.requires_data and any(
            not clean_label(data.get(key)) for key in section.requires_data
        ):
            if section.empty_policy == "error" or section.required:
                raise ValueError(
                    f"{message_type}.{section.slot} 缺少运行数据："
                    + "、".join(section.requires_data)
                )
            if section.empty_policy == "omit":
                continue
        if section.source == "world_text":
            body = _world_copy_text(
                message_type,
                section,
                bindings=bindings,
                catalog=catalog,
            )
        elif section.source in {"core", "runtime"}:
            body = section.body
        else:
            raise ValueError(
                f"{message_type}.{section.slot} 的 source 无效：{section.source}"
            )
        if not clean_label(body):
            if section.empty_policy == "fallback":
                body = section.body
            elif section.empty_policy == "error" or section.required:
                raise ValueError(
                    f"{message_type}.{section.slot} 缺少必需世界文案"
                )
            else:
                continue
        body = _substitute(body, data, prefix)
        if body:
            output.append(
                {
                    "slot": section.slot,
                    "kind": section.kind,
                    "source": section.source,
                    "body": body,
                }
            )
    return output


def project_message_view_from_definition(
    definition: MessageDefinition,
    data: Mapping[str, Any] | None = None,
    *,
    prefix: str = DEFAULT_COMMAND_PREFIX,
    audience: str = "",
) -> dict[str, Any]:
    """按定义 + 数据投影 PlayerMessageView（含受众裁剪）。"""

    if not isinstance(definition, MessageDefinition):
        raise TypeError("消息定义必须是 MessageDefinition")
    data = dict(data or {})
    command_prefix = clean_label(prefix, DEFAULT_COMMAND_PREFIX) or DEFAULT_COMMAND_PREFIX
    effective_audience = str(audience or definition.audience or "public").strip()

    if (
        definition.privacy == "private"
        and effective_audience not in _PRIVATE_AUDIENCES
    ):
        return project_player_message_view(
            definition.message_type,
            title="私密消息已隐藏",
            summary="这条信息只向指定玩家显示，不会在群聊中公开。",
            sections=[],
            entities=[],
            actions=[],
            audience="public",
            privacy="public",
            delivery_policy="webui_only",
            fallback_message_type=definition.fallback_message_type,
            prefix=command_prefix,
            snapshot_test_id=definition.snapshot_test_id or definition.message_type,
        )

    sections = _section_bodies(
        definition.message_type,
        definition.sections,
        data,
        command_prefix,
        effective_audience,
    )
    actions = tuple(
        _substitute(action, data, command_prefix)
        for action in definition.actions
    )
    return project_player_message_view(
        definition.message_type,
        title=_substitute(definition.title, data, command_prefix),
        summary=_substitute(definition.summary, data, command_prefix),
        sections=sections,
        entities=[],
        actions=actions,
        audience=effective_audience,
        privacy=definition.privacy,
        delivery_policy=definition.delivery_policy,
        fallback_message_type=definition.fallback_message_type,
        prefix=command_prefix,
        snapshot_test_id=definition.snapshot_test_id or definition.message_type,
    )


def project_message_view(
    message_type: str,
    data: Mapping[str, Any] | None = None,
    *,
    prefix: str = DEFAULT_COMMAND_PREFIX,
    audience: str = "",
) -> dict[str, Any]:
    """按消息类型投影；未注册类型抛出 :class:`KeyError`。"""

    definition = get_message(message_type)
    if definition is None:
        raise KeyError(f"未注册的消息类型：{message_type}")
    return project_message_view_from_definition(
        definition,
        data=data,
        prefix=prefix,
        audience=audience,
    )


__all__ = [
    "project_message_view",
    "project_message_view_from_definition",
]
