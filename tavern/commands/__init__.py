"""D1-ARC-001 命令应用层包。

每个命令组模块只编排应用服务，不持有 AstrBot 事件对象；共享契约位于
``tavern/commands/models.py``，平台无关请求上下文复用
``tavern/runtime/request.py`` 的权威定义。

注意：本包初始化只导入纯数据模块（零 AstrBot 顶层依赖），
避免影响 ``tavern/commands/turn_commands.py`` 等兄弟模块的既有导入行为。
"""

from .models import (
    INTENT_CANDIDATE_BUNDLE,
    INTENT_GROUP_NOTICE,
    INTENT_PRIVATE_REPLY,
    INTENT_PERSIST_VERIFIED_TARGET,
    INTENT_REVOKE_PRIVATE_TARGET,
    CardFlowProtocol,
    CommandResult,
    DeliveryIntent,
    ParsedCommand,
    RequestContext,
)

__all__ = [
    "INTENT_CANDIDATE_BUNDLE",
    "INTENT_GROUP_NOTICE",
    "INTENT_PRIVATE_REPLY",
    "INTENT_PERSIST_VERIFIED_TARGET",
    "INTENT_REVOKE_PRIVATE_TARGET",
    "CardFlowProtocol",
    "CommandResult",
    "DeliveryIntent",
    "ParsedCommand",
    "RequestContext",
]
