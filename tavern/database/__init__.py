"""SQLite facade composed from bounded transactional responsibilities."""

from ..database_support import *
from ..repositories import RepositoryFacade
from .core import DatabaseCoreMixin
from .events import EventProjectionMixin
from .actor_state import ActorStateMixin
from .finalization import RescueFinalizationMixin
from .schema import SchemaMixin
from .maintenance import DatabaseMaintenanceMixin
from .rows import RowProjectionMixin


class TavernDatabase(
    DatabaseCoreMixin,
    EventProjectionMixin,
    ActorStateMixin,
    RescueFinalizationMixin,
    SchemaMixin,
    DatabaseMaintenanceMixin,
    RowProjectionMixin,
    RepositoryFacade,
):
    """Schema 29 database with one facade and explicit domain mixins."""


__all__ = [
    "TavernDatabase",
    "DatabaseConflictError",
    "DatabaseNotFoundError",
    "InvalidTransitionError",
]
