"""D1 WebUI / BOT 共用语义视图（纯投影）。"""

from __future__ import annotations

from .actor_fate import (
    TERMINATION_LABELS,
    project_actor_fate_view,
    project_technical_detail_view,
    project_terminal_view,
)
from .delivery_status import (
    DELIVERY_STATE_LABELS,
    DELIVERY_STATUS_MAP,
    project_delivery_status_view,
)
from .message import project_player_message_view
from .module_panel import (
    MODULE_STATE_LABELS,
    MODULE_STATES,
    project_module_panel_view,
)
from .narrative_control import (
    CONTROL_MODE_LABELS,
    CONTROL_PHASE_LABELS,
    project_narrative_control_view,
)
from .player_choice import (
    CHOICE_COMPATIBILITY_LABELS,
    project_player_choice_view,
    project_player_choice_views,
)
from .world_summary import project_world_summary_view

__all__ = [
    "CHOICE_COMPATIBILITY_LABELS",
    "CONTROL_MODE_LABELS",
    "CONTROL_PHASE_LABELS",
    "DELIVERY_STATE_LABELS",
    "DELIVERY_STATUS_MAP",
    "MODULE_STATE_LABELS",
    "MODULE_STATES",
    "TERMINATION_LABELS",
    "project_actor_fate_view",
    "project_delivery_status_view",
    "project_module_panel_view",
    "project_narrative_control_view",
    "project_player_choice_view",
    "project_player_choice_views",
    "project_player_message_view",
    "project_technical_detail_view",
    "project_terminal_view",
    "project_world_summary_view",
]

