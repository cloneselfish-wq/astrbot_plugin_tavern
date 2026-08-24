from __future__ import annotations

from .assets import AssetOperationMixin
from .core import EngineCoreMixin
from .directing import DirectingMixin
from .errors import (
    EngineReply,
    ProgressCallback,
    ProgressPayload,
    TavernBusyError,
    TavernEngineError,
    TavernOperationCancelled,
    TavernPlayerDisabledError,
    TavernStoryGenerationError,
    TavernTurnOrderError,
    story_generation_failure_message,
)
from .generation import GenerationMixin
from .participation import ParticipationMixin
from .turn_commit import TurnCommitMixin
from .turn_context import TurnContextMixin
from .turn_delivery import TurnDeliveryMixin
from .turn_generation import TurnGenerationMixin
from .turn_orchestrator import TurnOrchestratorMixin
from .turn_validation import TurnValidationMixin
from .voting import VotingMixin


class TavernEngine(
    TurnOrchestratorMixin,
    TurnContextMixin,
    TurnGenerationMixin,
    TurnValidationMixin,
    TurnCommitMixin,
    TurnDeliveryMixin,
    DirectingMixin,
    VotingMixin,
    ParticipationMixin,
    AssetOperationMixin,
    GenerationMixin,
    EngineCoreMixin,
):
    """Tavern runtime engine composed from stable domain responsibilities."""


__all__ = [
    "EngineReply",
    "ProgressCallback",
    "ProgressPayload",
    "TavernBusyError",
    "TavernEngine",
    "TavernEngineError",
    "TavernOperationCancelled",
    "TavernPlayerDisabledError",
    "TavernStoryGenerationError",
    "TavernTurnOrderError",
    "story_generation_failure_message",
]
