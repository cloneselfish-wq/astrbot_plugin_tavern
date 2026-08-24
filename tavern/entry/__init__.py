"""AstrBot 平台事件入口适配层（D1_PLAN 02 D1-ARC-001 §2.2）。

本包只承担平台边界职责：把 AstrBot 事件对象转换为平台无关的
``RequestContext``。``from_astrbot_event`` 是整仓唯一允许接触平台
事件对象的适配入口；业务模块不得从其它位置读取 AstrBot 事件。
"""
from __future__ import annotations

from .event_context import from_astrbot_event

__all__ = ["from_astrbot_event"]
