"""Host-independent RC8 projections for the fourteen non-live workspaces.

Repository data is cropped into safe VisualEnvelopes at the server boundary.
Object keys are short-lived principal-scoped handles; writes must resolve,
re-authorise, and revision-check the underlying operation.
"""

from __future__ import annotations

import inspect
import hashlib
import json
import re
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable

from ...constants import PLUGIN_VERSION
from ...projections.session_dashboard import enrich_session_display_labels
from ...visualization import OpaqueKeyFactory, VisualProblem, visual_envelope
from ...visualization.common import latest_timestamp, number_or_none
from ...visualization.envelopes import problem_from_exception
from ..errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    UnauthorizedError,
)
from ..query import QueryAdapter, parse_int


RouteResult = dict[str, Any]
SurfaceLoader = Callable[["SurfaceContext"], Awaitable["SurfaceProjection"]]

_QUERY_FIELDS = (
    "session_key",
    "world_key",
    "object_key",
    "q",
    "status",
    "scope",
    "cursor",
    "page_size",
    "mode",
    "group",
    "type",
    "layer",
    "world",
    "importance",
    "tag",
    "governance",
    "author",
    "capability",
    "time",
    "object",
    "actor",
    "action",
    "consumer",
    "expected_revision",
)

_OBJECT_KINDS_BY_WORKSPACE = {
    "dashboard": frozenset({"session", "world", "job", "todo"}),
    "tendencies": frozenset({"session", "evidence"}),
    "sessions": frozenset(
        {
            "session",
            "world",
            "pacing",
            "timer",
            "snapshot",
            "archive",
            "fate-preview",
        }
    ),
    "characters": frozenset({"session", "character"}),
    "memories": frozenset({"session", "memory"}),
    "worlds": frozenset(
        {"world", "world-module", "github-source", "github-preview"}
    ),
    "designer": frozenset(
        {"world", "designer-field", "designer-preset", "world-character"}
    ),
    "author_jobs": frozenset({"world", "job"}),
    "todo": frozenset({"session", "todo"}),
    "audit": frozenset({"session", "audit", "delivery"}),
    "health": frozenset({"health"}),
    "settings": frozenset({"setting", "recovery"}),
    "modules": frozenset({"module"}),
    "about": frozenset(),
}

_MAX_OBJECT_KEYS = 4096
_OBJECT_KEYS: "OrderedDict[tuple[str, str], tuple[str, str]]" = OrderedDict()

