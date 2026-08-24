from .common import *
from .sessions import *
from .characters import *
from .reviews import *
from .story import *

def _format_remaining_time(value: Any) -> str:
    try:
        remaining = max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        remaining = 0
    days, remaining = divmod(remaining, 24 * 60 * 60)
    hours, remaining = divmod(remaining, 60 * 60)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts)


def _story_reply_parts(value: str) -> list[str]:
    text = str(value or "").strip()
    marker = "【回合秩序】"
    idx = text.find(marker)
    if idx == -1:
        return [text] if text else []
    # 兼容标记前可能带有的 emoji 前缀（如 ⚔️ ），整段作为回合内容保留
    prefix_start = idx
    while prefix_start > 0 and text[prefix_start - 1] not in "\n\r":
        prefix_start -= 1
    story = text[:prefix_start].strip()
    turn = text[prefix_start:].strip()
    if not story:
        return [turn]
    return [story, turn]


__all__ = [name for name in globals() if not name.startswith('__')]

