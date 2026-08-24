from __future__ import annotations

from .registry import *


_CAPABILITY_SPECS = (
    ("opening", "session", "准备与开局", "sessions", "查看副本准备、开演条件与开场进度。"),
    ("companions", "session", "AI 队友", "sessions", "查看副本中的 AI 队友配置与行动边界。"),
    ("growth", "session", "成长与补充", "characters", "查看角色成长、补充申请与审核进度。"),
    ("economy", "session", "资产与经济", "sessions", "查看当前副本的资源、交易与可用边界。"),
    ("recovery", "session", "保护点与恢复", "audit", "查看快照、恢复影响与已保留的安全点。"),
    ("group-policy", "session", "群组配额与托管", "sessions", "查看群组用量、配额和临时托管状态。"),
    ("world-package", "author", "世界内容包", "worlds", "查看已安装世界、内容能力和安全检查结果。"),
    ("author-edit", "author", "作者编辑", "designer", "编辑字段、预设、常驻角色与声明式界面。"),
    ("author-artifact", "author", "任务产物", "author_jobs", "查看作者任务进度、失败恢复和已生成报告。"),
    ("resolution", "author", "检定与元素表", "designer", "查看世界的检定规则、元素表和反应覆盖。"),
    ("providers", "system", "模型供应链", "health", "查看模型链健康、自动恢复与可用性边界。"),
    ("panel-status", "system", "独立面板", "settings", "查看独立面板启用、连接和安全会话设置。"),
    ("extensions", "system", "扩展与订阅", "modules", "查看公开扩展职责、事件订阅和当前消费者。"),
    ("maintenance", "system", "维护与恢复", "health", "查看安装完整性、备份状态与可安全执行的恢复动作。"),
)


