from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORLD_PATH = ROOT / "worlds" / "aelvion-ashen-crown.json"
NPC_PATH = ROOT / "worlds" / "aelvion-ashen-crown-npcs.json"


def option(value: dict, *, knowledge: str) -> dict:
    result = dict(value)
    result.setdefault("value", result.get("name") or result.get("label") or result["id"])
    result.setdefault("label", result.get("name") or result["value"])
    result["knowledge_profile_ref"] = knowledge
    result["content_profile_ref"] = "content.default_player"
    return result


world = json.loads(WORLD_PATH.read_text("utf-8"))
rules = world["rules"]
card = rules["character_card"]
card["version"] = 6
card["fields"] = [
    item for item in card["fields"]
    if item.get("key") not in {"knowledge_boundary", "content_boundaries"}
]

profession_options = []
for item in card.get("profession_presets", []):
    value = option(item, knowledge=f"knowledge.profession.{item['id']}")
    value["capability_grants"] = [f"capability:{item['id']}.signature"]
    profession_options.append(value)
origin_options = [
    option(item, knowledge=f"knowledge.origin.{item['id']}")
    for item in card.get("origin_region_presets", [])
]
identity_options = [
    option(item, knowledge=f"knowledge.identity.{item['id']}")
    for item in card.get("social_identity_presets", [])
]
species_options = [
    option(item, knowledge=f"knowledge.species.{item['id']}")
    for item in card.get("preset_sets", {}).get("species_presets", [])
]
card["preset_dimensions"] = [
    {"id": "species", "label": "种族", "selection": {"mode": "single", "min": 1, "max": 1},
     "required": True, "allow_custom": False, "randomizable": True, "player_editable": True,
     "display_order": 10, "options": species_options},
    {"id": "profession", "label": "职业", "selection": {"mode": "single", "min": 1, "max": 1},
     "required": True, "allow_custom": False, "randomizable": True, "player_editable": True,
     "display_order": 20, "options": profession_options},
    {"id": "origin_region", "label": "出身地区", "selection": {"mode": "single", "min": 1, "max": 1},
     "required": True, "allow_custom": False, "randomizable": True, "player_editable": True,
     "display_order": 30, "options": origin_options},
    {"id": "social_identity", "label": "社会身份", "selection": {"mode": "single", "min": 1, "max": 1},
     "required": True, "allow_custom": False, "randomizable": True, "player_editable": True,
     "display_order": 40, "options": identity_options},
]

rules["knowledge_boundary"] = {
    "policy": "strict",
    "global_rules": [
        "角色只获得角色卡预设、亲历事件、已公开线索和合理来源授权的知识。",
        "职业能力不等于全领域知识；显示名称改变不得扩大知识授权。",
        "导演秘密、其他角色私人秘密和未公开幕后动机一律不可读取。",
    ],
    "forbidden_domains": ["导演秘密", "其他角色私人秘密", "未公开幕后动机"],
    "metagame_policy": "deny",
    "unknown_fact_behavior": "表现为不知道、传闻认知或请求调查",
}
rules["content_boundary"] = {
    "hard_blocked_tags": ["露骨性内容", "非自愿性内容", "替玩家决定重大意志"],
    "default_profile_ref": "content.default_player",
    "player_may_relax": False,
    "safety_pause_overrides_all": True,
}

entities = [
    {"entity_type": "custom", "id": "check_modifier", "label": "本次裁定修正"},
    {"entity_type": "custom", "id": "damage_multiplier", "label": "世界定义效果倍率"},
    {"entity_type": "custom", "id": "action.damage_tag", "label": "行动效果标签"},
    {"entity_type": "custom", "id": "target.creature_tag", "label": "目标生物标签"},
    {"entity_type": "custom", "id": "scene.environment_tag", "label": "环境标签"},
    {"entity_type": "story_flag", "id": "ashen_crown_attuned", "label": "已与灰烬王冠共鸣"},
    {"entity_type": "custom_tag", "id": "authority.silver_torch", "label": "银炬公会权限"},
    {"entity_type": "custom_tag", "id": "material.consecrated_silver", "label": "祝圣白银材质"},
    {"entity_type": "custom_tag", "id": "authority.royal", "label": "王家权限"},
]
for dimension in card["preset_dimensions"]:
    entities.append({"entity_type": "custom", "id": f"preset.{dimension['id']}", "label": dimension["label"]})
for attribute in card.get("stats", {}).get("attributes", []):
    entities.append({"entity_type": "stat", "id": attribute["key"], "label": attribute.get("label", attribute["key"])})

