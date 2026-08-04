from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


def write(name: str, value: object) -> None:
    (TEMPLATES / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )


base = json.loads((TEMPLATES / "world-package.template.json").read_text("utf-8"))


def v5_base(slug: str, name: str) -> dict:
    value = deepcopy(base)
    value["slug"] = slug
    value["name"] = name
    value["description"] = "世界协议 v5 模块化示例；只启用本文件实际使用的功能。"
    value["world_schema_version"] = 5
    value["world_content_version"] = "1.0.0"
    value["minimum_plugin_version"] = "0.11.0"
    metadata = value.get("template_metadata")
    if isinstance(metadata, dict):
        metadata["compatible_plugin_version"] = "0.11.0"
        metadata["minimum_plugin_version"] = "0.11.0"
        if isinstance(metadata.get("purpose"), str):
            metadata["purpose"] = metadata["purpose"].replace("v0.10.0", "v0.11.0")
    value.pop("capabilities", None)
    value["protocol"] = {"core_version": 5, "features": {}}
    value["required_features"] = []
    value["rules"]["world_schema_version"] = 5
    value["rules"].pop("capabilities", None)
    value["rules"]["character_card"]["version"] = 6
    value["rules"]["knowledge_boundary"] = {
        "policy": "strict", "global_rules": ["只读取明确授权的知识。"],
        "forbidden_domains": ["导演秘密"], "metagame_policy": "deny",
        "unknown_fact_behavior": "承认不知道或发起调查",
    }
    value["rules"]["content_boundary"] = {
        "hard_blocked_tags": ["替玩家决定重大意志"],
        "player_may_relax": False, "safety_pause_overrides_all": True,
    }
    return value


capabilities = v5_base("v5-capabilities-template", "v5 能力接口模板")
capabilities["protocol"]["features"] = {
    "entity_registry": "1.0", "condition_engine": "1.0",
    "operation_engine": "1.0", "event_pipeline": "1.0",
    "resolution_receipt": "1.0", "capabilities": "1.0",
    "resources": "1.0", "action_intents": "1.0",
}
capabilities["required_features"] = [
    "entity_registry@>=1.0", "condition_engine@>=1.0",
    "operation_engine@>=1.0", "capabilities@>=1.0",
]
capabilities["rules"]["entity_registry"] = {"entities": [
    {"entity_type": "stat", "id": "mastery", "label": "熟练度"},
    {"entity_type": "story_flag", "id": "advanced_training", "label": "完成进阶训练"},
]}
capabilities["rules"]["resources"] = {"definitions": [
    {"resource_id": "focus", "label": "专注", "value_type": "integer",
     "range": {"min": 0, "max": 10}, "initial_value": 5,
     "persistence_scope": "scene"}
]}
capabilities["rules"]["capabilities"] = {
    "definitions": [
        {"capability_id": "basic", "label": "基础能力", "description": "可替换为武学、调查、权限或配方。",
         "costs": [{"resource_ref": "resource:focus", "operation": "subtract", "value": 1}],
         "effects": [{"op": "add_narrative_constraint", "value": "按世界规则表现基础能力。"}]},
        {"capability_id": "advanced", "label": "进阶能力", "description": "示例进阶节点。"},
    ],
    "initial_grants": [],
    "transitions": [
        {"transition_id": "basic_to_advanced", "from": ["capability:basic"],
         "when": {"all": [
             {"scope": "actor", "ref": "stat:mastery", "operator": ">=", "value": 3},
             {"scope": "world", "ref": "story_flag:advanced_training", "operator": "==", "value": True},
         ]},
         "operations": [
             {"op": "set_availability", "target_ref": "capability:basic", "value": False},
             {"op": "grant_reference", "target_ref": "capability:advanced"},
         ]}
    ],
}
write("world-package-capabilities.template.json", capabilities)

interaction = v5_base("v5-interaction-rules-template", "v5 通用交互规则模板")
interaction["protocol"]["features"] = {
    "entity_registry": "1.0", "condition_engine": "1.0",
    "operation_engine": "1.0", "event_pipeline": "1.0",
    "resolution_receipt": "1.0", "interaction_rules": "1.0",
}
interaction["required_features"] = [
    "entity_registry@>=1.0", "condition_engine@>=1.0",
    "operation_engine@>=1.0", "interaction_rules@>=1.0",
]
interaction["rules"]["entity_registry"] = {"entities": [
    {"entity_type": "custom", "id": "source.kind", "label": "行动方世界标签"},
    {"entity_type": "custom", "id": "target.kind", "label": "目标世界标签"},
    {"entity_type": "custom", "id": "check_modifier", "label": "裁定修正"},
]}
interaction["rules"]["interaction_rules"] = {
    "enabled": True,
    "settings": {"conflict_strategy": "priority_then_stack", "max_matches_per_event": 10},
    "rules": [
        {"rule_id": "world_defined_relation", "mode": "hybrid", "enabled": True,
         "priority": 100,
         "triggers": [{"event": "action.requested", "phase": "before_resolution"}],
         "when": {"all": [
             {"scope": "action", "ref": "custom:source.kind", "operator": "==", "value": "author_defined_source"},
             {"scope": "target", "ref": "custom:target.kind", "operator": "==", "value": "author_defined_target"},
         ]},
         "effects": [
             {"op": "modify_value", "target_ref": "custom:check_modifier", "value": 2},
             {"op": "add_narrative_constraint", "value": "这里写世界包自己的关系含义。"},
         ],
         "stacking": {"group": "example_relation", "strategy": "highest", "limit": 1}}
    ],
}
write("world-package-interaction-rules.template.json", interaction)

