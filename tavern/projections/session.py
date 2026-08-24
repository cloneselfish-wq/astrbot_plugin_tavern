from __future__ import annotations

from .character import *
from .world import *



# ── D1 Web 后端投影层 ─────────────────────────────────────────
#
# D1 统一外显 DTO：WorldSummaryView、ModulePanelView、
# NarrativeControlView、DeliveryStatusView、ActorFateView、TerminalView。
# 普通视图不返回稳定 ID / revision / 原始 JSON / UMO；技术字段只出现在
# 显式 ``technical_details`` / ``technical`` 且仅授权角色可见。

TECHNICAL_VIEWERS = frozenset({"admin", "author"})

MODULE_PANEL_LABELS: dict[str, str] = {
    "quest_graph": "任务",
    "faction_state": "阵营",
    "npc_lifecycle": "NPC",
    "time_clock": "时钟",
    "scene_graph": "场景",
    "knowledge_graph": "知识",
    "ending": "结局",
}

NARRATIVE_MODE_LABELS = {
    "auto": "AI 自动主持",
    "dm": "人工 DM",
}

NARRATIVE_PHASE_LABELS = {
    "auto": "AI 推进",
    "awaiting_dm": "等待主持指令",
    "generating": "生成推进中",
    "player_handoff": "交棒给角色",
    "npc_handoff": "交棒给 NPC",
}

DELIVERY_STATE_SPECS: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "pending": (
        "queued",
        "等待投递",
        ("retry", "cancel"),
        "平台暂未确认发送，系统会自动重试。",
    ),
    "leased": ("sending", "发送中", (), "平台正在发送，请稍候。"),
    "partially_sent": (
        "partially_sent",
        "部分送达",
        ("retry", "cancel"),
        "部分分片已送达，剩余内容等待重试。",
    ),
    "retry_wait": (
        "queued",
        "等待重试",
        ("cancel",),
        "发送失败，系统将按计划自动重试。",
    ),
    "sent": ("delivered", "已送达", (), "平台已确认送达。"),
    "delivered_on_reply": (
        "delivered",
        "已送达",
        (),
        "已随玩家消息送达。",
    ),
    "permanently_failed": (
        "failed",
        "发送失败",
        ("retry",),
        "多次重试后仍未送达，可手动重试。",
    ),
    "cancelled": ("cancelled", "已取消", (), "消息已取消，不会继续发送。"),
    "dismissed": ("cancelled", "已取消", (), "消息已由主持人关闭。"),
    "webui_only": (
        "queued",
        "等待投递",
        ("retry", "cancel"),
        "仅在网页端展示，尚未发送到平台。",
    ),
}

FATE_STATE_LABELS: dict[str, tuple[str, bool, bool]] = {
    "healthy": ("正常", True, False),
    "wounded": ("负伤", True, False),
    "critical": ("濒危", False, False),
    "dead": ("死亡", False, True),
}

TERMINATION_LABELS: dict[str, tuple[str, str]] = {
    "completed": ("正常完结", "正常"),
    "failed": ("失败归档", "失败"),
    "aborted": ("强制终止", "终止"),
}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_technical_viewer(role: str) -> bool:
    return str(role or "").strip().lower() in TECHNICAL_VIEWERS


