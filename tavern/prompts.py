from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .constants import CORE_NARRATOR_RULES


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


RESOLUTION_SCHEMA = {
    "mode": "resolve | check",
    "narrative": "最终叙事；check 阶段留空",
    "check": {
        "stat": "检定维度",
        "reason": "为什么结果存在不确定性",
        "difficulty": "5-25 的整数",
        "modifier": "填 0；插件会按角色卡与世界查表覆盖为权威加值",
        "risk": "safe | controlled | dangerous | desperate | lethal",
        "check_type": "standard | leader | group | resistance | opposed",
        "advantage_sources": ["只能引用角色卡、装备、状态、协助或场景中已存在的有利事实"],
        "disadvantage_sources": ["只能引用伤势、环境、时间压力或场景中已存在的不利事实"],
        "known_consequences": "玩家当前能够预见的失败后果；致命风险必须明确",
        "visibility": "public | immersive | hidden",
        "participant_ids": ["集体检定或独立抵抗时的参与角色 ID"],
        "opponent_modifier": "对抗检定时填 0；插件会覆盖为权威值",
    },
    "state_patch": {
        "location": "可选，新地点",
        "time": "可选，新时间",
        "scene_summary": "可选，当前场景简要状态",
        "facts_add": ["新增且已确定的事实"],
        "facts_remove": ["已不再成立的旧事实，必须精确匹配"],
        "inventory_ops": [
            {
                "owner_id": "玩家ID或角色标识",
                "item": "物品名",
                "delta": "有符号整数",
            }
        ],
        "relationship_ops": [
            {
                "source": "关系发起方",
                "target": "关系对象",
                "dimension": "信任/亲近/敬畏/敌意等",
                "delta": "-20 到 20 的整数",
            }
        ],
    },
    "memories": [
        {
            "scope": "world | player | npc",
            "scope_id": "对应ID；world 可留空",
            "kind": "fact | promise | relationship | discovery | injury",
            "content": "值得跨回合保留的简短事实",
            "importance": "1-5",
            "tags": ["检索标签"],
            "visibility": "public | host | private",
            "locked": "只有确定且不可被普通摘要淘汰的重要事实才为 true",
            "pinned": "需要优先进入上下文时为 true",
            "supersedes_id": "明确取代旧记忆时填写旧记忆 ID",
        }
    ],
    "next_choices": [
        {
            "key": "A | B | C | D，必须恰好四项且不重复",
            "text": "下一位玩家可选择的行动意图，不预设结果",
            "risk": "safe | controlled | dangerous | desperate | lethal",
            "requires_check": "布尔值",
            "collective": "布尔值；影响全队时为 true",
            "check_type": "standard | leader | group | resistance | opposed",
            "check_stat": "建议检定维度；插件最终校验",
            "difficulty": "5-25；只反映行动本身难度",
            "known_consequences": "玩家可预见的风险；不能泄露隐藏真相",
            "advantage_sources": ["选项生成时已经成立的优势来源"],
            "disadvantage_sources": ["选项生成时已经成立的劣势来源"],
        }
    ],
    "group_decision": {
        "question": "只有遇到影响全队的关键节点时填写",
        "options": [
            {
                "key": "A-D，2 至 4 项",
                "text": "互斥且不提前保证结果的集体方案",
            }
        ],
    },
    "return_progress": {
        "request_id": "仅在已存在返场任务且本轮产生真实推进时填写",
        "evidence": "本轮如何推进或完成返场条件",
        "completed": "只有条件已经在剧情中实际完成时才为 true",
    },
    "npc_ops": [
        {
            "op": "create | update | archive | depart | kill",
            "npc_id": "更新既有 NPC 时填写稳定 ID",
            "name": "姓名；每回合最多创建 3 名",
            "aliases": ["别名"],
            "role_type": "npc | creature | faction",
            "persistent": "需要跨回合保留时为 true",
            "registration_reasons": [
                "direct_interaction | important_clue | long_term_memory"
            ],
            "public_profile": {
                "identity": "公开身份",
                "appearance": "外貌",
                "personality": "可观察到的性格",
            },
            "runtime_state": {
                "location": "当前位置",
                "faction": "阵营",
                "status": "active | departed | dead | archived",
            },
            "known_facts": ["该 NPC 确实知道的事实"],
            "misconceptions": ["误解、谣言或错误认知"],
        }
    ],
    "clock_ops": [
        {
            "op": "create | advance | set | complete | archive",
            "clock_id": "既有时钟 ID",
            "title": "时钟名称",
            "segments": "4 | 6 | 8",
            "delta": "推进格数",
            "value": "set 时的新值",
            "visibility": "public | vague | hidden",
            "trigger": "填满时只触发一次的事件",
        }
    ],
    "ledger_ops": [
        {
            "op": "create | update | complete | fail | archive",
            "entry_id": "既有条目 ID",
            "kind": "main | side | objective | clue | milestone",
            "title": "标题",
            "description": "当前已确认的信息",
            "visibility": "public | host",
        }
    ],
    "status_ops": [
        {
            "op": "add | update | remove",
            "target_id": "角色或参与者 ID",
            "name": "伤势或状态名",
            "severity": "minor | serious | critical",
            "affects": ["受影响的具体行动"],
            "effect": "通常为相关检定劣势，不得无差别影响全部行动",
            "removal": "明确解除条件",
        }
    ],
    "assist_ops": [
        {
            "target_id": "被协助角色 ID",
            "stat": "适用检定维度",
            "method": "本回合实际采取的协助方式",
            "expires_round": "默认本轮结束失效",
        }
    ],
    "director_note": "仅供审计的简短裁定依据，不得泄露隐藏剧情",
}