full = deepcopy(capabilities)
full["slug"] = "v5-full-example"
full["name"] = "v5 完整模块示例"
full["protocol"]["features"].update(interaction["protocol"]["features"])
full["required_features"] = sorted(set(full["required_features"] + interaction["required_features"]))
full["rules"]["entity_registry"]["entities"].extend(interaction["rules"]["entity_registry"]["entities"])
full["rules"]["interaction_rules"] = interaction["rules"]["interaction_rules"]
full["rules"]["runtime_effects"] = {"definitions": [
    {"runtime_effect_id": "example_effect", "label": "示例持续规则", "duration_units": ["scene", "until_condition"]}
]}
full["rules"]["objects"] = {"definitions": [
    {"object_id": "example_object", "label": "示例对象", "provides": ["capability:basic"]}
]}
full["protocol"]["features"].update({"runtime_effects": "1.0", "objects": "1.0"})
write("world-package-v5-full-example.json", full)

write("migration-v4-to-v5.example.json", {
    "migration_id": "replace-with-stable-id",
    "source_core_version": 4,
    "target_core_version": 5,
    "mode": "clone_and_upgrade",
    "id_aliases": {"capability:old_id": "capability:new_id"},
    "operations": [
        {"op": "revoke_reference", "target_ref": "capability:old_id"},
        {"op": "grant_reference", "target_ref": "capability:new_id"},
    ],
    "requires_preview": True,
    "requires_confirmation": True,
    "mutate_running_session": False,
})
write("id-aliases.example.json", {
    "id_aliases": {
        "capability:old_fire_spell": "capability:fire_spell",
        "object:old_royal_seal": "object:royal_seal"
    },
    "rules": [
        "别名只用于旧引用读取；所有新写入使用目标 ID。",
        "显示名称变化不需要别名。",
        "删除仍被引用的 ID 时必须同时提供替代或终止操作。"
    ]
})

npc = {
    "template_version": 2,
    "minimum_plugin_version": "0.11.0",
    "world_slug": "replace-with-world-slug",
    "items": [
        {"slug": "simple-actor", "name": "简化 NPC", "role": "npc",
         "profile": {"actor_complexity": "simplified", "identity": "身份",
                     "capability_refs": [], "resource_refs": [], "runtime_effect_refs": [],
                     "object_refs": [], "custom_tags": []},
         "prompt": "只写该 NPC 的私密动机与行为边界。", "sort_order": 10},
        {"slug": "full-actor", "name": "完整 NPC", "role": "npc",
         "profile": {"actor_complexity": "full", "identity": "身份",
                     "capability_refs": ["capability:basic"],
                     "resources": {"resource:focus": 5},
                     "runtime_effect_refs": [], "object_refs": ["object:example_object"],
                     "custom_tags": ["faction.example"]},
         "prompt": "不得读取玩家私密信息或临时获得未声明能力。", "sort_order": 20},
    ]
}
write("npc-import.template.json", npc)

manifest = {
    "template_bundle_version": "3.0.0",
    "compatible_plugin_version": "0.11.0",
    "world_schema_versions": [2, 3, 4, 5],
    "character_card_template_versions": [5, 6],
    "npc_import_template_version": 2,
    "feature_protocol_versions": {
        "entity_registry": "1.0", "condition_engine": "1.0", "operation_engine": "1.0",
        "event_pipeline": "1.0", "resolution_receipt": "1.0", "capabilities": "1.0",
        "resources": "1.0", "runtime_effects": "1.0", "objects": "1.0",
        "resolution_methods": "1.0", "interaction_rules": "1.0", "action_intents": "1.0"
    },
    "files": {
        "instructions": "templates/README.md",
        "v4_world_package": "templates/world-package.template.json",
        "v4_preset_stack_world_package": "templates/world-package-preset-stack.template.json",
        "capabilities": "templates/world-package-capabilities.template.json",
        "interaction_rules": "templates/world-package-interaction-rules.template.json",
        "v5_full_example": "templates/world-package-v5-full-example.json",
        "npc_import": "templates/npc-import.template.json",
        "v4_to_v5_migration": "templates/migration-v4-to-v5.example.json",
        "id_aliases": "templates/id-aliases.example.json"
    },
    "release_policy": {"validated_in_test_suite": True, "unknown_extensions_round_trip": True,
                       "running_sessions_keep_frozen_snapshot": True}
}
write("template-manifest.json", manifest)
