"""D1 主动消息投递服务层（D1_PLAN 15）。

投递链：``platform.send_by_session`` 优先 → ``context.send_message`` 回退 →
持久化 outbox → 后台租约重试 → WebUI 待送达/失败状态。

本包不在导入阶段触碰 AstrBot 宿主类型；``MessageSession`` 与
``MessageChain`` 一律惰性导入并做多签名构造尝试。导入失败或签名不匹配时
返回结构化 ``unavailable``，调用方继续走回退或 outbox，插件导入不会失败。
"""

from .adapter import DeliveryAdapter, PlatformResolveResult, SendAttemptResult
from .outbox_worker import OutboxWorker, WorkerRunSummary
from .privacy import (
    AUDIENCE_ADMIN,
    AUDIENCE_DM,
    AUDIENCE_GROUP,
    AUDIENCE_PRIVATE_OWNER,
    AUDIENCE_WEBUI_DM,
    channel_label,
    contains_umo,
    find_umo,
    public_failure_notice,
    redact_card_code,
    sanitize_log,
    status_label,
    trim_for_audience,
)
from .retry_policy import (
    add_seconds,
    is_permanently_failed,
    lease_seconds_for,
    max_attempts_for,
    next_retry_at,
    next_retry_delay,
)
from .service import (
    KIND_POLICIES,
    DeliveryOutcome,
    DeliveryOutboxRepository,
    DeliveryService,
    KindPolicy,
)
from .session_factory import (
    MessageChainResult,
    MessageSessionFactory,
    SessionAvailability,
    SessionBuildResult,
)
from .target import (
    TARGET_KIND_CHANNEL,
    TARGET_KIND_GROUP,
    TARGET_KIND_PRIVATE,
    TARGET_KIND_THREAD,
    TARGET_KIND_WEBUI_ONLY,
    TARGET_KINDS,
    DeliveryTarget,
)

__all__ = [
    "AUDIENCE_ADMIN",
    "AUDIENCE_DM",
    "AUDIENCE_GROUP",
    "AUDIENCE_PRIVATE_OWNER",
    "AUDIENCE_WEBUI_DM",
    "DeliveryAdapter",
    "DeliveryOutboxRepository",
    "DeliveryOutcome",
    "DeliveryService",
    "DeliveryTarget",
    "KIND_POLICIES",
    "KindPolicy",
    "MessageChainResult",
    "MessageSessionFactory",
    "OutboxWorker",
    "PlatformResolveResult",
    "SendAttemptResult",
    "SessionAvailability",
    "SessionBuildResult",
    "TARGET_KIND_CHANNEL",
    "TARGET_KIND_GROUP",
    "TARGET_KIND_PRIVATE",
    "TARGET_KIND_THREAD",
    "TARGET_KIND_WEBUI_ONLY",
    "TARGET_KINDS",
    "WorkerRunSummary",
    "add_seconds",
    "channel_label",
    "contains_umo",
    "find_umo",
    "is_permanently_failed",
    "lease_seconds_for",
    "max_attempts_for",
    "next_retry_at",
    "next_retry_delay",
    "public_failure_notice",
    "redact_card_code",
    "sanitize_log",
    "status_label",
    "trim_for_audience",
]
