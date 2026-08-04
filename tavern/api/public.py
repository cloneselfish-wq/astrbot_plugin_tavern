from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..diagnostics import build_diagnostic_report, redact
from ..presets import (
    check_character_knowledge,
    check_content_permission,
    explain_boundary_sources,
    normalize_preset_dimensions,
    resolve_character_presets,
)


class TavernPublicAPI:
    """Supported service facade; extensions never receive raw SQL access."""

    def __init__(self, database: Any, hooks: Any, extensions: Any, engine: Any = None) -> None:
        self._database = database
        self.hooks = hooks
        self.extensions = extensions
        self._engine = engine

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

    async def resolve_action_intent(
        self,
        session_id: str,
        intent: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = True,
        operation_id: str = "",
        actor_id: str = "",
    ) -> dict[str, Any]:
        """Validate/simulate or atomically commit a protocol-v5 action."""

        return await self._database.resolve_action_intent(
            session_id,
            intent,
            context,
            dry_run=dry_run,
            operation_id=operation_id,
            actor_id=actor_id,
        )

    async def get_resolution_receipt(
        self, receipt_id: str, *, public_only: bool = True
    ) -> dict[str, Any]:
        return await self._database.get_resolution_receipt(
            receipt_id, public_only=public_only
        )

    async def get_control_state(self, session_id: str) -> dict[str, Any]:
        return await self._database.get_control_state(session_id)

    async def enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.enable_dm_mode(session_id, dm_user_id, actor_id)

    async def set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.set_dm_directive(session_id, directive, actor_id)

    async def generate_dm_beat(
        self, event: Any, session_id: str, instruction: str, actor_id: str
    ) -> dict[str, Any]:
        if self._engine is None:
            raise RuntimeError("当前运行时没有连接叙事引擎")
        return await self._engine.process_dm_beat(
            event=event, session_id=session_id,
            dm_user_id=actor_id, instruction=instruction,
        )

    async def append_dm_narrative(
        self, session_id: str, narrative: str, actor_id: str
    ) -> dict[str, Any]:
        session = await self._database.get_session(session_id)
        return await self._database.commit_dm_beat(
            session_id=session_id,
            expected_revision=int(session["revision"]),
            dm_user_id=actor_id,
            instruction=narrative,
            narrative=narrative,
            world_state=session["world_state"],
            direct=True,
        )

    async def handoff_to_actor(
        self, session_id: str, actor_type: str, actor_ref: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.set_dm_handoff(
            session_id, actor_type, actor_ref, actor_id
        )

    async def return_to_auto(
        self, session_id: str, participant_ref: str, actor_id: str
    ) -> dict[str, Any]:
        participant = await self._database.get_participant(
            session_id, participant_ref=participant_ref
        )
        turn = await self._database.designate_turn(
            session_id, participant["group_user_id"], actor_id
        )
        await self._database.disable_dm_mode(session_id, actor_id)
        return {"control": await self._database.get_control_state(session_id), "turn": turn}

    async def takeover_dm(
        self, session_id: str, new_dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._database.enable_dm_mode(
            session_id, new_dm_user_id, actor_id
        )

    @staticmethod
    def list_preset_dimensions(world: Mapping[str, Any]) -> list[dict[str, Any]]:
        rules = world.get("rules") if isinstance(world, Mapping) else {}
        rules = rules if isinstance(rules, Mapping) else {}
        card = rules.get("character_card")
        return normalize_preset_dimensions(
            card if isinstance(card, Mapping) else {}
        )

    @staticmethod
    def resolve_character_presets(
        world: Mapping[str, Any], selections: Mapping[str, Any]
    ) -> dict[str, Any]:
        return resolve_character_presets(world, selections)

    @staticmethod
    def explain_boundary_sources(
        world: Mapping[str, Any], selections: Mapping[str, Any]
    ) -> dict[str, Any]:
        return explain_boundary_sources(world, selections)

    @staticmethod
    def check_character_knowledge(
        resolved: Mapping[str, Any], domain: str
    ) -> dict[str, Any]:
        return check_character_knowledge(resolved, domain)

    @staticmethod
    def check_content_permission(
        resolved: Mapping[str, Any], content_tags: Any
    ) -> dict[str, Any]:
        return check_content_permission(resolved, content_tags)

    async def export_diagnostic(
        self,
        session_id: str,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        report = await build_diagnostic_report(self._database, session_id)
        report["extension_context"] = redact(dict(context))
        return report
