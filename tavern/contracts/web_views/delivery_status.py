"""D1-UX-012 / D1-WEB-011：DeliveryStatusView 纯投影。

输入是 ``delivery_outbox`` 行（或 D1 扩展后的投递记录）映射。状态
映射覆盖 C6 现有枚举（pending/sent/delivered_on_reply/dismissed）与 D1
扩展枚举（leased/partially_sent/retry_wait/permanently_failed/cancelled/
webui_only）。普通视图绝不返回 UMO / origin；``technical`` 只包含
delivery ID、尝试次数等排障字段。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..common import clean_label, safe_int


# outbox 内部状态 → (D1 状态, 中文状态名)
DELIVERY_STATUS_MAP = {
    "pending": ("queued", "等待投递"),
    "leased": ("sending", "正在发送"),
    "partially_sent": ("partially_sent", "部分送达"),
    "retry_wait": ("queued", "等待重试"),
    "sent": ("delivered", "已送达"),
    "delivered_on_reply": ("delivered", "已送达"),
    "permanently_failed": ("failed", "发送失败"),
    "cancelled": ("cancelled", "已取消"),
    "dismissed": ("cancelled", "已取消"),
    "webui_only": ("queued", "等待投递"),
}

DELIVERY_STATE_LABELS = {
    "queued": "等待投递",
    "sending": "正在发送",
    "partially_sent": "部分送达",
    "delivered": "已送达",
    "failed": "发送失败",
    "cancelled": "已取消",
}

_STATE_MESSAGE = {
    "queued": "平台尚未确认发送，系统会自动重试。",
    "sending": "消息正在发送，请稍候。",
    "partially_sent": "部分内容已送达，系统将继续发送剩余内容。",
    "delivered": "消息已送达。",
    "failed": "平台没有确认消息已发送。消息已进入待投递队列，不会重复执行原操作。",
    "cancelled": "该消息已取消，不会继续发送。",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def project_delivery_status_view(
    delivery: Mapping[str, Any] | None,
    *,
    target_label: str = "",
    target_kind: str = "private",
    verified: bool | None = None,
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """把一条投递记录规范化为主持 / 后台可读的投递状态视图。

    ``verified`` 为空时：群聊目标视为已验证；私聊目标视为未验证（临时
    目标不允许发送高敏感密语，也不允许显示为已绑定）。
    """

    delivery = _mapping(delivery)
    raw_status = str(delivery.get("status") or "pending").strip().lower()
    state, state_label = DELIVERY_STATUS_MAP.get(
        raw_status, ("failed", "状态异常")
    )
    kind = str(target_kind or delivery.get("kind") or "private").strip().lower()
    if kind == "group":
        verified_value = True if verified is None else bool(verified)
    else:
        verified_value = bool(verified) if verified is not None else False

    label = clean_label(target_label)
    if kind == "group":
        target_view = label or "群聊"
    else:
        target_view = f"{label or '玩家'}的私聊" if label else "私聊"

    message = _STATE_MESSAGE.get(state, "投递状态无法识别，系统不会重复发送。")
    if not verified_value and kind != "group":
        message = "私聊目标尚未验证，暂不发送高敏感内容。" + message

    actions: list[str] = []
    if state in {"queued", "failed"}:
        if verified_value or kind == "group":
            actions = ["retry", "cancel"]
    elif state == "sending":
        actions = ["cancel"]

    view: dict[str, Any] = {
        "schema": "tavern-delivery-status/1.0.0-rc10",
        "state": state,
        "state_label": state_label,
        "target_label": target_view,
        "verified": verified_value,
        "message": message,
        "available_actions": actions,
        "technical": None,
    }
    if include_technical_refs:
        view["technical"] = {
            "delivery_id": str(delivery.get("id") or ""),
            "kind": str(delivery.get("kind") or ""),
            "attempts": safe_int(delivery.get("attempts")),
            "next_retry_at": str(delivery.get("next_retry_at") or ""),
            "error_message": clean_label(delivery.get("last_error")),
        }
    return view


__all__ = [
    "DELIVERY_STATE_LABELS",
    "DELIVERY_STATUS_MAP",
    "project_delivery_status_view",
]
