"""D1-ARC-001 会话命令应用层（平台无关，不持有平台事件对象）。

本模块把 main.py 中「世界列表 / 副本列表 / 状态 / 开启 / 加入 /
准备 / 开演 / 退出 / 暂停 / 恢复 / 继续 / 关闭 / 完结 / 强制终止」会话
生命周期分支抽为纯应用服务：

- 输入：``RequestContext``（``tavern/runtime/request.py`` 权威定义）、
  ``ParsedCommand``（已解析命令）、``SessionGateway``（数据与配置依赖
  协议）；
- 输出：``CommandResult``（``tavern/commands/models.py`` 权威定义）：
  外显文本 + 投递意图（delivery）+ 是否已消费（handled）；
- 所有写操作只编排 gateway 协议方法，本模块不写 SQL、不 import
  任何宿主平台类、不持有平台事件对象；玩家无法通过命令文本宣布或伪造
  状态变化，一切权威状态变化由仓储层在事务内完成。

入口适配层（main.py 路由接线时）职责：

1. 从平台事件构建 ``RequestContext``（``tavern/entry/event_context.py``），
   注入 ``roles``（admin/host/moderator/player）与 ``session_id``；
2. 调用 :meth:`SessionCommandService.handle`；返回 ``None`` 表示本服务
   不处理该动作，``CommandResult(handled=True, text=None)`` 表示已消费
   但无需回复（未授权且配置为静默忽略）；
3. 回复 ``text`` 并分发 ``delivery`` 意图：
   - ``broker_event``：按 payload 发布副本事件（如 session prepare）；
   - ``sync_group_target``：保存当前群真实投递目标（join）；
   - ``revoke_private_target``：私聊来源丢失 / 退场时降级已验证目标。
4. gateway 中的适配方法（``send_card_code_private`` /
   ``validate_world_runtime`` / ``release_session_lock``）由宿主实现，
   其返回结果直接影响 join / perform / resume 的回复文案。

``SESSION_ACTIONS`` 是本服务负责的命令集合（唯一来源；路由接线时用本
常量替换 main.py 中的对应分发分支，避免双源）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Protocol, runtime_checkable

from ..constants import (
    SESSION_CLOSED,
    SESSION_FINISHED,
    SESSION_MAINTENANCE,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from ..database import DatabaseNotFoundError, InvalidTransitionError
from ..lifecycle import format_choices, normalize_time_rules, player_limits
from ..presentation import (
    format_instance_list,
    format_recovered_timer,
    format_roster,
    format_vote,
    parse_instance_list_page,
)
from ..runtime.request import (
    ROLE_ADMIN,
    ROLE_HOST,
    ROLE_MODERATOR,
    RequestContext,
)
from ..security import ParsedCommand

from .models import (
    INTENT_REVOKE_PRIVATE_TARGET,
    CommandResult,
    DeliveryIntent,
)

logger = logging.getLogger(__name__)


def format_gameplay_brief(world: Mapping[str, Any]) -> str:
    """Render the frozen public gameplay contract without inventing details."""

    brief = world.get("gameplay_brief")
    if not isinstance(brief, Mapping):
        return "【副本玩法】\n作者未提供玩法说明。开演前请向主持人确认规则与风险。"
    tone = str(brief.get("tone") or "").strip()
    core_loop = str(brief.get("core_loop") or "").strip()
    special_rules = [
        str(item or "").strip()
        for item in brief.get("special_rules", [])
        if str(item or "").strip()
    ] if isinstance(brief.get("special_rules"), list) else []
    recommended = str(brief.get("recommended_for") or "").strip()
    lines = ["【副本玩法】"]
    lines.extend(item for item in (tone, core_loop) if item)
    lines.extend(f"- {item}" for item in special_rules)
    if recommended:
        lines.append(f"适合：{recommended}")
    if len(lines) == 1:
        lines.append("作者未提供玩法说明。开演前请向主持人确认规则与风险。")
    return "\n".join(lines)


def format_preparation_steps() -> str:
    """Render a compact, high-contrast preparation flow for chat clients."""

    return (
        "【接下来怎么做】"
        "\n\n① 玩家加入"
        "\n/团 加入"
        "\n按私聊提示完成角色卡。"
        "\n\n② 玩家准备"
        "\n/团 准备"
        "\n\n③ 可选：添加 AI 队友（主持人）"
        "\n/团 AI队友 2 确认"
        "\n\n④ 开始故事（主持人）"
        "\n/团 开演"
        "\n系统不会自动开演。"
    )

# 会话命令集合（唯一来源）。「世界列表 / 副本列表」是会话的列表入口，
# 「加入」同时覆盖重加入（席位归档后再次占位）路径。
SESSION_ACTIONS = frozenset(
    {
        "worlds",
        "instances",
        "status",
        "start",
        "join",
        "ai_companions",
        "ready",
        "perform",
        "leave",
        "pause",
        "cancel_generation",
        "retry_turn",
        "recover",
        "resume",
        "close",
        "finish",
        "abort",
    }
)

# 投递意图种类（入口适配层按 kind 分派到对应平台操作）。
INTENT_BROKER_EVENT = "broker_event"
INTENT_SYNC_GROUP_TARGET = "sync_group_target"

# 需要主持人权限的动作。
_HOST_ACTIONS = frozenset(
    {
        "worlds",
        "instances",
        "start",
        "ai_companions",
        "perform",
        "pause",
        "retry_turn",
        "recover",
        "resume",
        "close",
        "finish",
        "abort",
    }
)

# 普通玩家在已授权群可直接执行的动作。
_PLAYER_ACTIONS = frozenset(
    {"join", "ready", "leave", "cancel_generation", "retry_turn"}
)

# 未授权群时仍放行查看的动作（与 main.py 行为一致）。
_UNAUTHORIZED_GROUP_EXEMPT = frozenset({"help", "unknown", "worlds", "instances"})

# 暂停态拦截的玩法动作（与 main.py 暂停守卫一致）。
_PAUSED_BLOCKED_ACTIONS = frozenset({"join", "ready", "leave", "perform"})

_SESSION_STATE_LABELS = {
    SESSION_CLOSED: "已关闭",
    SESSION_PREPARING: "准备中",
    SESSION_RUNNING: "运行中",
    SESSION_PAUSED: "已暂停",
    SESSION_FINISHED: "已完结",
    SESSION_MAINTENANCE: "维护中",
}

_OPERATION_LABELS = {
    "worlds": "查看世界列表",
    "instances": "查看副本列表",
    "status": "查看副本状态",
    "start": "开启副本",
    "join": "加入副本",
    "ai_companions": "配置 AI 队友",
    "ready": "准备",
    "perform": "开演",
    "leave": "退出副本",
    "pause": "暂停副本",
    "cancel_generation": "取消本轮生成",
    "retry_turn": "重试本轮",
    "recover": "进入恢复准备大厅",
    "resume": "继续剧情",
    "close": "关闭副本",
    "finish": "完结副本",
    "abort": "强制终止副本",
}

_DOMAIN_RECOVERY = {
    "worlds": "重新发送 /团 世界列表。",
    "instances": "重新发送 /团 副本列表。",
    "status": "重新发送 /团 状态。",
    "start": "重新发送 /团 开启 <副本标识>，或查看 /团 帮助。",
    "join": "确认本群已开团并处于准备阶段后，重新发送 /团 加入。",
    "ai_companions": "准备阶段发送 /团 AI队友 <数量> <确认|自动|暂停>。",
    "ready": "确认角色卡已通过审核后，重新发送 /团 准备。",
    "perform": "确认全员准备完成后，重新发送 /团 开演。",
    "leave": "重新发送 /团 退出；如提示无权限，请联系主持人处理。",
    "pause": "确认副本状态后重试；如需恢复请发送 /团 恢复。",
    "cancel_generation": "确认本轮仍在生成后发送 /团 取消。",
    "retry_turn": "确认表决结果或本轮操作仍可恢复后发送 /团 重试本轮。",
    "recover": "确认副本状态后重试；如仍无法恢复请联系主持人。",
    "resume": "确认已进入恢复准备大厅后，重新发送 /团 继续。",
    "close": "确认副本状态后重新发送 /团 关闭。",
    "finish": "确认无误后重新发送 /团 完结 确认。",
    "abort": "确认无误后重新发送 /团 强制终止 确认 <原因>。",
}

_ARCHIVED_TEXT = (
    "【开团】该副本已经完结并永久归档为只读，本轮命令未执行。\n"
    "下一步：查看 /团 回顾 或 /团 顺序 查看最终档案；"
    "新冒险由管理员重新开启。"
)

_PAUSED_TEXT = (
    "【开团】剧情已暂停，暂不可进行选项 / 投票 / 行动 / 建卡等玩法操作。\n"
    "请先由主持人发送 /团 恢复 进入恢复准备大厅，"
    "再发送 /团 继续 续演。"
)


@runtime_checkable
class SessionGateway(Protocol):
    """会话命令对适配层的数据与配置依赖协议（宿主装配时由
    ``TavernDatabase`` 与引擎/投递能力组合实现）。

    只包含本服务使用到的方法；签名与 ``tavern/repositories`` 现有公开
    方法一致，宿主接入时自然满足。
    """

    # ── 配置读取 ──────────────────────────────────────────────
    def is_group_allowed(self, group_id: str) -> bool: ...

    def public_status_enabled(self) -> bool: ...

    def unauthorized_behavior(self) -> str: ...

    def trigger_prefix(self) -> str: ...

    def time_rules(self) -> Mapping[str, Any]: ...

    # ── 安全审计 ──────────────────────────────────────────────
    async def write_security_audit(
        self,
        *,
        sender_id: str,
        action: str,
        group_id: str,
        platform_id: str,
        reason: str,
    ) -> None: ...

    # ── 会话 / 世界读取 ────────────────────────────────────────
    async def get_session_by_group(
        self,
        platform_id: str,
        group_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_session_by_group_ref(
        self,
        platform_id: str,
        group_id: str,
        instance_ref: str,
    ) -> Mapping[str, Any] | None: ...

    async def list_group_sessions(
        self,
        platform_id: str,
        group_id: str,
    ) -> list[Mapping[str, Any]]: ...

    async def list_worlds(self) -> list[Mapping[str, Any]]: ...

    async def get_world(self, world_ref: str) -> Mapping[str, Any]: ...

    async def get_instance_config(self, session_id: str) -> Mapping[str, Any]: ...

    async def get_session_rule_state(
        self,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    async def get_control_state(self, session_id: str) -> Mapping[str, Any]: ...

    async def get_turn_status(self, session_id: str) -> Mapping[str, Any]: ...

    async def list_roster(self, session_id: str) -> list[Mapping[str, Any]]: ...

    async def list_ai_companions(self, session_id: str) -> Mapping[str, Any]: ...

    async def configure_ai_companions(
        self,
        *,
        session_id: str,
        count: int,
        mode: str,
        expected_session_revision: int,
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    async def active_vote(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def active_choice_set(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def recent_events(
        self,
        session_id: str,
        limit: int,
    ) -> list[Mapping[str, Any]]: ...

    async def list_timers(self, session_id: str) -> list[Mapping[str, Any]]: ...

    async def get_participant(
        self,
        session_id: str,
        *,
        user_id: str = "",
        participant_ref: str = "",
    ) -> Mapping[str, Any]: ...

    # ── 会话写操作（宿主接入 TavernDatabase；本服务只编排） ────
    async def ensure_session(
        self,
        platform_id: str,
        group_id: str,
        unified_origin: str,
        world_ref: str,
        actor_id: str,
        instance_slug: str = "",
        instance_name: str = "",
    ) -> Mapping[str, Any]: ...

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def save_instance_time_rules(
        self,
        session_id: str,
        rules: Mapping[str, Any],
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def reserve_participant(
        self,
        session_id: str,
        user_id: str,
        display_name: str,
    ) -> Mapping[str, Any]: ...

    async def set_participant_ready(
        self,
        session_id: str,
        user_id: str,
        ready: bool,
    ) -> Mapping[str, Any]: ...

    async def retire_self(
        self,
        session_id: str,
        user_id: str,
    ) -> Mapping[str, Any]: ...

    async def retire_participant(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        forced: bool,
        reason: str,
    ) -> Mapping[str, Any]: ...

    async def pause_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int: ...

    async def active_session_operation(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def latest_retryable_session_operation(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def cancel_and_wait_operation(
        self,
        session_id: str,
        actor_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> Mapping[str, Any]: ...

    async def retry_session_operation(
        self,
        session_id: str,
        actor_id: str,
    ) -> Mapping[str, Any]: ...

    async def resume_session_timers(
        self,
        session_id: str,
        actor_id: str,
    ) -> int: ...

    async def activate_story(
        self,
        session_id: str,
        actor_id: str,
        *,
        resume: bool,
    ) -> Mapping[str, Any]: ...

    async def opening_decision(
        self,
        session_id: str,
    ) -> Mapping[str, Any] | None: ...

    async def prepare_opening_decision(
        self,
        session_id: str,
    ) -> Mapping[str, Any]: ...

    async def override_opening_decision(
        self,
        session_id: str,
        scene_ref: str,
        principal_ref: str,
        expected_revision: int,
    ) -> Mapping[str, Any]: ...

    async def finalize_session(
        self,
        session_id: str,
        actor_id: str,
        *,
        termination_type: str,
        reason: str,
    ) -> Mapping[str, Any]: ...

    async def opening_preflight(self, session_id: str) -> Mapping[str, Any]: ...

    async def allow_group(
        self,
        group_id: str,
        platform_id: str,
        actor_id: str,
    ) -> bool: ...

    # ── 引擎 / 投递适配（结果影响回复文案） ────────────────────
    async def send_card_code_private(
        self,
        session_id: str,
        participant: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def validate_world_runtime(self, world_snapshot: Mapping[str, Any]) -> None: ...

    async def release_session_lock(self, session_id: str) -> None: ...


from .session_queries import SessionQueriesMixin
from .session_lifecycle import SessionLifecycleMixin
from .session_participation import SessionParticipationMixin
from .session_generation import SessionGenerationMixin

class SessionCommandService(SessionQueriesMixin, SessionLifecycleMixin, SessionParticipationMixin, SessionGenerationMixin):
    """会话命令应用服务：只编排应用服务，不持有平台对象、不直接写库。"""




    # ── 分发 ──────────────────────────────────────────────────

    # ── 列表与状态 ────────────────────────────────────────────



    # ── 开启 ──────────────────────────────────────────────────



    # ── 加入 / 准备 / 开演 ────────────────────────────────────



    # ── 退出 ──────────────────────────────────────────────────

    # ── 暂停 / 恢复 / 继续 ────────────────────────────────────





    # ── 关闭 / 完结 / 强制终止 ────────────────────────────────



    # ── 错误投影 ──────────────────────────────────────────────


__all__ = [
    "INTENT_BROKER_EVENT",
    "INTENT_SYNC_GROUP_TARGET",
    "SESSION_ACTIONS",
    "format_gameplay_brief",
    "format_preparation_steps",
    "SessionCommandService",
    "SessionGateway",
]
