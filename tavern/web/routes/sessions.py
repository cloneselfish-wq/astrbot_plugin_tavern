"""D1-ARC-003：会话路由（纯服务）。

只消费 ``TavernDatabase`` 与既有投影函数，不接触 AstrBot 请求/响应对象。
输出为可直接 JSON 序列化的语义 DTO：

- ``session_list_view``：副本概览列表（普通玩家仅见自己所属副本）；
- ``session_detail_view``：单个副本详情（普通玩家剥离平台连接字段与私密
  数据，归档副本标记 ``readonly``）；
- ``session_changes_view``：``session_events`` 增量 DTO（WP-11；非管理员
  只见 public 事件，payload 只含中文安全语义，不返回原始行）。

错误统一抛出 ``tavern.web.errors.WebApiError``，由宿主 Web 层转成标准信封。
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from typing import Any

from ...projections.dashboard import dashboard_sessions as build_dashboard_sessions
from ...projections.session_timeline import session_dashboard as build_session_dashboard
from ...projections.character import project_actor_view
from ..errors import bad_request, forbidden, not_found

#: 普通玩家会话 DTO 中必须剥离的平台/内部连接字段。
_PRIVATE_SESSION_FIELDS = frozenset(
    {
        "unified_origin",
        "unified_msg_origin",
        "origin",
        "platform_id",
        "group_id",
        "world_id",
    }
)

#: session_events 原始行中仅管理员可见的技术列。
_TECHNICAL_EVENT_FIELDS = (
    "actor_ref",
    "command_id",
    "causation_id",
    "correlation_id",
)

#: 事件 payload 中允许进入视图的安全语义字段。
_SAFE_PAYLOAD_FIELDS = ("title", "summary", "affected_modules", "turn_no")


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bool(value: Any) -> bool:
    return bool(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


async def resolve_viewer_participant(
    database: Any,
    session_id: str,
    username: str = "",
    participant_ref: str = "",
) -> dict[str, Any] | None:
    """Resolve a verified participant without treating a binding as a user id.

    Platform principals use ``username``.  Miniprogram principals instead
    carry an opaque binding reference plus a repository participant reference;
    only the latter may be matched to a roster row.  Retired rows never grant
    current membership.
    """

    username = _text(username)
    participant_ref = _text(participant_ref)
    if not username and not participant_ref:
        return None
    try:
        roster = await database.list_roster(session_id)
    except Exception:
        return None
    for item in roster or ():
        if not isinstance(item, Mapping):
            continue
        if _text(item.get("participation_status")) in {"retired", "archived"}:
            continue
        if participant_ref and _text(item.get("id")) == participant_ref:
            return dict(item)
        if not username:
            continue
        for key in ("group_user_id", "user_id", "private_user_id"):
            if _text(item.get(key)) == username:
                return dict(item)
    return None


async def resolve_viewer_role(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> str:
    """解析查看者角色：admin / dm / player / ""（非成员）。"""
    if bool(principal.get("is_admin")):
        return "admin"
    username = _text(principal.get("username"))
    participant_ref = _text(principal.get("participant_ref"))
    if not username and not participant_ref:
        return ""
    try:
        control = await database.get_control_state(session_id)
    except Exception:
        control = {}
    if (
        _text(control.get("mode")) == "dm"
        and _text(control.get("active_dm_user_id")) == username
    ):
        return "dm"
    try:
        grants = await database.list_permission_grants(session_id)
    except Exception:
        grants = []
    for item in grants or ():
        if not isinstance(item, Mapping):
            continue
        if (
            _text(item.get("user_id")) == username
            and _text(item.get("role")) == "host"
        ):
            return "dm"
    participant = await resolve_viewer_participant(
        database,
        session_id,
        username,
        participant_ref,
    )
    if participant is None:
        return ""
    declared_role = _text(principal.get("member_role")).lower()
    if participant_ref and declared_role in {"dm", "host", "moderator"}:
        return "dm"
    return "player"


async def require_member(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> str:
    """返回查看者角色；非成员直接抛 403，不泄漏副本存在性之外的信息。"""
    role = await resolve_viewer_role(database, session_id, principal)
    if not role:
        raise forbidden(
            "你不是该副本的成员，无法查看",
            recovery="请确认当前账号已加入该副本，或由主持人为你绑定角色卡。",
        )
    return role


def _actor_projection_row(
    world: Mapping[str, Any],
    raw: Mapping[str, Any],
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """附加普通展示契约并移除原始卡 profile payload。"""
    item = dict(raw)
    source = profile
    if source is None:
        candidate = item.get("card_profile") or item.get("draft_profile") or {}
        source = candidate if isinstance(candidate, Mapping) else {}
    view = project_actor_view(world, source, viewer_role="admin")
    if not _text(view.get("title")):
        fallback = _text(item.get("character_name"))
        if fallback:
            view["title"] = fallback
        else:
            view.setdefault("problems", []).append(
                {
                    "code": "projection.actor_title_missing",
                    "path": "actor.identity.name",
                    "message": "角色名称数据缺失",
                }
            )
    if not _text(view.get("subtitle")):
        fallback = _text(item.get("character_code"))
        if fallback:
            view["subtitle"] = fallback
    for key in ("card_profile", "draft_profile", "profile", "profile_json"):
        item.pop(key, None)
    item["actor_view"] = view
    return item


def _revision_projection_rows(
    world: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    roster: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """角色卡修订列表投影：移除原始 profile，附加候选/基准 actor_view。"""
    roster_views = {
        _text(item.get("id")): _actor_projection_row(world, item)["actor_view"]
        for item in roster or ()
        if isinstance(item, Mapping) and _text(item.get("id"))
    }
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        profile = raw.get("profile")
        profile = profile if isinstance(profile, Mapping) else {}
        item = dict(raw)
        for key in ("profile", "profile_json", "card_profile", "draft_profile"):
            item.pop(key, None)
        candidate = _actor_projection_row(
            world, item, profile=profile
        )["actor_view"]
        item["actor_view"] = candidate
        item["candidate_actor_view"] = candidate
        item["base_actor_view"] = roster_views.get(
            _text(item.get("participant_id")),
            {
                "schema": "tavern-actor-view/1.0.0-rc10",
                "title": "",
                "subtitle": "",
                "sections": [],
                "problems": [
                    {
                        "code": "projection.base_actor_missing",
                        "path": "card_revision",
                        "message": "当前角色卡投影不可用",
                    }
                ],
            },
        )
        result.append(item)
    return result


def _with_token_context(
    usage: Mapping[str, Any],
    instance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """在 Token 用量摘要上附加世界包上下文预算（只读展示）。"""
    result = dict(usage) if isinstance(usage, Mapping) else {}
    result["context_budget"] = {}
    if instance and isinstance(instance.get("world_snapshot"), Mapping):
        rules = instance["world_snapshot"].get("rules") or {}
        if isinstance(rules, Mapping):
            result["context_budget"] = dict(rules.get("context_budget") or {})
    result["last_trim_at"] = ""
    return result


async def session_list_view(
    database: Any,
    principal: Mapping[str, Any],
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """副本概览列表；普通玩家只见自己所属（或主持）的副本。"""
    sessions = await build_dashboard_sessions(database)
    items: list[dict[str, Any]] = []
    for item in sessions:
        if not isinstance(item, Mapping):
            continue
        session_id = _text(item.get("id"))
        if not session_id:
            continue
        if bool(principal.get("is_admin")):
            role = "admin"
        else:
            role = await resolve_viewer_role(
                database, session_id, principal
            )
            if not role:
                continue
        row = dict(item)
        row["viewer_role"] = role
        items.append(row)
    raw_query = dict(query) if isinstance(query, Mapping) else {}
    try:
        page = max(1, int(raw_query.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(
            1,
            min(100, int(raw_query.get("page_size") or 20)),
        )
    except (TypeError, ValueError):
        page_size = 20
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    return {
        "sessions": page_items,
        "count": len(page_items),
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


async def session_shell_view(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> dict[str, Any]:
    """Lightweight header, permission and tab contract for first paint."""

    session_id = _text(session_id)
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请选择一个要查看的副本。",
        )
    session = await database.get_session(session_id)
    if session is None:
        raise not_found(
            "副本不存在或已删除",
            recovery="请刷新副本列表后重新选择。",
        )
    session = _mapping(session)
    role = await require_member(database, session_id, principal)
    readonly = _text(session.get("state")) == "finished"
    public_session = {
        "instance_name": _text(session.get("instance_name")),
        "world_name": _text(session.get("world_name")),
        "state": _text(session.get("state")),
        "turn_no": int(session.get("turn_no") or 0),
    }
    can_manage = role in {"dm", "admin"} and not readonly
    return {
        "schema": "tavern-session-shell/1.0.0-rc10",
        "session": public_session,
        "viewer_role": role,
        "readonly": readonly,
        "permissions": {
            "can_admin": bool(principal.get("is_admin")),
            "can_manage_narrative": can_manage,
            "can_view_private": role in {"dm", "admin"},
            "role_source": _text(
                principal.get("role_source"),
                "unmapped",
            ),
        },
        "tabs": [
            {"id": "state", "label": "总览与规则", "load": "immediate"},
            {"id": "roster", "label": "准备与角色", "load": "on_demand"},
            {"id": "npcs", "label": "NPC", "load": "on_demand"},
            {"id": "memory", "label": "长期记忆", "load": "on_demand"},
            {"id": "timing", "label": "时间与流程", "load": "on_demand"},
            {"id": "rescue", "label": "急救与诊断", "load": "on_demand"},
            {"id": "growth", "label": "成长", "load": "on_demand"},
            {"id": "access", "label": "权限与封禁", "load": "on_demand"},
            {"id": "saves", "label": "存档", "load": "on_demand"},
            {"id": "events", "label": "时间线", "load": "on_demand"},
        ],
    }


async def session_detail_view(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
) -> dict[str, Any]:
    """单个副本详情 DTO（语义投影，不返回原始数据库行给普通玩家）。"""
    session_id = _text(session_id)
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请选择一个要查看的副本。",
        )
    session = await database.get_session(session_id)
    if session is None:
        raise not_found(
            "副本不存在或已删除",
            recovery="请刷新副本列表后重新选择。",
        )
    session = _mapping(session)
    role = await require_member(database, session_id, principal)
    is_privileged = role in {"dm", "admin"}
    if not is_privileged:
        session = {
            key: value
            for key, value in session.items()
            if key not in _PRIVATE_SESSION_FIELDS
        }
    dashboard_view = await build_session_dashboard(
        database,
        session_id,
        viewer_role=role,
        include_technical_refs=bool(principal.get("is_admin")),
    )
    instance_config = await database.get_instance_config(session_id)
    world = (
        instance_config.get("world_snapshot")
        if isinstance(instance_config, Mapping)
        and isinstance(instance_config.get("world_snapshot"), Mapping)
        else {}
    )
    roster_rows = await database.list_roster(session_id)
    roster = [
        _actor_projection_row(world, item)
        for item in roster_rows
        if isinstance(item, Mapping)
    ]
    revision_rows = await database.list_card_revisions(session_id)
    card_revisions = _revision_projection_rows(
        world, revision_rows, roster_rows
    )
    session_characters = await database.list_session_characters(session_id)
    story_ledger = await database.list_story_ledger(session_id)
    world_state_view = dashboard_view.get("world_state_view", {})
    permission_grants = (
        await database.list_permission_grants(session_id)
        if is_privileged
        else []
    )
    archive_view = dashboard_view.get("archive")
    readonly = bool(
        isinstance(archive_view, Mapping) and archive_view.get("readonly")
    ) or _text(session.get("state")) == "finished"
    memories = await database.list_memories(
        session_id,
        "",
        500,
        include_invalidated=is_privileged,
    )
    if not is_privileged:
        memories = [
            item
            for item in memories
            if isinstance(item, Mapping)
            and _text(item.get("visibility"), "public") == "public"
        ]
    can_manage_narrative = is_privileged
    return {
        "session": session,
        "players": await database.list_players(session_id),
        "roster": roster,
        "turn": await database.get_turn_status(session_id),
        "narrative_control": dashboard_view.get("narrative_control", {}),
        "events": await database.recent_events(session_id, 80),
        "snapshots": await database.list_snapshots(session_id),
        "instance_config": instance_config,
        "timers": await database.list_timers(session_id),
        "timer_policy": await database.get_timer_policy(session_id),
        "token_usage": _with_token_context(
            await database.token_usage_summary(session_id),
            instance_config,
        ),
        "choice": await database.active_choice_set(session_id),
        "vote": await database.active_vote(session_id),
        "bans": (
            await database.list_bans(session_id) if is_privileged else []
        ),
        "permissions": {
            "can_admin": bool(principal.get("is_admin")),
            "can_dm": can_manage_narrative,
            "can_manage_narrative": (
                can_manage_narrative and not readonly
            ),
            "can_review_cards": can_manage_narrative and not readonly,
            "can_force_ready": (
                can_manage_narrative
                and not readonly
                and _text(session.get("state")) == "preparing"
            ),
            "can_view_private": can_manage_narrative,
            "role_source": _text(principal.get("role_source"), "unmapped"),
        },
        "permission_grants": permission_grants,
        "economy": await database.economy_summary(session_id),
        "delegations": await database.list_delegations(session_id),
        "pending_operations": await database.pending_operations(session_id),
        "return_requests": await database.list_return_requests(session_id),
        "preflight": await database.opening_preflight(session_id),
        "opening_decision": (
            await database.opening_decision(session_id)
            or (
                await database.prepare_opening_decision(session_id)
                if _text(session.get("state")) == "preparing"
                else None
            )
        ),
        "ai_companions": await database.list_ai_companions(session_id),
        "rule_state": await database.get_session_rule_state(session_id),
        "session_characters": session_characters,
        "story_ledger": story_ledger,
        "world_state_view": world_state_view,
        "scene_clocks": await database.list_scene_clocks(session_id),
        "memories": memories,
        "archive": archive_view,
        "readonly": readonly,
        "module_panels": dashboard_view.get("module_panels", {}),
        "module_summary": dashboard_view.get("module_summary", {}),
        "actor_fate_view": dashboard_view.get("actor_fate_view", {}),
        "terminal_report": dashboard_view.get("terminal_report"),
        "storage": await database.get_storage_info(session_id),
        "operations": await database.list_session_operations(session_id, 50),
        "card_revisions": card_revisions,
        "latest_seq": await database.latest_session_event_seq(session_id),
    }


def _project_change_item(
    row: Mapping[str, Any],
    *,
    include_technical: bool,
) -> dict[str, Any]:
    """把 session_events 原始行投影为安全增量 DTO。"""
    payload: dict[str, Any] = {}
    raw_payload = row.get("payload_json")
    if isinstance(raw_payload, str):
        try:
            parsed = _json.loads(raw_payload)
        except _json.JSONDecodeError:
            parsed = {}
    else:
        parsed = _mapping(raw_payload)
    for key in _SAFE_PAYLOAD_FIELDS:
        if key not in parsed:
            continue
        value = parsed[key]
        if key == "affected_modules":
            if isinstance(value, (list, tuple)):
                value = [
                    str(item).strip()[:60]
                    for item in value
                    if str(item).strip()
                ]
        payload[key] = value
    item: dict[str, Any] = {
        "seq": _int(row.get("seq")),
        "type": _text(row.get("type")),
        "payload": payload,
        "visibility": _text(row.get("visibility"), "public"),
        "created_at": _text(row.get("created_at")),
    }
    if include_technical:
        technical: dict[str, str] = {}
        for key in _TECHNICAL_EVENT_FIELDS:
            value = _text(row.get(key))
            if value:
                technical[key] = value
        if technical:
            item["technical"] = technical
    return item


async def _projection_checkpoint_seq(
    database: Any,
    session_id: str,
) -> int | None:
    """读取 ``webui_live`` 投影检查点序号；无检查点或读取失败返回 None。"""
    try:
        checkpoint = await database.get_projection_checkpoint(
            session_id, "webui_live"
        )
    except Exception:
        return None
    if not isinstance(checkpoint, Mapping) or "last_seq" not in checkpoint:
        return None
    return _int(checkpoint.get("last_seq"), -1)


async def session_changes_view(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """``after_seq`` 之后的副本增量事件 DTO（WP-11）。"""
    session_id = _text(session_id)
    if not session_id:
        raise bad_request(
            "缺少 session_id",
            recovery="请选择一个要查看的副本。",
        )
    role = await require_member(database, session_id, principal)
    is_admin = bool(principal.get("is_admin"))
    after_seq = max(0, _int(after_seq))
    limit = max(1, min(500, _int(limit, 200)))
    rows = await database.list_session_events(
        session_id,
        after_seq=after_seq,
        limit=limit,
        visibility="" if is_admin else "public",
    )
    items = [
        _project_change_item(row, include_technical=is_admin)
        for row in rows
        if isinstance(row, Mapping)
    ]
    latest_seq = await database.latest_session_event_seq(session_id)
    checkpoint_seq = await _projection_checkpoint_seq(database, session_id)
    full_refresh = after_seq <= 0 or (
        checkpoint_seq is not None and after_seq < checkpoint_seq
    )
    return {
        "session_id": session_id,
        "after_seq": after_seq,
        "latest_seq": latest_seq,
        "items": items,
        "has_more": bool(items and items[-1]["seq"] < latest_seq),
        "full_refresh": full_refresh,
        "viewer_role": role,
    }


__all__ = [
    "require_member",
    "resolve_viewer_participant",
    "resolve_viewer_role",
    "session_changes_view",
    "session_detail_view",
    "session_list_view",
    "session_shell_view",
]

__all__ = [name for name in globals() if not name.startswith('__')]

