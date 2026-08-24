from .common import *
from .dashboard import *
from .session_dashboard import *

def _world_package_projection(
    instance_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Player-facing package summary; dynamic data stays in typed views."""
    snapshot = (
        instance_config.get("world_snapshot")
        if isinstance(instance_config, Mapping)
        else None
    )
    if not isinstance(snapshot, Mapping):
        return {}
    rules = snapshot.get("rules")
    rules = rules if isinstance(rules, Mapping) else {}
    contract = world_contract(snapshot)
    economy = contract.get("economy") or {}
    content_version = _text(
        snapshot.get("content_version")
        or snapshot.get("world_content_version")
    )
    return {
        "player_limits": dict(rules.get("player_limits") or {}),
        "content_version": content_version,
        "economy_available": bool(economy.get("available")),
        "shop_count": len(economy.get("shops") or []),
    }


async def session_dashboard(
    database: Any,
    session_id: str,
    *,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """单个副本的实时聚合（状态机 / 行动者 / 计时器 / 选项 / 投票 / 事件）。

    D1：普通视图（默认）剪除 revision、原始 control、原始 world_state JSON
    与平台 ID；技术字段只放入 ``technical_details`` 且由调用方按权限传入
    ``include_technical_refs``。归档副本返回 ``archive`` / ``readonly``，
    失败归档附 ``terminal_report``（结局、死亡顺序、存活与完成度统计）。
    """
    session = await database.get_session(session_id)
    session = dict(session)
    world_state = session.get("world_state") or {}
    turn = await database.get_turn_status(session_id)
    timers = await session_timers(database, session_id, order="desc")
    choice_set = await database.active_choice_set(session_id)
    vote = await database.active_vote(session_id)
    events = await database.recent_events(session_id, 12)
    story_event = await database.latest_public_story_event(session_id)
    providers = await database.list_provider_health()
    # A17：LIVE 仪表盘所需的控制权 / 托管 / 待处理任务 / 阵容轻量投影。
    control = await database.get_control_state(session_id)
    try:
        archive = await database.get_session_archive(session_id)
    except Exception:
        archive = None
    if not isinstance(archive, Mapping):
        archive = None
    readonly = bool(archive and archive.get("readonly")) or _text(
        session.get("state")
    ) == "finished"
    delegations = await database.list_delegations(session_id)
    pending_ops = await database.pending_operations(session_id)
    statuses: dict[str, Any] = {}
    try:
        roster_rows = await database.list_roster(session_id)
    except Exception as exc:
        roster_rows = []
        statuses["roster"] = _status_entry(
            "error",
            "dashboard.roster.read_failed",
            "阵容数据读取失败",
        )
    else:
        statuses["roster"] = _status_entry(
            "empty" if not roster_rows else "ready",
            message="当前没有可显示的阵容成员",
        )
    roster = [
        {
            "id": _text(item.get("id")),
            "character_name": _text(item.get("character_name")),
            "display_name": _text(item.get("display_name")),
            "participation_status": _text(item.get("participation_status")),
        }
        for item in roster_rows
        if isinstance(item, Mapping)
    ]
    host_labels = {
        str(item.get("group_user_id") or item.get("user_id") or ""): _text(
            item.get("display_name") or item.get("character_name")
        )
        for item in roster_rows
        if isinstance(item, Mapping)
        and str(item.get("group_user_id") or item.get("user_id") or "")
    }
    narrative_control = project_narrative_control_view(
        control,
        host_labels=host_labels,
        input_locked=_bool(session.get("input_locked")),
        readonly=True if readonly else None,
        pending_count=len(pending_ops),
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    active_choice_list = []
    if choice_set and isinstance(choice_set, Mapping):
        # A20: active_choice_set 返回的键是 choices（已归一化），
        # 兼容旧数据仍以 choices_json 为键的形态。
        active_choice_list = _options(
            choice_set.get("choices")
            if isinstance(choice_set.get("choices"), (list, Mapping))
            else choice_set.get("choices_json")
        )
    # A18: 小队 / NPC / 剧情账本 / 场景时钟 / 队伍关系。
    try:
        return_requests = await database.list_return_requests(session_id)
    except Exception as exc:
        return_requests = []
        statuses["return_requests"] = _status_entry(
            "error",
            "dashboard.return_requests.read_failed",
            "离场请求读取失败",
        )
    else:
        statuses["return_requests"] = _status_entry(
            "empty" if not return_requests else "ready",
            message="当前没有离场请求",
        )
    try:
        instance_config = await database.get_instance_config(session_id)
    except Exception as exc:
        instance_config = {}
        statuses["instance_config"] = _status_entry(
            "error",
            "dashboard.instance_config.read_failed",
            "副本配置读取失败",
        )
    else:
        statuses["instance_config"] = _status_entry(
            "empty" if not instance_config else "ready",
            message="当前副本没有配置记录",
        )
    world_snapshot = (
        instance_config.get("world_snapshot")
        if isinstance(instance_config, Mapping)
        and isinstance(instance_config.get("world_snapshot"), Mapping)
        else {}
    )
    projection_world = world_snapshot
    world_slug = _text(world_snapshot.get("slug"))
    if world_slug:
        try:
            current_world = await database.get_world(world_slug)
        except Exception:
            current_world = None
        projection_world = _projection_world(
            world_snapshot,
            current_world,
        )
    features = {
        feature: world_has_capability(world_snapshot, feature)
        for feature in ("inventory", "economy", "quests", "story")
    }
    item_instances_by_participant: dict[str, list[dict[str, Any]]] = {}
    all_item_instances: list[dict[str, Any]] = []
    owner_labels: dict[str, str] = {}
    if features["inventory"]:
        for row in roster_rows:
            if not isinstance(row, Mapping):
                continue
            participant_id = _text(row.get("id"))
            if not participant_id:
                continue
            display_name = _text(
                row.get("character_name") or row.get("display_name"),
                "角色名称不可用",
            )
            owner_labels[f"character:{participant_id}"] = display_name
            owner_labels[f"player:{participant_id}"] = display_name
            try:
                raw_items = await database.list_item_instances(
                    session_id, participant_id
                )
            except Exception as exc:
                raw_items = []
                statuses["inventory"] = _status_entry(
                    "error",
                    "dashboard.inventory.read_failed",
                    "物品清单读取失败",
                )
            else:
                statuses.setdefault(
                    "inventory",
                    _status_entry(
                        "empty" if not raw_items else "ready",
                        message="当前没有物品记录",
                    ),
                )
            items = _enrich_item_instances(raw_items or [], world_snapshot)
            item_instances_by_participant[participant_id] = items
            all_item_instances.extend(items)
    try:
        economy = await database.economy_summary(session_id)
    except Exception as exc:
        economy = {"enabled": False, "currencies": [], "wallets": [], "recent": []}
        statuses["economy"] = _status_entry(
            "error",
            "dashboard.economy.read_failed",
            "经济数据读取失败",
        )
    if not isinstance(economy, Mapping):
        economy = {"enabled": False, "currencies": [], "wallets": [], "recent": []}
        statuses["economy"] = _status_entry(
            "error",
            "dashboard.economy.invalid_shape",
            "经济数据格式异常",
        )
    else:
        statuses.setdefault(
            "economy",
            _status_entry(
                "empty" if not economy.get("enabled") else "ready",
                message="当前世界未启用经济模块",
            ),
        )
    market_views: list[dict[str, Any]] = []
    if features["economy"]:
        economy_contract = world_contract(world_snapshot).get("economy") or {}
        for raw_shop in economy_contract.get("shops") or []:
            if not isinstance(raw_shop, Mapping):
                continue
            shop_ref = _text(raw_shop.get("shop_id"))
            if not shop_ref:
                continue
            market_views.append(
                project_market_view(
                    world=world_snapshot,
                    runtime=world_state if isinstance(world_state, Mapping) else {},
                    shop_ref=shop_ref,
                )
            )
    squad = _squad(
        roster_rows,
        turn,
        world_state,
        world_snapshot,
        item_instances_by_participant,
    )
    opening_recommendations = recommend_opening_scenarios(
        world_snapshot,
        roster_rows,
    )
    resource_view = project_resource_view(
        world_snapshot,
        item_instances=all_item_instances,
        economy=economy,
        owner_labels=owner_labels,
        viewer_role="admin",
        include_technical_refs=True,
    )
    safe_world_state = dict(world_state) if isinstance(world_state, Mapping) else {}
    if "runtime" in safe_world_state:
        try:
            runtime_view = await database.world_runtime_state(session_id)
            projected_runtime = dict(runtime_view.get("runtime") or {})
            safe_world_state.pop("runtime", None)
            safe_world_state["runtime"] = projected_runtime
        except Exception:
            # 投影失败时宁可不展示模块运行态，也不把原始运行态回退给浏览器。
            safe_world_state.pop("runtime", None)
            statuses["runtime"] = _status_entry(
                "error",
                "dashboard.runtime.read_failed",
                "模块运行态投影失败",
            )
    world_labels = _world_labels(instance_config)
    progress = (
        safe_world_state.get("progress")
        if isinstance(safe_world_state.get("progress"), Mapping)
        else {}
    )
    live_context = {
        "location": _text(
            safe_world_state.get("current_location")
            or safe_world_state.get("location")
            or safe_world_state.get("scene_id")
        ),
        "world_time": _display_world_time(
            safe_world_state.get("world_time")
            or safe_world_state.get("time")
        ),
        "scene_summary": _text(safe_world_state.get("scene_summary")),
        "chapter": _text(
            progress.get("chapter")
            or safe_world_state.get("current_chapter")
        ),
        "objective": _text(
            progress.get("current_objective")
            or safe_world_state.get("current_objective")
        ),
    }
    try:
        ledger = await database.list_story_ledger(session_id)
    except Exception as exc:
        ledger = []
        statuses["ledger"] = _status_entry(
            "error",
            "dashboard.ledger.read_failed",
            "剧情账本读取失败",
        )
    else:
        statuses["ledger"] = _status_entry(
            "empty" if not ledger else "ready",
            message="当前没有剧情账本记录",
        )
    try:
        clocks = await database.list_scene_clocks(session_id)
    except Exception as exc:
        clocks = []
        statuses["clocks"] = _status_entry(
            "error",
            "dashboard.clocks.read_failed",
            "场景时钟读取失败",
        )
    else:
        statuses["clocks"] = _status_entry(
            "empty" if not clocks else "ready",
            message="当前没有场景时钟",
        )
    try:
        npc_rows = await _session_npcs(database, session_id)
    except Exception as exc:
        npc_rows = []
        statuses["npcs"] = _status_entry(
            "error",
            "dashboard.npcs.read_failed",
            "NPC 数据读取失败",
        )
    else:
        statuses["npcs"] = _status_entry(
            "empty" if not npc_rows else "ready",
            message="当前没有常住或动态 NPC",
        )
    content_views = project_world_state_view(
        projection_world,
        safe_world_state,
        ledger=ledger,
        session_npcs=npc_rows,
        viewer_role="admin",
    )
    quest_view = content_views["quest_view"]
    faction_view = content_views["faction_view"]
    npc_view = content_views["npc_view"]
    npcs = list(npc_view.get("items") or [])
    # D1：模块面板五状态与权威模块统计。
    module_hints: dict[str, str] = {}
    if statuses.get("clocks", {}).get("state") == "error":
        module_hints["time_clock"] = "error"
    if statuses.get("npcs", {}).get("state") == "error":
        module_hints["npc_lifecycle"] = "error"
    if statuses.get("ledger", {}).get("state") == "error":
        module_hints["knowledge_graph"] = "error"
    session_started = _text(session.get("state")) in {
        "running",
        "paused",
        "finished",
    }
    module_panels = project_module_panels(
        projection_world,
        safe_world_state,
        quest_view=quest_view,
        faction_view=faction_view,
        npc_view=npc_view,
        clocks=[
            {
                "title": _text(item.get("title")),
                "segments": _int(item.get("segments")),
                "current_value": _int(item.get("current_value")),
                "status": _text(item.get("status")),
            }
            for item in clocks
            if isinstance(item, Mapping)
        ],
        ledger=ledger,
        session_started=session_started,
        state_hints=module_hints,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    panel_states = {
        str(panel.get("state") or "")
        for panel in module_panels.values()
        if isinstance(panel, Mapping)
    }
    world_state_problems = [
        {
            "code": str(problem.get("code") or "projection.module.read_failed"),
            "message": str(problem.get("message") or "模块状态读取失败。"),
        }
        for panel in module_panels.values()
        if isinstance(panel, Mapping)
        for problem in panel.get("problems") or []
        if isinstance(problem, Mapping)
    ]
    world_state_view = {
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
        "module_panels": module_panels,
        "problems": world_state_problems,
    }
    module_summary = world_module_summary(world_snapshot)
    # D1：角色命运队伍聚合与终局视图。
    approved_roster = [
        item
        for item in roster_rows
        if isinstance(item, Mapping)
        and _text(item.get("card_status")) == "approved"
        and _text(item.get("participation_status"))
        in {"active", "standby", "away"}
    ]
    fate_read_error = False
    try:
        authoritative_fates = await database.list_actor_fate_states(session_id)
    except Exception:
        authoritative_fates = []
        fate_read_error = True
    fate_rows = [
        {
            "actor_name": _text(
                item.get("character_name") or item.get("display_name")
            ),
            "state_id": _text(item.get("state")),
            "rescue_open": _bool(item.get("rescue_open")),
            "updated_at": _text(item.get("updated_at")),
        }
        for item in authoritative_fates
        if isinstance(item, Mapping)
    ]
    actor_fate_view = project_actor_fate_summary(
        fate_rows,
        world=world_snapshot,
        permanent_death=world_module_declared(world_snapshot, "actor_fate"),
        tpk_label=(
            "小队全部死亡时，副本立即失败并永久归档。"
            if world_module_declared(world_snapshot, "actor_fate")
            else ""
        ),
        roster_count=len(approved_roster),
        session_started=session_started,
        read_error=fate_read_error,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    ending_label = ""
    archive_view = project_terminal_view(
        archive,
        ending_label=ending_label,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    terminal_report = None
    if archive and str(archive.get("termination_type") or "") == "failed":
        quest_items = list(quest_view.get("items") or [])
        clue_items = [
            dict(item)
            for item in ledger
            if isinstance(item, Mapping)
            and str(item.get("kind") or item.get("entry_type") or "").lower()
            in {"clue", "fact"}
        ]
        quest_definitions = (
            world_snapshot.get("rules", {}).get("quest_graph", {}).get("quests")
            if isinstance(world_snapshot, Mapping)
            and isinstance(world_snapshot.get("rules"), Mapping)
            and isinstance(
                world_snapshot["rules"].get("quest_graph"), Mapping
            )
            else None
        )
        knowledge_definitions = (
            world_snapshot.get("rules", {}).get("knowledge_graph", {}).get("facts")
            if isinstance(world_snapshot, Mapping)
            and isinstance(world_snapshot.get("rules"), Mapping)
            and isinstance(
                world_snapshot["rules"].get("knowledge_graph"), Mapping
            )
            else None
        )
        total_quests = (
            len(quest_definitions)
            if isinstance(quest_definitions, (list, dict))
            else None
        )
        total_clues = (
            len(knowledge_definitions)
            if isinstance(knowledge_definitions, (list, dict))
            else None
        )
        terminal_report = project_terminal_report_view(
            archive,
            ending_label=ending_label,
            fate_rows=fate_rows,
            quest_items=quest_items,
            total_quests=total_quests,
            clue_items=clue_items,
            total_clues=total_clues,
            knowledge_declared=world_module_declared(
                world_snapshot, "knowledge_graph"
            ),
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )
    current_choice = None
    choice_recovery_receipt = None
    if choice_set and isinstance(choice_set, Mapping):
        try:
            choice_recovery_receipt = await database.latest_choice_recovery(
                session_id,
                _text(choice_set.get("id")),
            )
        except Exception:
            choice_recovery_receipt = None
        participant = choice_set.get("participant") or {}
        selected_key = _text(choice_set.get("selected_key"))
        selected_label = next(
            (
                _text(option.get("text"))
                for option in active_choice_list
                if str(option.get("key") or "") == selected_key
            ),
            "",
        )
        current_choice = {
            "id": _text(choice_set.get("id")),
            "participant_id": _text(choice_set.get("participant_id")),
            "round_no": _int(choice_set.get("round_no")),
            "status": _text(choice_set.get("status")),
            "reroll_count": _int(choice_set.get("reroll_count")),
            "selected_label": selected_label,
            "player_elaboration": _text(choice_set.get("flavor_text")),
            "choices": active_choice_list,
            "participant": {
                "id": _text(participant.get("id")),
                "character_name": _text(participant.get("character_name")),
                "display_name": _text(participant.get("display_name")),
            }
            if isinstance(participant, Mapping)
            else None,
            "technical_details": None,
            "recovery": choice_recovery_receipt,
        }
        if selected_key and not selected_label:
            current_choice["display_error"] = "已选行动名称解析失败，请刷新后重试。"
        if include_technical_refs and str(viewer_role or "player").lower() in {
            "admin",
            "author",
        }:
            current_choice["technical_details"] = {
                "selected_key": selected_key,
            }
    current_story = None
    if story_event and isinstance(story_event, Mapping):
        story_meta = (
            story_event.get("meta")
            if isinstance(story_event.get("meta"), Mapping)
            else {}
        )
        source = _text(story_meta.get("source"), "system")
        story_payload = {
            "event_id": _text(story_event.get("id")),
            "seq": _int(story_event.get("seq")),
            "turn_no": _int(story_event.get("turn_no")),
            "revision": _int(story_meta.get("story_revision")),
            "session_revision": _int(story_meta.get("session_revision")),
            "title": "故事推进",
            "body": _text(story_event.get("content")),
            "source": source,
            "source_label": {
                "ai": "AI 叙事",
                "dm": "人工 DM 修订",
                "system": "系统叙事",
            }.get(source, "系统叙事"),
            "generated_at": _text(story_event.get("created_at")),
            "visibility": _text(story_meta.get("visibility"), "public"),
            "edited_by_dm": _bool(story_meta.get("edited_by_dm")),
            "mode": _text(story_meta.get("mode"), "append"),
            "supersedes_event_id": _text(
                story_meta.get("supersedes_event_id")
            ),
        }
        current_story = project_story_view(
            story_payload,
            world=world_snapshot,
            viewer_role="admin",
            include_technical_refs=True,
        )
    elif features["story"]:
        current_story = project_story_view(
            {},
            world=world_snapshot,
            viewer_role="admin",
            include_technical_refs=True,
        )
    technical_viewer = str(viewer_role or "player").lower() in {
        "admin",
        "author",
    }
    session_technical = None
    if include_technical_refs and technical_viewer:
        session_technical = {
            "id": session.get("id"),
            "revision": _int(session.get("revision")),
            "group_id": _text(session.get("group_id")),
            "world_revision": _int(session.get("world_revision")),
            "world_state": safe_world_state,
        }
    return {
        "session": {
            "id": session.get("id"),
            "name": _text(
                session.get("instance_name") or session.get("name")
            ),
            "state": _text(session.get("state"), "closed"),
            "turn_no": _int(session.get("turn_no")),
            "input_locked": _int(session.get("input_locked"), 0),
            "readonly": readonly,
            # 0.12.0-A3：副本运行卡片信息（群 ID / 回合数 / 进度 / 等待）。
            "waiting_for": await _waiting_for(
                database, session_id, _text(session.get("state"))
            ),
            "world": {
                "name": _text(session.get("world_name")),
                "slug": _text(session.get("world_slug")),
            },
            # D1：受控世界状态 JSON 移入技术详情，普通页面只消费模块面板。
            "id_labels": await _session_id_labels(database, session_id),
            "world_labels": world_labels,
            "technical_details": session_technical,
        },
        "turn": {
            "round_no": _int(turn.get("round_no")),
            "current_user_id": _text(turn.get("current_user_id")),
            "current_name": _text(turn.get("current_name")),
            "order": [
                {
                    "position": _int(item.get("position")),
                    "user_id": _text(item.get("user_id")),
                    "name": _text(
                        item.get("name")
                        or item.get("character_name")
                        or item.get("display_name")
                    ),
                }
                for item in turn.get("order", [])
                if isinstance(item, Mapping)
            ],
        },
        "timers": [_normalize_timer(item) for item in timers],
        "active_choices": active_choice_list,
        "active_vote": _normalize_vote(vote),
        "current_choice": current_choice,
        "choice_recovery_receipt": choice_recovery_receipt,
        "story_view": current_story,
        "features": features,
        "resource_view": resource_view,
        "economy": {"enabled": bool(economy.get("enabled"))},
        "market_views": market_views,
        "world_state_view": world_state_view,
        "live_context": live_context,
        "squad": squad,
        "opening_recommendations": opening_recommendations,
        "npcs": npcs,
        "narrative_control": narrative_control,
        "module_panels": module_panels,
        "module_summary": module_summary,
        "actor_fate_view": actor_fate_view,
        "archive": archive_view,
        "terminal_report": terminal_report,
        "ledger": [
            {
                "id": _text(item.get("id")),
                "kind": _text(item.get("kind")),
                "title": _text(item.get("title")),
                "description": _text(item.get("description"))[:200],
                "status": _text(item.get("status")),
                "visibility": _text(item.get("visibility")),
                "updated_at": _text(item.get("updated_at")),
            }
            for item in ledger
            if isinstance(item, Mapping)
        ],
        "clocks": [
            {
                "id": _text(item.get("id")),
                "title": _text(item.get("title")),
                "segments": _int(item.get("segments")),
                "current_value": _int(item.get("current_value")),
                "visibility": _text(item.get("visibility")),
                "status": _text(item.get("status")),
                "trigger_text": _text(item.get("trigger_text")),
            }
            for item in clocks
            if isinstance(item, Mapping)
        ],
        "world_package": _world_package_projection(instance_config),
        "return_requests": [
            {
                "id": _text(item.get("id")),
                "character_name": _text(item.get("character_name")),
                "display_name": _text(item.get("display_name")),
                "objective": _text(item.get("objective")),
                "status": _text(item.get("status")),
            }
            for item in return_requests
            if isinstance(item, Mapping)
        ],
        "delegations": delegations,
        "pending_operations": pending_ops,
        "roster": roster,
        "recent_events": [
            _event_projection(
                item,
                _text(session.get("instance_name") or session.get("name")),
            )
            for item in events
            if isinstance(item, Mapping)
        ],
        "provider_health": [
            {
                "provider_id": _text(item.get("provider_id")),
                "success": _bool(item.get("success")),
                "reason": _text(item.get("reason"))[:120],
            }
            for item in providers
            if isinstance(item, Mapping)
        ],
        "data_status": statuses,
    }


__all__ = [name for name in globals() if not name.startswith('__')]