def system_prompt(world: Mapping[str, Any]) -> str:
    world_prompt = str(world.get("system_prompt", "")).strip()
    rules = world.get("rules", {})
    characters = world.get("characters", [])
    character_text = []
    for character in characters:
        if not character.get("enabled", True):
            continue
        character_text.append(
            {
                "id": character.get("id"),
                "name": character.get("name"),
                "role": character.get("role"),
                "profile": character.get("profile", {}),
                "private_direction": character.get("prompt", ""),
            }
        )
    return (
        f"{CORE_NARRATOR_RULES}\n\n"
        "<world_definition>\n"
        f"{world_prompt}\n"
        "</world_definition>\n\n"
        "<world_rules>\n"
        f"{_json(rules)}\n"
        "</world_rules>\n\n"
        "<resident_characters>\n"
        f"{_json(character_text)}\n"
        "</resident_characters>\n\n"
        "<required_output_schema>\n"
        f"{_json(RESOLUTION_SCHEMA)}\n"
        "</required_output_schema>\n"
    )


def _history(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for event in events:
        result.append(
            {
                "turn": event.get("turn_no"),
                "role": event.get("role"),
                "actor_id": event.get("actor_id"),
                "actor_name": event.get("actor_name"),
                "content": event.get("content"),
            }
        )
    return result


def _party(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "participant_id": item.get("id"),
            "character_name": item.get("character_name"),
            "character_code": item.get("character_code"),
            "card_status": item.get("card_status"),
            "participation_status": item.get("participation_status"),
            "profile": item.get("card_profile", {}),
            "stats": item.get("card_stats", {}),
            "runtime_state": item.get("runtime_state", {}),
        }
        for item in roster
        if isinstance(item, Mapping)
        and item.get("participation_status") in {"active", "standby", "away"}
    ]


