"""玩家私聊回复解析（纯函数，无 I/O）。"""

from __future__ import annotations

import re
from typing import Any


_SEPARATORS = re.compile(r"[,，、\s]+")


def parse_supplement_reply(
    raw: str,
    *,
    max_candidates: int = 9,
) -> dict[str, Any]:
    """把玩家私聊裸回复解析为动作。

    - 纯数字（可逗号/空格分隔）→ confirm + 序号列表；
    - 「暂缓/稍后/晚点/later」→ postpone；
    - 「取消/撤销/cancel」→ cancel；
    - 「拒绝 N / 不要 N / 换 N」→ reject + 序号；裸「拒绝」→ unknown
      （需要明确目标，由仓储给出下一步命令）；
    - 其余非空文本 → text（自由文本补充内容）。
    """

    text = str(raw or "").strip()
    if not text:
        return {"action": "unknown", "text": ""}
    lowered = text.casefold()
    tokens = [token for token in _SEPARATORS.split(text) if token]
    if tokens and all(token.isdigit() for token in tokens):
        indexes = [int(token) for token in tokens]
        if any(index < 1 or index > max_candidates for index in indexes):
            return {"action": "unknown", "text": text}
        return {"action": "confirm", "indexes": indexes, "text": text}
    head = tokens[0].casefold() if tokens else ""
    for keyword in ("拒绝", "不要", "换一个", "换"):
        if lowered.startswith(keyword):
            rest = text[len(keyword):].strip(" ，、\t")
            if rest.isdigit():
                index = int(rest)
                if 1 <= index <= max_candidates:
                    return {
                        "action": "reject",
                        "indexes": [index],
                        "text": text,
                    }
                return {"action": "unknown", "text": text}
            if not rest:
                return {"action": "unknown", "text": text}
            break
    if head in {"暂缓", "稍后", "晚点", "再等等", "later"}:
        return {"action": "postpone", "text": text}
    if head in {"取消", "撤销", "cancel"}:
        return {"action": "cancel", "text": text}
    return {"action": "text", "text": text}


__all__ = ["parse_supplement_reply"]
