"""注册表消息渲染：PlayerMessageView → BOT 文本。

渲染器只负责布局与平台分页，不决定领域事实；实体标记由
``tavern.copy`` 的统一装饰规则添加，普通字段永不输出内部 ID。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts.common import DEFAULT_COMMAND_PREFIX, clean_label
from ..copy.document import MessageDocument, MessageSection
from ..copy.pagination import paginate_text
from ..copy.render import render_message

from .projection import project_message_view


def _document_from_view(view: Mapping[str, Any]) -> MessageDocument:
    sections: list[MessageSection] = []
    summary = str(view.get("summary") or "").strip()
    if summary:
        sections.append(MessageSection(kind="text", body=summary))
    sections.extend(
        MessageSection(
            kind=str(item.get("kind") or "text"),
            body=str(item.get("body") or ""),
            items=list(item.get("items") or []),
        )
        for item in view.get("sections") or ()
        if isinstance(item, Mapping)
    )
    return MessageDocument(
        kind="notice",
        title=str(view.get("title") or ""),
        sections=sections,
        actions=list(view.get("actions") or []),
        audience=str(view.get("audience") or "public"),
    )


def render_message_type(
    message_type: str,
    data: Mapping[str, Any] | None = None,
    *,
    prefix: str = DEFAULT_COMMAND_PREFIX,
    audience: str = "",
) -> str:
    """渲染一条注册消息为单页 BOT 文本。"""

    view = project_message_view(
        message_type, data=data, prefix=prefix, audience=audience
    )
    return render_message(_document_from_view(view))


def render_message_pages(
    message_type: str,
    data: Mapping[str, Any] | None = None,
    *,
    prefix: str = DEFAULT_COMMAND_PREFIX,
    audience: str = "",
    maximum: int = 3500,
) -> list[str]:
    """渲染注册消息并按逻辑块分页；物理分片不会打断候选块。"""

    text = render_message_type(
        message_type, data=data, prefix=prefix, audience=audience
    )
    return paginate_text(text, maximum, title=clean_label(message_type))


__all__ = ["render_message_pages", "render_message_type"]
