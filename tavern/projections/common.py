"""跑团现场聚合（v1.0-A2）。

对单个副本做只读聚合，供控制台「跑团现场」与「时间线回放」视图
消费。全部数据来自既有仓库只读接口，不产生写入；字段提取保持防御式
（``.get`` + 类型归一），避免把底层表结构的偶发变化泄漏给前端。

对外函数：
- ``dashboard_sessions(database)``：副本概览列表（含状态/世界/当前行动者）。
- ``session_dashboard(database, session_id)``：单个副本的实时聚合。
- ``session_timeline(database, session_id, limit)``：事件时间线（回放视图）。
"""


from __future__ import annotations


import re


from typing import Any, Mapping


from ..constants import SESSION_RUNNING


from ..projections.character import project_actor_view


from ..projections.delivery import (
    project_actor_fate_summary,
    project_terminal_report_view,
    project_terminal_view,
)


from ..projections.session import (
    project_module_panels,
    project_narrative_control_view,
    world_module_declared,
    world_module_summary,
)


from ..projections.world import (
    project_resource_view,
    project_story_view,
    project_world_state_view,
    world_has_capability,
)


from ..market_projection import project_market_view


from ..story_context import recommend_opening_scenarios


from ..world_contract import world_contract


_INTERNAL_DISPLAY_REF = re.compile(r"^[a-z0-9][a-z0-9_.:/-]*$", re.IGNORECASE)



__all__ = [name for name in globals() if not name.startswith('__')]