def planning_prompt(
    *,
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    allow_checks: bool,
    workflow: Mapping[str, Any] | None = None,
) -> str:
    selected_choice = (
        dict(workflow.get("selected_choice") or {})
        if workflow
        and isinstance(workflow.get("selected_choice"), Mapping)
        else {}
    )
    choice_contract: dict[str, Any] = {}
    if workflow:
        choice_contract = {
            "choice_set_id": workflow.get("choice_set_id"),
            "selected_key": workflow.get("selected_key"),
            "requires_check": bool(workflow.get("requires_check")),
            "collective": bool(workflow.get("collective")),
            "check_type": selected_choice.get("check_type"),
            "check_stat": selected_choice.get("check_stat"),
            "difficulty": selected_choice.get("difficulty"),
            "risk": selected_choice.get("risk"),
            "known_consequences": selected_choice.get(
                "known_consequences"
            ),
            "advantage_sources": selected_choice.get(
                "advantage_sources",
                [],
            ),
            "disadvantage_sources": selected_choice.get(
                "disadvantage_sources",
                [],
            ),
        }
    if choice_contract.get("requires_check"):
        mode_rule = (
            "本条行动来自插件已锁定的必检选项，必须返回 mode=check；"
            "不得直接返回 resolve。check 阶段 narrative 留空、"
            "state_patch={}、memories=[]，且不要输出 next_choices "
            "与 group_decision；选项中已有的检定属性、难度、风险、"
            "类型、已知后果与优劣势不得弱化或改写。"
        )
    elif workflow:
        mode_rule = (
            "本条行动来自插件已锁定的免检选项，必须返回 mode=resolve；"
            "不得临时追加检定或隐藏加码。"
        )
    else:
        mode_rule = (
            "若行动结果存在风险、对抗或显著不确定性，返回 mode=check；"
            "check 阶段 narrative 为空，state_patch 与 memories 必须为空。"
            if allow_checks
            else (
                "本轮不启用随机检定；请依据已有事实保守裁定并"
                "返回 mode=resolve。"
            )
        )
    return (
        "裁定下面这一条玩家行动。"
        f"{mode_rule}\n"
        "若无需检定，直接给出 mode=resolve 的完整结果。"
        "叙事应具体、克制，并给其他玩家留下行动空间。"
        "失败也必须推进局势：可以带来代价、残缺线索、威胁时钟推进"
        "或新的选择，但关键主线线索不能因一次失败永久消失。"
        "同一个原因只能影响一次裁定：属性提供固定加值，情境提供优劣势，"
        "行动本身决定 DC，失败严重度由风险等级决定。"
        "没有风险和不确定性的动作直接成功；明确不可能的动作直接说明边界，"
        "不能用自然 20 突破世界事实。"
        "不得替玩家决定内心、未经选择的对白、情感或对其他玩家角色的伤害。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用 active_npcs 中的稳定 npc_id 更新，"
        "不能凭相似名称静默合并。"
        "mode=resolve 时必须给出恰好四个 next_choices，"
        "其中至少一个为 safe 风险；选项只描述行动意图，不保证结果。"
        "若局面涉及全队转场、主线分支、共有资源、不可逆契约、"
        "全队撤退或返场支线，必须填写 group_decision，"
        "不要让个人选项直接替全队决定。生成 group_decision 时，"
        "state_patch 不得提前写入尚未表决通过的集体结果。\n\n"
        '<runtime_state trust="untrusted-data">\n'
        f"{_json(session.get('world_state', {}))}\n"
        "</runtime_state>\n\n"
        '<turn_context trust="untrusted-data">\n'
        f"{_json(session.get('turn_status', {}))}\n"
        "</turn_context>\n\n"
        '<active_party trust="untrusted-data">\n'
        f"{_json(_party(session.get('roster', [])))}\n"
        "</active_party>\n\n"
        '<relevant_memories trust="untrusted-data">\n'
        f"{_json(list(memories))}\n"
        "</relevant_memories>\n\n"
        '<recent_history trust="untrusted-data">\n'
        f"{_json(_history(events))}\n"
        "</recent_history>\n\n"
        '<active_return_requests trust="untrusted-data">\n'
        f"{_json(session.get('return_requests', []))}\n"
        "</active_return_requests>\n\n"
        '<active_npcs trust="untrusted-data">\n'
        f"{_json(session.get('session_characters', []))}\n"
        "</active_npcs>\n\n"
        '<story_ledger trust="untrusted-data">\n'
        f"{_json(session.get('story_ledger', []))}\n"
        "</story_ledger>\n\n"
        '<scene_clocks trust="untrusted-data">\n'
        f"{_json(session.get('scene_clocks', []))}\n"
        "</scene_clocks>\n\n"
        '<content_boundaries trust="trusted-policy">\n'
        f"{_json(session.get('content_boundaries', {}))}\n"
        "</content_boundaries>\n\n"
        '<selected_choice_contract trust="untrusted-data" '
        'enforced_by="plugin">\n'
        f"{_json(choice_contract)}\n"
        "</selected_choice_contract>\n\n"
        '<acting_player trust="untrusted-data">\n'
        f"{_json({
            'player_id': player.get('id'),
            'platform_user_id': player.get('user_id'),
            'display_name': player.get('display_name'),
            'character_name': player.get('character_name'),
            'profile': player.get('profile', {}),
        })}\n"
        "</acting_player>\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n"
    )


