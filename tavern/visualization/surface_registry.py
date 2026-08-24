"""Closed RC10 surface component/data/placement registry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


REGISTRY_VERSION = "1.0.0-rc10"

COMPONENT_REGISTRY: dict[str, dict[str, Any]] = {
    "world_overview": {"data_kind": "world_overview_view", "placements": ("world_detail",), "mobile": "stacked_cards"},
    "content_inventory": {"data_kind": "content_inventory_view", "placements": ("world_detail",), "mobile": "semantic_table"},
    "readme_reader": {"data_kind": "readme_section_view", "placements": ("world_detail",), "mobile": "stacked_cards"},
    "character_build_catalog": {"data_kind": "character_build_view", "placements": ("world_detail", "character_creation"), "mobile": "stacked_cards"},
    "quest_board": {"data_kind": "quest_track_view", "placements": ("world_detail", "live_session", "session_detail"), "mobile": "ordered_list"},
    "challenge_board": {"data_kind": "challenge_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "mode_cards"},
    "evidence_board": {"data_kind": "evidence_ledger_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "semantic_table"},
    "clock_board": {"data_kind": "clock_view", "placements": ("live_session", "session_detail", "replay"), "mobile": "progress_list"},
    "route_map": {"data_kind": "route_view", "placements": ("world_detail", "live_session", "session_detail"), "mobile": "ordered_route"},
    "relation_graph": {"data_kind": "relation_view", "placements": ("world_detail", "live_session", "session_detail"), "mobile": "relationship_list"},
    "npc_state_board": {"data_kind": "npc_state_view", "placements": ("live_session", "session_detail", "replay", "world_detail"), "mobile": "stacked_cards"},
    "resource_ledger": {"data_kind": "resource_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "ledger_table"},
    "assembly_board": {"data_kind": "assembly_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "agenda_list"},
    "accord_ledger": {"data_kind": "accord_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "obligation_table"},
    "rumor_network": {"data_kind": "rumor_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "propagation_list"},
    "element_matrix": {"data_kind": "element_view", "placements": ("world_detail", "live_session", "session_detail"), "mobile": "accessible_matrix"},
    "environment_board": {"data_kind": "environment_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "condition_list"},
    "tactical_board": {"data_kind": "tactical_conflict_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "zones_objectives_list"},
    "ending_outlook": {"data_kind": "ending_outlook_view", "placements": ("world_detail", "live_session", "session_detail", "replay"), "mobile": "factor_list"},
    "replay_timeline": {"data_kind": "replay_view", "placements": ("replay", "session_detail"), "mobile": "chronological_list"},
}

MODULE_COMPONENTS = {
    "readme": "readme_reader",
    "actor": "character_build_catalog",
    "scene_graph": "route_map",
    "quest_graph": "quest_board",
    "knowledge_graph": "evidence_board",
    "evidence_ledger": "evidence_board",
    "time_clock": "clock_board",
    "relationship_graph": "relation_graph",
    "npc_lifecycle": "npc_state_board",
    "items_inventory": "resource_ledger",
    "economy": "resource_ledger",
    "resources": "resource_ledger",
    "accords": "accord_ledger",
    "assembly": "assembly_board",
    "rumor_network": "rumor_network",
    "elemental_interactions": "element_matrix",
    "scene_environment": "environment_board",
    "challenge_engine": "challenge_board",
    "tactical_conflict": "tactical_board",
    "ending": "ending_outlook",
    "terminal_conditions": "ending_outlook",
    "actor_fate": "npc_state_board",
}

MODULE_LABELS = {
    "readme": ("世界设定", "按当前身份阅读世界说明和恢复指引。"),
    "actor": ("推荐人物", "查看建卡字段、角色职责、限制和推荐组合。"),
    "scene_graph": ("地点与路线", "查看当前位置、可达路线、阻断原因和撤退出口。"),
    "quest_graph": ("任务进展", "查看目标、里程碑、替代路径和失败推进。"),
    "knowledge_graph": ("调查与线索", "核对已公开线索、来源、矛盾和后续追索。"),
    "evidence_ledger": ("证据账本", "核对证据来源、保管、可靠度、矛盾和公开范围。"),
    "time_clock": ("时钟与期限", "查看当前阶段、剩余额度、暂停状态和超时后果。"),
    "relationship_graph": ("关系网络", "查看公开关系、方向、变化原因和可用入口。"),
    "npc_lifecycle": ("人物与见证", "查看人物公开角色、状态变化和作证意愿。"),
    "items_inventory": ("物资与装备", "查看数量、归属、可用性和变化原因。"),
    "economy": ("资源与后勤", "查看资源余额、债务、交易和后勤影响。"),
    "accords": ("承诺账本", "查看义务、担保、期限、违约和修复方式。"),
    "assembly": ("会盟议程", "查看资格、法定人数、议程、动议和认证。"),
    "rumor_network": ("传闻网络", "查看传播范围、可信度、失真和反证。"),
    "elemental_interactions": ("元素矩阵", "查看元素语义、方向作用、反应、暴露和衰减。"),
    "scene_environment": ("场景环境", "查看环境来源、强度、持续、影响和缓解方式。"),
    "challenge_engine": ("当前挑战", "查看阶段、目标、风险预告、选择、退出和失败推进。"),
    "tactical_conflict": ("战术冲突", "查看区域、目标、敌方预兆、行动额度和非战斗出口。"),
    "ending": ("结局展望", "查看已知影响因素、代价、锁定原因和可能方向。"),
    "terminal_conditions": ("终局条件", "查看当前可见终局条件、限制和恢复入口。"),
    "actor_fate": ("角色命运", "查看伤势、濒危、救援窗口和退出状态。"),
}


def registry_entry(component_kind: object) -> dict[str, Any] | None:
    value = COMPONENT_REGISTRY.get(str(component_kind or ""))
    return dict(value) if isinstance(value, Mapping) else None


def component_for_module(module_id: object) -> str:
    return MODULE_COMPONENTS.get(str(module_id or ""), "")


def public_registry() -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "components": {
            key: {
                "data_kind": value["data_kind"],
                "placements": list(value["placements"]),
                "mobile_presentation": value["mobile"],
                "visual_recipe": f"{key}.standard",
            }
            for key, value in sorted(COMPONENT_REGISTRY.items())
        },
    }


__all__ = [
    "COMPONENT_REGISTRY",
    "MODULE_COMPONENTS",
    "MODULE_LABELS",
    "REGISTRY_VERSION",
    "component_for_module",
    "public_registry",
    "registry_entry",
]
