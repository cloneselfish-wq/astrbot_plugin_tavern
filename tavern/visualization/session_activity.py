"""History, delivery, and generation lazy-lens orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .common import integer, latest_timestamp, mapping, text
from .deliveries import project_deliveries
from .envelopes import VisualEnvelope, visual_envelope
from .generation import project_generation
from .history import project_history
from .keys import OpaqueKeyFactory
from .service import _read, _readonly, visual_permissions


async def build_session_history(
    database: Any,
    session: Mapping[str, Any],
    *,
    role: str,
    is_admin: bool,
    keys: OpaqueKeyFactory,
    cursor: str,
    page_size: int,
    delivery_cursor: str = "",
    delivery_service: Any = None,
    delivery_viewer: str = "",
) -> VisualEnvelope:
    problems: list[dict[str, Any]] = []
    session_id = text(session.get("id"), limit=180)
    after_sequence = keys.read_cursor("historyseq", cursor) if cursor else 0
    latest_sequence = await _read(
        problems,
        "visual.history.sequence_read_failed",
        "时间线位置读取失败。",
        lambda: database.latest_session_event_seq(session_id),
        0,
    )
    rows = await _read(
        problems,
        "visual.history.events_read_failed",
        "副本时间线读取失败。",
        lambda: database.list_session_events(
            session_id,
            after_seq=after_sequence,
            limit=max(1, min(100, int(page_size))) + 1,
            visibility="" if role in {"dm", "admin"} else "public",
        ),
        [],
    )
    timeline = project_history(
        rows,
        latest_sequence=integer(latest_sequence, 0),
        keys=keys,
        page_size=page_size,
        expose_latest=role in {"dm", "admin"},
    )
    turn_runs = []
    if callable(getattr(database, "list_turn_delivery_runs", None)):
        turn_runs = await _read(
            problems,
            "visual.history.turn_deliveries_read_failed",
            "逐段投递状态读取失败。",
            lambda: database.list_turn_delivery_runs(session_id, limit=200),
            [],
        )
    queued: Sequence[Mapping[str, Any]] = []
    if delivery_service is not None and delivery_viewer:
        queued = await _read(
            problems,
            "visual.history.deliveries_read_failed",
            "平台投递状态读取失败。",
            lambda: delivery_service.list_status(
                session_id,
                viewer=delivery_viewer,
                limit=200,
            ),
            [],
        )
    deliveries = project_deliveries(
        turn_runs=turn_runs,
        queued=queued,
        privileged=role in {"dm", "admin"},
        keys=keys,
        cursor=delivery_cursor,
        page_size=10,
    )
    readonly = _readonly(session)
    count = len(timeline.get("items") or ()) + len(deliveries.get("items") or ())
    return visual_envelope(
        kind="history",
        data={"timeline": timeline, "deliveries": deliveries},
        revision=session.get("revision"),
        updated_at=latest_timestamp(
            session.get("updated_at"),
            *(item.get("created_at") for item in timeline.get("items") or ()),
            *(item.get("updated_at") for item in deliveries.get("items") or ()),
        ),
        summary={"label": "副本回放与投递", "count": count},
        permissions=visual_permissions(role, readonly=readonly, is_admin=is_admin),
        problems=problems,
        empty=count == 0,
        readonly=readonly,
        stale=bool(session.get("stale")),
    )


async def build_session_generation(
    database: Any,
    session: Mapping[str, Any],
    *,
    role: str,
    is_admin: bool,
    keys: OpaqueKeyFactory,
    cursor: str,
    page_size: int,
) -> VisualEnvelope:
    problems: list[dict[str, Any]] = []
    session_id = text(session.get("id"), limit=180)
    operations = await _read(
        problems,
        "visual.generation.read_failed",
        "故事生成阶段读取失败。",
        lambda: database.list_session_operations(session_id, 200),
        [],
    )
    active_reader = getattr(database, "active_operations", None)
    active = (
        mapping(
            await _read(
                problems,
                "visual.generation.active_read_failed",
                "当前生成取消状态读取失败。",
                lambda: active_reader([session_id]),
                {},
            )
        )
        if role in {"dm", "admin"} and callable(active_reader)
        else {}
    )
    projected = project_generation(
        operations,
        privileged=role in {"dm", "admin"},
        diagnostics=bool(is_admin),
        keys=keys,
        cursor=cursor,
        page_size=page_size,
        active_operation=mapping(active.get(session_id)),
    )
    problems.extend(projected.pop("problems", []))
    readonly = _readonly(session)
    return visual_envelope(
        kind="generation",
        data=projected,
        revision=session.get("revision"),
        updated_at=latest_timestamp(
            session.get("updated_at"),
            *(item.get("updated_at") for item in projected.get("items") or ()),
        ),
        summary={"label": "故事生成阶段", "count": projected["total_items"]},
        permissions=visual_permissions(role, readonly=readonly, is_admin=is_admin),
        problems=problems,
        empty=projected["total_items"] == 0,
        readonly=readonly,
        stale=bool(session.get("stale")),
    )


__all__ = ["build_session_generation", "build_session_history"]
