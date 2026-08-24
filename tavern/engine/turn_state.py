from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnProcessState:
    """Explicit state shared by the five atomic turn-processing phases."""

    event: Any = None
    session_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    workflow: Mapping[str, Any] | None = None
    progress: Any = None
    item_ops: Sequence[Mapping[str, Any]] | None = None
    config: Any = None
    acting_round: int = 0
    acting_participant: Mapping[str, Any] | None = None
    player: dict[str, Any] = field(default_factory=dict)
    player_input: str = ""
    session: dict[str, Any] = field(default_factory=dict)
    world: dict[str, Any] = field(default_factory=dict)
    roster: list[Mapping[str, Any]] = field(default_factory=list)
    events: list[Mapping[str, Any]] = field(default_factory=list)
    memories: list[Mapping[str, Any]] = field(default_factory=list)
    rule_state: Mapping[str, Any] = field(default_factory=dict)
    narrative_policy: Mapping[str, Any] = field(default_factory=dict)
    opening_projection: Mapping[str, Any] | None = None
    provider_ids: list[str] = field(default_factory=list)
    generation_budget: Any = None
    operation_turn: int = 0
    turn_operation_id: str = ""
    system: str = ""
    first_prompt: str = ""
    capability_projection: list[Mapping[str, Any]] = field(default_factory=list)
    generation_notice_sent: bool = False
    resolution: Any = None
    used_provider_id: str = ""
    dice: Any = None
    check_request: Any = None
    first_mode: str = ""
    operation_id: str | None = None
    check_event: Mapping[str, Any] | None = None
    new_state: dict[str, Any] = field(default_factory=dict)
    staged_item_ops: list[dict[str, Any]] = field(default_factory=list)
    staged_economy_ops: list[dict[str, Any]] = field(default_factory=list)
    normalized_memories: tuple[Mapping[str, Any], ...] = ()
    narrative: str = ""
    narrative_document: Any = None
    quality: dict[str, Any] = field(default_factory=dict)
    check_payload: Mapping[str, Any] | None = None
    commit_workflow: Mapping[str, Any] | None = None
    updated_session: dict[str, Any] = field(default_factory=dict)


__all__ = ["TurnProcessState"]
