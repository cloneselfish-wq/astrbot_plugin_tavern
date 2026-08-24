"""玩家可见消息权威模型与最终渲染入口。

业务层优先返回 :class:`PlayerMessage`，注册消息由 ``message_type`` 和
结构化数据渲染；模型生成的剧情正文、世界文本和迁移中的历史回执使用
``dynamic`` 模式，但仍经过同一移动端布局、隐私清理与内部状态门禁。

``render_player_text`` 是迁移期唯一兼容入口。它不会猜测业务事实，也不
修改事务结果，只把已经产生的玩家文本整理为 的可读布局。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..contracts.common import DEFAULT_COMMAND_PREFIX, clean_label
from ..copy.document import MessageDocument, MessageSection
from ..copy.render import mobile_format_text, render_message
from ..turn_budget import player_generation_stage_label
from .registry import get_message
from .render import render_message_type


_TITLE_LINE = re.compile(r"^\s*(?:[^\w\s【]{1,3}\s*)?【([^】]+)】\s*(.*)$")
# Do not split commands already wrapped in Markdown inline code.  The old
# expression turned ``> `/团 重整选项``` into a dangling quote/backtick plus
# a separate command, which is exactly the malformed block visible in QQ.
_COMMAND_INLINE = re.compile(r"(?<![\n`])(/团(?:\s+[^，。；\n]+)?)")
_INTERNAL_STAGE = re.compile(
    r"(?P<prefix>当前阶段|生成阶段|阶段)\s*[：:]\s*"
    r"(?P<stage>[a-z][a-z0-9_.-]{2,})",
    re.IGNORECASE,
)
_INTERNAL_FIELD = re.compile(
    r"(?im)^\s*(?:session_id|group_id|user_id|actor_id|revision|"
    r"schema|abi|artifact|json|database_field|field_key)\s*[：:].*$"
)
_LEGACY_TITLES = {
    "开团": "操作提示",
    "开团状态": "副本状态",
    "私聊建卡": "角色卡",
    "开团倒计时": "时间提醒",
    "开团计时": "时间到",
    "回合秩序": "行动顺序",
    "技能成长": "角色成长",
}
_SECTION_LINE = re.compile(
    r"^(失败操作|原因|自动处理|下一步|影响|限制|条件|风险|当前状态|"
    r"处理结果|可用操作|判断依据|故事进展|公开信息)[：:]?\s*(.*)$"
)
_OPTION_MARKDOWN = re.compile(
    r"^(?:〔)?([A-D]|\d+)(?:〕|[\.、])\s*(.+)$"
)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
_MARKDOWN_OPTION = re.compile(
    r"^\s*[-*]\s+\*\*([A-D]|\d+)[\s　、.:：-]*(.*?)\*\*\s*(.*)$"
)
_MARKDOWN_BULLET = re.compile(r"^\s*[-*]\s+(.+)$")
_MARKDOWN_QUOTE = re.compile(r"^\s*>\s*(.*)$")
_MARKDOWN_RULE = re.compile(r"^\s*[-*_]{3,}\s*$")
_INLINE_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_INLINE_STRONG = re.compile(r"\*\*([^*]+)\*\*")
_FEATURE_LINE = re.compile(
    r"^(优势|限制|条件|风险|故事入口|适配状态|适配说明|已知影响)｜(.+)$"
)


def _plain_inline_markdown(value: str) -> str:
    text = _INLINE_LINK.sub(r"\1", str(value or ""))
    text = _INLINE_CODE.sub(r"\1", text)
    text = _INLINE_STRONG.sub(r"\1", text)
    return text.replace("**", "").replace("`", "").strip()


def _markdown_to_plain(value: str) -> str:
    """Render Markdown-shaped legacy copy as readable mobile plain text.

    QQ Official can reject Markdown for accounts without the capability.  The
    plain renderer therefore must never leak ``#``/``**``/backticks as UI
    punctuation.  This conversion changes presentation only, not facts.
    """

    output: list[str] = []
    for raw in str(value or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            output.append("")
            continue
        heading = _MARKDOWN_HEADING.match(stripped)
        if heading:
            title = _plain_inline_markdown(heading.group(2).rstrip("#").strip())
            output.append(
                f"【{title}】" if len(heading.group(1)) == 1 else f"◆ {title}"
            )
            continue
        option = _MARKDOWN_OPTION.match(stripped)
        if option:
            label = _plain_inline_markdown(option.group(2))
            suffix = _plain_inline_markdown(option.group(3))
            output.append(
                f"〔{option.group(1)}〕 {label}"
                + (f" {suffix}" if suffix else "")
            )
            continue
        quote = _MARKDOWN_QUOTE.match(stripped)
        if quote:
            body = _plain_inline_markdown(quote.group(1))
            if body:
                output.append(body if body.startswith(DEFAULT_COMMAND_PREFIX) else f"↳ {body}")
            continue
        if _MARKDOWN_RULE.match(stripped):
            output.append("────────")
            continue
        section = _SECTION_LINE.match(stripped)
        if section and not section.group(2).strip():
            output.append(f"◆ {section.group(1)}")
            continue
        bullet = _MARKDOWN_BULLET.match(stripped)
        if bullet:
            output.append("• " + _plain_inline_markdown(bullet.group(1)))
            continue
        output.append(_plain_inline_markdown(stripped))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()
@dataclass(frozen=True, slots=True)
class PlayerMessage:
    """平台无关的玩家消息 DTO。

    ``message_type`` 非空时由消息注册表决定布局、受众和隐私；动态剧情或
    世界原文使用 ``title/summary/sections/actions``，仍不能携带内部字段。
    """

    message_type: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    audience: str = "public"
    title: str = ""
    summary: str = ""
    sections: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    entities: tuple[Mapping[str, Any], ...] = ()
    privacy: str = "public"
    pagination_policy: str = "logical_blocks"
    delivery_policy: str = "group"
    source_revision: str = ""
    dedupe_key: str = ""
    source: str = "core"
    fallback_text: str = ""

    @classmethod
    def registered(
        cls,
        message_type: str,
        data: Mapping[str, Any] | None = None,
        *,
        audience: str = "",
    ) -> "PlayerMessage":
        if get_message(message_type) is None:
            raise KeyError(f"未注册的玩家消息类型：{message_type}")
        return cls(
            message_type=str(message_type),
            data=dict(data or {}),
            audience=str(audience or ""),
        )

    @classmethod
    def dynamic(
        cls,
        *,
        title: str,
        summary: str = "",
        sections: Sequence[str] = (),
        actions: Sequence[str] = (),
        audience: str = "public",
        source: str = "runtime",
        privacy: str = "public",
        pagination_policy: str = "logical_blocks",
        delivery_policy: str = "group",
        source_revision: str = "",
    ) -> "PlayerMessage":
        return cls(
            audience=str(audience or "public"),
            title=clean_label(title),
            summary=str(summary or "").strip(),
            sections=tuple(str(item or "").strip() for item in sections if str(item or "").strip()),
            actions=tuple(str(item or "").strip() for item in actions if str(item or "").strip()),
            privacy=str(privacy or "public"),
            pagination_policy=str(pagination_policy or "logical_blocks"),
            delivery_policy=str(delivery_policy or "group"),
            source_revision=str(source_revision or ""),
            source=str(source or "runtime"),
        )

    @classmethod
    def from_text(
        cls,
        text: Any,
        *,
        default_title: str = "酒馆消息",
        audience: str = "public",
    ) -> "PlayerMessage":
        """将迁移中的直接文本在应用层收敛为结构化 DTO。"""

        message = _split_legacy_text(
            _public_text(text),
            default_title=default_title,
        )
        return cls(
            message_type=message.message_type,
            data=message.data,
            audience=str(audience or message.audience or "public"),
            title=message.title,
            summary=message.summary,
            sections=message.sections,
            actions=message.actions,
            entities=message.entities,
            privacy=message.privacy,
            pagination_policy=message.pagination_policy,
            delivery_policy=message.delivery_policy,
            source_revision=message.source_revision,
            dedupe_key=message.dedupe_key,
            source=message.source,
            fallback_text=message.fallback_text,
        )


@dataclass(frozen=True, slots=True)
class PlayerOutput:
    """One authoritative player payload with a platform fallback.

    ``markdown`` is the standard BOT output. ``plain`` exists only for an
    adapter that cannot consume Markdown; both variants are rendered from the
    same source value so the fallback never becomes the input of the rich
    renderer and silently loses structure.
    """

    markdown: str
    plain: str

    def select(self, *, markdown: bool = True) -> str:
        return self.markdown if markdown else self.plain


def _public_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    text = _INTERNAL_FIELD.sub("", text)

    def stage_label(match: re.Match[str]) -> str:
        label = player_generation_stage_label(match.group("stage"))
        return f"{match.group('prefix')}：{label}"

    text = _INTERNAL_STAGE.sub(stage_label, text)
    text = _COMMAND_INLINE.sub(r"\n\n\1", text)
    text = re.sub(r"(?m)^(/团[^\n]*?)[。；，]+$", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render_player_message(
    message: PlayerMessage,
    *,
    prefix: str = DEFAULT_COMMAND_PREFIX,
) -> str:
    """将结构化消息渲染为最终玩家文本。"""

    if message.message_type:
        return render_message_type(
            message.message_type,
            data=message.data,
            prefix=prefix,
            audience=message.audience,
        )
    document = MessageDocument(
        kind="notice",
        title=_public_text(message.title),
        sections=[
            MessageSection(kind="text", body=_public_text(item))
            for item in (message.summary, *message.sections)
            if _public_text(item)
        ]
        + (
            [MessageSection(kind="text", body="下一步")]
            if message.actions
            and not any(
                _public_text(item).rstrip("：:").endswith("下一步")
                for item in (message.summary, *message.sections)
            )
            else []
        ),
        actions=[_public_text(item) for item in message.actions if _public_text(item)],
        audience=message.audience,
    )
    return render_message(document)


def _split_legacy_text(text: str, *, default_title: str) -> PlayerMessage:
    """把迁移期直接文本转换为结构化动态消息，不改变业务事实。"""

    value = _public_text(text)
    if not value:
        return PlayerMessage()
    lines = value.splitlines()
    first = lines[0].strip() if lines else ""
    match = _TITLE_LINE.match(first)
    title = clean_label(default_title or "酒馆消息")
    remainder: list[str] = []
    if match:
        title = clean_label(match.group(1), title)
        title = _LEGACY_TITLES.get(title, title)
        inline = str(match.group(2) or "").strip()
        if inline:
            remainder.append(inline)
        remainder.extend(lines[1:])
    else:
        remainder = lines
    body = "\n".join(remainder).strip()
    body = re.sub(
        r"(?ms)^下一步[：:][^\n]*(?=\n{2}/团)",
        "下一步",
        body,
    )
    if any(token in title for token in ("失败", "无法", "未能")):
        body = re.sub(r"(?m)^操作[：:]", "失败操作：", body)
    blocks = [
        item.strip()
        for item in re.split(r"\n{2,}", body)
        if item.strip()
    ]
    actions = tuple(
        block
        for block in blocks
        if block.startswith(DEFAULT_COMMAND_PREFIX)
    )
    sections = tuple(block for block in blocks if block not in actions)
    summary = sections[0] if sections else ""
    return PlayerMessage.dynamic(
        title=title,
        summary=summary,
        sections=sections[1:],
        actions=actions,
        source="legacy_adapter",
    )


def render_player_text(
    value: PlayerMessage | Any,
    *,
    default_title: str = "",
    prefix: str = DEFAULT_COMMAND_PREFIX,
) -> str:
    """最终玩家文本入口；重复调用保持幂等。"""

    if isinstance(value, PlayerMessage):
        rendered = render_player_message(value, prefix=prefix)
    else:
        text = _public_text(value)
        if not text:
            return ""
        message = PlayerMessage.from_text(text, default_title=default_title)
        rendered = render_player_message(message, prefix=prefix)
    if not rendered:
        return ""
    return mobile_format_text(_markdown_to_plain(rendered))


def render_player_markdown(
    value: PlayerMessage | Any,
    *,
    default_title: str = "",
) -> str:
    """把权威玩家文本转换为统一 BOT Markdown。

    这里只改变排版，不改变业务事实、事务状态或隐私裁剪；平台出口负责
    携带 Markdown 标志，不支持的适配器再使用同源纯文本降级。
    """

    if isinstance(value, PlayerMessage):
        rendered = render_player_message(value)
    else:
        text = _public_text(value)
        if not text:
            return ""
        rendered = render_player_message(
            PlayerMessage.from_text(text, default_title=default_title)
        )
    if not rendered:
        return ""
    action_lines = {
        _public_text(item)
        for item in (
            value.actions if isinstance(value, PlayerMessage) else ()
        )
        if _public_text(item)
    }
    output: list[str] = []
    for raw in rendered.splitlines():
        line = raw.strip()
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue
        if line.startswith("# "):
            output.append(line)
            continue
        if _MARKDOWN_HEADING.match(line):
            output.append(line)
            continue
        if _MARKDOWN_RULE.match(line):
            output.append("---")
            continue
        if line.startswith(("- ", "* ", "> ")):
            output.append(line)
            continue
        title = _TITLE_LINE.match(line)
        if title and not title.group(2):
            output.append(f"# {clean_label(title.group(1), '酒馆消息')}")
            continue
        # Candidate traits use the full-width divider as an explicit compact
        # row contract.  Match it before the generic section parser: the
        # latter intentionally accepts a missing colon, so ``限制｜...`` used
        # to be consumed as a level-two heading while ``优势｜...`` remained a
        # compact row.  That produced visibly different Markdown for two
        # sibling fields on QQ Official.
        feature = _FEATURE_LINE.match(line)
        if feature:
            output.append(f"> **{feature.group(1)}**｜{feature.group(2)}")
            continue
        section = _SECTION_LINE.match(line)
        if section:
            output.append(f"## {section.group(1)}")
            remainder = section.group(2).strip()
            if remainder:
                output.append(remainder)
            continue
        option = _OPTION_MARKDOWN.match(line)
        if option:
            output.append(f"- **{option.group(1)}**　{option.group(2)}")
            continue
        if line in action_lines or line.startswith(DEFAULT_COMMAND_PREFIX):
            output.append(f"> `{line}`")
            continue
        output.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()


def prepare_player_output(
    value: PlayerMessage | Any,
    *,
    default_title: str = "",
) -> PlayerOutput:
    """Build the sole outbound BOT payload before transport selection."""

    return PlayerOutput(
        markdown=render_player_markdown(value, default_title=default_title),
        plain=render_player_text(value, default_title=default_title),
    )


def qqbot_markdown_for_event(event: Any, *, fallback: bool = False) -> bool:
    """Enable rich Markdown for verified QQ Official realtime events.

    The concrete event type and stable platform name are stronger evidence
    than the user-defined platform-instance ID.  Other transports continue to
    honor only the explicit fallback switch used by proactive delivery.
    """

    names: set[str] = set()
    for getter_name in ("get_platform_name", "get_platform_id"):
        getter = getattr(event, getter_name, None)
        if callable(getter):
            try:
                value = str(getter() or "").strip().lower()
            except Exception:
                value = ""
            if value:
                names.add(value.replace("-", "_"))
    event_type = type(event)
    names.add(str(getattr(event_type, "__name__", "")).lower())
    names.add(str(getattr(event_type, "__module__", "")).lower())
    if any(
        token in name
        for name in names
        for token in ("qq_official", "qqofficial")
    ):
        return True
    return bool(fallback)


__all__ = [
    "PlayerMessage",
    "PlayerOutput",
    "prepare_player_output",
    "render_player_message",
    "render_player_markdown",
    "render_player_text",
    "qqbot_markdown_for_event",
]
