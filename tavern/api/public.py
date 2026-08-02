from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..diagnostics import build_diagnostic_report, redact


class TavernPublicAPI:
    """Supported service facade; extensions never receive raw SQL access."""

    def __init__(self, database: Any, hooks: Any, extensions: Any) -> None:
        self._database = database
        self.hooks = hooks
        self.extensions = extensions

    async def get_current_session(
        self,
        platform_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        return await self._database.get_session_by_group(
            platform_id,
            group_id,
        )

    async def get_turn_context(self, session_id: str) -> dict[str, Any]:
        return await self._database.get_turn_status(session_id)

    async def get_active_character(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._database.get_participant(
            session_id,
            user_id=user_id,
        )

    async def create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._database.create_snapshot(
            session_id,
            name,
            actor_id,
        )

    async def append_story_event(
        self,
        session_id: str,
        text: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._database.emergency_append_narrative(
            session_id,
            text,
            actor_id,
        )

    async def export_diagnostic(
        self,
        session_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        report = await build_diagnostic_report(self._database, session_id)
        report["extension_context"] = redact(dict(context))
        return report