_INTERNAL_REF = re.compile(r"^[a-z][a-z0-9_.-]*:[a-z0-9_.:/-]+$", re.I)
_UNSAFE_TEXT = re.compile(
    r"(?:provider[_-]?id|trace[_-]?id|correlation[_-]?id|"
    r"system[_-]?prompt|operation[_-]?id|participant[_-]?id)",
    re.I,
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_mapping", None)
    if callable(converter):
        converted = converter()
        return dict(converted) if isinstance(converted, Mapping) else {}
    return {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _text(value: Any, *, limit: int = 200, default: str = "") -> str:
    if value is None:
        return default
    result = " ".join(str(value).replace("\x00", "").split())
    if len(result) > limit:
        result = result[: max(0, limit - 1)].rstrip() + "…"
    return result


def _public_text(value: Any, *, limit: int = 200, default: str = "") -> str:
    result = _text(value, limit=limit, default=default)
    if not result:
        return default
    if _INTERNAL_REF.fullmatch(result) or _UNSAFE_TEXT.search(result):
        return default
    return result


_WAITING_SUMMARIES = {
    "vote": "集体表决仍在等待完成。",
    "choice": "当前行动仍在等待选择。",
    "preparation": "开演准备尚未完成。",
    "admin": "副本已暂停，等待主持处理。",
}


def waiting_summary(value: Any, *, unknown: str = "") -> str:
    waiting_for = _text(value, limit=50).strip().casefold()
    return _WAITING_SUMMARIES.get(waiting_for, unknown) if waiting_for else ""


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


_TIME_FILTER_OPTIONS = (
    ("last_24_hours", "最近 24 小时"),
    ("last_7_days", "最近 7 天"),
    ("last_30_days", "最近 30 天"),
    ("older_than_7_days", "早于 7 天"),
)


def _timestamp(value: Any) -> datetime | None:
    raw = _text(value, limit=100)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _matches_time_filter(value: Any, selected: str, *, now: datetime | None = None) -> bool:
    selected = _text(selected, limit=40).lower()
    if not selected:
        return True
    allowed = {value for value, _label in _TIME_FILTER_OPTIONS}
    if selected not in allowed:
        raise BadRequestError(
            "时间范围无效。",
            code="tavern.surface.time_filter_invalid",
            recovery="请重新选择最近一天、七天、三十天或更早记录。",
        )
    stamp = _timestamp(value)
    if stamp is None:
        return False
    reference = now or datetime.now(timezone.utc)
    age = reference - stamp
    if selected == "last_24_hours":
        return timedelta(0) <= age <= timedelta(hours=24)
    if selected == "last_7_days":
        return timedelta(0) <= age <= timedelta(days=7)
    if selected == "last_30_days":
        return timedelta(0) <= age <= timedelta(days=30)
    return age > timedelta(days=7)


def _opaque_filter_value(
    context: "SurfaceContext",
    kind: str,
    internal: Any,
) -> str:
    return context.key(kind, _text(internal, limit=500))


def _resolve_filter_value(
    context: "SurfaceContext",
    kind: str,
    value: Any,
    *,
    label: str,
) -> str:
    key = _text(value, limit=300)
    if not key:
        return ""
    resolved = context.resolve(kind, key)
    if resolved is None:
        raise BadRequestError(
            f"{label}筛选已经失效。",
            code="tavern.surface.filter_key_invalid",
            recovery="请清除筛选并从当前页面重新选择。",
        )
    return resolved


def _principal_roles(principal: Mapping[str, Any]) -> frozenset[str]:
    principal = _mapping(principal)
    roles = {
        _text(value, limit=30).lower()
        for value in _sequence(principal.get("roles"))
        if _text(value, limit=30)
    }
    capabilities = _mapping(principal.get("capabilities"))
    if bool(principal.get("is_admin")) or bool(capabilities.get("admin")):
        roles.add("admin")
    if bool(principal.get("is_author")) or bool(capabilities.get("author")):
        roles.add("author")
    if (
        bool(principal.get("is_host"))
        or bool(principal.get("is_dm"))
        or bool(capabilities.get("dm"))
        or bool(capabilities.get("host"))
    ):
        roles.add("host")
    if (
        bool(principal.get("is_player"))
        or bool(capabilities.get("member"))
        or _text(principal.get("member_role"), limit=30).lower()
        in {"player", "member"}
    ):
        roles.add("player")
    if not roles and _text(principal.get("username"), limit=200):
        roles.add("readonly")
    return frozenset(roles)


def _principal_scope(principal: Mapping[str, Any]) -> str:
    principal = _mapping(principal)
    username = _text(
        principal.get("username")
        or principal.get("binding_ref")
        or principal.get("platform_user_id"),
        limit=300,
    )
    roles = ",".join(sorted(_principal_roles(principal))) or "public"
    source = _text(principal.get("auth_source"), limit=80, default="unknown")
    return f"{source}|{username}|{roles}"


def _remember_object_key(
    principal: Mapping[str, Any],
    kind: str,
    key: str,
    internal_value: Any,
) -> None:
    if not key or internal_value in (None, ""):
        return
    registry_key = (_principal_scope(principal), key)
    _OBJECT_KEYS[registry_key] = (str(kind), str(internal_value))
    _OBJECT_KEYS.move_to_end(registry_key)
    while len(_OBJECT_KEYS) > _MAX_OBJECT_KEYS:
        _OBJECT_KEYS.popitem(last=False)


def resolve_surface_key(
    principal: Mapping[str, Any],
    workspace: str,
    key: object,
    *,
    kind: str | None = None,
) -> str | None:
    """Resolve a response handle inside the same process and principal scope.

    This is not an authorisation decision.  A write route must still load the
    real object, enforce its current permissions, and check expected revision.
    Handles rotate on process restart and fail closed across principals.
    """

    allowed = _OBJECT_KINDS_BY_WORKSPACE.get(str(workspace), frozenset())
    value = _OBJECT_KEYS.get((_principal_scope(principal), _text(key, limit=200)))
    expected_kind = _text(kind, limit=80) if kind is not None else ""
    if (
        value is None
        or value[0] not in allowed
        or (expected_kind and value[0] != expected_kind)
    ):
        return None
    return value[1]


def issue_surface_key(
    principal: Mapping[str, Any],
    workspace: str,
    kind: str,
    internal_value: Any,
) -> str:
    """Issue one short-lived key for a server-created console continuation.

    Upload previews and other multi-step actions are not loaded from a normal
    list surface, but their continuation still needs the same principal scope
    and object-kind checks.  Only kinds explicitly owned by the workspace may
    be issued here.
    """

    workspace_name = _text(workspace, limit=80)
    kind_name = _text(kind, limit=80)
    if kind_name not in _OBJECT_KINDS_BY_WORKSPACE.get(
        workspace_name,
        frozenset(),
    ):
        raise ValueError("console continuation kind is not registered")
    scope = _principal_scope(_mapping(principal))
    key = OpaqueKeyFactory(scope=f"console-objects:{scope}").key(
        kind_name,
        internal_value,
    )
    _remember_object_key(principal, kind_name, key, internal_value)
    return key


@dataclass(slots=True)
class SurfaceProjection:
    data: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    revision: int | str | None = None
    updated_at: str = ""
    permissions: dict[str, bool] = field(default_factory=dict)
    problems: list[VisualProblem | Mapping[str, Any]] = field(default_factory=list)
    state: str | None = None
    empty: bool = False
    stale: bool = False
    readonly: bool = False


@dataclass(slots=True)
class SurfaceContext:
    workspace: str
    principal: dict[str, Any]
    database: Any
    services: Any
    query: dict[str, Any]
    roles: frozenset[str]
    object_keys: OpaqueKeyFactory
    cursor_keys: OpaqueKeyFactory

    def key(self, kind: str, internal_value: Any) -> str:
        key = self.object_keys.key(kind, internal_value)
        _remember_object_key(self.principal, kind, key, internal_value)
        return key

    def resolve(self, kind: str, value: Any) -> str | None:
        key = _text(value, limit=200)
        if not key:
            return None
        stored = _OBJECT_KEYS.get((_principal_scope(self.principal), key))
        if stored is None or stored[0] != kind:
            return None
        return stored[1]

    def page(self, *, default: int, maximum: int = 100) -> tuple[int, int]:
        normalized = QueryAdapter(
            self.query,
            allowed_fields=("cursor", "page_size"),
        ).normalize()
        page_size = parse_int(
            normalized,
            "page_size",
            default=default,
            minimum=1,
            maximum=maximum,
        )
        cursor = _text(normalized.get("cursor"), limit=500)
        try:
            offset = self.cursor_keys.read_cursor(self.workspace, cursor)
        except ValueError as exc:
            raise BadRequestError(
                "分页位置已经失效。",
                code="tavern.surface.cursor_invalid",
                recovery="请从当前列表第一页重新开始。",
            ) from exc
        return offset, page_size


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    kind: str
    roles: frozenset[str]
    loader: SurfaceLoader
    public: bool = False
    manage_roles: frozenset[str] = frozenset()


def _service(services: Any, name: str) -> Any:
    if isinstance(services, Mapping):
        return services.get(name)
    return getattr(services, name, None) if services is not None else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _service_value(services: Any, name: str, *args: Any) -> Any:
    value = _service(services, name)
    if callable(value):
        value = value(*args)
    return await _maybe_await(value)


def _route_body(result: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    result = _mapping(result)
    status = _integer(result.get("status"), 500)
    if status >= 400:
        raw = _mapping(result.get("error"))
        if not raw:
            raw = _mapping(result.get("body")).get("error", {})
            raw = _mapping(raw)
        raise WebRouteAdapterError(
            status,
            _text(raw.get("code"), limit=100, default="surface.source_failed"),
            _public_text(
                raw.get("message") or raw.get("reason"),
                limit=240,
                default=f"{operation}未能完成。",
            ),
            _public_text(
                raw.get("recovery") or raw.get("next_command"),
                limit=240,
                default="请刷新当前板块后重试。",
            ),
        )
    return _mapping(result.get("body"))


class WebRouteAdapterError(Exception):
    """Local route error with the fields understood by the Web boundary."""

    def __init__(self, status: int, code: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.status_code = int(status)
        self.code = str(code)
        self.message = str(message)
        self.recovery = str(recovery)


def _problem_from_adapter(
    exc: WebRouteAdapterError,
) -> tuple[int, VisualProblem]:
    return exc.status_code, VisualProblem(
        code=exc.code,
        message=exc.message,
        recovery=exc.recovery,
        retryable=exc.status_code in {409, 429, 500, 502, 503, 504},
        retry_after_seconds=getattr(exc, "retry_after_seconds", None),
    )


def _pagination(
    context: SurfaceContext,
    *,
    offset: int,
    page_size: int,
    returned: int,
    total: int | None,
    has_more: bool,
) -> dict[str, Any]:
    next_cursor = (
        context.cursor_keys.cursor(context.workspace, offset + returned)
        if has_more and returned
        else ""
    )
    return {
        "next_cursor": next_cursor,
        "has_more": bool(has_more),
        "page_size": int(page_size),
        "visible_from": offset + 1 if returned else 0,
        "visible_to": offset + returned,
        "total": total,
    }


def _session_state(value: Any) -> str:
    return {
        "preparing": "准备中",
        "running": "运行中",
        "paused": "已暂停",
        "maintenance": "维护中",
        "finished": "已归档",
        "closed": "已结束",
    }.get(_text(value, limit=40).lower(), "状态待确认")


def _job_state(value: Any) -> str:
    return {
        "queued": "等待中",
        "leased": "运行中",
        "running": "运行中",
        "retry_wait": "等待重试",
        "failed": "可重试失败",
        "permanently_failed": "已停止重试",
        "succeeded": "已完成",
        "completed": "已完成",
        "cancelled": "已取消",
    }.get(_text(value, limit=50).lower(), "状态待确认")


def _delivery_state(value: Any) -> str:
    return {
        "pending": "计划发送",
        "leased": "发送中",
        "sending": "发送中",
        "partially_sent": "部分送达",
        "confirmed": "已确认",
        "delivered": "已送达",
        "failed": "发送失败",
        "retry_wait": "等待重试",
        "permanently_failed": "无法自动续发",
    }.get(_text(value, limit=50).lower(), "状态待确认")


def _safe_label(value: Any, fallback: str) -> str:
    label = _public_text(value, limit=100)
    if label:
        return label
    return fallback


def _available_action(
    action_id: str,
    intent: str,
    label: str,
    *,
    target_kind: str,
    expected_revision: int | str,
    description: str,
    fields: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return the only action shape the console client is allowed to execute.

    The descriptor contains no transport path or internal object reference;
    the surrounding row's principal-scoped ``key`` is the target.
    """

    result = {
        "action_id": action_id,
        "intent": intent,
        "label": label,
        "target_kind": target_kind,
        "description": description,
        "expected_revision": expected_revision,
        "transportReady": True,
        "focus_return": "opener",
    }
    if fields:
        result["fields"] = [dict(item) for item in fields]
    return result


def _session_lifecycle_fields(intent: str) -> tuple[dict[str, Any], ...]:
    if intent not in {"session.lifecycle.finish", "session.lifecycle.abort"}:
        return ()
    return (
        {"name": "reason", "type": "textarea", "labelKey": "action.field.reason", "required": True},
        {"name": "confirmation_name", "type": "text", "labelKey": "action.field.confirmation_name", "required": True},
        {"name": "acknowledge_archive", "type": "checkbox", "labelKey": "action.field.acknowledge_archive", "required": True},
    )


def health_component_revision(value: Mapping[str, Any]) -> int:
    """Stable JS-safe revision for one health component projection."""

    item = _mapping(value)
    canonical = json.dumps(
        {
            "code": _text(item.get("code"), limit=100),
            "state": _text(item.get("state"), limit=50),
            "metrics": _mapping(item.get("metrics")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:13], 16)


def _session_lifecycle_actions(
    raw: Mapping[str, Any],
    *,
    roles: Collection[str],
) -> list[dict[str, Any]]:
    raw = _mapping(raw)
    readonly = bool(raw.get("readonly")) or _text(raw.get("state")) == "finished"
    revision = _integer(raw.get("revision"), 0)
    if not set(roles).intersection({"admin", "host"}) or readonly or revision <= 0:
        return []
    from ...session_lifecycle import lifecycle_capabilities

    capabilities = _mapping(
        lifecycle_capabilities(raw, {}, authorized=True).get("capabilities")
    )
    definitions = (
        (
            "can_close",
            "C03",
            "session.lifecycle.close",
            "关闭副本",
            "暂停新的业务写入；未完成草稿会保留，重新开放后可继续。",
            _session_lifecycle_fields("session.lifecycle.close"),
        ),
        (
            "can_reopen",
            "C04",
            "session.lifecycle.reopen",
            "重新开放副本",
            "恢复此前关闭的副本，已有草稿与状态继续沿用。",
            _session_lifecycle_fields("session.lifecycle.reopen"),
        ),
        (
            "can_finish",
            "C01",
            "session.lifecycle.finish",
            "完结故事",
            "永久归档已经开演的故事，并停止待处理选择与计时。",
            _session_lifecycle_fields("session.lifecycle.finish"),
        ),
        (
            "can_abort",
            "C02",
            "session.lifecycle.abort",
            "放弃本轮",
            "永久归档当前副本；需要填写原因并再次确认。",
            _session_lifecycle_fields("session.lifecycle.abort"),
        ),
    )
    return [
        _available_action(
            action_id,
            intent,
            label,
            target_kind="session",
            expected_revision=revision,
            description=description,
            fields=fields,
        )
        for capability, action_id, intent, label, description, fields in definitions
        if bool(capabilities.get(capability))
    ]


def _project_session(context: SurfaceContext, raw: Mapping[str, Any]) -> dict[str, Any]:
    raw = _mapping(raw)
    internal = _text(raw.get("id"), limit=300)
    name = _safe_label(raw.get("name") or raw.get("instance_name"), "副本名称缺失")
    world = _safe_label(raw.get("world") or raw.get("world_name"), "世界资料缺失")
    turn = _mapping(raw.get("turn_state"))
    round_no = number_or_none(
        raw.get("round_no")
        if raw.get("round_no") is not None
        else turn.get("round_no")
    )
    active_timers = _integer(
        raw.get("active_timers"),
        _integer(raw.get("active_timer_count"), 0),
    )
    current = _public_text(raw.get("current_name"), limit=100)
    world_state = _mapping(raw.get("world_state"))
    scene_label = _public_text(
        raw.get("scene_label") or world_state.get("location_label"),
        limit=120,
    ) or None
    player_count = number_or_none(raw.get("player_count"))
    ready_count = number_or_none(raw.get("ready_count"))
    player_summary = ""
    if player_count is not None:
        player_summary = (
            f"{int(ready_count)} / {int(player_count)}"
            if ready_count is not None
            else str(int(player_count))
        )
    progress = _mapping(raw.get("progress"))
    progress_total = _integer(progress.get("total_milestones"), 0)
    progress_current = _integer(progress.get("completed_milestones"), 0)
    waiting_for = _text(raw.get("waiting_for"), limit=40)
    summary_parts = [world]
    if round_no is not None:
        summary_parts.append(f"第 {int(round_no)} 轮")
    if current:
        summary_parts.append(f"当前行动：{current}")
    key = context.key("session", internal or f"{name}|{world}")
    readonly = bool(raw.get("readonly")) or _text(raw.get("state")) == "finished"
    revision = _integer(raw.get("revision"), 0)
    actions = _session_lifecycle_actions(raw, roles=context.roles)
    if "admin" in context.roles and revision > 0:
        actions.append(
            _available_action(
                "E10",
                "session.clone",
                "克隆为新分支",
                target_kind="session",
                expected_revision=revision,
                description="从当前状态或最终保护点建立独立关闭分支；原副本、历史与存档保持不变。",
                fields=[
                    {
                        "name": "instance_name",
                        "type": "text",
                        "labelKey": "action.field.name",
                        "required": True,
                    },
                    {
                        "name": "instance_slug",
                        "type": "text",
                        "labelKey": "action.field.slug",
                        "required": True,
                    },
                ],
            )
        )
    return {
        "key": key,
        "object_kind": "session",
        "label": name,
        "summary": " · ".join(summary_parts),
        "state": _session_state(raw.get("state")),
        "world_label": world,
        "scene_label": scene_label,
        "round": int(round_no) if round_no is not None else None,
        "actor_label": current,
        "player_summary": player_summary,
        "player_current": int(ready_count) if ready_count is not None else None,
        "player_total": int(player_count) if player_count is not None else None,
        "todo_count": 1 if waiting_for else 0,
        "pending_count": 1 if waiting_for else 0,
        "risk_summary": waiting_summary(waiting_for),
        "progress_label": _safe_label(
            progress.get("current_objective") or progress.get("chapter"),
            "公开进度",
        ) if progress_total > 0 else "",
        "progress_current": progress_current if progress_total > 0 else None,
        "progress_total": progress_total if progress_total > 0 else None,
        "active_timers": active_timers,
        "revision": revision,
        "updated_at": _text(raw.get("updated_at"), limit=80),
        "readonly": readonly,
        "readonly_reason": (
            "副本已经永久归档，只能查看或从存档克隆。" if readonly else ""
        ),
        "available_actions": actions,
    }


async def _visible_session_page(
    context: SurfaceContext,
    *,
    offset: int,
    page_size: int,
    query: str = "",
    state: str = "",
) -> tuple[list[dict[str, Any]], int, bool, dict[str, int]]:
    """Read the required repository-owned visibility, filtering and paging contract."""

    loader = getattr(context.database, "list_visible_sessions_page", None)
    if not callable(loader):
        raise RuntimeError("session visibility repository contract is unavailable")
    result = await loader(
        viewer_id=_text(context.principal.get("username"), limit=300),
        viewer_participant_ref=_text(
            context.principal.get("participant_ref"),
            limit=300,
        ),
        is_admin="admin" in context.roles,
        query=query,
        state=state,
        offset=offset,
        page_size=page_size,
    )
    result = _mapping(result)
    items = [
        dict(item)
        for item in _sequence(result.get("items"))
        if isinstance(item, Mapping)
    ]
    return (
        items,
        _integer(result.get("total"), len(items)),
        bool(result.get("has_more")),
        {
            _text(key, limit=50): _integer(value, 0)
            for key, value in _mapping(result.get("state_counts")).items()
            if _text(key, limit=50)
        },
    )


def _session_world_ref(raw: Mapping[str, Any]) -> str:
    item = _mapping(raw)
    return _text(
        item.get("world_id") or item.get("world_slug") or item.get("world_name"),
        limit=300,
    )


def _session_world_label(raw: Mapping[str, Any]) -> str:
    item = _mapping(raw)
    return _safe_label(item.get("world_name") or item.get("world"), "世界资料缺失")


def _session_group_ref(raw: Mapping[str, Any]) -> str:
    item = _mapping(raw)
    platform = _text(item.get("platform_id"), limit=200)
    group = _text(item.get("group_id"), limit=300)
    return f"{platform}|{group}" if group else ""


def _session_group_label(raw: Mapping[str, Any], fallback_number: int) -> str:
    item = _mapping(raw)
    return _public_text(
        item.get("group_remark") or item.get("group_name") or item.get("group_label"),
        limit=100,
        default=f"群组 {fallback_number}",
    )


async def _collect_visible_session_rows(
    context: SurfaceContext,
    *,
    query: str,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], bool, dict[str, int]]:
    """Collect a bounded, already principal-cropped set for safe facets.

    The repository remains the permission boundary.  Exact world/group facets
    are projected here because the current repository API has no facet
    parameters; an honest partial problem is returned when the visible set is
    larger than this bounded scan.
    """

    rows: list[dict[str, Any]] = []
    offset = 0
    state_counts: dict[str, int] = {}
    truncated = False
    while len(rows) < limit:
        page_size = min(100, limit - len(rows))
        page, _total, has_more, page_counts = await _visible_session_page(
            context,
            offset=offset,
            page_size=page_size,
            query=query,
            state="",
        )
        if not state_counts:
            state_counts = page_counts
        rows.extend(page)
        offset += len(page)
        if not has_more or not page:
            break
        if len(rows) >= limit:
            truncated = True
            break
    list_sessions = getattr(context.database, "list_sessions", None)
    if callable(list_sessions) and rows:
        raw_rows = await list_sessions() or ()
        raw_by_id = {
            _text(item.get("id"), limit=300): _mapping(item)
            for item in raw_rows
            if isinstance(item, Mapping) and _text(item.get("id"), limit=300)
        }
        for item in rows:
            raw = raw_by_id.get(_text(item.get("id"), limit=300), {})
            for field_name in (
                "world_id",
                "world_slug",
                "world_name",
                "platform_id",
                "group_id",
                "group_remark",
                "group_name",
                "group_label",
            ):
                if item.get(field_name) in (None, "") and raw.get(field_name) not in (
                    None,
                    "",
                ):
                    item[field_name] = raw[field_name]
    return rows, truncated, state_counts


def _resolve_session_context(context: SurfaceContext, *, required: bool = True) -> str:
    for name in ("session_key", "object_key"):
        key = _text(context.query.get(name), limit=200)
        if key:
            resolved = context.resolve("session", key)
            if not resolved:
                raise BadRequestError(
                    "所选副本已经失效。",
                    code="tavern.surface.session_key_invalid",
                    recovery="请返回副本列表后重新选择。",
                )
            return resolved
    if required:
        raise BadRequestError(
            "缺少要查看的副本。",
            code="tavern.surface.session_required",
            recovery="请从副本列表选择一个副本。",
        )
    return ""


def _resolve_world_context(context: SurfaceContext, *, required: bool = True) -> str:
    for name in ("world_key", "object_key"):
        key = _text(context.query.get(name), limit=200)
        if key:
            resolved = context.resolve("world", key)
            if not resolved:
                raise BadRequestError(
                    "所选世界已经失效。",
                    code="tavern.surface.world_key_invalid",
                    recovery="请返回世界库后重新选择。",
                )
            return resolved
    if required:
        raise BadRequestError(
            "缺少要查看的世界。",
            code="tavern.surface.world_required",
            recovery="请从世界库选择一个世界。",
        )
    return ""



__all__ = [name for name in globals() if not name.startswith('__')]