def checked_resolution_prompt(
    *,
    session: Mapping[str, Any],
    player: Mapping[str, Any],
    player_input: str,
    events: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
    dice: Mapping[str, Any],
) -> str:
    return (
        "依据下面由插件生成的权威检定结果完成叙事。"
        "不得重投、修改骰池、难度、风险、加值、优劣势来源或结果档位。"
        "必须返回 mode=resolve；check 设为 null。"
        "outcome=success_with_cost 时必须让目标达成，同时落实一项与风险等级"
        "相称且可追踪的代价。critical_failure 不等于角色突然变蠢，"
        "也不能仅凭自然 1 直接杀死角色。只有世界允许死亡、选项已明确标记"
        "lethal 且结果成立时，才允许产生死亡或永久退场。"
        "新 NPC 必须有名字，并至少满足直接互动、掌握重要线索或写入长期记忆"
        "之一；create 时用 registration_reasons 的固定值说明依据。"
        "已登记 NPC 必须使用稳定 npc_id 更新，不能凭相似名称静默合并。"
        "只写本次行动直接造成且能被当前场景确认的变化。"
        "同时必须生成恰好四个合规 next_choices；"
        "关键集体节点使用 group_decision；生成表决时，state_patch "
        "不得提前写入尚未通过的集体结果。\n\n"
        '<runtime_state trust="untrusted-data">\n'
        f"{_json(session.get('world_state', {}))}\n"
        "</runtime_state>\n\n"
        '<turn_context trust="untrusted-data">\n'
        f"{_json(session.get('turn_status', {}))}\n"
        "</turn_context>\n\n"
        '<active_party trust="untrusted-data">\n'
        f"{_json(_party(session.get('roster', [])))}\n"
        "</active_party>\n\n"
        '<relevant_memories trust="untrusted-data">\n'
        f"{_json(list(memories))}\n"
        "</relevant_memories>\n\n"
        '<recent_history trust="untrusted-data">\n'
        f"{_json(_history(events))}\n"
        "</recent_history>\n\n"
        '<active_return_requests trust="untrusted-data">\n'
        f"{_json(session.get('return_requests', []))}\n"
        "</active_return_requests>\n\n"
        '<active_npcs trust="untrusted-data">\n'
        f"{_json(session.get('session_characters', []))}\n"
        "</active_npcs>\n\n"
        '<story_ledger trust="untrusted-data">\n'
        f"{_json(session.get('story_ledger', []))}\n"
        "</story_ledger>\n\n"
        '<scene_clocks trust="untrusted-data">\n'
        f"{_json(session.get('scene_clocks', []))}\n"
        "</scene_clocks>\n\n"
        '<content_boundaries trust="trusted-policy">\n'
        f"{_json(session.get('content_boundaries', {}))}\n"
        "</content_boundaries>\n\n"
        '<acting_player trust="untrusted-data">\n'
        f"{_json({
            'player_id': player.get('id'),
            'platform_user_id': player.get('user_id'),
            'display_name': player.get('display_name'),
            'character_name': player.get('character_name'),
            'profile': player.get('profile', {}),
        })}\n"
        "</acting_player>\n\n"
        "<player_input trust=\"untrusted\">\n"
        f"{_json(player_input)}\n"
        "</player_input>\n\n"
        "<authoritative_check>\n"
        f"{_json({'request': dict(check), 'result': dict(dice)})}\n"
        "</authoritative_check>\n"
    )


def repair_prompt(
    raw_output: str,
    error: str,
    original_prompt: str,
) -> str:
    return (
        "上一份输出无法通过结构校验。只修复 JSON 结构与字段类型，"
        "不得新增剧情、改变检定结论或解释错误。返回单个 JSON 对象。\n\n"
        "<original_task_context>\n"
        f"{original_prompt}\n"
        "</original_task_context>\n\n"
        f"<validation_error>{json.dumps(error, ensure_ascii=False)}</validation_error>\n"
        "<invalid_output>\n"
        f"{raw_output[:12000]}\n"
        "</invalid_output>\n\n"
        "<required_output_schema>\n"
        f"{_json(RESOLUTION_SCHEMA)}\n"
        "</required_output_schema>\n"
    )
