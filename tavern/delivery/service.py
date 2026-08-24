"""D1 主动投递服务（D1_PLAN 15 §5-6、§10-12、§16）。

投递顺序：``send_by_session`` → ``context.send_message`` → 持久化 outbox。
领域事务只写入待投递记录，不在数据库事务中等待平台网络请求；发送结果
（成功/部分/失败/取消）由 worker 或 WebUI 重试入口按租约驱动。

仓储通过 :class:`DeliveryOutboxRepository` 协议注入，服务层不依赖数据库
实现；数据库迁移由主线在 ``database.py`` 完成。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..database_support import new_id
from ..messaging.player import prepare_player_output
from ..platform_delivery import markdown_supported
from .adapter import DeliveryAdapter, SendAttemptResult
from .privacy import (
    AUDIENCE_PRIVATE_OWNER,
    PRIVILEGED_VIEWERS,
    channel_label,
    row_visible_to_private_viewer,
    sanitize_log,
    status_label,
    trim_for_audience,
)
from .retry_policy import (
    is_permanently_failed,
    lease_seconds_for,
    next_retry_at,
)
from .target import (
    TARGET_KIND_PRIVATE,
    TARGET_KIND_WEBUI_ONLY,
    DeliveryTarget,
)


@dataclass(frozen=True, slots=True)
class KindPolicy:
    allow_temporary: bool
    require_verified: bool
    allow_group_fallback: bool
    max_attempts: int
    stored_kind: str = ""


# 消息类型策略（D1_PLAN 15 §7）：临时目标、验证要求、群聊回退与重试上限。
KIND_POLICIES: dict[str, KindPolicy] = {
    "card_code": KindPolicy(True, False, False, 6, "card_code"),
    "card_reminder": KindPolicy(True, False, False, 8, "card_reminder"),
    "staged_supplement": KindPolicy(
        True,
        False,
        False,
        8,
        "staged_supplement",
    ),
    "dm_whisper": KindPolicy(False, True, False, 10, "dm_whisper"),
    "death_confirm": KindPolicy(False, True, False, 8, "death_confirm"),
    "vote_reminder": KindPolicy(False, False, True, 8, "vote_reminder"),
    "group_notice": KindPolicy(False, False, True, 8, "group_notice"),
    "generation_reminder": KindPolicy(
        False,
        False,
        True,
        8,
        "generation_reminder",
    ),
    "webui_notice": KindPolicy(False, False, False, 4, "webui_only"),
}

# 终态：已送达/已取消/仅 WebUI/永久失败。永久失败不再领取租约、不可重试
# （D1-DEL-010：永久失败仅可查看）。
TERMINAL_DELIVERY_STATUSES = frozenset(
    {"delivered", "cancelled", "webui_only", "permanently_failed"}
)
PERMANENT_FAILURE_REASON = "已达发送上限，不可重试"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    ok: bool
    status: str
    delivery_id: str = ""
    reason: str = ""
    method: str = ""
    attempts: int = 0
    sent_parts: int = 0
    total_parts: int = 0
    next_retry_at: str = ""


@runtime_checkable
class DeliveryOutboxRepository(Protocol):
    """outbox 仓储协议：worker 与 WebUI 重试的持久化边界。

    ``record`` 键（D1_PLAN 15 §6）：delivery_id、session_id、audience、
    target_snapshot、message_type、projection_snapshot、rendered_parts、
    next_part_index、status、priority、attempts、next_retry_at、
    last_error_code、last_error_message、dedupe_key、created_at、updated_at、
    delivered_at、cancelled_at、lease_token、lease_until、meta。
    """

    async def create(self, record: dict[str, Any]) -> dict[str, Any]: ...

    async def get(self, delivery_id: str) -> dict[str, Any] | None: ...

    async def dedupe(self, dedupe_key: str) -> dict[str, Any] | None: ...

    async def list_due(self, *, limit: int, now: str) -> list[dict[str, Any]]: ...

    async def lease(
        self,
        delivery_id: str,
        token: str,
        lease_until: str,
    ) -> dict[str, Any] | None: ...

    async def complete(
        self,
        delivery_id: str,
        token: str,
        *,
        sent_parts: int,
        delivered_at: str,
    ) -> dict[str, Any] | None: ...

    async def mark_partial(
        self,
        delivery_id: str,
        token: str,
        *,
        next_part_index: int,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None: ...

    async def mark_retry(
        self,
        delivery_id: str,
        token: str,
        *,
        attempts: int,
        next_retry_at: str,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None: ...

    async def mark_failed(
        self,
        delivery_id: str,
        token: str,
        *,
        attempts: int,
        last_error_code: str,
        last_error_message: str,
    ) -> dict[str, Any] | None: ...

    async def cancel(
        self,
        delivery_id: str,
        *,
        actor: str,
        reason: str,
        token: str = "",
    ) -> dict[str, Any] | None: ...

    async def list_status(
        self,
        session_id: str,
        *,
        viewer: str,
        limit: int,
    ) -> list[dict[str, Any]]: ...


class DeliveryService:
    def __init__(
        self,
        *,
        context: Any = None,
        repository: DeliveryOutboxRepository | None = None,
        adapter: DeliveryAdapter | None = None,
        markdown_enabled=None,
        now_fn=None,
    ) -> None:
        self.context = context
        self.repository = repository
        self.adapter = adapter or DeliveryAdapter()
        self.markdown_enabled = markdown_enabled
        self.now_fn = now_fn or _default_now

    @staticmethod
    def kind_policy(kind: str) -> KindPolicy | None:
        """消息类型策略查询：调用方据此决定群聊说明等业务动作。"""

        return KIND_POLICIES.get(kind)

    def _markdown_enabled_for(self, target: DeliveryTarget) -> bool:
        platform = target.platform_instance_id or target.unified_origin
        enabled = markdown_supported(platform)
        if callable(self.markdown_enabled):
            try:
                enabled = enabled or bool(self.markdown_enabled(target))
            except Exception:
                pass
        elif self.markdown_enabled is not None:
            enabled = enabled or bool(self.markdown_enabled)
        return enabled

    def _prepare_text(
        self,
        target: DeliveryTarget,
        text: Any,
    ) -> tuple[str, bool]:
        output = prepare_player_output(text, default_title="酒馆通知")
        markdown = self._markdown_enabled_for(target)
        return output.select(markdown=markdown), markdown

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------

    async def send(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        kind: str,
        text: str,
        audience: str = AUDIENCE_PRIVATE_OWNER,
        dedupe_key: str = "",
        priority: int = 100,
        projection: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> DeliveryOutcome:
        """按 D1-DEL-004 顺序投递：优先会话、回退发送、失败入队。"""

        policy = KIND_POLICIES.get(kind)
        if policy is None:
            return DeliveryOutcome(False, "unsupported_kind", reason=f"未知消息类型 {kind}")
        if not isinstance(target, DeliveryTarget):
            return DeliveryOutcome(False, "invalid_target", reason="缺少投递目标")
        text, markdown = self._prepare_text(target, text)
        if not text:
            return DeliveryOutcome(False, "invalid_target", reason="消息内容为空")
        if target.message_type == TARGET_KIND_WEBUI_ONLY or kind == "webui_notice":
            # 仅 WebUI 可见：不触达平台，直接入队。
            return await self._enqueue_record(
                session_id=session_id,
                target=target,
                kind=kind,
                text=text,
                audience=audience,
                dedupe_key=dedupe_key,
                priority=priority,
                projection=projection,
                meta=meta,
                actor=actor,
                use_markdown=markdown,
            )
        if target.message_type == TARGET_KIND_PRIVATE:
            verified_ok = target.verified_binding or target.source == "admin_specified"
            if policy.require_verified and not verified_ok:
                return DeliveryOutcome(False, "unverified", reason="目标未验证，禁止发送")
            if not policy.allow_temporary and not verified_ok:
                return DeliveryOutcome(False, "unverified", reason="该消息只允许发送给已验证私聊目标")
        parts = self.adapter.split_for(
            target.platform_instance_id or target.unified_origin,
            text,
        )
        if self.context is None:
            return await self._enqueue_record(
                session_id=session_id,
                target=target,
                kind=kind,
                text=text,
                audience=audience,
                dedupe_key=dedupe_key,
                priority=priority,
                projection=projection,
                meta=meta,
                actor=actor,
                parts=parts,
                use_markdown=markdown,
            )
        attempt = await self.adapter.deliver(
            self.context,
            target,
            parts,
            start_index=0,
            use_markdown=markdown,
        )
        if attempt.ok:
            return DeliveryOutcome(
                True,
                "sent",
                method=attempt.method,
                sent_parts=attempt.sent_parts,
                total_parts=len(parts),
            )
        return await self._enqueue_record(
            session_id=session_id,
            target=target,
            kind=kind,
            text=text,
            audience=audience,
            dedupe_key=dedupe_key,
            priority=priority,
            projection=projection,
            meta=meta,
            actor=actor,
            parts=attempt.parts or parts,
            attempt=attempt,
            use_markdown=markdown,
        )

    async def enqueue(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        kind: str,
        text: str,
        audience: str = AUDIENCE_PRIVATE_OWNER,
        dedupe_key: str = "",
        priority: int = 100,
        projection: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> DeliveryOutcome:
        """领域事务路径：只写待投递记录，不等待平台网络请求。"""

        policy = KIND_POLICIES.get(kind)
        if policy is None:
            return DeliveryOutcome(False, "unsupported_kind", reason=f"未知消息类型 {kind}")
        if not isinstance(target, DeliveryTarget):
            return DeliveryOutcome(False, "invalid_target", reason="缺少投递目标")
        text, markdown = self._prepare_text(target, text)
        if not text:
            return DeliveryOutcome(False, "invalid_target", reason="消息内容为空")
        return await self._enqueue_record(
            session_id=session_id,
            target=target,
            kind=kind,
            text=text,
            audience=audience,
            dedupe_key=dedupe_key,
            priority=priority,
            projection=projection,
            meta=meta,
            actor=actor,
            use_markdown=markdown,
        )

    def build_record(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        kind: str,
        text: str,
        audience: str = AUDIENCE_PRIVATE_OWNER,
        dedupe_key: str = "",
        priority: int = 100,
        projection: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        actor: str = "system",
        parts: list[str] | None = None,
        content_format: str = "",
    ) -> dict[str, Any]:
        """纯构造待投递记录（无任何 I/O），``enqueue`` 与密语原子路径复用。

        D1-DEL-010：密语等场景需要把领域事件与 outbox 放在同一 SQLite
        事务中，因此先把记录构造为普通 dict，由调用方决定写入时机。
        输入非法时抛 ``ValueError``，调用方自行转为错误响应。
        """
        policy = KIND_POLICIES.get(kind)
        if policy is None:
            raise ValueError(f"未知消息类型 {kind}")
        if not isinstance(target, DeliveryTarget):
            raise ValueError("缺少投递目标")
        format_name = str(content_format or "").strip().lower()
        if format_name not in {"markdown", "plain"}:
            text, use_markdown = self._prepare_text(target, text)
            format_name = "markdown" if use_markdown else "plain"
        if not str(text or "").strip():
            raise ValueError("消息内容为空")
        stored_kind = (policy.stored_kind or kind) or kind
        if parts is None:
            parts = self.adapter.split_for(
                target.platform_instance_id or target.unified_origin,
                text,
            )
        now = self.now_fn()
        stored_meta = dict(meta or {})
        stored_meta["content_format"] = format_name
        return {
            "delivery_id": new_id("delivery"),
            "session_id": str(session_id or ""),
            "audience": audience,
            "target_snapshot": target.to_snapshot(),
            "message_type": stored_kind,
            "projection_snapshot": trim_for_audience(projection or {}, audience),
            "rendered_parts": parts,
            "next_part_index": 0,
            "status": "webui_only" if stored_kind == "webui_only" else "pending",
            "priority": int(priority or 100),
            "attempts": 0,
            "next_retry_at": now,
            "last_error_code": "",
            "last_error_message": "",
            "dedupe_key": str(dedupe_key or ""),
            "created_at": now,
            "updated_at": now,
            "delivered_at": "",
            "cancelled_at": "",
            "lease_token": "",
            "lease_until": "",
            "meta": stored_meta,
        }

    async def deliver(self, delivery_id: str, *, actor: str = "system") -> DeliveryOutcome:
        """手动/WebUI 重试：领取新租约后执行一次投递。"""

        if self.repository is None:
            return DeliveryOutcome(False, "discarded", reason="未配置待投递队列")
        record = await self.repository.get(delivery_id)
        if record is None:
            return DeliveryOutcome(False, "discarded", reason="待投递记录不存在")
        status = str(record.get("status") or "")
        if status in TERMINAL_DELIVERY_STATUSES:
            if status == "permanently_failed":
                return DeliveryOutcome(
                    False,
                    "permanently_failed",
                    delivery_id=delivery_id,
                    reason=PERMANENT_FAILURE_REASON,
                    attempts=int(record.get("attempts") or 0),
                )
            return DeliveryOutcome(True, status, delivery_id=delivery_id)
        token = uuid.uuid4().hex
        now = self.now_fn()
        lease_until = _add_seconds(
            now,
            lease_seconds_for(str(record.get("message_type") or "notice")),
        )
        leased = await self.repository.lease(delivery_id, token, lease_until)
        if leased is None:
            # 领取失败时复查当前状态：终态记录给出准确结果，
            # 而不是误导为“正在被其他投递任务处理”。
            fresh = await self.repository.get(delivery_id)
            if fresh is not None:
                fresh_status = str(fresh.get("status") or "")
                if fresh_status == "permanently_failed":
                    return DeliveryOutcome(
                        False,
                        "permanently_failed",
                        delivery_id=delivery_id,
                        reason=PERMANENT_FAILURE_REASON,
                        attempts=int(fresh.get("attempts") or 0),
                    )
                if fresh_status in {"delivered", "cancelled", "webui_only"}:
                    return DeliveryOutcome(
                        True,
                        fresh_status,
                        delivery_id=delivery_id,
                    )
            return DeliveryOutcome(
                False,
                "leased",
                delivery_id=delivery_id,
                reason="消息正在被其他投递任务处理",
            )
        return await self.deliver_leased(delivery_id, token, record=leased)

    async def cancel(
        self,
        delivery_id: str,
        *,
        actor: str = "system",
        reason: str = "",
    ) -> DeliveryOutcome:
        """取消待投递消息；取消后后台任务不得再发送。"""

        if self.repository is None:
            return DeliveryOutcome(False, "discarded", reason="未配置待投递队列")
        cancelled = await self.repository.cancel(
            delivery_id,
            actor=actor,
            reason=reason or "cancelled",
        )
        if cancelled is None:
            return DeliveryOutcome(False, "discarded", delivery_id=delivery_id, reason="待投递记录不存在")
        return DeliveryOutcome(True, "cancelled", delivery_id=delivery_id)

    async def deliver_leased(
        self,
        delivery_id: str,
        token: str,
        *,
        record: dict[str, Any] | None = None,
    ) -> DeliveryOutcome:
        """worker/重试共用：在持有租约的前提下执行一次投递。"""

        if self.repository is None:
            return DeliveryOutcome(False, "discarded", reason="未配置待投递队列")
        if record is None:
            record = await self.repository.get(delivery_id)
        if record is None:
            return DeliveryOutcome(False, "discarded", reason="待投递记录不存在")
        if str(record.get("lease_token") or "") != token:
            return DeliveryOutcome(
                False,
                "leased",
                delivery_id=delivery_id,
                reason="投递租约不匹配",
            )
        status = str(record.get("status") or "")
        if status in {"delivered", "cancelled"}:
            return DeliveryOutcome(True, status, delivery_id=delivery_id)
        if status == "webui_only":
            return DeliveryOutcome(True, "webui_only", delivery_id=delivery_id)
        if status == "permanently_failed":
            return DeliveryOutcome(
                False,
                "permanently_failed",
                delivery_id=delivery_id,
                reason=PERMANENT_FAILURE_REASON,
                attempts=int(record.get("attempts") or 0),
            )
        kind = str(record.get("message_type") or "notice")
        meta = dict(record.get("meta") or {})
        # 建卡码过期：取消未送达消息，不能继续发送失效验证码。
        expires_at = str(meta.get("expires_at") or "")
        if kind == "card_code" and expires_at and expires_at <= self.now_fn():
            await self.repository.cancel(
                delivery_id,
                actor="system",
                reason="expired",
                token=token,
            )
            return DeliveryOutcome(False, "cancelled", delivery_id=delivery_id, reason="建卡码已过期，消息已取消")
        try:
            target = DeliveryTarget.from_snapshot(record.get("target_snapshot") or {})
        except ValueError as exc:
            attempts = int(record.get("attempts") or 0) + 1
            await self.repository.mark_failed(
                delivery_id,
                token,
                attempts=attempts,
                last_error_code="invalid_target",
                last_error_message=str(exc)[:200],
            )
            return DeliveryOutcome(
                False,
                "permanently_failed",
                delivery_id=delivery_id,
                reason="投递目标快照损坏，无法恢复",
                attempts=attempts,
            )
        attempts = int(record.get("attempts") or 0)
        next_attempts = attempts + 1
        parts = list(record.get("rendered_parts") or [])
        start_index = int(record.get("next_part_index") or 0)
        # 发送前复核（D1-DEL-010）：取消/完成/永久失败或租约被换走后
        # 不得再发送，避免取消与发送之间的竞态导致消息仍被发出。
        fresh = await self.repository.get(delivery_id)
        if fresh is None:
            return DeliveryOutcome(
                False,
                "discarded",
                delivery_id=delivery_id,
                reason="待投递记录不存在",
            )
        fresh_status = str(fresh.get("status") or "")
        if fresh_status == "cancelled":
            return DeliveryOutcome(True, "cancelled", delivery_id=delivery_id)
        if fresh_status == "delivered":
            return DeliveryOutcome(True, "delivered", delivery_id=delivery_id)
        if fresh_status == "permanently_failed":
            return DeliveryOutcome(
                False,
                "permanently_failed",
                delivery_id=delivery_id,
                reason=PERMANENT_FAILURE_REASON,
                attempts=int(fresh.get("attempts") or 0),
            )
        if str(fresh.get("lease_token") or "") != token:
            return DeliveryOutcome(
                False,
                "leased",
                delivery_id=delivery_id,
                reason="投递租约不匹配",
            )
        if self.context is None:
            result = SendAttemptResult(
                False,
                "unavailable",
                reason="没有可用宿主上下文",
                method="send_by_session",
                attempted_parts=len(parts),
                sent_parts=0,
                next_part_index=start_index,
                parts=tuple(parts),
            )
        else:
            content_format = str(meta.get("content_format") or "").lower()
            use_markdown = (
                content_format == "markdown"
                if content_format in {"markdown", "plain"}
                else self._markdown_enabled_for(target)
            )
            result = await self.adapter.deliver(
                self.context,
                target,
                parts,
                start_index=start_index,
                use_markdown=use_markdown,
            )
        now = self.now_fn()
        if result.ok:
            await self.repository.complete(
                delivery_id,
                token,
                sent_parts=result.sent_parts,
                delivered_at=now,
            )
            return DeliveryOutcome(
                True,
                "sent",
                delivery_id=delivery_id,
                method=result.method,
                sent_parts=result.sent_parts,
                total_parts=result.attempted_parts,
                attempts=next_attempts,
            )
        if result.sent_parts > 0:
            retry_at = next_retry_at(next_attempts, kind, now=now)
            await self.repository.mark_partial(
                delivery_id,
                token,
                next_part_index=result.next_part_index,
                attempts=next_attempts,
                next_retry_at=retry_at,
                last_error_code=result.status,
                last_error_message=result.reason,
            )
            return DeliveryOutcome(
                False,
                "partially_sent",
                delivery_id=delivery_id,
                reason=result.reason,
                method=result.method,
                sent_parts=result.sent_parts,
                total_parts=result.attempted_parts,
                attempts=next_attempts,
                next_retry_at=retry_at,
            )
        if is_permanently_failed(next_attempts, kind):
            await self.repository.mark_failed(
                delivery_id,
                token,
                attempts=next_attempts,
                last_error_code=result.status,
                last_error_message=result.reason,
            )
            return DeliveryOutcome(
                False,
                "permanently_failed",
                delivery_id=delivery_id,
                reason=result.reason,
                method=result.method,
                attempts=next_attempts,
            )
        retry_at = next_retry_at(next_attempts, kind, now=now)
        await self.repository.mark_retry(
            delivery_id,
            token,
            attempts=next_attempts,
            next_retry_at=retry_at,
            last_error_code=result.status,
            last_error_message=result.reason,
        )
        return DeliveryOutcome(
            False,
            "retry_wait",
            delivery_id=delivery_id,
            reason=result.reason,
            method=result.method,
            attempts=next_attempts,
            next_retry_at=retry_at,
        )

    async def list_status(
        self,
        session_id: str,
        *,
        viewer: str = "dm",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """WebUI 投递状态视图：不包含 UMO、目标 ID 与完整私密正文。"""

        if self.repository is None:
            return []
        rows = await self.repository.list_status(session_id, viewer=viewer, limit=limit)
        views: list[dict[str, Any]] = []
        for row in rows:
            view = self._status_view(row, viewer)
            if view is not None:
                views.append(view)
        return views

    def _status_view(
        self,
        row: dict[str, Any],
        viewer: str,
    ) -> dict[str, Any] | None:
        # 普通玩家只能看到属于自己的 private_owner 投递；身份缺失或
        # 不一致的行 fail closed，看不到密语等他人物件。
        if viewer not in PRIVILEGED_VIEWERS:
            if not row_visible_to_private_viewer(row, viewer):
                return None
        try:
            target = DeliveryTarget.from_snapshot(row.get("target_snapshot") or {})
        except ValueError:
            target = None
        status = str(row.get("status") or "pending")
        meta = dict(row.get("meta") or {})
        view: dict[str, Any] = {
            "delivery_id": str(row.get("delivery_id") or ""),
            "message_type": str(row.get("message_type") or ""),
            "status": status,
            "status_label": status_label(status),
            "channel": channel_label(target.message_type if target else ""),
            "verified": bool(target and target.verified_binding),
            "verified_label": (
                "已验证私聊"
                if (target and target.verified_binding)
                else ("临时目标" if target else "未知")
            ),
            "attempts": int(row.get("attempts") or 0),
            "next_retry_at": str(row.get("next_retry_at") or ""),
            "last_error_code": str(row.get("last_error_code") or ""),
            "created_at": str(row.get("created_at") or ""),
            "delivered_at": str(row.get("delivered_at") or ""),
        }
        recipient = str(meta.get("recipient_name") or "")
        if recipient:
            view["recipient_label"] = recipient
        # 只有主持/管理员能看到正文预览；预览永远经过日志净化。
        if viewer in {"dm", "admin"}:
            parts = list(row.get("rendered_parts") or [])
            if parts:
                view["text_preview"] = sanitize_log(parts[0])[:40]
        return view

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _enqueue_record(
        self,
        *,
        session_id: str,
        target: DeliveryTarget,
        kind: str,
        text: str,
        audience: str,
        dedupe_key: str,
        priority: int,
        projection: dict[str, Any] | None,
        meta: dict[str, Any] | None,
        actor: str,
        parts: list[str] | None = None,
        attempt: SendAttemptResult | None = None,
        use_markdown: bool = False,
    ) -> DeliveryOutcome:
        if self.repository is None:
            return DeliveryOutcome(False, "discarded", reason="未配置待投递队列，消息未被保存")
        policy = KIND_POLICIES.get(kind)
        stored_kind = (policy.stored_kind if policy else kind) or kind
        if dedupe_key:
            existing = await self.repository.dedupe(dedupe_key)
            if existing is not None:
                return DeliveryOutcome(
                    True,
                    "already_exists",
                    delivery_id=str(existing.get("delivery_id") or ""),
                    method=attempt.method if attempt else "",
                )
        if parts is None:
            parts = self.adapter.split_for(
                target.platform_instance_id or target.unified_origin,
                text,
            )
        try:
            record = self.build_record(
                session_id=session_id,
                target=target,
                kind=kind,
                text=text,
                audience=audience,
                dedupe_key=dedupe_key,
                priority=priority,
                projection=projection,
                meta=meta,
                actor=actor,
                parts=parts,
                content_format="markdown" if use_markdown else "plain",
            )
        except ValueError as exc:
            return DeliveryOutcome(
                False,
                "invalid_target",
                reason=str(exc),
            )
        if attempt is not None:
            record["next_part_index"] = attempt.next_part_index
            record["last_error_code"] = (attempt.status if attempt else "") or ""
            record["last_error_message"] = (attempt.reason if attempt else "") or ""
        try:
            stored = await self.repository.create(record)
        except Exception as exc:  # noqa: BLE001
            return DeliveryOutcome(
                False,
                "discarded",
                reason=f"待投递记录写入失败：{type(exc).__name__}",
            )
        delivery_id = str(stored.get("delivery_id") or record["delivery_id"])
        if attempt is not None and attempt.sent_parts > 0:
            outcome_status = "partially_sent"
        else:
            outcome_status = (
                "queued" if stored_kind != "webui_only" else "webui_only"
            )
        return DeliveryOutcome(
            False,
            outcome_status,
            delivery_id=delivery_id,
            reason=attempt.reason if attempt else "",
            method=attempt.method if attempt else "",
            sent_parts=attempt.sent_parts if attempt else 0,
            total_parts=len(parts),
        )


def _default_now() -> str:
    from ..database_support import utc_now

    return utc_now()


def _add_seconds(value: str, seconds: float) -> str:
    from .retry_policy import add_seconds

    return add_seconds(value, seconds)


__all__ = [
    "KIND_POLICIES",
    "DeliveryOutboxRepository",
    "DeliveryOutcome",
    "DeliveryService",
    "KindPolicy",
]
