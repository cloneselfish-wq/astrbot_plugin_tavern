"""console lazy session visual routes.

All five operations return a ``VisualEnvelope`` for success and failure.  They
authorize at the repository boundary, rotate opaque keys by principal/session,
and never return the session id used to perform the lookup.
"""


from __future__ import annotations


import asyncio


import functools


import hashlib


import inspect


import json


from collections.abc import Mapping


from typing import Any, Callable


from ...session_events import CATEGORY_LABELS, project_session_event


from ...visualization import (
    OpaqueKeyFactory,
    VisualProblem,
    build_session_generation,
    build_session_history,
    build_session_party,
    build_session_summary,
    build_session_world_visuals,
    visual_envelope,
)


from ...visualization.common import display_label


from ...visualization.realtime import visual_kinds_for_event


from ...visualization.envelopes import problem_from_exception


from ...repositories.timers_support import timer_revision


from ..errors import bad_request, not_found


from . import WebRouteError, mapping, require_login, text, to_int


from .sessions import (
    require_member,
    resolve_viewer_participant,
)


from ..surfaces.registry import issue_surface_key, resolve_surface_key


_HISTORY_SCAN_LIMIT = 500


COMMON_DECLARED_EXPORTS = [
    "session_generation_view",
    "session_history_view",
    "session_party_view",
    "session_summary_view",
    "session_world_visuals_view",
]



__all__ = [name for name in globals() if not name.startswith('__')]

