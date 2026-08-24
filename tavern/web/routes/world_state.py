"""D1-ARC-003：受控世界状态路由（纯服务，不依赖 Web 框架与 AstrBot）。

只消费 ``TavernDatabase`` 公开方法与既有投影函数。输出为可直接 JSON
序列化的语义 DTO：

- ``world_state_view``：任务、阵营、NPC、时钟、场景、知识与结局面板
  （``ModulePanelView`` 五状态），外加跨模块概览与队伍状态；
- 普通玩家视角不返回裸 world_state JSON、副本/世界 revision、稳定引用
  ID 与内部模块键；
- 管理员视角额外提供折叠技术详情（D1-UX-010）；
- 读取失败进入模块面板 ``error`` 状态并给出明确说明，不以空数组掩盖
  （D1-WEB-002 / D1-WEB-009）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ...projections.delivery import project_actor_fate_summary
from ...projections.session import (
    project_module_panels,
    project_world_summary_view,
    world_module_declared,
    world_module_summary,
)
from ...projections.world import project_world_state_view
from ..errors import bad_request, not_found
from .sessions import require_member, resolve_viewer_participant

from . import (
    ok,
    require_login,
    route_errors,
    text,
)

__all__ = [
    "world_state_view",
]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


async def _world_snapshot(database: Any, session_id: str) -> dict[str, Any]:
    try:
        instance = _mapping(await database.get_instance_config(session_id))
    except Exception:
        return {}
    return _mapping(instance.get("world_snapshot"))


def _fate_rows(roster: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """从参与者运行态提取角色命运行（与 dashboard 聚合语义一致）。"""

    rows: list[dict[str, Any]] = []
    for item in roster:
        if not isinstance(item, Mapping):
            continue
        runtime = _mapping(item.get("runtime_state"))
        raw_fate = (
            runtime.get("fate")
            or runtime.get("lifecycle_state")
            or None
        )
        if raw_fate is None:
            state_block = runtime.get("state")
            if isinstance(state_block, Mapping):
                raw_fate = state_block.get("fate")
        if isinstance(raw_fate, Mapping):
            fate_id = text(
                raw_fate.get("state")
                or raw_fate.get("state_id")
                or raw_fate.get("id")
            )
            window = raw_fate.get("rescue_window")
            rescue_open = bool(
                isinstance(window, Mapping) and window.get("open")
            )
        elif raw_fate not in (None, ""):
            fate_id = text(raw_fate)
            rescue_open = False
        else:
            continue
        rows.append(
            {
                "actor_name": text(
                    item.get("character_name") or item.get("display_name")
                ),
                "state_id": fate_id,
                "rescue_open": rescue_open,
                "rescue_message": (
                    text(raw_fate.get("rescue_message"))
                    if isinstance(raw_fate, Mapping)
                    else ""
                ),
                "updated_at": text(item.get("updated_at")),
            }
        )
    return rows


def _clock_items(clocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """场景时钟安全条目：标题、进度与中文状态。"""

    status_labels = {
        "active": "进行中",
        "paused": "已暂停",
        "completed": "已触发",
        "archived": "已归档",
    }
    items: list[dict[str, Any]] = []
    for item in clocks:
        if not isinstance(item, Mapping):
            continue
        title = text(item.get("title"))
        if not title:
            continue
        status = text(item.get("status"), "active")
        items.append(
            {
                "title": title,
                "segments": _int(item.get("segments")),
                "current_value": _int(item.get("current_value")),
                "progress": (
                    f"{_int(item.get('current_value'))}/{_int(item.get('segments'))}"
                    if _int(item.get("segments")) > 0
                    else ""
                ),
                "status": status,
                "status_label": status_labels.get(status, "状态解析失败"),
            }
        )
    return items


def _public_ledger_items(
    ledger: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """公开剧情账本条目（事实/线索）语义视图。"""

    items: list[dict[str, Any]] = []
    for item in ledger:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("visibility") or "public").lower() != "public":
            continue
        title = text(item.get("title"))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "summary": text(item.get("description")),
                "kind": text(item.get("kind")),
                "status": text(item.get("status")),
                "updated_at": text(item.get("updated_at")),
            }
        )
    return items


def _recent_change(ledger: Sequence[Mapping[str, Any]]) -> str:
    """最近一条公开变化标题；缺失时为空串。"""

    public = _public_ledger_items(ledger)
    if not public:
        return ""
    ordered = sorted(
        public,
        key=lambda item: str(item.get("updated_at") or ""),
        reverse=True,
    )
    return text(ordered[0].get("title"))


def _scene_overview(
    panels: Mapping[str, Any],
    module_summary: Mapping[str, Any],
    clocks: Sequence[Mapping[str, Any]],
    fate_view: Mapping[str, Any],
    recent_change: str,
) -> dict[str, Any]:
    """D1-WEB-003 跨模块概览：场景、模块状态、时钟、队伍与最近变化。"""

    scene_panel = _mapping(panels.get("scene_graph"))
    scene_items = [
        _mapping(item)
        for item in scene_panel.get("items") or ()
        if isinstance(item, Mapping)
    ]
    current_scene = next(
        (
            text(item.get("label"))
            for item in scene_items
            if bool(item.get("current"))
        ),
        text(scene_items[0].get("label")) if scene_items else "",
    )
    clock_items = [
        _mapping(item)
        for item in _mapping(panels.get("time_clock")).get("items") or ()
        if isinstance(item, Mapping)
    ]
    active_clocks = sum(
        1 for item in clock_items if text(item.get("status")) == "active"
    )
    panel_states = {
        str(key): text(_mapping(value).get("state"), "disabled")
        for key, value in panels.items()
        if isinstance(value, Mapping)
    }
    return {
        "current_scene": current_scene,
        "module_states": panel_states,
        "module_summary": dict(module_summary),
        "clock_count": len(clocks),
        "active_clock_count": active_clocks,
        "party": dict(fate_view),
        "recent_change": recent_change,
    }


@route_errors
async def world_state_view(
    principal: Mapping[str, Any],
    database: Any,
    session_id: str,
    *,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """受控世界状态：模块面板集合 + 跨模块概览（D1-WEB-002/003）。"""

    require_login(principal)
    session_id = text(session_id)
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
    is_admin = bool(principal.get("is_admin"))
    world = await _world_snapshot(database, session_id)
    raw_state = _mapping(session.get("world_state"))
    safe_state = dict(raw_state)
    # 原始运行态绝不直接回退给浏览器；缺失时由投影函数显式降级。
    safe_state.pop("runtime", None)
    try:
        projected_runtime = await database.world_runtime_state(session_id)
        runtime = projected_runtime.get("runtime")
        if isinstance(runtime, Mapping):
            safe_state["runtime"] = dict(runtime)
    except Exception:
        pass

    status_hints: dict[str, str] = {}
    try:
        ledger = await database.list_story_ledger(session_id)
    except Exception:
        ledger = []
        status_hints["knowledge_graph"] = "error"
    try:
        clocks = await database.list_scene_clocks(session_id)
    except Exception:
        clocks = []
        status_hints["time_clock"] = "error"
    try:
        npc_rows = await database.list_session_characters(session_id)
    except Exception:
        npc_rows = []
        status_hints["npc_lifecycle"] = "error"
    try:
        roster = await database.list_roster(session_id)
    except Exception:
        roster = []

    content_views = project_world_state_view(
        world,
        safe_state,
        ledger=ledger,
        session_npcs=npc_rows,
        viewer_role=role,
        include_technical_refs=is_admin,
    )
    session_started = text(session.get("state")) in {
        "running",
        "paused",
        "finished",
    }
    panels = project_module_panels(
        world,
        safe_state,
        quest_view=content_views["quest_view"],
        faction_view=content_views["faction_view"],
        npc_view=content_views["npc_view"],
        clocks=_clock_items(clocks),
        ledger=ledger,
        session_started=session_started,
        state_hints=status_hints,
        viewer_role=role,
        include_technical_refs=is_admin,
    )
    module_summary = world_module_summary(world)
    approved_roster = [
        item
        for item in roster
        if isinstance(item, Mapping)
        and text(item.get("card_status")) == "approved"
        and text(item.get("participation_status"))
        in {"active", "standby", "away"}
    ]
    fate_read_error = False
    try:
        stored_fates = await database.list_actor_fate_states(session_id)
    except Exception:
        stored_fates = []
        fate_read_error = True
    fate_view = project_actor_fate_summary(
        [
            {
                "actor_name": text(
                    item.get("character_name") or item.get("display_name")
                ),
                "state_id": text(item.get("state")),
                "rescue_open": bool(item.get("rescue_open")),
                "updated_at": text(item.get("updated_at")),
            }
            for item in stored_fates
            if isinstance(item, Mapping)
        ],
        world=world,
        permanent_death=world_module_declared(world, "actor_fate"),
        tpk_label=(
            "小队全部死亡时，副本立即失败并永久归档。"
            if world_module_declared(world, "actor_fate")
            else ""
        ),
        roster_count=len(approved_roster),
        session_started=session_started,
        read_error=fate_read_error,
        viewer_role=role,
        include_technical_refs=is_admin,
    )
    overview = _scene_overview(
        panels,
        module_summary,
        clocks,
        fate_view,
        _recent_change(ledger),
    )
    panel_states = {
        text(panel.get("state"))
        for panel in panels.values()
        if isinstance(panel, Mapping)
    }
    problems = [
        {
            "code": text(problem.get("code"), "projection.module.read_failed"),
            "message": text(problem.get("message"), "模块状态读取失败。"),
        }
        for panel in panels.values()
        if isinstance(panel, Mapping)
        for problem in panel.get("problems") or []
        if isinstance(problem, Mapping)
    ]
    state_view = {
        "schema": "tavern-world-state-view/1.0.0-rc10",
        "session_revision": _int(session.get("revision")),
        "status": (
            "error"
            if "error" in panel_states
            else (
                "waiting"
                if "waiting" in panel_states
                and not panel_states.intersection({"ready", "empty"})
                else "ready"
            )
        ),
        "module_panels": panels,
        "problems": problems,
    }
    body: dict[str, Any] = {
        "session_id": session_id,
        "viewer_role": role,
        "world": project_world_summary_view(
            world,
            viewer_role=role,
            include_technical_refs=is_admin,
        ),
        "module_summary": module_summary,
        "world_state_view": state_view,
        "module_panels": panels,
        "clocks": _clock_items(clocks),
        "overview": overview,
        "session_state": text(session.get("state")),
        "readonly": bool(
            isinstance(session.get("archive"), Mapping)
            and session.get("archive", {}).get("readonly")
        )
        or text(session.get("state")) == "finished",
        "permissions": {
            "can_view_all": role in {"dm", "admin"},
            "can_view_private": role in {"dm", "admin"},
            "role_source": text(principal.get("role_source"), "unmapped"),
        },
    }
    if role == "player":
        auth_source = text(principal.get("auth_source"))
        viewer = await resolve_viewer_participant(
            database,
            session_id,
            "" if auth_source == "miniprogram_binding" else text(
                principal.get("username")
            ),
            text(principal.get("participant_ref"))
            if auth_source == "miniprogram_binding"
            else "",
        )
        fate_consent: dict[str, Any] = {
            "state": "unavailable",
            "count": 0,
            "message": "当前账号没有可读取的本人命运预览。",
            "available_actions": [],
        }
        if viewer:
            try:
                previews = await database.list_actor_fate_previews(
                    session_id,
                    text(viewer.get("id")),
                )
            except Exception:
                fate_consent.update(
                    {
                        "state": "error",
                        "message": (
                            "读取本人命运预览失败；系统没有显示占位结果。"
                        ),
                    }
                )
            else:
                count = len(
                    [
                        item
                        for item in previews
                        if isinstance(item, Mapping)
                        and text(item.get("status")) == "pending_consent"
                    ]
                )
                fate_consent.update(
                    {
                        "state": "pending" if count else "empty",
                        "count": count,
                        "message": (
                            "有致命命运预览等待角色本人处理。"
                            if count
                            else "当前没有等待本人处理的致命命运预览。"
                        ),
                        "available_actions": [],
                        "read_endpoint": (
                            "sessions/actor-fate" if count else ""
                        ),
                    }
                )
        body["actor_fate_consent"] = fate_consent
    if is_admin:
        body["technical"] = {
            "world_internal_model_revision": text(
                world.get("internal_world_model_revision")
            ),
            "session_revision": text(session.get("revision")),
            "world_state_revision": text(
                raw_state.get("revision")
                or raw_state.get("state_revision")
            ),
            "module_summary": dict(module_summary),
            "state_hints": dict(status_hints),
        }
    return ok(body)
