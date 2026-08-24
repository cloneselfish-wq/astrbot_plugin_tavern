from __future__ import annotations

import re
from typing import Any


def _logical_blocks(text: str) -> list[str]:
    paragraphs = [
        item.strip()
        for item in re.split(r"\n{2,}", text)
        if item.strip()
    ]
    return paragraphs or ([text.strip()] if text.strip() else [])


def paginate_blocks(text: Any, maximum: int) -> list[str]:
    """Split visible text at paragraph/sentence boundaries without adding copy."""

    value = str(text or "").strip()
    limit = max(256, int(maximum or 3500))
    if not value:
        return []
    blocks = _logical_blocks(value)
    pages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pages.append(current)
            current = ""
        remaining = block
        while len(remaining) > limit:
            window = remaining[: limit + 1]
            cut = max(
                window.rfind("\n"),
                window.rfind("。"),
                window.rfind("；"),
                window.rfind("，"),
            )
            if cut < limit // 3:
                cut = limit
            else:
                cut += 1
            pages.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        current = remaining
    if current:
        pages.append(current)
    return pages


def paginate_text(
    text: Any,
    maximum: int,
    *,
    title: str = "",
) -> list[str]:
    pages = paginate_blocks(text, maximum)
    if not pages:
        return []
    if len(pages) <= 1:
        return pages
    clean_title = str(title or "").strip("【】 \n")
    if not clean_title:
        first_line = pages[0].splitlines()[0].strip()
        clean_title = (
            first_line.strip("【】")
            if first_line.startswith("【") and first_line.endswith("】")
            else "酒馆消息"
        )
    total = len(pages)
    return [
        f"【{clean_title}｜{index}/{total}】\n\n{page}"
        for index, page in enumerate(pages, start=1)
    ]
