from __future__ import annotations


import json


from collections.abc import Mapping, Sequence


from typing import Any


from ..constants import CORE_NARRATOR_RULES


from ..world_contract import world_contract


from ..market_projection import project_market_view


from ..chat_experience import narrator_directives


from ..twp.runtime import runtime_projection as twp_runtime_projection


from ..projections.character import project_actor_view


from ..item_catalog import item_candidate_projection


from ..rules_digest import (
    build_rules_digest_block,
    load_rules_digest,
)


from ..context.compiler import RelevantContextCompiler


from ..narrative_modes import (
    NARRATIVE_DIALOGUE_INSTRUCTION,
    narrative_length_instruction_for_session,
)
from ..narrative_styles import narrative_style_instruction_for_session
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_OUTPUT_SCHEMA,
)


_CONTEXT_COMPILERS: dict[
    tuple[tuple[str, int], ...],
    RelevantContextCompiler,
] = {}


RESOLUTION_SCHEMA = {
    "mode": "resolve | check",
    "narrative_document": {
        **NARRATIVE_DOCUMENT_OUTPUT_SCHEMA,
        "when": "resolve 阶段必填；check 阶段必须为 null",
    },
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
        "relationship_ops": [
            {
                "source": "关系发起方（优先用插件提供的 participant_id / NPC stable_key / 名称）",
                "target": "关系对象（同上）",
                "dimension": "信任/亲近/敬畏/敌意等",
                "delta": "-20 到 20 的整数",
            }
        ],
    },
    "item_ops": [
        {
            "op": "prop | mention | grant | consume | transfer",
            "item_ref": (
                "grant/consume/transfer 必须使用 entity_candidates 中的稳定引用；"
                "prop/mention 可留空"
            ),
            "label": "prop/mention 的叙事临时物件显示名，不产生库存记录",
            "quantity": "1-100 的正整数",
            "owner_ref": "grant/consume 的角色稳定引用；self 表示当前行动角色",
            "from_owner": "transfer 的来源角色稳定引用；self 表示当前行动角色",
            "to_owner": "transfer 的目标角色稳定引用",
            "reason": "变动原因（写入审计）",
        }
    ],
    "economy_ops": [
        {
            "kind": "credit | debit | transfer | reward | fine | purchase | sale | adjust",
            "currency_id": "世界包声明的货币稳定 ID",
            "amount": "主单位金额",
            "from_owner_type": "player | party | npc | shop | faction | 世界包自定义",
            "from_owner_ref": "所有者稳定 ID",
            "to_owner_type": "同上",
            "to_owner_ref": "同上",
            "reason": "变动原因（写入交易日志）",
        }
    ],
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
            "actor_id": "必须等于插件提供的 next_actor.participant_id",
            "text": "下一位玩家可选择的行动意图，不预设结果",
            "danger_id": (
                "只能填写以下五个固定值之一：safe | controlled | dangerous | "
                "desperate | lethal；禁止填写 danger:xxx、世界事件 ID 或自造值"
            ),
            "check": {
                "required": "布尔值",
                "attribute_id": "attribute 模式填写世界属性 ID；其他模式留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "5-25",
                "known_consequences": "玩家可预见的风险；不能泄露隐藏真相",
                "advantage_sources": ["选项生成时已经成立的优势来源"],
                "disadvantage_sources": ["选项生成时已经成立的劣势来源"],
            },
            "collective": "布尔值；影响全队时为 true（全队决定一律走字母选项，选中后由插件发起集体表决）",
        }
    ],
    "group_decision": {
        "question": "仅旧回合存档兼容：新剧情请勿生成，改用在 next_choices 里把某个选项标记为 collective=true",
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
    "entity_mentions": [
        {
            "type": "character | location | item | ability | status | quest | faction",
            "ref": "插件上下文中已存在的稳定引用",
            "surface": "正文中实际出现的完整名称",
        }
    ],
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
            "removal": "明确解除条件；重创/无法行动类状态需写明恢复途径（医疗道具、治疗技能或剧情）",
        }
    ],
    "fate_consequences": [
        {
            "severity": "serious | lethal",
            "target_actor": "插件提供的玩家 participant_id、完整角色名或副本昵称",
            "source": "本轮已经发生的危险来源",
            "reason": "导致该后果的明确事实",
            "rescue_window": "是否请求进入世界声明的救援窗口",
            "alternatives_shown": "致命后果必须为 true，表示玩家事前看到了替代方案",
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


_NON_NARRATIVE_RULE_KEYS = {
    "context_budget",
    "danger_levels",
    "default_difficulty",
    "difficulty_max",
    "difficulty_min",
    "event_pool",
    "opening_choices",
    "option_presentation",
    "world_stats",
    "internal_world_model_revision",
    "protocol",
}


_CARD_ONLY_SETTING_KEYS = {
    "attribute_progression",
    "factions",
    "origin_regions",
    "power_systems",
    "professions",
    "regions",
    "social_identities",
    "species_and_identities",
}


_NARRATIVE_RULE_KEYS = {
    "allow_player_result_claims",
    "check_density",
    "content_boundaries",
    "content_boundary",
    "death_requires_confirmation",
    "dice_rules",
    "interaction_policy",
    "knowledge_boundary",
    "npc_policy",
    "progress",
    "resolution",
    "return_rules",
    "safe_exit_templates",
    "safety",
    "strict_choices",
}


_NARRATIVE_SETTING_MODULE_KEYS = {
    "canon_policy",
    "terminology",
}


_CHOICE_SCHEMA = {
    "choices": [
        {
            "key": "A | B | C | D",
            "actor_id": "插件提供的 participant_id",
            "text": "不预设结果的行动意图，最多 50 字",
            "danger_id": "safe | controlled | dangerous | desperate | lethal",
            "resolution_kind": "none | check | automatic_consequence | vote_only",
            "check": {
                "required": True,
                "attribute_id": "世界属性稳定 ID；纯骰或免检留空",
                "type": "standard | leader | group | resistance | opposed",
                "difficulty": "由插件按 danger_id 覆盖",
                "known_consequences": "玩家当前可预见的失败后果",
                "advantage_sources": ["已经成立的优势来源"],
                "disadvantage_sources": ["已经成立的劣势来源"],
            },
            "collective": False,
        }
    ]
}



__all__ = [name for name in globals() if not name.startswith('__')]

