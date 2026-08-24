"""会话叙事事件唯一权威写入器。

D1 要求各 repository 对 ``events`` 表的写入全部收敛到本模块
（D1-RUN-006 / WP-04 / WP-11）。调用方应已处于既有数据库事务中，
保证事件与权威状态、Receipt、Outbox 同事务落库。

WP-11：同一事务内同步写一条结构化 ``session_events`` 增量行
（副本单调序号、event_id 幂等、payload 只含中文安全语义，类型由
``meta.kind`` / ``meta.event_type`` / ``role`` 规范化）。若调用方未
开启事务（历史只读路径），则以同一连接包裹 ``events`` 与
``session_events`` 两处写入并一次性提交，杜绝「事件已落库而增量
丢失」的半写状态。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..database_support import (
    insert_session_event,
    json_dump,
    json_load,
    new_id,
    utc_now,
)

#: 允许的副本事件可见性（D1-RUN-006）。
_EVENT_VISIBILITIES = frozenset(
    {"public", "private", "dm", "admin", "character"}
)

#: role → 中文标题（玩家可见安全语义）。
_ROLE_TITLES = {
    "player": "玩家行动",
    "narrator": "叙事推进",
    "ooc": "场外发言",
    "system": "系统事件",
    "dm": "主持操作",
    "whisper": "主持密语",
}

#: 事件域 → 受影响模块的语义别名。
_MODULE_ALIASES = {
    "story_progress": "story",
    "opening": "story",
    "snapshot": "session",
    "terminal": "session",
    "session_finalized": "session",
    "session_branch": "session",
}


def _normalize_event_type(role: str, meta: Mapping[str, Any]) -> str:
    """从 meta.kind / meta.event_type / role 规范化事件类型（D1-RUN-006）。"""
    raw = str(
        meta.get("kind") or meta.get("event_type") or role or "system"
    ).strip().lower()
    cleaned = re.sub(r"[^a-z0-9_.-]+", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_.-")
    if not cleaned:
        cleaned = "system"
    return f"event:{cleaned}"[:80]


def _event_visibility(meta: Mapping[str, Any]) -> str:
    visibility = str(meta.get("visibility") or "public").strip().lower()[:40]
    return visibility if visibility in _EVENT_VISIBILITIES else "public"


def _meta_text(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_title(role: str, meta: Mapping[str, Any]) -> str:
    title = str(meta.get("title") or "").strip()
    if title:
        return title[:80]
    return _ROLE_TITLES.get(str(role or "").strip(), "事件")


def _safe_summary(content: Any, meta: Mapping[str, Any]) -> str:
    summary = str(meta.get("summary") or content or "").strip()
    return summary[:240]


def _affected_modules(role: str, meta: Mapping[str, Any]) -> list[str]:
    raw = meta.get("affected_modules")
    if isinstance(raw, (list, tuple)):
        modules = [
            str(item).strip()[:60]
            for item in raw
            if isinstance(item, str) and item.strip()
        ]
        if modules:
            return modules[:8]
    domain = (
        str(meta.get("kind") or meta.get("event_type") or "")
        .split(".", 1)[0]
        .strip()
        .lower()
    )
    if domain:
        return [_MODULE_ALIASES.get(domain, domain)[:60]]
    return [
        "story"
        if str(role or "").strip() in {"player", "narrator"}
        else "session"
    ]


def _session_event_payload(
    *,
    role: str,
    content: str,
    turn_no: int,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """构造只含安全语义的副本事件 payload。

    不包含任何稳定 ID / 内部引用（actor_ref、participant_id、快照 ID、
    条件 ID 等只进 audit 或事件列，不进玩家可见 payload）。
    """
    payload: dict[str, Any] = {
        "title": _safe_title(role, meta),
        "summary": _safe_summary(content, meta),
        "affected_modules": _affected_modules(role, meta),
        "turn_no": 0,
    }
    try:
        payload["turn_no"] = int(turn_no or 0)
    except (TypeError, ValueError, OverflowError):
        pass
    return payload


def append_event(
    connection: Any,
    *,
    session_id: str,
    turn_no: int,
    role: str,
    actor_id: str = "",
    actor_name: str = "",
    content: str = "",
    meta: Mapping[str, Any] | None = None,
    event_id: str | None = None,
    created_at: str | None = None,
) -> str:
    """在既有事务中追加一条叙事事件并返回 event_id。

    - 事件结构保持与 events 表列一一对应，不改变历史语义；
    - 同一事务内同步写入结构化 ``session_events`` 增量行（WP-11），
      event_id 幂等，payload 只含中文安全语义；
    - 调用方传入 event_id 时原样使用（幂等/关联场景）；
    - 已开启事务时由调用方统一 COMMIT/ROLLBACK；未开启事务时本函数
      以同一连接包裹两处写入并一次性提交。
    """
    resolved_id = event_id or new_id("event")
    now = created_at or utc_now()
    meta = dict(meta) if isinstance(meta, Mapping) else {}
    owned_transaction = not bool(getattr(connection, "in_transaction", False))
    if owned_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT * FROM events WHERE id = ?",
            (resolved_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["session_id"] or "") != str(session_id):
                raise ValueError("事件 ID 已被其他副本占用")
            existing_meta = json_load(existing["meta_json"], {})
            existing_meta = (
                dict(existing_meta)
                if isinstance(existing_meta, Mapping)
                else {}
            )
            insert_session_event(
                connection,
                session_id=str(existing["session_id"]),
                event_id=resolved_id,
                type_=_normalize_event_type(
                    str(existing["role"] or ""),
                    existing_meta,
                ),
                actor_ref=str(existing["actor_id"] or ""),
                command_id=_meta_text(existing_meta.get("command_id")),
                causation_id=_meta_text(existing_meta.get("causation_id")),
                correlation_id=_meta_text(
                    existing_meta.get("correlation_id")
                ),
                payload=_session_event_payload(
                    role=str(existing["role"] or ""),
                    content=str(existing["content"] or ""),
                    turn_no=int(existing["turn_no"] or 0),
                    meta=existing_meta,
                ),
                visibility=_event_visibility(existing_meta),
                created_at=str(existing["created_at"] or now),
            )
            if owned_transaction:
                connection.execute("COMMIT")
            return resolved_id
        connection.execute(
            """
            INSERT INTO events(
                id, session_id, turn_no, role, actor_id, actor_name,
                content, meta_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_id,
                str(session_id),
                int(turn_no or 0),
                str(role or ""),
                str(actor_id or ""),
                str(actor_name or ""),
                str(content or ""),
                json_dump(meta),
                now,
            ),
        )
        insert_session_event(
            connection,
            session_id=str(session_id),
            event_id=resolved_id,
            type_=_normalize_event_type(role, meta),
            actor_ref=str(actor_id or ""),
            command_id=_meta_text(meta.get("command_id")),
            causation_id=_meta_text(meta.get("causation_id")),
            correlation_id=_meta_text(meta.get("correlation_id")),
            payload=_session_event_payload(
                role=role,
                content=content,
                turn_no=turn_no,
                meta=meta,
            ),
            visibility=_event_visibility(meta),
            created_at=now,
        )
        if owned_transaction:
            connection.execute("COMMIT")
    except BaseException:
        if owned_transaction:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
        raise
    return resolved_id
