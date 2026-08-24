"""权威世界命令层。

为 TWP 世界运行态提供权威领域命令，替代“模型直接
改状态”。每个命令包含：幂等键、预期会话修订、操作者、权限、稳定目标 ID、
原因、可见性、结果摘要与派生事件。模型只能提出意图，不能覆盖整个运行态。

本模块为纯函数层（不直接访问数据库），由 repositories/world_commands.py 的
WorldCommandRepository 负责读取/提交会话状态与审计。命令只允许修改 runtime
中的白名单字段，其余运行态不可触碰。
"""


from __future__ import annotations


from collections.abc import Mapping, Sequence


from copy import deepcopy


from typing import Any


from ...story_context import recommended_transition, scene_transition_blockers


COMMAND_DOMAINS = frozenset(
    {
        "scene", "quest", "knowledge", "npc", "faction", "clock",
        "challenge", "progression", "crafting", "handout",
        # B2（A2）：节点状态、联盟签署、加冕与结局结算。
        "node", "alliance", "crown", "ending",
    }
)


EVENT_MAX_DEPTH = 6


EVENT_MAX_TRIGGERS = 32


RUNTIME_EVENT_LOG_LIMIT = 80


_DOMAIN_ACTIONS: dict[str, list[str]] = {
    "scene": ["transition", "set_exit", "record_visit"],
    "quest": ["accept", "complete_objective", "fail", "settle", "set_visible"],
    "knowledge": ["reveal", "correct", "discover"],
    "npc": ["move", "set_intent", "leave", "die", "set_faction"],
    "faction": ["set_stance", "adjust_resource", "advance_clock", "set_control"],
    "clock": ["advance", "read"],
    "challenge": ["start", "advance_phase", "end", "victory", "defeat", "retreat"],
    "progression": ["advance", "unlock_milestone"],
    "crafting": ["start", "resolve", "abort"],
    "handout": ["unlock", "send", "resend"],
    "node": ["set_status"],
    "alliance": ["sign", "withdraw"],
    "crown": ["nominate", "recognize", "accept_price"],
    "ending": ["commit"],
}


_DOMAIN_ACTION_LABELS: dict[str, str] = {
    "scene.transition": "转场",
    "scene.set_exit": "开放/封锁出口",
    "scene.record_visit": "记录回访",
    "quest.accept": "接取任务",
    "quest.complete_objective": "完成目标",
    "quest.fail": "失败任务",
    "quest.settle": "结算任务",
    "quest.set_visible": "显示/隐藏任务",
    "knowledge.reveal": "揭示事实",
    "knowledge.correct": "纠正误解",
    "knowledge.discover": "定向获知",
    "npc.move": "移动 NPC",
    "npc.set_intent": "更新 NPC 意图",
    "npc.leave": "NPC 离场",
    "npc.die": "NPC 死亡",
    "npc.set_faction": "NPC 阵营变化",
    "faction.set_stance": "调整立场",
    "faction.adjust_resource": "调整资源",
    "faction.advance_clock": "推进时钟",
    "faction.set_control": "地区控制",
    "challenge.start": "开始遭遇",
    "challenge.advance_phase": "推进阶段",
    "challenge.end": "结束遭遇",
    "challenge.victory": "遭遇胜利",
    "challenge.defeat": "遭遇失败",
    "challenge.retreat": "遭遇撤退",
    "progression.advance": "推进成长轨迹",
    "progression.unlock_milestone": "解锁里程碑",
    "crafting.start": "开始制作",
    "crafting.resolve": "制作结算",
    "crafting.abort": "中止制作",
    "handout.unlock": "解锁手册",
    "handout.send": "发送手册",
    "handout.resend": "重新发送手册",
    "node.set_status": "设置节点状态",
    "alliance.sign": "阵营签署联盟",
    "alliance.withdraw": "阵营退出联盟",
    "crown.nominate": "确定加冕候选人",
    "crown.recognize": "节点认可加冕",
    "crown.accept_price": "承载者承认代价",
    "ending.commit": "结算结局",
}


COMMON_DECLARED_EXPORTS = [
    "COMMAND_DOMAINS",
    "EVENT_MAX_DEPTH",
    "EVENT_MAX_TRIGGERS",
    "WorldCommandError",
    "apply_command",
    "list_commands",
    "preview_command",
    "validate_command",
]



__all__ = [name for name in globals() if not name.startswith('__')]

