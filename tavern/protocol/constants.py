from __future__ import annotations

TWP_VERSION = "1.0.0-rc10"
TWP_CORE_VERSION = "1.0.0-rc10"
TWP_MODULE_API_VERSION = "1.0.0-rc10"

TWP_FORMAT = "astrbot-tavern-world"
TWP_PACKAGE_FORMAT = 2
TWP_NAME = "twp"
TWP_COMPILER_ABI = "1.0.0-rc10"
TWP_MATURITY = "rc"
TWP_ARTIFACT_SCHEMA = "twp-artifact/1.0.0-rc10"
TWP_COMPILED_WORLD_SCHEMA = "twp-world-model/1.0.0-rc10"
TWP_RUNTIME_SCHEMA = "twp-runtime/1.0.0-rc10"
TWP_COMMAND_SCHEMA = "twp-command/1.0.0-rc10"

THIRTEENTH_SEAT_PACKAGE_ID = "builtin.thirteenth-seat-new-era"
THIRTEENTH_SEAT_NAMESPACE = "builtin.thirteenth-seat"
THIRTEENTH_SEAT_CONTENT_VERSION = "1.0.0-rc10"

RUNTIME_STATUSES = frozenset(
    {
        "initialized",
        "disabled",
        "not_applicable",
        "empty",
        "degraded",
        "corrupt",
    }
)

VIEWER_ROLES = frozenset(
    {"player", "character", "dm", "admin", "author", "remote"}
)

STANDARD_MODULES = (
    "actor",
    "scene_graph",
    "quest_graph",
    "knowledge_graph",
    "npc_lifecycle",
    "faction_state",
    "time_clock",
    "relationship_graph",
    "items_inventory",
    "crafting",
    "economy",
    "capability_effects",
    "progression",
    "challenge_engine",
    "actor_fate",
    "terminal_conditions",
    "ending",
    "chat_experience",
    "human_dm",
    "maps_handouts",
    "localization",
    "distribution",
    "simulation",
    "elemental_interactions",
    "evidence_ledger",
    "accords",
    "assembly",
    "rumor_network",
    "scene_environment",
)

OPTIONAL_MODULES = frozenset(
    {
        "relationship_graph",
        "items_inventory",
        "crafting",
        "economy",
        "capability_effects",
        "progression",
        "challenge_engine",
        "actor_fate",
        "terminal_conditions",
        "ending",
        "maps_handouts",
        "localization",
        "distribution",
        "simulation",
        "rumor_network",
        "scene_environment",
    }
)

CORE_CAPABILITIES = (
    {
        "id": "manifest",
        "label": "包清单",
        "description": "声明世界包身份、内容版本、入口文件、模块和依赖。",
    },
    {
        "id": "dependency_graph",
        "label": "依赖图",
        "description": "按模块依赖顺序加载，并拒绝缺失或循环依赖。",
    },
    {
        "id": "deterministic_compile",
        "label": "确定性编译",
        "description": "相同源文件和模块开关生成相同 Artifact 与内容哈希。",
    },
    {
        "id": "frozen_artifact",
        "label": "冻结 Artifact",
        "description": "副本锁定已验证的编译结果，避免运行中规则漂移。",
    },
    {
        "id": "stable_errors",
        "label": "稳定错误码",
        "description": "导入、引用、命令和运行态错误使用可追踪的稳定代码。",
    },
    {
        "id": "safe_archive",
        "label": "安全导入边界",
        "description": "限制文件数量、体积、路径和成员类型，拒绝可执行内容。",
    },
)

