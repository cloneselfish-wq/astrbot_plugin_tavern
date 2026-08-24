"""Deterministic BOT rendering for the shared command contracts."""

from __future__ import annotations

import logging

from .messaging.player import PlayerMessage, render_player_message, render_player_text
from .runtime.contracts import CommandResult


logger = logging.getLogger(__name__)


def render_bot_result(result: CommandResult) -> str | None:
    if not result.handled or result.status == "ignored":
        return None
    if result.error is not None:
        error = result.error
        if error.correlation_id and error.status_code >= 500:
            logger.error(
                "Tavern BOT command failed: correlation_id=%s status=%s code=%s",
                error.correlation_id,
                error.status_code,
                error.code,
            )
        return render_player_message(
            PlayerMessage.dynamic(
                title=f"{error.operation}失败",
                summary=f"失败操作：{error.operation}。",
                sections=(
                    f"原因：{error.reason}",
                    f"自动处理：{error.automatic_action}",
                ),
                actions=(error.next_command or "/团 帮助",),
                audience=error.audience,
            )
        )
    if isinstance(result.player_message, PlayerMessage):
        return render_player_message(result.player_message)
    return render_player_text(
        result.message or result.text,
        default_title="操作已完成" if result.ok else "操作未完成",
    )


__all__ = ["render_bot_result"]
