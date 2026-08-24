"""B/C 补充的玩家可见文案与公开安全投影（纯函数）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..lifecycle import stage_label


def offer_private_text(
    *,
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    character_name: str,
    candidate_views: Sequence[Mapping[str, Any]],
    free_text: bool = False,
    fallback: bool = False,
) -> str:
    """发送给玩家本人的私聊补充提议（含序号与命令提示）。"""

    stage = str(field.get("stage") or "B")
    label = str(field.get("label") or "")
    lines = [
        f"【角色补充 · {stage_label(template, stage)}】",
        f"「{character_name}」的{label}等待确认。",
    ]
    if fallback:
        lines.append("当前已无可选候选项，请选择：")
        for index, option in enumerate(candidate_views, start=1):
            description = str(option.get("description") or "")
            suffix = f"——{description}" if description else ""
            lines.append(f"{index}. {option.get('label') or ''}{suffix}")
    elif free_text:
        lines.append("请直接私聊回复补充内容。")
    elif candidate_views:
        lines.append("请回复序号选择：")
        for index, option in enumerate(candidate_views, start=1):
            description = str(option.get("description") or "")
            suffix = f"——{description}" if description else ""
            lines.append(f"{index}. {option.get('label') or ''}{suffix}")
        lines.append("如需更换候选项，可回复「拒绝 序号」；暂不处理请回复「暂缓」。")
    else:
        lines.append("该字段暂无可选候选项，可回复「暂缓」稍后再试。")
    return "\n".join(lines)


def offer_group_hint(*, character_name: str) -> str:
    """无法主动私聊时的群聊提示（不含候选与秘密内容）。"""

    return (
        f"「{character_name}」有一项新的私密角色补充等待确认。\n"
        "请本人私聊 BOT 发送：\n"
        "/团 当前"
    )


def confirm_group_projection(
    *,
    template: Mapping[str, Any],
    field: Mapping[str, Any],
    character_name: str,
) -> str:
    """确认后的群聊公开投影：绝不包含候选名、秘密内容或字段内部键。"""

    name = str(character_name or "角色")
    override = str(field.get("public_note") or field.get("public_note_template") or "")
    if override:
        return override.format(name=name, character_name=name)
    role = str(field.get("semantic_role") or "").casefold()
    if "contact" in role:
        return f"「{name}」在剧情中与一位联系人建立了联系。"
    if any(token in role for token in ("rival", "enemy", "pursuer", "nemesis")):
        return f"「{name}」在剧情中与一位敌手或追踪者有了交集。"
    if bool(field.get("private")) or "secret" in role:
        return f"「{name}」似乎认出了某物。"
    return f"「{name}」的一项角色补充已确认。"


def supplement_list_line(
    *,
    stage: str,
    field_label: str,
    state: str,
    expired: bool = False,
) -> str:
    """「/团 当前」列表中的单行说明（玩家可见）。"""

    state_labels = {
        "offered": "等待确认",
        "postponed": "已暂缓",
    }
    if expired:
        state_text = "已过期"
    else:
        state_text = state_labels.get(state, "等待确认")
    return f"· {field_label}（{state_text}）"


__all__ = [
    "confirm_group_projection",
    "offer_group_hint",
    "offer_private_text",
    "supplement_list_line",
]
