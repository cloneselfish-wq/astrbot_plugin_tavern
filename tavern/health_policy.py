"""health thresholds and public recovery metadata.

The WebUI only renders this policy; it must not duplicate thresholds or infer
whether a component is safe from record counts alone.
"""

from __future__ import annotations

from dataclasses import dataclass


HEALTH_STATES = frozenset({"ready", "degraded", "blocked", "maintenance"})


@dataclass(frozen=True, slots=True)
class HealthThreshold:
    degraded_after_seconds: int
    blocked_after_seconds: int


OUTBOX_THRESHOLD = HealthThreshold(30, 300)
AUTHOR_JOB_THRESHOLD = HealthThreshold(60, 300)
AUTHOR_LEASE_BLOCKED_SECONDS = 120
OPERATION_RECOVERY_FAILURE_LIMIT = 3
PROJECTION_DEGRADED_LAG = 3
PROJECTION_BLOCKED_LAG = 20
BACKUP_DEGRADED_SECONDS = 24 * 60 * 60
BACKUP_BLOCKED_SECONDS = 7 * 24 * 60 * 60


COMPONENT_COPY = {
    "database": {
        "label": "数据库",
        "automatic_action": "异常时系统停止不安全写入",
        "next_label": "导出脱敏诊断",
    },
    "migration": {
        "label": "数据库迁移",
        "automatic_action": "保留原数据库与迁移前备份",
        "next_label": "查看迁移结果",
    },
    "delivery_outbox": {
        "label": "消息投递",
        "automatic_action": "系统回收过期租约并按退避策略继续处理",
        "next_label": "重试失败投递",
    },
    "storage_outbox": {
        "label": "副本存储",
        "automatic_action": "系统按目录权威版本重新同步",
        "next_label": "重试失败同步",
    },
    "event_outbox": {
        "label": "事件投递",
        "automatic_action": "系统回收过期租约并按退避策略继续处理",
        "next_label": "重试失败事件",
    },
    "projection": {
        "label": "数据投影",
        "automatic_action": "系统优先增量追赶，出现缺口时要求完整重建",
        "next_label": "重建指定投影",
    },
    "operations": {
        "label": "操作回执",
        "automatic_action": "系统回收过期生成租约，不重放已提交业务",
        "next_label": "释放过期租约",
    },
    "author_jobs": {
        "label": "作者任务",
        "automatic_action": "系统回收过期租约并按任务策略重试",
        "next_label": "查看或重试任务",
    },
    "world_integrity": {
        "label": "世界完整性",
        "automatic_action": "保留上一份可用世界，不覆盖运行中副本快照",
        "next_label": "重新检查世界",
    },
    "provider_health": {
        "label": "模型服务",
        "automatic_action": "只使用世界声明允许的可用服务与回退",
        "next_label": "重新探测服务",
    },
    "backup": {
        "label": "备份",
        "automatic_action": "新建备份时不覆盖已有备份",
        "next_label": "创建并验证备份",
    },
    "release_artifact": {
        "label": "发布产物",
        "automatic_action": "发现版本或校验不一致时阻止继续发布",
        "next_label": "重新干净构建",
    },
}


def timed_state(
    age_seconds: int,
    *,
    has_retry: bool = False,
    has_permanent_failure: bool = False,
    threshold: HealthThreshold = OUTBOX_THRESHOLD,
) -> str:
    if has_permanent_failure or age_seconds > threshold.blocked_after_seconds:
        return "blocked"
    if has_retry or age_seconds >= threshold.degraded_after_seconds:
        return "degraded"
    return "ready"


def projection_state(lag: int, *, failed: bool = False) -> str:
    if failed or lag > PROJECTION_BLOCKED_LAG:
        return "blocked"
    if lag > PROJECTION_DEGRADED_LAG:
        return "degraded"
    return "ready"


__all__ = [
    "AUTHOR_JOB_THRESHOLD",
    "AUTHOR_LEASE_BLOCKED_SECONDS",
    "BACKUP_BLOCKED_SECONDS",
    "BACKUP_DEGRADED_SECONDS",
    "COMPONENT_COPY",
    "HEALTH_STATES",
    "OUTBOX_THRESHOLD",
    "OPERATION_RECOVERY_FAILURE_LIMIT",
    "PROJECTION_BLOCKED_LAG",
    "PROJECTION_DEGRADED_LAG",
    "HealthThreshold",
    "projection_state",
    "timed_state",
]
