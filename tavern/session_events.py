"""D1 WP-11：副本领域事件的安全语义投影。

将 ``session_events`` 原始行投影为普通用户可读的
``title / summary / affected_modules / visibility``，管理员可查看折叠的
``technical`` 详情。普通用户响应绝不包含 ``actor_ref``、``command_id``、
``condition_id``、``event_id``、原始 ``type`` 或原始 payload——内部技术
契约与玩家可见说明分离（D1 文档 03 §9 / 12 WP-11）。

本模块只做只读投影，不触碰任何写入路径。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


# 结构性事件：发生后客户端应放弃增量状态并重新全量拉取。
# 以公开 category 判定为主（普通用户响应不含原始 type），此集合用于
# 管理员响应中按 type 精确兜底。
STRUCTURAL_EVENT_TYPES = frozenset(
    {
        "event:session.archived",
        "event:session.restored",
        "event:terminal.pending",
        "event:snapshot.restored",
        "event:world.rebuilt",
        "event:session.created",
    }
)

# 结构性公开分类（与事件类型一一对应，供非管理员响应判定）。
STRUCTURAL_CATEGORIES = frozenset({"archive", "terminal"})

# 公开分类 → 玩家可见中文说明（前端模块徽标与时间线分类使用）。
CATEGORY_LABELS: dict[str, str] = {
    "whisper": "密语",
    "archive": "归档",
    "terminal": "终局",
    "delivery": "投递",
    "story": "故事",
    "turn": "回合",
    "actor_fate": "角色命运",
    "party": "小队",
    "system": "副本状态",
}

_TERMINATION_LABELS = {
    "completed": "圆满结束",
    "failed": "失败",
    "aborted": "中止",
}

_DELIVERY_STATUS_LABELS = {
    "queued": "已排队",
    "sending": "发送中",
    "partially_sent": "部分送达",
    "delivered": "已送达",
    "failed": "发送失败",
    "permanently_failed": "发送失败（已达上限）",
    "cancelled": "已取消",
    "webui_only": "仅面板展示",
}

_DEFAULT_SUMMARY = "副本记录了一条新状态，请刷新页面查看最新内容。"
_DEFAULT_TITLE = "副本状态更新"
_DEFAULT_MODULES = ("system",)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = 120) -> str:
    """归一化任意标量字段为截断后的纯文本。"""
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _summary_whisper(payload: Mapping[str, Any]) -> str:
    recipient = _text(payload.get("recipient_name") or payload.get("target_name"))
    body = _text(payload.get("text") or payload.get("content"), limit=80)
    if recipient:
        return f"已发送给「{recipient}」。"
    if body:
        return f"内容：{body}"
    return "主持人发送了一条密语。"


def _summary_archived(payload: Mapping[str, Any]) -> str:
    ending = _text(payload.get("ending_name") or payload.get("ending_title"))
    termination = str(payload.get("termination_type") or "").strip()
    label = _TERMINATION_LABELS.get(termination, "")
    if ending and label:
        return f"结局「{ending}」达成（{label}），副本进入只读归档。"
    if ending:
        return f"结局「{ending}」达成，副本进入只读归档。"
    return "副本已归档，之后内容只读。"


def _summary_terminal_pending(payload: Mapping[str, Any]) -> str:
    ending = _text(payload.get("ending_name") or payload.get("ending_title"))
    reason = _text(payload.get("reason") or payload.get("summary"), limit=80)
    if ending and reason:
        return f"结局「{ending}」条件已满足：{reason}。等待主持人确认。"
    if ending:
        return f"结局「{ending}」条件已满足，等待主持人确认。"
    return "终局条件已满足，等待主持人确认。"


def _summary_delivery_updated(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    label = _DELIVERY_STATUS_LABELS.get(status, "")
    recipient = _text(payload.get("recipient_name"))
    parts: list[str] = []
    if label:
        parts.append(label)
    if recipient:
        parts.append(f"收件人：{recipient}")
    return "，".join(parts) + "。" if parts else "投递状态已更新。"


def _summary_generic(payload: Mapping[str, Any]) -> str:
    """未知事件类型的语义兜底：只信任 payload 中的中文语义字段。"""
    title = _text(payload.get("title"))
    summary = _text(payload.get("summary") or payload.get("message"), limit=100)
    if summary:
        return summary
    if title:
        return title
    return _DEFAULT_SUMMARY


def _title_generic(payload: Mapping[str, Any]) -> str:
    title = _text(payload.get("title"))
    return title if title else _DEFAULT_TITLE


def _spec_for(type_: str) -> dict[str, Any]:
    """事件类型 → 投影规格；未知类型走系统兜底。"""
    specs: dict[str, dict[str, Any]] = {
        "event:dm.whisper": {
            "category": "whisper",
            "title": "主持人密语",
            "modules": ("delivery",),
            "summary": _summary_whisper,
        },
        "event:session.archived": {
            "category": "archive",
            "title": "副本归档",
            "modules": ("session", "story"),
            "summary": _summary_archived,
        },
        "event:session.restored": {
            "category": "archive",
            "title": "副本恢复",
            "modules": ("session", "story"),
            "summary": lambda payload: _text(
                payload.get("summary") or "副本已从快照恢复。", limit=100
            ),
        },
        "event:terminal.pending": {
            "category": "terminal",
            "title": "终局待确认",
            "modules": ("session", "actor_fate"),
            "summary": _summary_terminal_pending,
        },
        "event:delivery.updated": {
            "category": "delivery",
            "title": "投递状态更新",
            "modules": ("delivery",),
            "summary": _summary_delivery_updated,
        },
        "event:story_progress": {
            "category": "story",
            "title": "故事推进",
            "modules": ("story",),
            "summary": _summary_generic,
        },
        "event:turn.changed": {
            "category": "turn",
            "title": "回合变更",
            "modules": ("turn",),
            "summary": _summary_generic,
        },
        "event:actor.fate_changed": {
            "category": "actor_fate",
            "title": "角色命运变化",
            "modules": ("actor_fate",),
            "summary": _summary_generic,
        },
        "event:actor.state_changed": {
            "category": "party",
            "title": "队友状态更新",
            "modules": ("party",),
            "summary": _summary_generic,
        },
        "event:item.inventory_changed": {
            "category": "party",
            "title": "小队背包更新",
            "modules": ("party",),
            "summary": _summary_generic,
        },
        "event:snapshot.restored": {
            "category": "archive",
            "title": "快照恢复",
            "modules": ("session",),
            "summary": lambda payload: _text(
                payload.get("summary") or "副本恢复到了此前的存档状态。",
                limit=100,
            ),
        },
        "event:world.rebuilt": {
            "category": "system",
            "title": "世界数据重建",
            "modules": ("world", "session"),
            "summary": lambda payload: _text(
                payload.get("summary") or "世界数据已重建，请刷新页面。",
                limit=100,
            ),
        },
    }
    # ``append_event`` derives the authoritative underscore form from
    # ``story_progress_meta``.  Keep the dotted kind as an explicit safe alias
    # for existing system producers without treating arbitrary unknown kinds as
    # story events.
    if type_ == "event:story.progress":
        type_ = "event:story_progress"
    return specs.get(type_, {})


def project_session_event(
    row: Mapping[str, Any],
    *,
    is_admin: bool = False,
) -> dict[str, Any]:
    """将一条 ``session_events`` 原始行投影为公开响应条目。

    普通用户响应只包含 ``seq / category / title / summary /
    affected_modules / visibility / created_at``；管理员额外获得
    ``type`` 与折叠的 ``technical``（event_id、actor_ref、command_id、
    causation_id、correlation_id、raw_payload）。

    无论 payload 中是否存在 ``condition_id``、内部 ID 或原始 ref，
    普通用户响应都只从白名单语义字段生成，绝不透传原始 payload。
    """

    if not isinstance(row, Mapping):
        row = {}
    event_type = str(row.get("type") or "").strip()
    payload = _payload(row)
    spec = _spec_for(event_type)
    category = str(spec.get("category") or "system")
    title = _text(
        payload.get("title")
        or spec.get("title")
        or _DEFAULT_TITLE
    )
    summary_fn = spec.get("summary")
    if callable(summary_fn):
        summary = _text(summary_fn(payload), limit=200)
    else:
        summary = _summary_generic(payload)
    if not summary:
        summary = _DEFAULT_SUMMARY
    modules = tuple(
        str(module)
        for module in (spec.get("modules") or _DEFAULT_MODULES)
        if str(module or "").strip()
    ) or _DEFAULT_MODULES
    projected: dict[str, Any] = {
        "seq": int(row.get("seq") or 0),
        "category": category,
        "title": title,
        "summary": summary,
        "affected_modules": list(modules),
        "visibility": str(row.get("visibility") or "public"),
        "created_at": str(row.get("created_at") or ""),
    }
    if is_admin:
        projected["type"] = event_type
        projected["technical"] = {
            "event_id": str(row.get("event_id") or ""),
            "actor_ref": str(row.get("actor_ref") or ""),
            "command_id": str(row.get("command_id") or ""),
            "causation_id": str(row.get("causation_id") or ""),
            "correlation_id": str(row.get("correlation_id") or ""),
            "raw_payload": payload,
        }
    return projected


def summarize_affected_modules(
    items: Sequence[Mapping[str, Any]],
) -> list[str]:
    """汇总增量事件的受影响模块，保持首次出现顺序、去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for module in item.get("affected_modules") or ():
            name = str(module or "").strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def has_structural_event(items: Sequence[Mapping[str, Any]]) -> bool:
    """增量中是否包含结构性事件（客户端应随后全量刷新）。"""
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("category") or "") in STRUCTURAL_CATEGORIES:
            return True
        if str(item.get("type") or "") in STRUCTURAL_EVENT_TYPES:
            return True
    return False
