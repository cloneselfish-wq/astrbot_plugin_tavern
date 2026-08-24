from __future__ import annotations

import re
from typing import Any

from .document import MessageDocument


_OPTION_LINE = re.compile(r"^(?:\d+|[A-D])[\.\、]\s*")
_COMMAND_LINE = re.compile(r"^/团(?:\s|$)")


def mobile_format_text(text: Any) -> str:
    raw_lines = str(text or "").replace("\r\n", "\n").split("\n")
    output: list[str] = []
    option_block_started = False
    for raw in raw_lines:
        line = raw.rstrip()
        stripped = line.strip()
        is_option = bool(_OPTION_LINE.match(stripped))
        is_title = stripped.startswith("【") and stripped.endswith("】")
        is_command = bool(_COMMAND_LINE.match(stripped))
        if (
            stripped
            and output
            and output[-1].strip()
            and (
                (is_option and option_block_started)
                or is_title
                or is_command
            )
        ):
            output.append("")
        output.append(line)
        if is_title and stripped:
            output.append("")
            option_block_started = False
        elif is_option:
            option_block_started = True
    compact: list[str] = []
    blank_count = 0
    for line in output:
        if line.strip():
            compact.append(line)
            blank_count = 0
        else:
            blank_count += 1
            if blank_count <= 1:
                compact.append("")
    return "\n".join(compact).strip()


def render_message(document: MessageDocument | Any) -> str:
    if not isinstance(document, MessageDocument):
        document = MessageDocument.from_text(document)
    parts: list[str] = []
    if document.title:
        parts.append(f"【{document.title.strip('【】')}】")
    for section in document.sections:
        if section.body:
            parts.append(section.body)
        if section.items:
            parts.append("\n\n".join(section.items))
    if document.actions:
        parts.append("\n\n".join(document.actions))
    return mobile_format_text("\n\n".join(item for item in parts if item))
