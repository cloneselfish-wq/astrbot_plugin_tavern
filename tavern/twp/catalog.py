from __future__ import annotations

# TWP protocol catalog.
from dataclasses import asdict, dataclass


# TWP 标准协议能力：由导入器、编译器和运行快照共同执行。
CORE_CAPABILITIES: tuple[dict[str, str], ...] = (
    {"id": "manifest", "label": "清单信封", "version": "1.0.0-rc10", "description": "统一身份、版本和入口。"},
    {"id": "layout", "label": "多文件布局", "version": "1.0.0-rc10", "description": "世界、模块与资源分离。"},
    {"id": "namespace", "label": "命名空间与稳定 ID", "version": "1.0.0-rc10", "description": "跨模块引用不依赖显示名。"},
    {"id": "module_lifecycle", "label": "模块生命周期", "version": "1.0.0-rc10", "description": "声明、启用、关闭与必需模块。"},
    {"id": "dependencies", "label": "依赖图", "version": "1.0.0-rc10", "description": "有向无环依赖与确定性装载。"},
    {"id": "compiler", "label": "确定性编译", "version": "1.0.0-rc10", "description": "生成规范运行快照和内容哈希。"},
    {"id": "migrations", "label": "内容版本迁移", "version": "1.0.0-rc10", "description": "面向未来内容版本的声明式升级。"},
    {"id": "validation", "label": "结构与语义校验", "version": "1.0.0-rc10", "description": "路径化错误、引用与边界检查。"},
    {"id": "conformance", "label": "一致性测试", "version": "1.0.0-rc10", "description": "包内场景与断言可重复执行。"},
    {"id": "snapshot_integrity", "label": "快照与完整性", "version": "1.0.0-rc10", "description": "散列、冻结运行态和安全资源。"},
)


@dataclass(frozen=True, slots=True)
class GameplayModuleSpec:
    id: str
    label: str
    release: str
    description: str
    dependencies: tuple[str, ...] = ()
    runtime_surface: str = "跑团现场"

    def export(self) -> dict[str, object]:
        result = asdict(self)
        result["dependencies"] = list(self.dependencies)
        return result


# 核心玩法模块 7 项。
LAUNCH_MODULES: tuple[GameplayModuleSpec, ...] = (
    GameplayModuleSpec("scene_graph", "场景图", "launch", "场景节点、出口、进入条件和回访状态。"),
    GameplayModuleSpec("quest_graph", "任务图", "launch", "任务、目标、分支、失败与结算。", ("scene_graph",)),
    GameplayModuleSpec("knowledge_graph", "知识图", "launch", "公开、私密、角色知识和揭示条件。"),
    GameplayModuleSpec("npc_lifecycle", "NPC 生命周期", "launch", "动机、日程、出场、离场与永久变化。", ("scene_graph",)),
    GameplayModuleSpec("faction_state", "阵营世界状态", "launch", "势力资源、立场、关系和世界推进。"),
    GameplayModuleSpec("chat_experience", "群聊体验", "launch", "聚光灯、缺席、安全工具、回顾与投递策略。"),
    GameplayModuleSpec("human_dm", "人工 DM 控制", "launch", "权限、导演层、秘密信息和裁定接管。", ("knowledge_graph",)),
)

# 后续模块 7 项；协议首版即稳定声明，模板默认关闭，可独立启用。
LATER_MODULES: tuple[GameplayModuleSpec, ...] = (
    GameplayModuleSpec("challenge_engine", "遭遇模板", "later", "可复用遭遇、阶段和胜负条件。", ("scene_graph",)),
    GameplayModuleSpec("progression", "成长系统", "later", "里程碑、能力解锁和成长轨迹。", ("quest_graph",)),
    GameplayModuleSpec("crafting", "制作系统", "later", "配方、材料、工序和制作风险。"),
    GameplayModuleSpec("localization", "本地化", "later", "多语言文本键、回退与术语表。"),
    GameplayModuleSpec("maps_handouts", "地图与手册", "later", "地图、手册、音频和可见性策略。", ("scene_graph",)),
    GameplayModuleSpec("distribution", "分发与签名", "later", "作者签名、外部依赖和更新通道。"),
    GameplayModuleSpec("simulation", "模拟与场景测试", "later", "批量场景、断言和世界状态回归。"),
)

GAMEPLAY_MODULES = LAUNCH_MODULES + LATER_MODULES
assert len(CORE_CAPABILITIES) == 10
assert len(LAUNCH_MODULES) == 7
assert len(LATER_MODULES) == 7


def module_catalog() -> list[dict[str, object]]:
    return [item.export() for item in GAMEPLAY_MODULES]


__all__ = [
    "CORE_CAPABILITIES",
    "GAMEPLAY_MODULES",
    "LATER_MODULES",
    "LAUNCH_MODULES",
    "GameplayModuleSpec",
    "module_catalog",
]