profession_roles = {
    "knight": ("盾墙守势", "以盾牌与站位保护一名相邻同伴。"),
    "mage": ("星火箭", "塑形一束可见星火；需要专注与奥术资源。"),
    "warlock": ("契约低语", "借契约媒介感知附近异常魔力，代价由契约约束。"),
    "priest": ("晨辉祷言", "以圣徽稳定伤势或驱散轻微恐惧。"),
    "hunter": ("猎径追踪", "根据真实痕迹判断近期通行方向。"),
    "berserker": ("破阵怒意", "短时强化正面压制，同时降低精细判断。"),
    "rogue": ("巧手探察", "使用工具检查机关、锁具和人为痕迹。"),
    "paladin": ("誓约守护", "依据公开誓约保护目标并对抗亵渎。"),
    "druid": ("地脉聆听", "在自然环境中辨认污染、天气和生灵异常。"),
    "bard": ("鼓舞曲调", "以表演稳定士气；不能强制改变他人意志。"),
    "alchemist": ("应急炼剂", "使用已有材料调制一次性基础炼剂。"),
    "monk": ("流息架势", "以内息调整身法和近身防御。"),
}
capability_definitions = []
for preset in card.get("profession_presets", []):
    profession_id = preset["id"]
    label, description = profession_roles[profession_id]
    capability_definitions.append({
        "capability_id": f"{profession_id}.signature",
        "label": label,
        "capability_type_ref": "capability_type:profession_signature",
        "description": description,
        "tags": [f"profession.{profession_id}"],
        "grant_policy": "ignore",
        "effects": [{"op": "add_narrative_constraint", "value": description}],
    })
capability_definitions.extend([
    {"capability_id": "mage.fireball", "label": "火球术", "capability_type_ref": "capability_type:arcane_spell",
     "description": "凝聚火焰投射物，受环境、专注和目标状态约束。", "tags": ["effect.flame"],
     "costs": [{"resource_ref": "resource:arcane_focus", "operation": "subtract", "value": 1}],
     "effects": [{"op": "add_narrative_constraint", "value": "以可见火焰影响目标与环境，不自动宣告伤害。"}]},
    {"capability_id": "mage.flame_blast", "label": "炎爆术", "capability_type_ref": "capability_type:arcane_spell",
     "description": "火球术的进阶形态，范围更大且代价更高。", "tags": ["effect.flame", "shape.blast"],
     "costs": [{"resource_ref": "resource:arcane_focus", "operation": "subtract", "value": 2}],
     "effects": [{"op": "add_narrative_constraint", "value": "形成范围火焰冲击，必须依据裁定凭证叙述结果。"}]},
    {"capability_id": "mage.crownfire", "label": "冠焰术", "capability_type_ref": "capability_type:arcane_spell",
     "description": "灰烬王冠篇章中的高阶火焰塑形。", "tags": ["effect.flame", "tier.high"],
     "costs": [{"resource_ref": "resource:arcane_focus", "operation": "subtract", "value": 3}]},
])

