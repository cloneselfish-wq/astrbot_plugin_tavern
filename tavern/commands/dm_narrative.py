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
from ..contracts.narrative_document import (
    narrative_document_from_plain_text,
    narrative_document_to_plain_text,
)
from ..narrative_modes import narrative_mode_from_session


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




class DmNarrativeMixin:
    async def disable_dm(
        self,
        session_id: str,
        request: DMRequest,
    ) -> CommandResult:
        return await self._safe(session_id, request, lambda: self._disable_dm_impl(
            session_id, request
        ))
    async def _disable_dm_impl(
        self,
        session_id: str,
        request: DMRequest,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        await self.database.disable_dm_mode(session_id, request.actor)
        return (
            "【已恢复 AI 自动模式】主持模式已关闭。",
            "dm_disabled",
            {},
            None,
        )
    async def status(
        self,
        session_id: str,
        request: DMRequest,
    ) -> CommandResult:
        return await self._safe(session_id, request, lambda: self._status_impl(
            session_id, request
        ))
    async def _status_impl(
        self,
        session_id: str,
        request: DMRequest,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        state = await self.database.get_control_state(session_id)
        phase_labels = {
            "auto": "自动叙事",
            "awaiting_dm": "等待主持指令",
            "generating": "AI 推进中",
            "player_handoff": "已交棒给玩家",
            "npc_handoff": "已交棒给角色",
        }
        is_dm = str(state.get("mode") or "") == "dm"
        message = (
            "【主持模式状态】\n"
            f"模式：{'真人主持' if is_dm else 'AI 自动'}\n"
            "当前主持人："
            f"{'已指定' if state.get('active_dm_user_id') else '无'}\n"
            "阶段："
            f"{phase_labels.get(str(state.get('phase') or 'auto'), '待确认')}\n"
            f"连续推进：{int(state.get('beat_no') or 0)} 段\n"
            f"一次性指引：{'已保存' if state.get('directive') else '无'}\n"
            "当前交棒目标："
            f"{'已指定' if state.get('current_actor_ref') else '无'}"
        )
        return (
            message,
            "dm_status",
            {
                "mode": state.get("mode"),
                "phase": state.get("phase"),
                "beat_no": int(state.get("beat_no") or 0),
            },
            None,
        )
    async def directive(
        self,
        session_id: str,
        request: DMRequest,
        *,
        text: str,
    ) -> CommandResult:
        return await self._safe(session_id, request, lambda: self._directive_impl(
            session_id, request, text
        ))
    async def _directive_impl(
        self,
        session_id: str,
        request: DMRequest,
        text: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        directive_text = str(text or "").strip()
        if not directive_text:
            raise ValueError("主持指引不能为空")
        await self.database.set_dm_directive(
            session_id,
            directive_text,
            request.actor,
        )
        return (
            "【一次性主持指引已保存】将在下一次 AI 推进成功后自动清除。",
            "directive_saved",
            {},
            None,
        )
    async def insert_narrative(
        self,
        session_id: str,
        request: DMRequest,
        *,
        narrative: str,
        mode: str = "append",
        group_mode: str = "group",
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._insert_narrative_impl(
                session_id, request, narrative, mode, group_mode
            ),
        )
    async def _insert_narrative_impl(
        self,
        session_id: str,
        request: DMRequest,
        narrative: str,
        mode: str,
        group_mode: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        narrative = str(narrative or "").strip()
        if not narrative:
            raise ValueError("追加叙事不能为空")
        mode = str(mode or "append").strip()
        if mode not in {"append", "override"}:
            raise ValueError("模式必须为 append 或 override")
        instance = await self.database.get_instance_config(session_id)
        document = narrative_document_from_plain_text(
            narrative,
            mode=narrative_mode_from_session(instance),
        )
        narrative = narrative_document_to_plain_text(document)
        result = await self.database.insert_dm_narrative(
            session_id,
            document,
            request.actor,
            mode,
        )
        delivery = self._build_group_intent(
            session_id,
            request,
            text=narrative,
            kind="group_notice",
            mode=group_mode,
        )
        label = "已追加" if mode == "append" else "已覆盖"
        return (
            f"【主持叙事{label}】\n{narrative}",
            "narrative_inserted",
            {
                "event_id": str(result.get("event_id") or ""),
                "operation_id": str(result.get("operation_id") or ""),
                "revision": int(result.get("revision") or 0),
                "turn_no": int(result.get("turn_no") or 0),
                "narrative_document": document.to_dict(),
            },
            delivery,
        )
    async def direct_narrative(
        self,
        session_id: str,
        request: DMRequest,
        *,
        narrative: str,
        group_mode: str = "group",
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._direct_narrative_impl(
                session_id, request, narrative, group_mode
            ),
        )
    async def _direct_narrative_impl(
        self,
        session_id: str,
        request: DMRequest,
        narrative: str,
        group_mode: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        session = await self._assert_writable(session_id)
        narrative = str(narrative or "").strip()
        if not narrative:
            raise ValueError("直述叙事不能为空")
        instance = await self.database.get_instance_config(session_id)
        document = narrative_document_from_plain_text(
            narrative,
            mode=narrative_mode_from_session(instance),
        )
        narrative = narrative_document_to_plain_text(document)
        result = await self.database.commit_dm_beat(
            session_id=session_id,
            expected_revision=int(session.get("revision") or 0),
            dm_user_id=request.user_id,
            instruction=narrative,
            narrative=narrative,
            narrative_document=document,
            world_state=session.get("world_state") or {},
            direct=True,
        )
        beat_no = int(result.get("beat_no") or 0)
        delivery = self._build_group_intent(
            session_id,
            request,
            text=narrative,
            kind="group_notice",
            mode=group_mode,
        )
        return (
            f"【主持直述 · 第 {beat_no} 段】\n{narrative}",
            "direct_narrative_committed",
            {
                "beat_no": beat_no,
                "revision": int(result.get("revision") or 0),
                "event_id": str(result.get("event_id") or ""),
                "operation_id": str(result.get("operation_id") or ""),
                "turn_no": int(result.get("turn_no") or 0),
                "narrative_document": document.to_dict(),
            },
            delivery,
        )
    async def resolve_participant(
        self,
        session_id: str,
        target_ref: str,
    ) -> dict[str, Any]:
        """把密语目标引用解析为参与者（群用户 ID → 角色名/代号/参与者 ID）。"""

        ref = str(target_ref or "").strip()
        if not ref:
            raise ValueError("缺少密语目标角色")
        try:
            return await self.database.get_participant(
                session_id,
                user_id=ref,
            )
        except DatabaseNotFoundError:
            pass
        try:
            return await self.database.get_participant(
                session_id,
                participant_ref=ref,
            )
        except DatabaseNotFoundError as exc:
            raise TargetNotFoundError("找不到目标角色，操作未执行。") from exc
        except ValueError as exc:
            raise TargetAmbiguousError(
                "角色标识不唯一，操作未执行。"
            ) from exc
