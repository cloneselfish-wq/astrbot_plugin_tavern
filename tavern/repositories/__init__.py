"""Repository mixins grouped by persistent domain."""

from .current_state import CurrentStateRepositoryMixin
from .worlds import WorldRepositoryMixin
from .sessions import SessionRepositoryMixin
from .story import StoryRepositoryMixin
from .rules import RuleRepositoryMixin
from .characters import CharacterRepositoryMixin
from .workflow import WorkflowRepositoryMixin
from .timers import TimerRepositoryMixin
from .admin import AdminRepositoryMixin

__all__ = [
    "CurrentStateRepositoryMixin",
    "WorldRepositoryMixin",
    "SessionRepositoryMixin",
    "StoryRepositoryMixin",
    "RuleRepositoryMixin",
    "CharacterRepositoryMixin",
    "WorkflowRepositoryMixin",
    "TimerRepositoryMixin",
    "AdminRepositoryMixin",
]