rules["entity_registry"] = {"entities": entities}
rules["capability_types"] = {"definitions": [
    {"id": "profession_signature", "label": "职业标志能力"},
    {"id": "arcane_spell", "label": "奥术塑形"},
]}
rules["resources"] = {"definitions": [
    {"resource_id": "arcane_focus", "label": "奥术专注", "value_type": "integer", "range": {"min": 0, "max": 6}, "initial_value": 6, "persistence_scope": "session"},
    {"resource_id": "divine_favor", "label": "神恩", "value_type": "integer", "range": {"min": 0, "max": 6}, "initial_value": 6, "persistence_scope": "session"},
    {"resource_id": "endurance", "label": "耐力", "value_type": "integer", "range": {"min": 0, "max": 10}, "initial_value": 10, "persistence_scope": "scene"},
]}
rules["capabilities"] = {
    "definitions": capability_definitions,
    "initial_grants": [
        {"grant_id": f"grant.{item['id']}", "capability_ref": f"capability:{item['id']}.signature",
         "source_ref": f"custom:preset.profession", "persistence_scope": "campaign",
         "when": {"ref": "custom:preset.profession", "scope": "actor", "operator": "==", "value": item["id"]}}
        for item in card.get("profession_presets", [])
    ],
    "transitions": [
        {"transition_id": "fireball_to_flame_blast", "from": ["capability:mage.fireball"],
         "when": {"ref": "stat:intelligence", "scope": "actor", "operator": ">=", "value": 9},
         "operations": [{"op": "set_availability", "target_ref": "capability:mage.fireball", "value": False},
                        {"op": "grant_reference", "target_ref": "capability:mage.flame_blast"}]},
        {"transition_id": "flame_blast_to_crownfire", "from": ["capability:mage.flame_blast"],
         "when": {"ref": "story_flag:ashen_crown_attuned", "scope": "world", "operator": "==", "value": True},
         "operations": [{"op": "grant_reference", "target_ref": "capability:mage.crownfire"}]},
    ],
}
rules["runtime_effects"] = {"definitions": [
    {"runtime_effect_id": "wounded", "label": "负伤", "duration_units": ["scene", "until_condition"]},
    {"runtime_effect_id": "graymoon_mark", "label": "灰月印记", "duration_units": ["chapter", "permanent"]},
    {"runtime_effect_id": "wanted", "label": "被通缉", "duration_units": ["world_time", "until_condition"]},
]}
rules["objects"] = {"definitions": [
    {"object_id": "silver_torch_badge", "label": "银炬徽章", "provides": ["custom_tag:authority.silver_torch"]},
    {"object_id": "consecrated_silver", "label": "祝圣白银", "provides": ["custom_tag:material.consecrated_silver"]},
    {"object_id": "royal_seal", "label": "王家印信", "provides": ["custom_tag:authority.royal"]},
]}
rules["action_types"] = {"definitions": [
    {"id": "investigate", "label": "调查"}, {"id": "persuade", "label": "交涉"},
    {"id": "travel", "label": "旅行"}, {"id": "craft", "label": "制作"},
    {"id": "use_capability", "label": "使用能力"},
]}
rules["resolution_methods"] = {"methods": [
    {"method_id": "aelvion_d20", "label": "阿尔维恩 D20", "default_outcome_id": "failure",
     "steps": [{"step_id": "roll", "op": "random_integer", "min": 1, "max": 20},
               {"step_id": "modifier", "op": "read_value", "scope": "actor", "ref": "custom:check_modifier"},
               {"step_id": "total", "op": "sum", "inputs": ["roll", "modifier"]}]}
]}
rules["interaction_rules"] = {"enabled": True, "settings": {"conflict_strategy": "priority_then_stack", "max_matches_per_event": 10}, "rules": [
    {"rule_id": "flame_in_heavy_rain", "mode": "hybrid", "enabled": True, "priority": 80,
     "triggers": [{"event": "action.requested", "phase": "before_resolution"}],
     "when": {"all": [{"ref": "custom:action.damage_tag", "scope": "action", "operator": "==", "value": "flame"},
                      {"ref": "custom:scene.environment_tag", "scope": "scene", "operator": "==", "value": "heavy_rain"}]},
     "effects": [{"op": "modify_value", "target_ref": "custom:check_modifier", "value": -2},
                 {"op": "add_narrative_constraint", "value": "暴雨削弱并扰动火焰表现。"}],
     "stacking": {"group": "environmental_flame", "strategy": "highest", "limit": 1}},
    {"rule_id": "consecrated_silver_against_undead", "mode": "hybrid", "enabled": True, "priority": 100,
     "triggers": [{"event": "action.requested", "phase": "before_resolution"}],
     "when": {"all": [{"ref": "custom:action.damage_tag", "scope": "action", "operator": "==", "value": "consecrated_silver"},
                      {"ref": "custom:target.creature_tag", "scope": "target", "operator": "==", "value": "undead"}]},
     "effects": [{"op": "modify_value", "target_ref": "custom:check_modifier", "value": 2},
                 {"op": "add_narrative_constraint", "value": "祝圣白银对当前亡灵结构产生世界定义的压制。"}],
     "stacking": {"group": "material_affinity", "strategy": "highest", "limit": 1}},
]}

world["world_schema_version"] = 5
world["world_content_version"] = "3.0.0"
world["minimum_plugin_version"] = "0.11.0"
world["protocol"] = {"core_version": 5, "features": {
    "entity_registry": "1.0", "condition_engine": "1.0", "operation_engine": "1.0",
    "event_pipeline": "1.0", "resolution_receipt": "1.0", "capabilities": "1.0",
    "resources": "1.0", "runtime_effects": "1.0", "objects": "1.0",
    "resolution_methods": "1.0", "interaction_rules": "1.0", "action_intents": "1.0",
}}
world["required_features"] = [
    "entity_registry@>=1.0", "condition_engine@>=1.0", "operation_engine@>=1.0",
    "event_pipeline@>=1.0", "resolution_receipt@>=1.0", "capabilities@>=1.0",
    "interaction_rules@>=1.0",
]
world["capabilities"] = rules["capabilities"]
rules["world_schema_version"] = 5

npcs = json.loads(NPC_PATH.read_text("utf-8"))
npc_capabilities = {
    "herbert-lane": ["capability:knight.signature"],
    "mira": ["capability:hunter.signature"],
    "sister-elena": ["capability:priest.signature"],
    "seville-raven": ["capability:mage.signature", "capability:mage.fireball"],
    "audra-stonering": ["capability:alchemist.signature"],
    "sharai-woodspeak": ["capability:druid.signature"],
}
for npc in npcs:
    profile = npc.setdefault("profile", {})
    profile["actor_complexity"] = "full" if npc["slug"] in {"herbert-lane", "seville-raven"} else "simplified"
    profile["capability_refs"] = npc_capabilities.get(npc["slug"], [])
    profile["resource_refs"] = ["resource:endurance"]
    profile["runtime_effect_refs"] = []
    profile["object_refs"] = []
    profile["custom_tags"] = [f"faction.{str(profile.get('faction') or 'independent').lower().replace(' ', '_')}"]

WORLD_PATH.write_text(json.dumps(world, ensure_ascii=False, indent=2) + "\n", "utf-8")
NPC_PATH.write_text(json.dumps(npcs, ensure_ascii=False, indent=2) + "\n", "utf-8")
