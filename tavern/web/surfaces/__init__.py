"""Web surface domain API."""

from .registry import (
    RouteResult,
    SurfaceLoader,
    _QUERY_FIELDS,
    _OBJECT_KINDS_BY_WORKSPACE,
    _MAX_OBJECT_KEYS,
    _OBJECT_KEYS,
    _INTERNAL_REF,
    _UNSAFE_TEXT,
    _mapping,
    _sequence,
    _text,
    _public_text,
    _integer,
    _TIME_FILTER_OPTIONS,
    _timestamp,
    _matches_time_filter,
    _opaque_filter_value,
    _resolve_filter_value,
    _principal_roles,
    _principal_scope,
    _remember_object_key,
    resolve_surface_key,
    issue_surface_key,
    SurfaceProjection,
    SurfaceContext,
    SurfaceSpec,
    _service,
    _maybe_await,
    _service_value,
    _route_body,
    WebRouteAdapterError,
    _problem_from_adapter,
    _pagination,
    _session_state,
    _job_state,
    _delivery_state,
    _safe_label,
    _available_action,
    health_component_revision,
    _project_session,
    _visible_session_page,
    _session_world_ref,
    _session_world_label,
    _session_group_ref,
    _session_group_label,
    _collect_visible_session_rows,
    _resolve_session_context,
    _resolve_world_context,
)

from .dashboard import (
    _dashboard_surface,
    _tendencies_surface,
    _sessions_surface,
)

from .runtime import (
    _stage_label,
    _character_status,
    _characters_surface,
    _memory_scope,
    _memory_importance,
    _memory_governance,
    _collect_visible_memories,
    _memories_surface,
    _WORLD_CAPABILITY_LABELS,
)

from .worlds import (
    _world_author,
    _world_capability_entries,
    _project_world,
    _worlds_surface,
    _designer_issue_counts,
    _designer_flow_projection,
    _designer_surface,
)

from .designer_matrix import designer_matrix_projection

from .operations import (
    _author_jobs_surface,
    _todo_surface,
    _AUDIT_ACTIONS,
    _audit_surface,
)
from .author_jobs_support import _job_type

from .health import _health_surface

from .health_support import health_state, health_summary
from .settings_contract import SETTING_ACTION_FIELDS as _SETTING_ACTION_FIELDS

from .system import (
    _SETTING_GROUP_LABELS,
    _SETTING_FIELDS,
    _path_value,
    _setting_value,
    _settings_group_projection,
    _settings_surface,
    _module_layer,
    _modules_surface,
    _about_surface,
    _SURFACE_SPECS,
    SURFACE_ROUTES,
    _error_response,
    route_surface_view,
)
