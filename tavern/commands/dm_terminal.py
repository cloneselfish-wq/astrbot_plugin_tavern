"""WP-02 主持命令应用层（D1-ARC-001）。

平台无关的 :class:`DMCommandService`：只编排仓储方法与投递服务，不接触
AstrBot 事件对象、不拼接 UMO、不自行写数据库。设计目标：

- 权限统一走 ``tavern.permissions.can_manage_dm``（可注入，宿主声明的
  ``is_admin`` 作为管理员放行通道）；
- 所有写操作委托注入的 ``database`` 仓储方法，本模块不含任何 SQL 写入；
- 投递只构造 :class:`DeliveryIntent``（``DeliveryService.build_record``
  纯构造，无 I/O），由宿主决定入队或回退到原频道回复；
- 密语目标解析意图：支持群用户 ID、角色名/代号与副本参与者 ID（主持
  工具内部使用）；缺失私聊来源时严格降级为 ``webui_only``，绝不回退群聊；
- 玩家可见文本不含 UMO、稳定 ID、JSON/表名字段；
- 错误统一为四要素：``code / message / recovery / correlation_id``。

当前接线状态（诚实声明）：

- 终局确认（``terminal_confirm``）：``terminal_receipts(status='pending')``
  已由 fate 链路写入，但仓储层尚无消费 pending 回执的完结入口；本服务通过
  注入的 ``terminal_finalizer`` 调用宿主完结应用服务，未注入时返回
  ``dm.terminal_confirm_unavailable``，不假装已归档。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from ..config import TavernConfig
from ..constants import SESSION_FINISHED
from ..database import DatabaseNotFoundError, InvalidTransitionError
from ..delivery.service import DeliveryService
from ..delivery.target import TARGET_KIND_WEBUI_ONLY, DeliveryTarget
from ..errors import PolicyRejection
from ..permissions import can_manage_dm, is_plugin_admin
from ..runtime.contracts import CommandError, CommandResult, DeliveryIntent


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


def _format_turn_status(turn: Mapping[str, Any]) -> str:
    """惰性导入玩家可见回合状态行（presentation 依赖宿主日志对象）。"""

    from ..presentation import format_turn_status

    return format_turn_status(turn)


@dataclass(frozen=True, slots=True)
class DMRequest:
    """平台无关的主持请求上下文（由宿主从事件或 Web 会话构造）。"""

    session_id: str
    user_id: str
    actor: str = ""
    is_admin: bool = False
    correlation_id: str = ""
    group_origin: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", str(self.session_id or "").strip())
        object.__setattr__(self, "user_id", str(self.user_id or "").strip())
        object.__setattr__(self, "actor", str(self.actor or self.user_id).strip())
        if not self.session_id or not self.user_id:
            raise ValueError("DM 请求缺少副本或用户身份")


class DMCommandError(ValueError):
    """主持命令应用层可预期错误（玩家可见消息来自 str(exc)）。"""

    code = "dm.invalid_input"
    recovery = "请按命令格式重新输入。"


class TargetNotFoundError(DMCommandError):
    code = "dm.target_not_found"
    recovery = "请核对角色名或群内用户 ID 后重新输入。"


class TargetAmbiguousError(DMCommandError):
    code = "dm.target_ambiguous"
    recovery = "请改用副本内唯一代号或群内用户 ID。"


class TerminalNonePendingError(DMCommandError):
    code = "dm.terminal_none_pending"
    recovery = "请先确认终局条件是否满足，或联系管理员。"


class ArchiveReadonlyError(PolicyRejection):
    """副本已永久归档（只读）：主持写操作统一拒绝。"""


class TerminalConfirmUnavailableError(PolicyRejection):
    """终局待确认已记录，但宿主尚未接线完结应用服务。"""


@runtime_checkable
class TerminalFinalizer(Protocol):
    """宿主注入的终局完结回调（仓储层尚无 pending 回执消费入口）。"""

    async def __call__(
        self,
        session_id: str,
        receipt: Mapping[str, Any],
        request: DMRequest,
    ) -> dict[str, Any]: ...


PermissionChecker = Callable[
    [Any, Any, str, Any, str],
    Awaitable[bool],
]


def _error_result(
    exc: BaseException,
    correlation_id: str,
) -> CommandResult:
    """把领域/应用异常映射为四要素错误结果（堆栈只进日志，不进文案）。"""

    if isinstance(exc, TargetNotFoundError):
        code, recovery = exc.code, exc.recovery
        message = str(exc) or "找不到目标角色，操作未执行。"
    elif isinstance(exc, TargetAmbiguousError):
        code, recovery = exc.code, exc.recovery
        message = str(exc) or "角色标识不唯一，操作未执行。"
    elif isinstance(exc, TerminalNonePendingError):
        code, recovery = exc.code, exc.recovery
        message = str(exc) or "当前副本没有待确认的终局。"
    elif isinstance(exc, ArchiveReadonlyError):
        code = "dm.archived"
        message = "副本已永久归档，系统未修改任何数据。"
        recovery = "如需继续，请从最终存档克隆新副本。"
    elif isinstance(exc, TerminalConfirmUnavailableError):
        code = "dm.terminal_confirm_unavailable"
        message = "终局确认暂不可用，系统已保留待办，未归档任何数据。"
        recovery = "请使用 WebUI 完结入口，或联系插件维护者完成接线。"
    elif isinstance(exc, PermissionError) or isinstance(exc, PolicyRejection):
        code = "dm.permission_denied"
        message = "当前账号没有主持权限，操作未执行。"
        recovery = "请联系副本主持人或插件管理员授权。"
    elif isinstance(exc, DatabaseNotFoundError):
        code = "dm.not_found"
        message = "找不到目标副本或角色，操作未执行。"
        recovery = "请核对名称后重新输入。"
    elif isinstance(exc, InvalidTransitionError):
        code = "dm.invalid_state"
        message = str(exc) or "当前副本状态不允许该操作，系统未修改数据。"
        recovery = "请先发送 /团 主持 状态 查看当前状态后再试。"
    elif isinstance(exc, ValueError):
        code = "dm.invalid_input"
        message = str(exc) or "输入不合法，操作未执行。"
        recovery = "请按命令格式重新输入。"
    else:
        code = "dm.command_failed"
        message = "主持操作执行失败，系统未修改任何数据。"
        recovery = "请稍后重试；若持续失败请联系插件维护者。"
    return CommandResult(
        ok=False,
        status=code,
        message=message,
        next_action=recovery,
        error=CommandError(
            code=code,
            message=message,
            recovery=recovery,
            correlation_id=correlation_id,
        ),
        data={"correlation_id": correlation_id},
    )




class DmTerminalMixin:
    async def lock_input(
        self,
        session_id: str,
        request: DMRequest,
        *,
        locked: bool,
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._lock_input_impl(session_id, request, locked),
        )
    async def _lock_input_impl(
        self,
        session_id: str,
        request: DMRequest,
        locked: bool,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        await self.database.set_input_lock(
            session_id,
            bool(locked),
            request.actor,
        )
        if locked:
            return "【玩家输入已锁定】所有玩家暂停输入。", "input_locked", {}, None
        return "【玩家输入已解锁】玩家可以继续输入。", "input_unlocked", {}, None
    async def checkpoint(
        self,
        session_id: str,
        request: DMRequest,
        *,
        name: str = "DM检查点",
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._checkpoint_impl(session_id, request, name),
        )
    async def _checkpoint_impl(
        self,
        session_id: str,
        request: DMRequest,
        name: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        checkpoint_name = str(name or "").strip() or "DM检查点"
        session = await self.database.get_session(session_id)
        result = await self.database.create_snapshot(
            session_id,
            checkpoint_name,
            request.actor,
            replace=False,
            expected_revision=int(session.get("revision") or 0),
            idempotency_key=(
                f"dm-checkpoint:{request.correlation_id or session_id}:"
                f"{checkpoint_name}:{int(session.get('revision') or 0)}"
            ),
        )
        snapshot = result["snapshot"]
        return (
            f"【检查点已创建】\n名称：{checkpoint_name}",
            "checkpoint_created",
            {
                "snapshot_name": str(snapshot.get("name") or checkpoint_name),
                "turn_no": int(snapshot.get("turn_no") or 0),
            },
            None,
        )
    async def terminal_confirm(
        self,
        session_id: str,
        request: DMRequest,
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._terminal_confirm_impl(session_id, request),
        )
    async def _terminal_confirm_impl(
        self,
        session_id: str,
        request: DMRequest,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        receipts = await self.database.execute_read(
            """
            SELECT * FROM terminal_receipts
            WHERE session_id = ? AND status = 'pending'
            ORDER BY priority DESC, created_at ASC
            """,
            (session_id,),
        )
        if not receipts:
            raise TerminalNonePendingError("当前副本没有待确认的终局。")
        receipt = dict(receipts[0])
        if self.terminal_finalizer is None:
            raise TerminalConfirmUnavailableError("终局确认尚未接线")
        result = dict(
            await self.terminal_finalizer(session_id, receipt, request)
        )
        message = str(
            result.get("message")
            or "【终局已确认】副本已按世界终局规则完成永久只读归档。"
        )
        return (
            message,
            str(result.get("status") or "finalized"),
            {
                "receipt": {
                    "condition_label": str(
                        receipt.get("condition_label") or ""
                    ),
                    "termination_type": str(
                        receipt.get("termination_type") or ""
                    ),
                    "ending_ref": str(receipt.get("ending_ref") or ""),
                },
                "result": result,
            },
            None,
        )
    def _build_group_intent(
        self,
        session_id: str,
        request: DMRequest,
        *,
        text: str,
        kind: str,
        mode: str,
    ) -> DeliveryIntent | None:
        mode = str(mode or "group").strip()
        if mode == "none":
            return None
        if mode == "webui_only":
            target = DeliveryTarget.webui_only(source="dm_command_webui_policy")
            stored_kind = "webui_notice"
            audience = "group"
        else:
            origin = str(request.group_origin or "").strip()
            target = DeliveryTarget.from_origin(
                origin,
                verified_binding=True,
                source="dm_command_group",
            )
            if target is None:
                return None
            stored_kind = "group_notice"
            audience = "group"
        record = self.delivery.build_record(
            session_id=session_id,
            target=target,
            kind=stored_kind,
            text=text,
            audience=audience,
            dedupe_key=f"dm:{session_id}:{kind}:{uuid.uuid4().hex}",
            projection={"kind": kind},
            meta={"source_kind": kind},
            actor=request.actor,
        )
        return DeliveryIntent(
            kind=stored_kind,
            text=str(record.get("text") or text),
            audience=audience,
            target=target,
            record=record,
            recipient_label="当前群聊",
        )
