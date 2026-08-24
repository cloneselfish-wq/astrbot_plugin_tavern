"""命令应用层使用的纯文本投影，不依赖 AstrBot。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def format_turn_status(turn: Mapping[str, Any]) -> str:
    order = turn.get("order")
    if not isinstance(order, list) or not order:
        return "【回合顺序】尚无玩家，请先发送 /团 加入。"
    current_id = str(turn.get("current_user_id") or "")
    lines: list[str] = []
    for item in order:
        if not isinstance(item, Mapping):
            continue
        marker = "▶" if str(item.get("user_id") or "") == current_id else "·"
        name = (
            item.get("name")
            or item.get("character_name")
            or item.get("display_name")
            or "角色资料缺失，请联系主持人"
        )
        lines.append(f"{marker} {item.get('position', '?')}. {name}")
    return f"【回合顺序】第 {turn.get('round_no', 1)} 轮\n" + "\n".join(lines)


def format_roster(roster: list[Mapping[str, Any]]) -> str:
    if not roster:
        return "【当前阵容】尚无玩家加入。"
    card_labels = {
        "uncreated": "未建卡",
        "draft": "建卡中",
        "pending_review": "待审核",
        "approved": "已通过",
        "rejected": "未通过",
    }
    participation_labels = {
        "reserved": "占位",
        "active": "出场",
        "standby": "候补",
        "away": "暂离",
        "retired": "已退场",
        "archived": "已归档",
    }
    lines = ["【当前阵容】"]
    for item in roster:
        name = (
            item.get("character_name")
            or item.get("display_name")
            or "角色资料缺失，请联系主持人"
        )
        ready = "已准备" if item.get("ready") else "未准备"
        lines.append(
            f"· 「{name}」"
            f" · {card_labels.get(item.get('card_status'), '状态异常')}"
            f" · {ready}"
            f" · {participation_labels.get(item.get('participation_status'), '状态异常')}"
        )
    return "\n".join(lines)


def format_vote(vote: Mapping[str, Any]) -> str:
    lines = [
        f"【集体决策 · 第 {vote.get('stage', 1)} 轮】",
        str(vote.get("question") or ""),
    ]
    lines.extend(
        f"{item.get('key')}. {item.get('text')}"
        for item in vote.get("options", [])
        if isinstance(item, Mapping)
    )
    lines.extend(
        [
            "",
            f"有效成员：{len(vote.get('eligible_user_ids', []))} 人",
            f"截止：{vote.get('deadline_at') or '不限时'}",
            "发送：/团 投票 A",
        ]
    )
    return "\n".join(lines)


__all__ = ["format_roster", "format_turn_status", "format_vote"]
