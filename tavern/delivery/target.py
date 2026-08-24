"""D1 投递目标模型（D1_PLAN 15 §2-4、§12）。

内部统一使用 :class:`DeliveryTarget`。目标解析以
``(platform_instance_id, message_type, target_id)`` 为第一边界，禁止跨平台
实例串用绑定。玩家正文与普通 WebUI 永远不接触 ``unified_origin``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

TARGET_KIND_GROUP = "group"
TARGET_KIND_PRIVATE = "private"
TARGET_KIND_CHANNEL = "channel"
TARGET_KIND_THREAD = "thread"
TARGET_KIND_WEBUI_ONLY = "webui_only"

TARGET_KINDS = frozenset(
    {
        TARGET_KIND_GROUP,
        TARGET_KIND_PRIVATE,
        TARGET_KIND_CHANNEL,
        TARGET_KIND_THREAD,
        TARGET_KIND_WEBUI_ONLY,
    }
)

# UMO 消息类型名 → 目标种类。QQ 私聊 UMO 形态：
# ``平台实例:FriendMessage:用户ID``；群聊 UMO 形态：
# ``平台实例:GroupMessage:群ID``。
_ORIGIN_TYPE_KIND = {
    "FriendMessage": TARGET_KIND_PRIVATE,
    "GroupMessage": TARGET_KIND_GROUP,
    "TempMessage": TARGET_KIND_PRIVATE,
    "ChannelMessage": TARGET_KIND_CHANNEL,
    "ThreadMessage": TARGET_KIND_THREAD,
}

# 目标种类 → UMO 消息类型名（构造临时会话时使用）。
# 显式声明：private 统一使用 FriendMessage，避免 TempMessage 覆盖。
_KIND_ORIGIN_TYPE = {
    TARGET_KIND_GROUP: "GroupMessage",
    TARGET_KIND_PRIVATE: "FriendMessage",
    TARGET_KIND_CHANNEL: "ChannelMessage",
    TARGET_KIND_THREAD: "ThreadMessage",
}


def origin_type_name(target_kind: str) -> str:
    """返回目标种类对应的 UMO 消息类型名（如 private → FriendMessage）。"""

    return _KIND_ORIGIN_TYPE.get(target_kind, "")


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    platform_instance_id: str
    message_type: str
    target_id: str
    unified_origin: str = ""
    target_kind: str = TARGET_KIND_PRIVATE
    verified_binding: bool = False
    source: str = "constructed"

    def __post_init__(self) -> None:
        if self.message_type not in TARGET_KINDS:
            raise ValueError(f"未知投递消息类型: {self.message_type}")
        if self.message_type == TARGET_KIND_WEBUI_ONLY:
            return
        if not str(self.platform_instance_id or "").strip():
            raise ValueError("投递目标缺少平台实例")
        if not str(self.target_id or "").strip():
            raise ValueError("投递目标缺少目标 ID")

    @property
    def identity(self) -> tuple[str, str, str]:
        """平台隔离键：(platform_instance_id, message_type, target_id)。"""

        return (self.platform_instance_id, self.message_type, self.target_id)

    @classmethod
    def from_origin(
        cls,
        origin: Any,
        *,
        verified_binding: bool = False,
        source: str = "inbound",
    ) -> DeliveryTarget | None:
        """从完整 UMO 解析目标；格式无法识别时返回 None。"""

        raw = str(origin or "").strip()
        if not raw:
            return None
        segments = raw.split(":", 2)
        if len(segments) < 3:
            return None
        platform_id, origin_type, target_id = segments
        kind = _ORIGIN_TYPE_KIND.get(origin_type)
        if kind is None:
            return None
        return cls(
            platform_instance_id=platform_id,
            message_type=kind,
            target_id=target_id,
            unified_origin=raw,
            target_kind=kind,
            verified_binding=bool(verified_binding),
            source=source,
        )

    @classmethod
    def temporary_private(
        cls,
        platform_instance_id: Any,
        user_id: Any,
        *,
        source: str = "temporary_friend",
    ) -> DeliveryTarget:
        """构造临时私聊目标（D1-DEL-003）。

        临时目标永远不是已验证绑定；缺少用户 ID 时直接拒绝构造。
        """

        platform_id = str(platform_instance_id or "").strip()
        target_user_id = str(user_id or "").strip()
        if not platform_id:
            raise ValueError("临时私聊目标缺少平台实例")
        if not target_user_id:
            raise ValueError("临时私聊目标缺少用户 ID")
        return cls(
            platform_instance_id=platform_id,
            message_type=TARGET_KIND_PRIVATE,
            target_id=target_user_id,
            target_kind=TARGET_KIND_PRIVATE,
            verified_binding=False,
            source=source,
        )

    @classmethod
    def webui_only(cls, *, source: str = "webui") -> DeliveryTarget:
        """仅 WebUI 可见的目标，不触达任何平台。"""

        return cls(
            platform_instance_id="",
            message_type=TARGET_KIND_WEBUI_ONLY,
            target_id="",
            target_kind=TARGET_KIND_WEBUI_ONLY,
            verified_binding=False,
            source=source,
        )

    @classmethod
    def from_snapshot(cls, data: Any) -> DeliveryTarget:
        """从 outbox 目标快照恢复；快照缺失或损坏时抛 ValueError。"""

        if isinstance(data, cls):
            return data
        values = dict(data) if isinstance(data, Mapping) else {}
        message_type = str(values.get("message_type") or "")
        target_kind = str(values.get("target_kind") or "") or message_type
        if not target_kind:
            target_kind = TARGET_KIND_PRIVATE
        return cls(
            platform_instance_id=str(values.get("platform_instance_id") or ""),
            message_type=message_type or target_kind,
            target_id=str(values.get("target_id") or ""),
            unified_origin=str(values.get("unified_origin") or ""),
            target_kind=target_kind,
            verified_binding=bool(values.get("verified_binding")),
            source=str(values.get("source") or "restored"),
        )

    @classmethod
    def from_authoritative(cls, data: Any) -> DeliveryTarget | None:
        """从 ``delivery_targets`` 权威行恢复目标（D1-DEL-002/003）。

        表行以 ``(platform_instance_id, message_type, target_id)`` 为唯一键，
        ``message_type`` 即目标种类（group/private/channel/webui_only）。
        行缺失、损坏或包含未知消息类型时返回 None，调用方自行回退。
        """

        if isinstance(data, cls):
            return data
        values = dict(data) if isinstance(data, Mapping) else {}
        message_type = str(values.get("message_type") or "").strip().lower()
        if message_type not in TARGET_KINDS:
            return None
        try:
            return cls(
                platform_instance_id=str(
                    values.get("platform_instance_id") or ""
                ),
                message_type=message_type,
                target_id=str(values.get("target_id") or ""),
                unified_origin=str(values.get("unified_origin") or ""),
                target_kind=message_type,
                verified_binding=bool(values.get("verified_binding")),
                source=str(values.get("source") or "authoritative"),
            )
        except ValueError:
            return None

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为 outbox 存储快照（含 UMO，仅用于发送与审计）。"""

        return {
            "platform_instance_id": self.platform_instance_id,
            "message_type": self.message_type,
            "target_id": self.target_id,
            "unified_origin": self.unified_origin,
            "target_kind": self.target_kind,
            "verified_binding": self.verified_binding,
            "source": self.source,
        }

    def for_audience(self, audience: str = "group") -> dict[str, Any]:
        """玩家/普通 WebUI 可见视图：永不包含 unified_origin。

        target_id 属于内部稳定 ID，只对主持与管理员显示；普通受众视图
        只保留平台实例、消息类型、绑定状态与来源标记。
        """

        view: dict[str, Any] = {
            "platform_instance_id": self.platform_instance_id,
            "message_type": self.message_type,
            "target_kind": self.target_kind,
            "verified_binding": self.verified_binding,
            "source": self.source,
        }
        if audience in {"dm", "admin"}:
            view["target_id"] = self.target_id
        return view


__all__ = [
    "TARGET_KIND_CHANNEL",
    "TARGET_KIND_GROUP",
    "TARGET_KIND_PRIVATE",
    "TARGET_KIND_THREAD",
    "TARGET_KIND_WEBUI_ONLY",
    "TARGET_KINDS",
    "DeliveryTarget",
    "origin_type_name",
]
