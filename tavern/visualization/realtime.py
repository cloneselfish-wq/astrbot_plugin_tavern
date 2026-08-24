"""Safe visual-kind projection for SSE/invalidation consumers.

This module does not publish events by itself.  It is the single mapping used
by the console API and SSE wiring, including AI teammate and inventory changes.
Unknown broker events fail closed to a scoped full refresh instead of exposing
their raw type or payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import integer, mapping, text
from .keys import OpaqueKeyFactory


def visual_kinds_for_event(event: Mapping[str, Any] | None) -> tuple[str, ...]:
    source = mapping(event)
    affected = source.get("affected_modules")
    if isinstance(affected, Sequence) and not isinstance(affected, (str, bytes)):
        affected_tokens = " ".join(text(item, limit=80) for item in affected)
    else:
        affected_tokens = text(affected, limit=160)
    tokens = " ".join(
        [
            text(source.get("type"), limit=100),
            text(source.get("action"), limit=100),
            text(source.get("category"), limit=100),
            affected_tokens,
        ]
    ).lower()
    kinds: list[str] = []

    def add(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    if any(
        token in tokens
        for token in (
            "ai_companion",
            "ai-companion",
            "ai_actor",
            "event:ai.",
        )
    ):
        add("party")
    if any(
        token in tokens
        for token in (
            "inventory",
            "item_instance",
            "items.",
            "event:item.",
            "resource",
        )
    ):
        add("party")
    if any(
        token in tokens
        for token in (
            "turn",
            "actor_fate",
            "actor_state",
            "actor.state",
            "participant",
            "party",
        )
    ):
        add("party")
    if "quest" in tokens:
        add("quest_tracks")
    if "clock" in tokens or "timer" in tokens:
        add("clocks")
    if "relation" in tokens:
        add("relations")
    if "challenge_engine" in tokens or "tactical_conflict" in tokens:
        add("world_visuals")
    if "scene" in tokens or "world" in tokens:
        add("scene_path")
    if "delivery" in tokens:
        add("deliveries")
    if any(token in tokens for token in ("generation", "operation", "quality", "repair")):
        add("generation")
    if "story" in tokens or "choice" in tokens or "vote" in tokens:
        add("session_summary")
    if any(token in tokens for token in ("story", "archive", "terminal", "session")):
        add("history")
    return tuple(kinds or ("session_summary",))


def _summary_for_kind(kind: str) -> str:
    return {
        "party": "小队状态已更新",
        "quest_tracks": "任务状态已更新",
        "clocks": "场景时钟已更新",
        "relations": "关系状态已更新",
        "world_visuals": "世界玩法状态已更新",
        "scene_path": "场景路径已更新",
        "deliveries": "投递状态已更新",
        "generation": "故事生成阶段已更新",
        "history": "副本时间线已更新",
        "session_summary": "跑团现场摘要已更新",
    }.get(kind, "跑团现场数据已更新")


def project_visual_events(
    event: Mapping[str, Any] | None,
    *,
    session_identity: object,
    keys: OpaqueKeyFactory,
) -> list[dict[str, Any]]:
    source = mapping(event)
    sequence = integer(source.get("sequence", source.get("seq")), 0)
    revision: int | str | None = source.get(
        "revision", source.get("session_revision")
    )
    structural = text(source.get("category"), limit=40) in {
        "archive",
        "terminal",
    }
    object_key = keys.key("session", session_identity)
    return [
        {
            "sequence": sequence,
            "scope": "session",
            "object_key": object_key,
            "kind": kind,
            "revision": revision,
            "mode": "refresh" if structural else "invalidate",
            "full_refresh": structural,
            "summary": _summary_for_kind(kind),
        }
        for kind in visual_kinds_for_event(source)
    ]


__all__ = ["project_visual_events", "visual_kinds_for_event"]