def world_module_manifest(world: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the authoritative module manifest of a compiled world.

    The compiler writes ``twp_modules`` into the artifact; legacy compiled
    worlds may only carry ``rules.world_stats.protocol.modules``.  When
    neither exists the manifest is empty and callers must report a read
    failure instead of inventing counts.
    """

    modules: list[dict[str, Any]] = []
    for raw in _sequence(world.get("twp_modules")):
        if isinstance(raw, Mapping):
            modules.append(dict(raw))
    if modules:
        return modules
    rules = _mapping(world.get("rules"))
    protocol = _mapping(_mapping(rules.get("world_stats")).get("protocol"))
    legacy = protocol.get("modules")
    if isinstance(legacy, Sequence) and not isinstance(legacy, (str, bytes)):
        return [
            {"module_id": str(item), "enabled": True}
            for item in legacy
            if str(item or "").strip()
        ]
    return []


def world_module_summary(world: Mapping[str, Any]) -> dict[str, Any]:
    """Module statistics strictly from the manifest; never fabricated zeros."""

    manifest = world_module_manifest(world)
    if not manifest:
        return {
            "declared": None,
            "enabled": None,
            "state": "error",
            "message": "模块统计读取失败",
        }
    declared = len(manifest)
    enabled = sum(1 for item in manifest if bool(item.get("enabled")))
    return {
        "declared": declared,
        "enabled": enabled,
        "state": "ready",
        "message": "",
    }


def world_module_declared(world: Mapping[str, Any], module_id: str) -> bool:
    """Whether the world package declares a standard module."""

    module_id = str(module_id or "").strip()
    if not module_id:
        return False
    for item in world_module_manifest(world):
        if str(item.get("module_id") or "") == module_id:
            return True
    rules = _mapping(world.get("rules"))
    if isinstance(rules.get(module_id), Mapping):
        return True
    modules = _mapping(rules.get("modules"))
    if isinstance(modules.get(module_id), Mapping):
        return True
    index = _mapping(world.get("capability_index"))
    return any(str(key).startswith(f"{module_id}.") for key in index)


def world_protocol_display(world: Mapping[str, Any]) -> str:
    """Return the current protocol display name."""

    protocol = _mapping(world.get("protocol"))
    version = str(
        protocol.get("version")
        or protocol.get("core")
        or protocol.get("world_protocol")
        or ""
    ).strip()
    if not version:
        rules = _mapping(world.get("rules"))
        version = str(
            _mapping(_mapping(rules.get("world_stats")).get("protocol")).get(
                "world_protocol"
            )
            or _mapping(rules.get("protocol")).get("version")
            or ""
        ).strip()
    return f"TWP {version}" if version else ""


def project_world_summary_view(
    world: Mapping[str, Any],
    *,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
    content_stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """D1-UX-006：世界卡与详情共用的外显摘要。

    普通视图不包含数据库 revision、包 ID、原始规则 JSON 或 artifact 哈希；
    这些只进入显式 ``technical_details`` 且仅授权角色可见。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知世界投影视角：{viewer_role}")
    rules = _mapping(world.get("rules"))
    limits_raw = _mapping(rules.get("player_limits"))
    if not limits_raw:
        limits_raw = _mapping(world.get("recommended_players"))
    limits = dict(limits_raw)
    if "recommended_min" not in limits and "minimum" in limits:
        limits["recommended_min"] = limits["minimum"]
    if "recommended_max" not in limits and "maximum" in limits:
        limits["recommended_max"] = limits["maximum"]
    player_limits = {
        key: limits[key]
        for key in ("recommended_min", "recommended_max", "maximum")
        if key in limits
    }
    world_stats = _mapping(rules.get("world_stats"))
    if content_stats is None:
        content_stats = _mapping(world_stats.get("content"))
    actor = _mapping(rules.get("actor"))
    actor_stats: Mapping[str, Any] = {}
    if actor:
        try:
            actor_stats = field_account(actor)
        except FieldAccountingError as exc:
            actor_stats = {
                "state": "error",
                "message": "建卡统计读取失败",
                "issue_code": exc.code,
            }
    result: dict[str, Any] = {
        "schema": "tavern-world-summary/1.0.0-rc10",
        "name": str(world.get("name") or "").strip(),
        "description": str(
            world.get("description") or world.get("summary") or ""
        ).strip(),
        "content_version": str(
            world.get("content_version")
            or world.get("world_content_version")
            or ""
        ).strip(),
        "protocol_display": world_protocol_display(world),
        "minimum_plugin_version": str(
            world.get("minimum_plugin_version") or ""
        ).strip(),
        "module_summary": world_module_summary(world),
        "player_limits": player_limits,
        "actor_stats": actor_stats,
        "content_stats": dict(content_stats),
        "technical_details": None,
    }
    if include_technical_refs and _is_technical_viewer(role):
        result["technical_details"] = {
            "id": str(world.get("id") or ""),
            "revision": _as_int(world.get("revision")),
            "package_id": str(
                world.get("package_id") or world.get("source_package_id") or ""
            ),
            "source_package_id": str(world.get("source_package_id") or ""),
            "slug": str(world.get("slug") or ""),
            "artifact_hash": str(
                world.get("artifact_hash") or world.get("source_artifact_hash") or ""
            ),
            "internal_world_model_revision": _as_int(
                world.get("internal_world_model_revision")
            ),
            "protocol": dict(_mapping(world.get("protocol"))),
            "source_kind": str(world.get("source_kind") or ""),
            "migration_status": str(world.get("migration_status") or ""),
            "created_at": str(world.get("created_at") or ""),
            "updated_at": str(world.get("updated_at") or ""),
        }
    return result


def project_module_panel_view(
    world: Mapping[str, Any],
    module_id: str,
    *,
    label: str | None = None,
    icon: str = "",
    items: Sequence[Mapping[str, Any]] | None = None,
    problems: Sequence[Mapping[str, Any]] | None = None,
    initialized: bool = False,
    state_hint: str = "",
    message: str = "",
    summary: str = "",
    has_snapshot: bool = False,
    last_success_at: str = "",
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """D1-UX-008：ModulePanelView 五状态视图。

    页面是否存在由 ``capability_declared`` 决定，不由 ``items`` 长度决定。
    读取失败使用 ``error`` 状态并给出明确说明，不以空数组掩盖。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知模块面板视角：{viewer_role}")
    module_id = str(module_id or "").strip()
    label = str(label or MODULE_PANEL_LABELS.get(module_id) or "模块").strip()
    issue_list = [
        dict(item) for item in problems or () if isinstance(item, Mapping)
    ]
    declared = world_module_declared(world, module_id)
    error_hint = str(state_hint or "").strip() == "error"
    if error_hint:
        state = "error"
        state_label = "读取失败"
    elif not declared:
        state = "not_applicable"
        state_label = "不适用"
    elif not initialized:
        state = "waiting"
        state_label = "等待初始化"
    elif not items:
        state = "empty"
        state_label = "暂无内容"
    else:
        state = "ready"
        state_label = "正常"
    default_message = {
        "not_applicable": f"该世界不使用{label}模块。",
        "waiting": f"副本尚未开演；开演后载入初始{label}。",
        "empty": f"「{label}」模块已启用，目前没有可展示的{label}。",
        "ready": "",
        "error": (
            f"「{label}」面板读取失败。系统没有修改副本数据，仍保留上一次"
            "成功快照。请刷新后重试；若仍失败，请联系管理员并说明面板名称"
            "与发生时间。"
        ),
    }[state]
    normalized_items = [
        dict(item) for item in items or () if isinstance(item, Mapping)
    ]
    if state == "not_applicable":
        normalized_items = []
        normalized_problems = []
    normalized_problems = [
        {
            "code": str(item.get("code") or "projection.module.read_failed"),
            "message": str(
                item.get("message")
                or message
                or default_message
                or f"{label}数据读取失败。"
            ),
            **(
                {"retryable": bool(item.get("retryable"))}
                if "retryable" in item
                else {}
            ),
        }
        for item in issue_list
    ]
    if error_hint and not normalized_problems:
        normalized_problems.append(
            {
                "code": f"projection.{module_id}.read_failed",
                "message": str(message or default_message),
                "retryable": True,
            }
        )
    result: dict[str, Any] = {
        "schema": "tavern-module-panel/1.0.0-rc10",
        "module_id": module_id,
        "capability_declared": declared,
        "state": state,
        "state_label": state_label,
        "count": (
            None
            if state in {"not_applicable", "waiting", "error"}
            else len(normalized_items)
        ),
        "summary": str(summary or ""),
        "message": str(message or default_message),
        "items": normalized_items,
        "problems": normalized_problems,
        "available_actions": [],
        "last_success_at": str(last_success_at) if last_success_at else None,
    }
    return result


def project_module_panels(
    world: Mapping[str, Any],
    state: Mapping[str, Any] | None,
    *,
    quest_view: Mapping[str, Any] | None = None,
    faction_view: Mapping[str, Any] | None = None,
    npc_view: Mapping[str, Any] | None = None,
    clocks: Sequence[Mapping[str, Any]] | None = None,
    ledger: Sequence[Mapping[str, Any]] | None = None,
    session_started: bool = False,
    state_hints: Mapping[str, str] | None = None,
    summary_overrides: Mapping[str, str] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, dict[str, Any]]:
    """D1-WEB-002：任务、阵营、NPC、时钟、场景、知识与结局面板集合。"""

    hints = dict(state_hints or {})
    summaries = dict(summary_overrides or {})
    source = _mapping(state)
    runtime = _mapping(source.get("runtime"))
    runtime_modules = _mapping(runtime.get("modules"))
    labels = _entity_label_index(world)

    def items_of(view: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in _sequence(view.get("items") if view else None)
            if isinstance(item, Mapping)
        ]

    def problems_of(view: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in _sequence(view.get("problems") if view else None)
            if isinstance(item, Mapping)
        ]

    def module_initialized(module_id: str) -> bool:
        if isinstance(runtime_modules.get(module_id), Mapping):
            return True
        return session_started

    def panel(
        module_id: str,
        *,
        items: Sequence[Mapping[str, Any]] | None,
        problems: Sequence[Mapping[str, Any]] | None = None,
        hint: str = "",
    ) -> dict[str, Any]:
        return project_module_panel_view(
            world,
            module_id,
            items=items,
            problems=problems,
            initialized=module_initialized(module_id),
            state_hint=hints.get(module_id, hint),
            summary=summaries.get(module_id),
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )

    scene_runtime = _mapping(runtime_modules.get("scene_graph"))
    current_scene = str(scene_runtime.get("current_scene") or "").strip()
    scene_definitions = _definition_index(
        _definition_items(world, "scene_graph", "nodes")
    )
    scene_items: list[dict[str, Any]] = []
    if current_scene:
        scene_label = str(
            labels.get(current_scene)
            or _mapping(scene_definitions.get(current_scene)).get("label")
            or ""
        ).strip()
        if scene_label:
            scene_items.append({"label": scene_label, "current": True})
    for ref in _sequence(scene_runtime.get("scene_history"))[:8]:
        scene_label = str(labels.get(str(ref)) or "").strip()
        if scene_label:
            scene_items.append({"label": scene_label, "current": False})

    ending_runtime = _mapping(runtime_modules.get("ending"))
    active_ending = str(ending_runtime.get("ending") or "").strip()
    ending_items: list[dict[str, Any]] = []
    for ref, definition in _definition_index(
        _definition_items(world, "ending", "endings")
    ).items():
        ending_label = str(
            definition.get("label")
            or definition.get("name")
            or labels.get(ref)
            or ""
        ).strip()
        if not ending_label:
            continue
        ending_items.append(
            {"label": ending_label, "active": ref == active_ending}
        )

    clue_items = [
        dict(item)
        for item in ledger or ()
        if isinstance(item, Mapping)
        and str(item.get("kind") or item.get("entry_type") or "").lower()
        in {"clue", "fact"}
    ]
    knowledge_items = [
        {"label": str(item.get("title") or item.get("label") or "")}
        for item in clue_items
    ]

    return {
        "quest_graph": panel(
            "quest_graph",
            items=items_of(quest_view),
            problems=problems_of(quest_view),
        ),
        "faction_state": panel(
            "faction_state",
            items=items_of(faction_view),
            problems=problems_of(faction_view),
        ),
        "npc_lifecycle": panel(
            "npc_lifecycle",
            items=items_of(npc_view),
            problems=problems_of(npc_view),
        ),
        "time_clock": panel("time_clock", items=clocks or []),
        "scene_graph": panel("scene_graph", items=scene_items),
        "knowledge_graph": panel(
            "knowledge_graph",
            items=knowledge_items,
        ),
        "ending": panel("ending", items=ending_items),
    }


def project_narrative_control_view(
    control: Mapping[str, Any] | None,
    *,
    host_labels: Mapping[str, str] | None = None,
    input_locked: bool = False,
    can_manage: bool | None = None,
    readonly: bool | None = None,
    pending_count: int = 0,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """D1-UX-007：NarrativeControlView 唯一叙事控制视图。

    SESSION INSPECTOR 与跑团现场只能消费该对象；字段为前端组件直接消费的
    扁平契约（mode / dm_display_name / phase_label / beat_no /
    input_locked / pending_count / last_change / can_manage / readonly /
    allowed_actions）。普通视图不暴露 ``active_dm_user_id`` 与控制修订号。

    ``can_manage`` / ``readonly`` 未提供时省略对应键，允许前端回退到
    ``permissions`` / ``archive.readonly``；显式 ``False`` 会抑制回退，
    因此调用方只在确认为真时传入。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知叙事控制视角：{viewer_role}")
    source = _mapping(control)
    problems: list[dict[str, Any]] = []
    mode = str(source.get("mode") or "auto").strip() or "auto"
    phase = str(source.get("phase") or "auto").strip() or "auto"
    mode_label = NARRATIVE_MODE_LABELS.get(mode)
    if not mode_label:
        problems.append(
            {
                "code": "projection.control_mode_unresolved",
                "message": "主持模式名称解析失败",
            }
        )
        mode_label = "主持模式待确认"
    phase_label = NARRATIVE_PHASE_LABELS.get(phase)
    if not phase_label:
        problems.append(
            {
                "code": "projection.control_phase_unresolved",
                "message": "会话阶段名称解析失败",
            }
        )
        phase_label = "阶段待确认"
    labels = {
        str(key): str(value)
        for key, value in _mapping(host_labels).items()
        if str(key).strip() and str(value).strip()
    }
    dm_user_id = str(source.get("active_dm_user_id") or "").strip()
    host_display = labels.get(dm_user_id) or ""
    if mode == "dm" and dm_user_id and not host_display:
        problems.append(
            {
                "code": "projection.host_label_missing",
                "message": "主持人显示名解析失败",
            }
        )
    pending_actions: list[dict[str, Any]] = []
    if str(source.get("directive") or "").strip():
        pending_actions.append(
            {"id": "dm_directive", "label": "待执行主持指引"}
        )
    if phase == "generating":
        pending_actions.append(
            {"id": "generating", "label": "AI 正在推进"}
        )
    if mode == "dm":
        allowed_actions = [
            "enable-dm",
            "directive",
            "direct",
            "disable-dm",
            "whisper",
            "set-next-actor",
            "lock-input",
            "checkpoint",
            "end-vote",
        ]
    else:
        allowed_actions = ["enable-dm"]
    beat_no = _as_int(source.get("beat_no")) or 0
    updated_at = str(source.get("updated_at") or "").strip()
    last_change: dict[str, Any] | None = None
    if mode == "dm":
        last_change = {
            "label": (
                f"主持推进第 {beat_no} 段"
                if beat_no
                else "开启人工主持"
            ),
            "at_label": updated_at[:16] if updated_at else "",
        }
    elif updated_at:
        last_change = {
            "label": "控制状态已同步",
            "at_label": updated_at[:16],
        }
    result: dict[str, Any] = {
        "schema": "tavern-narrative-control/1.0.0-rc10",
        "mode": mode,
        "mode_label": mode_label,
        "dm_display_name": host_display,
        "phase_label": phase_label,
        "beat_no": beat_no,
        "input_locked": bool(input_locked),
        "pending_count": max(0, _as_int(pending_count) or 0),
        "last_change": last_change,
        "allowed_actions": allowed_actions,
        "pending_actions": pending_actions,
        "problems": problems,
        "technical_details": None,
    }
    if can_manage is not None:
        result["can_manage"] = bool(can_manage)
    if readonly is not None:
        result["readonly"] = bool(readonly)
    if include_technical_refs and _is_technical_viewer(role):
        result["technical_details"] = {
            "session_id": str(source.get("session_id") or ""),
            "active_dm_user_id": dm_user_id,
            "directive": str(source.get("directive") or ""),
            "beat_no": _as_int(source.get("beat_no")) or 0,
            "current_actor_type": str(source.get("current_actor_type") or ""),
            "current_actor_ref": str(source.get("current_actor_ref") or ""),
            "revision": _as_int(source.get("revision")) or 0,
            "updated_at": str(source.get("updated_at") or ""),
        }
    return result

__all__ = [name for name in globals() if not name.startswith('__')]
