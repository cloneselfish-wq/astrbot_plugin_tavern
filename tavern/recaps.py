from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .copy.entities import decorate_entity


def _lines(events: Sequence[Mapping[str, Any]], limit: int) -> list[str]:
    return [
        f"· {str(item.get('content') or '').strip()}"
        for item in events
        if item.get("role") in {"narrator", "system"}
        and str(item.get("content") or "").strip()
    ][-limit:]


async def build_recap(
    database: Any,
    session: Mapping[str, Any],
    user_id: str,
    scope: str = "",
) -> str:
    scope = str(scope or "").strip()
    instance_name = str(session.get("instance_name") or "").strip()
    if not instance_name:
        return (
            "【故事回顾生成失败】\n"
            "操作：生成当前副本的故事回顾。\n"
            "原因：当前副本缺少可公开显示的名称。\n"
            "自动处理：系统已中止展示，未输出内部标识或不完整记录。\n"
            "下一步：请联系主持人修复副本资料后重试。\n\n"
            "/团 回顾"
        )
    event_limit = {
        "最近一轮": 4,
        "最近一章": 30,
        "请假摘要": 50,
    }.get(scope, 16)
    events = await database.recent_events(str(session["id"]), event_limit)
    title = (
        f"【故事回顾 · {scope or '当前局势'}】"
        f"{decorate_entity('story', instance_name)}"
    )
    if scope in {"任务线索", "当前任务与线索"}:
        ledger = await database.list_story_ledger(str(session["id"]))
        active = [
            item
            for item in ledger
            if str(item.get("status") or "") not in {"completed", "failed", "archived"}
        ]
        body = []
        for item in active[:12]:
            item_title = str(item.get("title") or "").strip()
            if not item_title:
                body.append(
                    "· 线索资料读取失败：一条进行中的线索缺少公开名称；"
                    "系统未显示内部标识。"
                )
                continue
            body.append(
                f"· {decorate_entity('quest', item_title)}："
                f"{item.get('description') or '暂无说明'}"
            )
    elif scope in {"NPC关系", "人物关系", "角色关系"}:
        characters = await database.list_session_characters(
            str(session["id"]), include_archived=False
        )
        body = []
        for item in characters[:15]:
            name = str(item.get("name") or "").strip()
            if not name:
                body.append(
                    "· 角色资料读取失败：一名 NPC 缺少公开名称；"
                    "系统未显示内部标识，请联系主持人检查角色资料。"
                )
                continue
            public_profile = item.get("public_profile")
            public_profile = (
                public_profile if isinstance(public_profile, Mapping) else {}
            )
            body.append(
                f"· {decorate_entity('npc', name)}："
                f"{public_profile.get('summary') or item.get('role_type') or '关系待确认'}"
            )
    elif scope in {"我的经历", "我的角色经历"}:
        roster = await database.list_roster(str(session["id"]))
        me = next(
            (x for x in roster if str(x.get("group_user_id") or "") == str(user_id)),
            None,
        )
        name = (
            (me or {}).get("character_name")
            or (me or {}).get("display_name")
            or "你的角色"
        )
        related = [
            item
            for item in events
            if name in str(item.get("content") or "")
            or str(item.get("actor_id") or "") == str(user_id)
        ]
        body = _lines(related or events, 10)
    else:
        body = _lines(events, 12 if scope == "请假摘要" else 6)
    state = session.get("world_state") or {}
    header = [
        title,
        "地点："
        + (
            decorate_entity("location", state.get("location"))
            if state.get("location")
            else "未记录"
        ),
        f"局势：{state.get('scene_summary', '暂无')}",
        "",
    ]
    return "\n".join(header + (body or ["暂无对应记录。"]))