async def capability_panel_projection(
    context: SurfaceContext,
    *,
    visible_sessions: int,
    running_sessions: int,
    attention_services: int,
    visible_services: int,
    groups: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build one role-trimmed, redacted aggregate for all capability tabs."""

    allowed_groups = {"session"}
    if context.roles & {"admin", "author"}:
        allowed_groups.add("author")
    if "admin" in context.roles:
        allowed_groups.add("system")
    if groups is not None:
        allowed_groups.intersection_update(groups)

    world_count = 0
    job_count = 0
    if "author" in allowed_groups:
        list_worlds = getattr(context.database, "list_worlds", None)
        list_jobs = getattr(context.database, "list_author_jobs", None)
        if callable(list_worlds):
            world_count = len(await _maybe_await(list_worlds(False)) or ())
        if callable(list_jobs):
            job_count = len(await _maybe_await(list_jobs(limit=100)) or ())

    stopped_sessions = max(visible_sessions - running_sessions, 0)
    healthy_services = max(visible_services - attention_services, 0)
    # These are deliberately aggregate projections.  This endpoint has no
    # selected-session DTO, so a capability must say that detailed data is not
    # reported instead of manufacturing the reference template's examples.
    panel_details: dict[str, dict[str, Any]] = {
        "opening": {"layout": "opening", "score": running_sessions, "scoreLabel": "可开演副本", "facts": [{"label": "正在运行", "value": running_sessions}, {"label": "其他可见副本", "value": stopped_sessions}], "boundary": "这里只汇总当前角色可见的副本；开局选择、冻结结果和阻塞需进入具体副本核对。"},
        "companions": {"layout": "companions", "score": "未单独报告", "scoreLabel": "AI 队友", "facts": [{"label": "可检查副本", "value": visible_sessions}, {"label": "队友明细", "value": "进入副本查看"}], "boundary": "聚合投影不读取模型推理、私密选择或其他玩家不可见的角色资料。"},
        "growth": {"layout": "growth", "score": "未单独报告", "scoreLabel": "成长进度", "facts": [{"label": "角色工作区", "value": "可进入"}, {"label": "可见副本", "value": visible_sessions}], "boundary": "成长证据、里程碑与补充申请按角色权限在角色工作区读取。"},
        "economy": {"layout": "economy", "score": "按世界声明", "scoreLabel": "经济能力", "facts": [{"label": "可见副本", "value": visible_sessions}, {"label": "资产明细", "value": "未在聚合层读取"}], "boundary": "世界未声明经济能力时不推断钱包、物品价值或交易记录。"},
        "recovery": {"layout": "recovery", "score": "需选择副本", "scoreLabel": "恢复上下文", "facts": [{"label": "可恢复范围", "value": visible_sessions}, {"label": "运行中副本", "value": running_sessions}], "boundary": "恢复必须在具体副本中先预览影响；聚合页面不提供绕过确认的恢复动作。"},
        "group-policy": {"layout": "group-policy", "score": visible_sessions, "scoreLabel": "受策略约束副本", "facts": [{"label": "可见副本", "value": visible_sessions}, {"label": "当前运行", "value": running_sessions}], "boundary": "这里只显示角色可见范围，不暴露平台账号标识、内部群标识或委托对象编号。"},
        "world-package": {"layout": "world-package", "score": world_count, "scoreLabel": "可用世界", "facts": [{"label": "已安装世界", "value": world_count}, {"label": "内容能力", "value": "进入世界查看"}], "boundary": "普通投影只说明世界是否存在；协议字段、编译信息与内部模块键不在此加载。"},
        "author-edit": {"layout": "author-edit", "score": world_count, "scoreLabel": "可编辑世界", "facts": [{"label": "当前世界数", "value": world_count}, {"label": "编辑入口", "value": "世界设计器"}], "boundary": "字段、预设、常驻角色与模拟各自读取当前修订并遵守作者权限。"},
        "author-artifact": {"layout": "author-artifact", "score": job_count, "scoreLabel": "最近作者任务", "facts": [{"label": "可见任务", "value": job_count}, {"label": "阶段产物", "value": "进入任务查看"}], "boundary": "普通页面不显示内部路径、执行器标识、原始异常或技术任务编号。"},
        "resolution": {"layout": "resolution", "score": world_count, "scoreLabel": "可检查世界", "facts": [{"label": "世界内容包", "value": world_count}, {"label": "检定与元素表", "value": "按世界读取"}], "boundary": "未选择世界时不猜测难度、元素关系或结果表；玩家只看到当前行动允许的公开说明。"},
        "providers": {"layout": "providers", "score": healthy_services, "scoreLabel": "正常服务", "facts": [{"label": "已检查服务", "value": visible_services}, {"label": "需要关注", "value": attention_services}], "boundary": "健康检查不冒充真实故事生成；凭证、供应方名称、请求正文与原始错误保持隐藏。"},
        "panel-status": {"layout": "panel-status", "score": "未单独报告", "scoreLabel": "独立访问", "facts": [{"label": "设置入口", "value": "安全与设置"}, {"label": "运行状态", "value": "进入设置核对"}], "boundary": "聚合健康投影没有面板监听配置 DTO，因此不推断端口、Cookie 或跨域策略。"},
        "extensions": {"layout": "extensions", "score": "未单独报告", "scoreLabel": "扩展与订阅", "facts": [{"label": "模块入口", "value": "可进入"}, {"label": "注册详情", "value": "未在聚合层读取"}], "boundary": "扩展按业务能力和消费者展示；技术注册键与原始读取异常不进入普通页面。"},
        "maintenance": {"layout": "maintenance", "score": attention_services, "scoreLabel": "需要关注", "facts": [{"label": "健康检查", "value": visible_services}, {"label": "异常服务", "value": attention_services}], "boundary": "备份、租约和恢复必须使用各自已有的安全动作；缺少预览链时不得执行恢复。"},
    }
    panels: list[dict[str, Any]] = []
    for panel_id, group, label, workspace, summary in _CAPABILITY_SPECS:
        if group not in allowed_groups:
            continue
        if group == "session":
            state = "可用" if visible_sessions else "尚无可见副本"
        elif group == "author":
            state = "可用" if world_count or job_count else "尚无作者内容"
        else:
            state = "需要关注" if attention_services else "正常"
        panels.append(
            {
                "key": panel_id,
                "group": group,
                "label": label,
                "summary": summary,
                "state": state,
                "workspace": workspace,
                **panel_details[panel_id],
                "readonly": not bool(context.roles & {"admin", "host", "author"}),
            }
        )
    return panels


__all__ = ["capability_panel_projection"]
