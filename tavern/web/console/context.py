from __future__ import annotations

import json

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    is_standalone_upload,
    json_response,
    request,
    stream_response,
)
from ..query import QueryAdapter
from ..routes.narrative_mode import (
    narrative_mode_view as route_narrative_mode_view,
)
from ..intents.dispatcher import execute_intent
from ..routes.event_stream import open_event_stream
from ..surfaces.registry import resolve_surface_key


from .context_identity import ContextIdentityMixin
from .context_sessions import ContextSessionsMixin

class ConsoleContextMethods(ContextIdentityMixin, ContextSessionsMixin):
    pass
