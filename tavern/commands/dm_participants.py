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




class DmParticipantsMixin:
    def build_whisper_record(
        self,
        session_id: str,
        participant: Mapping[str, Any],
        text: str,
        actor: str,
    ) -> tuple[dict[str, Any], DeliveryTarget]:
        """纯构造密语待投递记录；缺失私聊来源时降级为 webui_only。"""

        private_origin = str(
            (participant or {}).get("private_origin") or ""
        ).strip()
        target = DeliveryTarget.from_origin(
            private_origin,
            verified_binding=True,
            source="dm_command_whisper",
        )
        kind = "dm_whisper"
        if target is None:
            target = DeliveryTarget.webui_only(
                source="dm_whisper_no_private_origin"
            )
            kind = "webui_notice"
        meta: dict[str, Any] = {
            "recipient_name": "目标角色",
            "source_kind": "dm.whisper",
        }
        name = str(
            (participant or {}).get("character_name")
            or (participant or {}).get("display_name")
            or ""
        ).strip()
        if name:
            meta["recipient_name"] = name
        meta["recipient_user_id"] = str(
            (participant or {}).get("group_user_id") or ""
        )
        meta["recipient_participant_id"] = str(
            (participant or {}).get("id") or ""
        )
        record = self.delivery.build_record(
            session_id=session_id,
            target=target,
            kind=kind,
            text=f"【主持密语】\n{text}",
            audience="private_owner",
            dedupe_key=f"dm:{session_id}:whisper:{uuid.uuid4().hex}",
            projection={"kind": "dm.whisper"},
            meta=meta,
            actor=actor,
        )
        return record, target
    async def whisper(
        self,
        session_id: str,
        request: DMRequest,
        *,
        target_ref: str,
        text: str,
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._whisper_impl(session_id, request, target_ref, text),
        )
    async def _whisper_impl(
        self,
        session_id: str,
        request: DMRequest,
        target_ref: str,
        text: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        whisper_text = str(text or "").strip()
        if not whisper_text:
            raise ValueError("密语内容不能为空")
        participant = await self.resolve_participant(session_id, target_ref)
        record, target = self.build_whisper_record(
            session_id,
            participant,
            whisper_text,
            request.actor,
        )
        result = await self.database.whisper_to(
            session_id,
            whisper_text,
            str(participant.get("id") or ""),
            request.actor,
            delivery_record=record,
        )
        queued = bool(result.get("queued"))
        status = str(result.get("status") or ("queued" if queued else "webui_only"))
        if queued:
            message = (
                "【主持密语已发送】\n"
                "已进入待投递队列，送达后玩家将收到私聊消息。"
            )
            next_action = ""
        else:
            message = (
                f"【主持密语已保存】\n"
                f"「{record.get('meta', {}).get('recipient_name', '目标角色')}」"
                "尚未绑定私聊来源，密语仅保留在 WebUI 主持面板，"
                "未发送到任何群聊。"
            )
            next_action = "请该玩家私聊发送 /团 建卡 <验证码> 完成绑定。"
        recipient_label = str(
            record.get("meta", {}).get("recipient_name") or "目标角色"
        )
        intent = DeliveryIntent(
            kind=(
                "webui_notice"
                if target.message_type == TARGET_KIND_WEBUI_ONLY
                else "dm_whisper"
            ),
            text=f"【主持密语】\n{whisper_text}",
            audience="private_owner",
            target=target,
            record=record,
            delivery_id=str(result.get("delivery_id") or ""),
            status=status,
            queued=queued,
            recipient_label=recipient_label,
        )
        return (
            message,
            "queued" if queued else "webui_only",
            {
                "event_id": str(result.get("event_id") or ""),
                "delivery_id": intent.delivery_id,
                "recipient_label": recipient_label,
                "participant_id": str(participant.get("id") or ""),
            },
            intent,
        )
    async def handoff(
        self,
        session_id: str,
        request: DMRequest,
        *,
        target_ref: str,
    ) -> CommandResult:
        return await self._safe(
            session_id,
            request,
            lambda: self._handoff_impl(session_id, request, target_ref),
        )
    async def _handoff_impl(
        self,
        session_id: str,
        request: DMRequest,
        target_ref: str,
    ) -> tuple[str, str, dict[str, Any], DeliveryIntent | None]:
        await self._assert_dm_capability(session_id, request)
        await self._assert_writable(session_id)
        ref = str(target_ref or "").strip()
        if not ref:
            raise ValueError("格式：/团 主持 交棒 <角色名或 NPC:名称>")
        if ref.upper().startswith("NPC:") or ref.startswith("NPC："):
            npc_name = ref[4:].strip()
            if not npc_name:
                raise ValueError("格式：/团 主持 交棒 NPC:<名称>")
            npcs = await self.database.list_session_characters(
                session_id,
                include_archived=False,
            )
            npc = next(
                (
                    item
                    for item in npcs
                    if npc_name
                    in {
                        str(item.get("id") or ""),
                        str(item.get("name") or ""),
                        *[
                            str(alias)
                            for alias in item.get("aliases", [])
                        ],
                    }
                ),
                None,
            )
            if npc is None:
                raise TargetNotFoundError("没有找到该 NPC，操作未执行。")
            await self.database.set_dm_handoff(
                session_id,
                "npc",
                str(npc.get("id") or ""),
                request.actor,
            )
            instruction = (
                f"让 NPC“{npc.get('name') or npc_name}”依据其知识边界"
                "与当前状态行动一段；不替玩家行动。"
            )
            return (
                f"【已交棒给非玩家角色 · {npc.get('name') or npc_name}】\n"
                "主持模式保持开启，已回到等待主持人推进的状态。",
                "handoff_npc",
                {
                    "actor_type": "npc",
                    "npc_id": str(npc.get("id") or ""),
                    "npc_name": str(npc.get("name") or npc_name),
                    "instruction": instruction,
                },
                None,
            )
        try:
            participant = await self.database.get_participant(
                session_id,
                participant_ref=ref,
            )
        except DatabaseNotFoundError as exc:
            raise TargetNotFoundError(
                "找不到目标角色，操作未执行。"
            ) from exc
        except ValueError as exc:
            raise TargetAmbiguousError(
                "角色标识不唯一，操作未执行。"
            ) from exc
        await self.database.set_dm_handoff(
            session_id,
            "player",
            str(participant.get("id") or ""),
            request.actor,
        )
        turn = await self.database.designate_turn(
            session_id,
            str(participant.get("group_user_id") or ""),
            request.actor,
        )
        return (
            "【已交棒，主持模式保持开启】\n" + _format_turn_status(turn),
            "handoff_player",
            {
                "actor_type": "player",
                "participant_id": str(participant.get("id") or ""),
                "character_name": str(
                    participant.get("character_name")
                    or participant.get("display_name")
                    or ""
                ),
            },
            None,
        )
