"""Principal-scoped RC10 server-sent event projection.

The legacy ``/events`` endpoint is intentionally left untouched.  This module
backs the independent ``dashboard/events`` route: callers provide only the
principal-scoped opaque session handle received from a console surface.  The broker
is used solely as a wake-up signal; every user-visible event is re-read from
the durable ``session_events`` log and projected to a six-field safe envelope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json
from typing import Any

from ..errors import bad_request
from . import require_login, text, to_int
from .sessions import require_member
from ..surfaces.registry import resolve_surface_key


SAFE_EVENT_FIELD_ORDER = (
    "kind",
    "revision",
    "sequence",
    "object_key",
    "stale",
    "refresh",
)
SAFE_EVENT_FIELDS = frozenset(SAFE_EVENT_FIELD_ORDER)
CATCHUP_PAGE_SIZE = 50
MAX_CATCHUP_PAGES = 4
MAX_CATCHUP_EVENTS = CATCHUP_PAGE_SIZE * MAX_CATCHUP_PAGES

_EVENT_KIND_BY_TYPE = {
    "event:story_progress": "story.committed",
    "event:story.progress": "story.committed",
    "event:story.committed": "story.committed",
    "event:choice.changed": "choice.changed",
    "event:vote.changed": "vote.changed",
    "event:turn.changed": "turn.changed",
    "event:quest.changed": "quest.changed",
    "event:clock.changed": "clock.changed",
    "event:relation.changed": "relation.changed",
    "event:delivery.updated": "delivery.changed",
    "event:generation.changed": "generation.changed",
    "event:memory.changed": "memory.changed",
    "event:health.changed": "health.changed",
    "event:actor.state_changed": "party.changed",
    "event:item.inventory_changed": "party.changed",
    "event:narrative_mode.changed": "narrative-mode.changed",
    "event:challenge_engine.changed": "challenge.changed",
    "event:tactical_conflict.changed": "tactical.changed",
    "event:dm.whisper": "delivery.changed",
}

_FULL_REFRESH_TYPES = frozenset(
    {
        "event:session.archived",
        "event:session.restored",
        "event:session.created",
        "event:snapshot.restored",
        "event:world.rebuilt",
        "event:terminal.pending",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any, default: int = 0) -> int:
    result = to_int(value, default)
    return max(0, int(result if result is not None else default))


def _resume_sequence(values: Mapping[str, Any]) -> int:
    """Parse explicit and HTTP SSE recovery cursors without silent coercion.

    Native ``EventSource`` reconnects keep the original query string and send
    the most recently received SSE ``id`` as ``Last-Event-ID``.  Taking the
    maximum of both valid cursors prevents replay/regression for both the native
    transport and AstrBot's explicit ``after_seq`` bridge.
    """

    parsed: list[int] = []
    for field in ("after_seq", "last_event_id"):
        raw = values.get(field)
        if raw in (None, ""):
            continue
        value = to_int(raw)
        if value is None or int(value) < 0:
            raise bad_request(
                "事件续接位置无效。",
                recovery="请刷新跑团现场后重新连接。",
            )
        parsed.append(int(value))
    return max(parsed, default=0)


def _kind_for_event(type_: object) -> str:
    value = text(type_).lower()
    if value in _EVENT_KIND_BY_TYPE:
        return _EVENT_KIND_BY_TYPE[value]
    if value.startswith("event:choice."):
        return "choice.changed"
    if value.startswith("event:vote."):
        return "vote.changed"
    if value.startswith("event:quest."):
        return "quest.changed"
    if value.startswith("event:clock.") or value.startswith("event:timer."):
        return "clock.changed"
    if value.startswith("event:relation."):
        return "relation.changed"
    if value.startswith("event:challenge_engine."):
        return "challenge.changed"
    if value.startswith("event:tactical_conflict."):
        return "tactical.changed"
    if value.startswith("event:delivery."):
        return "delivery.changed"
    if value.startswith("event:generation."):
        return "generation.changed"
    if value.startswith("event:memory."):
        return "memory.changed"
    if value.startswith("event:actor.") or value.startswith("event:item."):
        return "party.changed"
    if value.startswith("event:story."):
        return "story.committed"
    return "session.summary"


def _safe_envelope(
    *,
    kind: str,
    sequence: int,
    object_key: str,
    stale: bool = False,
    refresh: str = "none",
) -> dict[str, Any]:
    """Create the complete public SSE contract without copying raw fields."""

    seq = max(0, int(sequence))
    return {
        "kind": str(kind),
        "revision": seq,
        "sequence": seq,
        "object_key": str(object_key),
        "stale": bool(stale),
        "refresh": str(refresh),
    }


def format_sse_event(item: Mapping[str, Any]) -> str:
    """Render one safe envelope with a reconnectable monotonic SSE id."""

    payload = {key: item[key] for key in SAFE_EVENT_FIELD_ORDER if key in item}
    if set(payload) != SAFE_EVENT_FIELDS:
        raise ValueError("SSE envelope is incomplete")
    sequence = _sequence(payload.get("sequence"))
    return (
        f"id: {sequence}\n"
        + "data: "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    )


def project_session_event(
    row: Mapping[str, Any],
    *,
    object_key: str,
) -> dict[str, Any]:
    """Project one durable row; only ``seq`` and internal type guide output."""

    event_type = text(row.get("type")).lower()
    return _safe_envelope(
        kind=_kind_for_event(event_type),
        sequence=_sequence(row.get("seq")),
        object_key=object_key,
        stale=False,
        refresh="full" if event_type in _FULL_REFRESH_TYPES else "invalidate",
    )


async def _checkpoint_sequence(database: Any, session_id: str) -> int | None:
    getter = getattr(database, "get_projection_checkpoint", None)
    if not callable(getter):
        return None
    try:
        checkpoint = await getter(session_id, "webui_live")
    except Exception:
        return None
    if not isinstance(checkpoint, Mapping) or "last_seq" not in checkpoint:
        return None
    return _sequence(checkpoint.get("last_seq"))


async def _read_rows(
    database: Any,
    session_id: str,
    *,
    after_seq: int,
    viewer_role: str,
    limit: int,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(CATCHUP_PAGE_SIZE + 1, int(limit)))
    visibility: str | tuple[str, ...]
    if viewer_role == "admin":
        visibility = ""
    elif viewer_role == "dm":
        visibility = ("public", "private", "dm", "character")
    else:
        visibility = "public"
    rows = await database.list_session_events(
        session_id,
        after_seq=max(0, int(after_seq)),
        limit=bounded_limit,
        visibility=visibility,
    )
    deduplicated: dict[int, dict[str, Any]] = {}
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        seq = _sequence(raw.get("seq"))
        if seq <= after_seq:
            continue
        # The durable key is (session_id, seq).  Keep one row per seq even if
        # a faulty adapter duplicates it, then restore authoritative ordering.
        deduplicated.setdefault(seq, dict(raw))
    return [deduplicated[key] for key in sorted(deduplicated)][:bounded_limit]


async def _read_recovery_pages(
    database: Any,
    session_id: str,
    *,
    after_seq: int,
    viewer_role: str,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Read visible durable events in bounded pages.

    At most ``MAX_CATCHUP_EVENTS`` rows are retained and every database request
    is bounded to ``CATCHUP_PAGE_SIZE + 1``.  The extra row proves whether a
    further page exists without treating audience-filtered sequence gaps as
    missing public events.
    """

    cursor = max(0, int(after_seq))
    collected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for _ in range(MAX_CATCHUP_PAGES):
        rows = await _read_rows(
            database,
            session_id,
            after_seq=cursor,
            viewer_role=viewer_role,
            limit=CATCHUP_PAGE_SIZE + 1,
        )
        has_more = len(rows) > CATCHUP_PAGE_SIZE
        page = rows[:CATCHUP_PAGE_SIZE]
        for row in page:
            sequence = _sequence(row.get("seq"))
            if sequence <= cursor or sequence in seen:
                continue
            seen.add(sequence)
            collected.append(row)
        if page:
            cursor = max(cursor, max(_sequence(row.get("seq")) for row in page))
        if not has_more:
            return collected, False, cursor
    return collected, True, cursor


