"""隐私与受众裁剪（D1_PLAN 15 §12-13、14 §17）。

- outbox 保存的是按受众裁剪后的投影或渲染结果，不保存可被错误重投影的
  完整秘密对象；
- 日志与玩家正文隐藏验证码、UMO 与私密正文；
- 普通页面不显示 UMO；技术详情只显示 delivery ID、平台实例、错误分类、
  尝试次数与下一次重试时间。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

AUDIENCE_GROUP = "group"
AUDIENCE_PRIVATE_OWNER = "private_owner"
AUDIENCE_DM = "dm"
AUDIENCE_ADMIN = "admin"
AUDIENCE_WEBUI_DM = "webui_dm"

AUDIENCES = frozenset(
    {AUDIENCE_GROUP, AUDIENCE_PRIVATE_OWNER, AUDIENCE_DM, AUDIENCE_ADMIN, AUDIENCE_WEBUI_DM}
)

# 特权观众：主持与管理员可以看到全部投递状态；其余观众一律按收件人身份裁剪。
PRIVILEGED_VIEWERS = frozenset({"dm", "admin"})

# 收件人身份在投递记录 meta 中的键：优先平台用户身份，其次副本参与者身份。
RECIPIENT_USER_ID_KEY = "recipient_user_id"
RECIPIENT_PARTICIPANT_ID_KEY = "recipient_participant_id"
RECIPIENT_IDENTITY_KEYS = (RECIPIENT_USER_ID_KEY, RECIPIENT_PARTICIPANT_ID_KEY)

# 验证码为 6 位数字：日志、群聊正文与审计中一律掩码。
CARD_CODE_PATTERN = re.compile(r"\b\d{6}\b")

# UMO：平台实例:消息类型:目标 ID。玩家可见文本不得出现。
UMO_PATTERN = re.compile(
    r"[A-Za-z0-9_\-]{1,64}:"
    r"(?:FriendMessage|GroupMessage|TempMessage|ChannelMessage|ThreadMessage):"
    r"[A-Za-z0-9_\-]{1,128}"
)

# 投影键白名单：按受众裁剪。未列入的键（含私密字段）一律丢弃。
_PROJECTION_ALLOWED: dict[str, tuple[str, ...]] = {
    AUDIENCE_GROUP: (
        "name",
        "summary",
        "public_fields",
        "public_status",
        "public_flags",
    ),
    AUDIENCE_PRIVATE_OWNER: (
        "name",
        "summary",
        "public_fields",
        "public_status",
        "public_flags",
        "own_secret_fields",
        "own_private",
    ),
    AUDIENCE_DM: (
        "name",
        "summary",
        "public_fields",
        "public_status",
        "public_flags",
        "secret_summary",
        "recipient",
    ),
    AUDIENCE_ADMIN: (
        "name",
        "summary",
        "public_fields",
        "public_status",
        "public_flags",
        "secret_summary",
        "recipient",
        "recipient_id",
    ),
    AUDIENCE_WEBUI_DM: (
        "name",
        "summary",
        "public_fields",
        "public_status",
        "public_flags",
        "secret_summary",
        "recipient",
    ),
}

STATUS_LABELS: dict[str, str] = {
    "pending": "等待投递",
    "leased": "正在投递",
    "partially_sent": "已部分送达",
    "retry_wait": "发送失败，等待重试",
    "delivered": "已送达",
    "permanently_failed": "发送失败（已达上限）",
    "cancelled": "已取消",
    "webui_only": "仅 WebUI 可见",
}

CHANNEL_LABELS: dict[str, str] = {
    "private": "私聊",
    "group": "群聊",
    "channel": "频道",
    "thread": "帖子",
    "webui_only": "WebUI",
}


def redact_card_code(text: Any, code: Any = "") -> str:
    """掩码验证码；未提供明文时按 6 位数字模式掩码。"""

    value = str(text or "")
    code_text = str(code or "").strip()
    if code_text and code_text in value:
        value = value.replace(code_text, "******")
    return CARD_CODE_PATTERN.sub("******", value)


def sanitize_log(text: Any) -> str:
    """日志净化：掩码 UMO 与 6 位验证码，保留可读正文。"""

    value = str(text or "")
    value = UMO_PATTERN.sub("***", value)
    return redact_card_code(value)


def contains_umo(text: Any) -> bool:
    return bool(UMO_PATTERN.search(str(text or "")))


def find_umo(text: Any) -> list[str]:
    return list(UMO_PATTERN.findall(str(text or "")))


def trim_for_audience(
    projection: Mapping[str, Any] | None,
    audience: str,
) -> dict[str, Any]:
    """按受众白名单裁剪投影；未知受众退化为群聊公开集。"""

    allowed = _PROJECTION_ALLOWED.get(audience, _PROJECTION_ALLOWED[AUDIENCE_GROUP])
    source = dict(projection) if isinstance(projection, Mapping) else {}
    return {key: source[key] for key in allowed if key in source}


def status_label(status: Any) -> str:
    return STATUS_LABELS.get(str(status or ""), "未知状态")


def viewer_identity(viewer: Any) -> str:
    """从 viewer 参数解析普通玩家的身份标识。

    特权角色（dm/admin）、空值与无身份占位（player）返回空串；
    支持 ``player:<身份>`` 与裸身份两种写法。返回空串表示 fail closed，
    不匹配任何投递行。
    """

    value = str(viewer or "").strip()
    if not value or value in PRIVILEGED_VIEWERS or value == "player":
        return ""
    if value.startswith("player:"):
        return value[len("player:"):].strip()
    return value


def recipient_identity(meta: Mapping[str, Any] | None) -> str:
    """从投递记录 meta 提取收件人身份。

    优先 ``recipient_user_id``，其次 ``recipient_participant_id``；
    两者都缺失时返回空串（旧/异常行，普通玩家一律不可见）。
    """

    source = dict(meta) if isinstance(meta, Mapping) else {}
    for key in RECIPIENT_IDENTITY_KEYS:
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def row_visible_to_private_viewer(row: Mapping[str, Any], viewer: Any) -> bool:
    """普通玩家可见性：仅 ``private_owner`` 且收件人身份与 viewer 身份一致。

    非 private_owner、身份缺失（旧/异常行）或身份不一致的行一律不可见
    （fail closed），防止玩家枚举他人密语/建卡码投递状态。收件人身份键
    （``recipient_user_id``/``recipient_participant_id``）任一匹配即可。
    """

    source = dict(row) if isinstance(row, Mapping) else {}
    if str(source.get("audience") or "") != AUDIENCE_PRIVATE_OWNER:
        return False
    identity = viewer_identity(viewer)
    if not identity:
        return False
    meta = source.get("meta")
    meta_source = dict(meta) if isinstance(meta, Mapping) else {}
    for key in RECIPIENT_IDENTITY_KEYS:
        value = str(meta_source.get(key) or "").strip()
        if value and value == identity:
            return True
    return False


def channel_label(kind: Any) -> str:
    return CHANNEL_LABELS.get(str(kind or ""), "未知渠道")


def public_failure_notice(kind: str = "", command_prefix: str = "/团") -> str:
    """主动私聊失败时群内可见的恢复说明（D1_PLAN 15 §15、14 §6.3）。"""

    command = "建卡" if kind == "card_code" else "当前"
    return (
        "【私聊消息未送达】\n"
        "平台没有确认消息已经发送。\n\n"
        "系统处理\n"
        "消息已进入待投递队列，不会重复执行原操作。\n\n"
        "下一步\n"
        f"请先私聊 BOT 发送：\n{command_prefix} {command}"
    )


__all__ = [
    "AUDIENCE_ADMIN",
    "AUDIENCE_DM",
    "AUDIENCE_GROUP",
    "AUDIENCE_PRIVATE_OWNER",
    "AUDIENCE_WEBUI_DM",
    "AUDIENCES",
    "CHANNEL_LABELS",
    "PRIVILEGED_VIEWERS",
    "RECIPIENT_IDENTITY_KEYS",
    "RECIPIENT_PARTICIPANT_ID_KEY",
    "RECIPIENT_USER_ID_KEY",
    "STATUS_LABELS",
    "channel_label",
    "contains_umo",
    "find_umo",
    "public_failure_notice",
    "recipient_identity",
    "redact_card_code",
    "row_visible_to_private_viewer",
    "sanitize_log",
    "status_label",
    "trim_for_audience",
    "viewer_identity",
]
