"""D1-ARC-001 建卡命令应用层共享契约（平台无关，导入阶段零 AstrBot 依赖）。

本模块是命令应用层的公共数据模型与依赖协议：

- ``RequestContext``：重导出 ``tavern/runtime/request.py`` 的权威上下文，
  是命令处理器唯一输入载体；入口适配层（``tavern/entry/event_context.py``）
  负责从平台事件构建，业务模块永远不接触 AstrBot 事件对象。
- ``ParsedCommand``：重导出 ``tavern/security.py`` 的已解析命令。
- ``CommandResult``：处理结果——外显文本、投递意图与是否已消费（handled）。
- ``DeliveryIntent``：结构化投递副作用（候选批次、群通知、绑定持久化、
  私聊目标降级），由入口适配层执行实际平台 I/O，应用层只声明意图。
- ``CardFlowProtocol``：建卡命令对数据层的依赖协议（鸭子类型），
  main.py 接入 ``TavernDatabase``（``repositories/characters.py``）时自然满足。

禁止在本模块 import 任何 AstrBot 宿主类型。
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from ..runtime.contracts import CommandResult, DeliveryIntent
from ..runtime.request import RequestContext
from ..security import ParsedCommand

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

# 投递意图类型。入口适配层按 kind 分发到对应平台操作：
# - candidate_bundle：发送候选批次物理段并持久化投递游标；
# - group_notice：群聊通知（如角色卡建成）；
# - persist_verified_target：私聊验证成功后持久化权威投递目标；
# - revoke_private_target：席位放弃/退场时降级已验证私聊目标。
INTENT_CANDIDATE_BUNDLE = "candidate_bundle"
INTENT_GROUP_NOTICE = "group_notice"
INTENT_PRIVATE_REPLY = "private_reply"
INTENT_PERSIST_VERIFIED_TARGET = "persist_verified_target"
INTENT_REVOKE_PRIVATE_TARGET = "revoke_private_target"


@runtime_checkable
class CardFlowProtocol(Protocol):
    """建卡命令对数据层的依赖协议（由 TavernDatabase 实现）。

    只包含本服务使用到的读取与写入方法；签名与
    ``tavern/repositories/characters.py`` 一致。
    """

    async def card_draft_for_private(
        self,
        private_origin: str,
    ) -> dict[str, Any] | None: ...

    async def bind_card_code(
        self,
        code: str,
        private_user_id: str,
        private_origin: str,
    ) -> dict[str, Any]: ...

    async def preview_card_draft(self, private_origin: str) -> dict[str, Any]: ...

    async def modify_card_field(
        self,
        private_origin: str,
        field_reference: str,
    ) -> dict[str, Any]: ...

    async def fill_card_draft(
        self,
        private_origin: str,
        value: str,
        source_event_id: str = "",
    ) -> dict[str, Any]: ...

    async def previous_card_step(self, private_origin: str) -> dict[str, Any]: ...

    async def restart_card_draft(self, private_origin: str) -> dict[str, Any]: ...

    async def reset_card_draft_stats(
        self,
        private_origin: str,
    ) -> dict[str, Any]: ...

    async def set_card_completion_reminder(
        self,
        private_origin: str,
        enabled: bool | None,
    ) -> dict[str, Any]: ...

    async def confirm_card_draft(self, private_origin: str) -> dict[str, Any]: ...

    async def cancel_card_draft(self, private_origin: str) -> dict[str, Any]: ...

    async def abandon_card_seat(self, private_origin: str) -> dict[str, Any]: ...

    async def get_session(self, session_id: str) -> dict[str, Any]: ...