async def open_event_stream(
    principal: Mapping[str, Any],
    database: Any,
    broker: Any,
    *,
    query: Mapping[str, Any] | None = None,
    keepalive_seconds: float = 25.0,
) -> AsyncIterator[dict[str, Any]]:
    """Authorise and return a console-safe event iterator.

    Authentication, opaque-key resolution and membership checks happen before
    a host creates the streaming HTTP response.  Re-authorisation also occurs
    for each real-time wake-up, so revoking membership closes the stream rather
    than continuing to expose invalidation activity.
    """

    principal_map = _mapping(principal)
    require_login(principal_map)
    values = _mapping(query)
    object_key = text(values.get("session_key"))
    if not object_key:
        raise bad_request(
            "缺少要订阅的副本。",
            recovery="请返回跑团现场重新选择副本。",
        )
    session_id = resolve_surface_key(principal_map, "dashboard", object_key)
    if not session_id:
        raise bad_request(
            "所选副本已经失效。",
            recovery="请返回跑团现场重新选择副本。",
        )
    viewer_role = await require_member(database, session_id, principal_map)

    after_seq = _resume_sequence(values)
    is_admin = bool(principal_map.get("is_admin"))
    initial_audience_role = (
        "admin" if is_admin else "dm" if viewer_role == "dm" else "player"
    )

    def same_session(event: Mapping[str, Any]) -> bool:
        # Only a top-level, exact internal identifier is accepted.  Nested raw
        # payloads are never searched and never reach this stream's queue.
        return text(event.get("session_id")) == session_id

    async def stream() -> AsyncIterator[dict[str, Any]]:
        subscription = broker.subscribe(
            same_session,
            notify_gaps=True,
            timeout_seconds=keepalive_seconds,
        )
        cursor = after_seq
        visible_sequence = after_seq
        audience_role = initial_audience_role
        try:
            # Register the filtered queue before taking the durable snapshot;
            # notifications committed during catch-up remain queued and cause
            # a second reconciliation instead of falling through a race gap.
            await anext(subscription)
            latest = _sequence(
                await database.latest_session_event_seq(session_id)
            )
            checkpoint = await _checkpoint_sequence(database, session_id)

            invalid_cursor = after_seq > latest
            expired_cursor = (
                after_seq > 0
                and checkpoint is not None
                and after_seq < checkpoint
            )
            if invalid_cursor or expired_cursor:
                cursor = latest
                visible_sequence = latest
                yield _safe_envelope(
                    kind="stream.gap",
                    sequence=latest,
                    object_key=object_key,
                    stale=True,
                    refresh="full",
                )
            elif after_seq <= 0:
                # A first connection already loads its active surfaces through
                # VisualEnvelope operations.  Anchor at current latest instead
                # of replaying the entire historical event log.
                cursor = latest
                visible_sequence = latest
                yield _safe_envelope(
                    kind="stream.ready",
                    sequence=latest,
                    object_key=object_key,
                    refresh="full",
                )
            else:
                yield _safe_envelope(
                    kind="stream.ready",
                    sequence=after_seq,
                    object_key=object_key,
                    refresh="none",
                )
                rows, overflow, page_cursor = await _read_recovery_pages(
                    database,
                    session_id,
                    after_seq=cursor,
                    viewer_role=audience_role,
                )
                if overflow:
                    cursor = latest
                    visible_sequence = latest
                    yield _safe_envelope(
                        kind="stream.gap",
                        sequence=latest,
                        object_key=object_key,
                        stale=True,
                        refresh="full",
                    )
                else:
                    for row in rows:
                        projected = project_session_event(
                            row, object_key=object_key
                        )
                        sequence = int(projected["sequence"])
                        if sequence <= visible_sequence:
                            continue
                        cursor = sequence
                        visible_sequence = sequence
                        yield projected
                    # Skip non-visible trailing rows internally so a private
                    # event cannot cause repeated scans on each keepalive.
                    cursor = max(cursor, page_cursor, latest)

            async for notice in subscription:
                notice_type = text(notice.get("type")).lower()
                if notice_type == "ready":
                    continue
                try:
                    current_role = await require_member(
                        database, session_id, principal_map
                    )
                    audience_role = (
                        "admin" if is_admin else "dm" if current_role == "dm" else "player"
                    )
                except Exception:
                    return
                if notice_type == "keepalive":
                    # A repository may commit a durable event without having
                    # access to the in-process broker (for example a resumed
                    # worker after restart).  Reconcile the log before sending
                    # a heartbeat so such updates are delayed at most one
                    # keepalive interval instead of being lost indefinitely.
                    latest = _sequence(
                        await database.latest_session_event_seq(session_id)
                    )
                    rows, overflow, page_cursor = await _read_recovery_pages(
                        database,
                        session_id,
                        after_seq=cursor,
                        viewer_role=audience_role,
                    )
                    if overflow:
                        cursor = latest
                        visible_sequence = max(visible_sequence, latest)
                        yield _safe_envelope(
                            kind="stream.gap",
                            sequence=visible_sequence,
                            object_key=object_key,
                            stale=True,
                            refresh="full",
                        )
                        continue
                    emitted = False
                    for row in rows:
                        projected = project_session_event(
                            row, object_key=object_key
                        )
                        sequence = int(projected["sequence"])
                        if sequence <= visible_sequence:
                            continue
                        cursor = sequence
                        visible_sequence = sequence
                        emitted = True
                        yield projected
                    cursor = max(cursor, page_cursor, latest)
                    if emitted:
                        continue
                    yield _safe_envelope(
                        kind="stream.keepalive",
                        sequence=visible_sequence,
                        object_key=object_key,
                        refresh="none",
                    )
                    continue

                latest = _sequence(
                    await database.latest_session_event_seq(session_id)
                )
                rows, overflow, page_cursor = await _read_recovery_pages(
                    database,
                    session_id,
                    after_seq=cursor,
                    viewer_role=audience_role,
                )
                broker_gap = bool(notice.get("_broker_gap"))
                if overflow:
                    broker_gap = True
                if broker_gap and not rows:
                    cursor = latest
                    visible_sequence = max(visible_sequence, latest)
                    yield _safe_envelope(
                        kind="stream.gap",
                        sequence=visible_sequence,
                        object_key=object_key,
                        stale=True,
                        refresh="full",
                    )
                    continue
                if overflow:
                    cursor = latest
                    visible_sequence = max(visible_sequence, latest)
                    yield _safe_envelope(
                        kind="stream.gap",
                        sequence=visible_sequence,
                        object_key=object_key,
                        stale=True,
                        refresh="full",
                    )
                    continue
                emitted = False
                for row in rows:
                    projected = project_session_event(
                        row, object_key=object_key
                    )
                    sequence = int(projected["sequence"])
                    if sequence <= visible_sequence:
                        continue
                    cursor = sequence
                    visible_sequence = sequence
                    emitted = True
                    yield projected
                cursor = max(cursor, page_cursor, latest)
                if broker_gap and not emitted:
                    yield _safe_envelope(
                        kind="stream.gap",
                        sequence=visible_sequence,
                        object_key=object_key,
                        stale=True,
                        refresh="full",
                    )
        finally:
            await subscription.aclose()

    return stream()


__all__ = [
    "CATCHUP_PAGE_SIZE",
    "MAX_CATCHUP_EVENTS",
    "MAX_CATCHUP_PAGES",
    "SAFE_EVENT_FIELD_ORDER",
    "SAFE_EVENT_FIELDS",
    "format_sse_event",
    "open_event_stream",
    "project_session_event",
]