MODULE_METADATA = {
    "actor": ("角色与建卡", "角色模板、预设、能力、属性和初始资源。"),
    "scene_graph": ("场景图", "场景节点、连通关系、进入条件和场景切换。"),
    "quest_graph": ("任务图", "任务、目标、前置条件、分支和完成结果。"),
    "knowledge_graph": ("知识图", "世界事实、可见性、发现条件和知识引用。"),
    "npc_lifecycle": ("NPC 生命周期", "常驻与动态 NPC 的创建、出场、状态和退场。"),
    "faction_state": ("阵营状态", "阵营关系、声望、阶段和世界级影响。"),
    "time_clock": ("时间与时钟", "游戏时间、场景时钟、倒计时和触发条件。"),
    "relationship_graph": ("关系图", "角色、NPC、阵营之间的关系与变化。"),
    "items_inventory": ("物品与背包", "物品定义、持有者、数量、装备和任务物品。"),
    "crafting": ("制作", "配方、材料、工程、产物和制作限制。"),
    "economy": ("经济", "货币、钱包、商店、价格、库存和交易账本。"),
    "capability_effects": ("能力效果", "能力、状态、效果计划、冲突与应用结果。"),
    "progression": ("成长", "经验、里程碑、解锁、专精和成长奖励。"),
    "challenge_engine": ("遭遇", "遭遇配置、敌对单位、阶段和结算。"),
    "actor_fate": ("角色命运", "角色命运状态、合法转换、救援窗口与行动资格。"),
    "terminal_conditions": ("自动终局", "按世界声明的条件、优先级和策略完成结局与归档。"),
    "ending": ("结局", "结局条件、准备度、锁定与最终提交。"),
    "chat_experience": ("群聊体验", "多人回合、行动选项、表决和移动端文本体验。"),
    "human_dm": ("人工 DM", "真人主持接管、指令、直述和受控干预。"),
    "maps_handouts": ("地图与手册", "地图、手册、图片和按条件解锁的辅助资料。"),
    "localization": ("本地化", "中文名称、说明、词条和缺失翻译检查。"),
    "distribution": ("分发", "内容统计、包检查、构建报告和发布信息。"),
    "simulation": ("模拟测试", "角色构筑、机械效果、冲突和可执行性模拟。"),
    "elemental_interactions": ("元素交互", "方向性关系、暴露、衰减、环境修正和统一结算回执。"),
    "evidence_ledger": ("证据账本", "来源、保管链、完整度、可靠度、矛盾和公开范围。"),
    "accords": ("承诺与协定", "政治承诺、担保、履约、违约、争议和债务后果。"),
    "assembly": ("听证与会盟", "资格、法定人数、议程、证人、动议、表决和认证。"),
    "rumor_network": ("传闻网络", "传播范围、可信度、失真、反证和公共影响。"),
    "scene_environment": ("场景环境", "环境状态、来源、强度、持续时间与跨模块影响。"),
}

# 世界卡片只接受这组面向玩家的短标签。世界作者源通过
# ``display_tags`` 选择最多四项；标签文字由插件统一维护，避免世界包
# 自行注入任意文案，也避免 WebUI 根据内容数量猜测玩法。
WORLD_TAG_PRESETS = {
    "solo": "单人可玩",
    "party": "多人协作",
    "short_campaign": "短团",
    "long_campaign": "长篇",
    "mystery": "悬疑调查",
    "deduction": "逻辑推理",
    "evidence": "证据追踪",
    "exploration": "探索发现",
    "survival": "生存抉择",
    "combat": "战术战斗",
    "social": "社交交涉",
    "political": "政治博弈",
    "diplomacy": "外交会盟",
    "factions": "阵营经营",
    "promises": "承诺债务",
    "voting": "议程表决",
    "countdown": "限时推进",
    "resource_management": "资源取舍",
    "progression": "角色成长",
    "crafting": "制作经营",
    "economy": "交易经济",
    "elemental": "元素互动",
    "supernatural": "超自然",
    "low_supernatural": "弱超自然",
    "high_stakes": "高后果",
    "open_world": "开放探索",
    "branching": "多线分支",
    "ensemble": "群像叙事",
    "roleplay": "角色扮演",
    "horror": "惊悚氛围",
    "puzzle": "谜题解密",
    "investigation": "现场勘查",
}

STABLE_ERROR_CODES = frozenset(
    {
        "protocol.unsupported",
        "protocol.manifest_invalid",
        "protocol.integrity_mismatch",
        "module.dependency_missing",
        "module.dependency_disabled",
        "module.runtime_schema_invalid",
        "reference.target_missing",
        "reference.type_mismatch",
        "reference.private_leak",
        "command.schema_invalid",
        "command.permission_denied",
        "command.plan_stale",
        "runtime.revision_conflict",
        "operation.idempotency_conflict",
        "transaction.partial_write",
        "cascade.target_invalid",
        "cascade.limit_exceeded",
        "migration.chain_missing",
        "migration.runtime_unmapped",
        "migration.rollback_failed",
        "projection.visibility_denied",
        "asset.missing",
        "localization.key_missing",
    }
)
