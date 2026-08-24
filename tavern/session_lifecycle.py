"""console lifecycle capabilities and player-facing action metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .constants import (
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)


LIFECYCLE_ACTIONS = frozenset({"close", "reopen", "finish", "abort"})


def lifecycle_capabilities(
    session: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    *,
    authorized: bool,
) -> dict[str, Any]:
    """Project one authoritative capability DTO for list and detail views."""

    state = str(session.get("state") or "")
    readonly = bool(session.get("readonly")) or state == SESSION_FINISHED
    turn_no = int(session.get("turn_no", 0) or 0)
    reason = "" if authorized else "当前登录身份没有副本生命周期管理权限"
    capabilities = {
        "can_close": bool(
            authorized
            and not readonly
            and state
            in {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_MAINTENANCE,
            }
        ),
        "can_reopen": bool(
            authorized and not readonly and state == SESSION_CLOSED
        ),
        "can_finish": bool(
            authorized
            and not readonly
            and state in {SESSION_RUNNING, SESSION_PAUSED}
            and turn_no > 0
        ),
        "can_abort": bool(
            authorized
            and not readonly
            and state
            in {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_MAINTENANCE,
                SESSION_CLOSED,
            }
        ),
    }
    blockers: dict[str, str] = {}
    if reason:
        blockers = {key: reason for key in capabilities}
    elif state in {SESSION_RUNNING, SESSION_PAUSED} and turn_no <= 0:
        blockers["can_finish"] = "故事尚未开演；如需结束本轮，请使用“放弃本轮”"
    elif state == SESSION_FINISHED or readonly:
        blockers = {
            key: "副本已经永久归档，只能查看或从存档克隆"
            for key in capabilities
        }
    capabilities["blockers"] = blockers
    return {
        "capabilities": capabilities,
        "lifecycle_context": {
            "active_card_drafts": int(
                (context or {}).get("active_card_drafts", 0) or 0
            ),
            "suspended_card_drafts": int(
                (context or {}).get("suspended_card_drafts", 0) or 0
            ),
            "active_participants": int(
                (context or {}).get("active_participants", 0) or 0
            ),
            "story_started": turn_no > 0,
            "turn_no": turn_no,
            "pending_votes": int(
                (context or {}).get("pending_votes", 0) or 0
            ),
            "pending_choices": int(
                (context or {}).get("pending_choices", 0) or 0
            ),
            "pending_timers": int(
                (context or {}).get("pending_timers", 0) or 0
            ),
            "pending_operations": int(
                (context or {}).get("pending_operations", 0) or 0
            ),
            "temporary_grants": int(
                (context or {}).get("temporary_grants", 0) or 0
            ),
        },
    }


def lifecycle_action_label(action: str) -> str:
    return {
        "close": "关闭",
        "reopen": "重新开放",
        "finish": "完结故事",
        "abort": "放弃本轮",
    }.get(str(action or ""), "处理副本")


__all__ = [
    "LIFECYCLE_ACTIONS",
    "lifecycle_action_label",
    "lifecycle_capabilities",
]

