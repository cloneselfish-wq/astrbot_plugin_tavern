from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class PluginModuleSpec:
    id: str
    label: str
    layer: str
    description: str
    dependencies: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    required: bool = False
    runtime_visible: bool = False
    webui_surface: str = "系统模块"

    def export(self) -> dict[str, object]:
        result = asdict(self)
        result["dependencies"] = list(self.dependencies)
        result["capabilities"] = list(self.capabilities)
        return result


def _m(
    id: str,
    label: str,
    layer: str,
    description: str,
    *,
    depends: tuple[str, ...] = (),
    caps: tuple[str, ...] = (),
    required: bool = False,
    runtime: bool = False,
    surface: str = "系统模块",
) -> PluginModuleSpec:
    return PluginModuleSpec(
        id=id,
        label=label,
        layer=layer,
        description=description,
        dependencies=depends,
        capabilities=caps,
        required=required,
        runtime_visible=runtime,
        webui_surface=surface,
    )


# 27 个明确边界。它们是运行期模块契约，不是仅用于文档展示的名称清单。
PLUGIN_MODULES: tuple[PluginModuleSpec, ...] = (
    _m("core", "核心业务", "core", "生命周期、共享上下文和领域规则。", required=True, caps=("runtime.context",)),
    _m("entrypoint", "宿主入口", "adapter", "AstrBot 事件接入、启动与关闭编排。", depends=("core",), required=True, caps=("host.lifecycle",)),
    _m("web_api", "Web API", "interface", "控制器路由、鉴权和统一错误响应。", depends=("core",), required=True, caps=("web.routes",)),
    _m("web_ui", "WebUI", "interface", "控制台页面、交互反馈和响应式视图。", depends=("web_api",), required=True, caps=("web.console",)),
    _m("frontend_state", "前端状态", "interface", "页面状态、缓存和实时刷新协调。", depends=("web_ui",), caps=("web.state",)),
    _m("database", "数据库", "infrastructure", "SQLite 事务、迁移和仓储边界。", depends=("core",), required=True, caps=("storage.sql",)),
    _m("state_machine", "状态机", "domain", "副本、回合、投票和建卡状态转换。", depends=("database",), required=True, runtime=True, surface="跑团现场", caps=("session.state",)),
    _m("ai_pipeline", "AI 调用链", "service", "模型选择、回退、限流和生成调用。", depends=("state_machine",), runtime=True, surface="跑团现场", caps=("ai.generate",)),
    _m("prompts", "提示词", "domain", "提示词装配、上下文预算和角色边界。", depends=("ai_pipeline",), caps=("ai.prompt",)),
    _m("model_parser", "模型输出解析", "domain", "结构化输出、修复和降级解析。", depends=("ai_pipeline",), caps=("ai.parse",)),
    _m("platforms", "平台适配", "adapter", "AstrBot 全平台文本能力归一化。", depends=("entrypoint",), required=True, caps=("platform.text",)),
    _m("notifications", "通知投递", "service", "主动文本投递、补偿队列与重试。", depends=("platforms", "database"), runtime=True, surface="跑团现场", caps=("delivery.outbox",)),
    _m("timers", "计时器", "service", "回合、投票、准备和提醒计时。", depends=("state_machine",), runtime=True, surface="跑团现场", caps=("runtime.timer",)),
    _m("permissions", "权限", "security", "管理员、DM、玩家和危险操作授权。", depends=("core",), required=True, caps=("security.policy",)),
    _m("human_dm", "人工 DM", "domain", "接管、直述、推进、交棒和导演指引。", depends=("permissions", "state_machine"), runtime=True, surface="跑团现场", caps=("dm.control",)),
    _m("delegation", "托管代操作", "domain", "角色托管、强制代选和控制权恢复。", depends=("permissions", "state_machine"), runtime=True, surface="跑团现场", caps=("player.delegate",)),
    _m("resources", "资源与经济", "domain", "背包、任务物品、资源池和钱包交易。", depends=("database",), runtime=True, surface="跑团现场", caps=("world.resources", "world.economy")),
    _m("relationships", "关系", "domain", "角色、NPC、队伍关系和阶段变化。", depends=("database",), runtime=True, surface="跑团现场", caps=("world.relationships",)),
    _m("saves", "存档恢复", "service", "快照、命名存档、回滚和恢复检查。", depends=("database", "state_machine"), required=True, runtime=True, surface="跑团现场", caps=("session.snapshot",)),
    _m("backups", "备份", "service", "完整备份、导入校验与自动保留。", depends=("database",), caps=("storage.backup",)),
    _m("extensions", "扩展点", "platform", "公开服务、能力注册和只读事件钩子。", depends=("core",), caps=("extension.registry",)),
    _m("configuration", "配置", "infrastructure", "配置解析、校验、保存和版本化。", depends=("core",), required=True, caps=("config.runtime",)),
    _m("audit", "审计诊断", "operations", "审计日志、脱敏诊断和健康信息。", depends=("database",), required=True, caps=("ops.audit",)),
    _m("errors", "错误处理", "operations", "稳定错误码、上下文报告和用户反馈。", depends=("core",), required=True, caps=("ops.errors",)),
    _m("tests", "质量验证", "quality", "单元、集成、传输和发布回归检查。", depends=("core",), caps=("quality.tests",)),
    _m("documentation", "文档", "quality", "使用、架构和运维说明。", depends=("core",), caps=("quality.docs",)),
    _m("release", "发布打包", "quality", "版本同步、清单、可重复打包和交付校验。", depends=("tests", "documentation"), caps=("quality.release",)),
)

assert len(PLUGIN_MODULES) == 27

__all__ = ["PLUGIN_MODULES", "PluginModuleSpec"]
