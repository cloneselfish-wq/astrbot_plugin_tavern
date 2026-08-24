from __future__ import annotations

from .character import *
from .world import *
from .session import *

def project_delivery_status_view(
    row: Mapping[str, Any] | None,
    *,
    target_labels: Mapping[str, str] | None = None,
    verified: bool | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """D1-UX-012：WebUI 主持密语与后台通知的投递状态视图。

    普通视图不返回完整 UMO 与消息正文；临时目标必须显示“尚未验证”，
    不能伪装成已绑定。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知投递状态视角：{viewer_role}")
    problems: list[dict[str, Any]] = []
    if not row or not isinstance(row, Mapping):
        return {
            "schema": "tavern-delivery-status/1.0.0-rc10",
            "state": "error",
            "state_label": "投递状态读取失败",
            "target_label": "",
            "verified": False,
            "message": "投递记录不存在或读取失败，请让管理员检查待投递队列。",
            "available_actions": [],
            "problems": [
                {
                    "code": "projection.delivery_row_missing",
                    "message": "投递记录缺失",
                }
            ],
            "technical_details": None,
        }
    status = str(row.get("status") or "pending").strip()
    spec = DELIVERY_STATE_SPECS.get(
        status, DELIVERY_STATE_SPECS["pending"]
    )
    state, state_label, actions, base_message = spec
    labels = {
        str(key): str(value)
        for key, value in _mapping(target_labels).items()
        if str(key).strip() and str(value).strip()
    }
    origin = str(row.get("origin") or "").strip()
    target_label = labels.get(origin) or ""
    if not target_label:
        problems.append(
            {
                "code": "projection.delivery_target_label_missing",
                "message": "收件人名称解析失败",
            }
        )
    verified_binding = bool(verified) if verified is not None else False
    messages: list[str] = []
    if base_message:
        messages.append(base_message)
    if not target_label:
        messages.append("收件人名称不可用，无法确认投递对象。")
    if not verified_binding:
        messages.append("临时私聊目标尚未验证，不允许发送高敏感密语。")
    result: dict[str, Any] = {
        "schema": "tavern-delivery-status/1.0.0-rc10",
        "state": state,
        "state_label": state_label,
        "target_label": target_label,
        "verified": verified_binding,
        "message": " ".join(messages),
        "available_actions": list(actions),
        "problems": problems,
        "technical_details": None,
    }
    if include_technical_refs and _is_technical_viewer(role):
        result["technical_details"] = {
            "delivery_id": str(row.get("id") or ""),
            "kind": str(row.get("kind") or ""),
            "origin": origin,
            "attempts": _as_int(row.get("attempts")) or 0,
            "last_error": str(row.get("last_error") or ""),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
    return result


def project_delivery_status_items(
    rows: Sequence[Mapping[str, Any]] | None,
    *,
    target_labels: Mapping[str, str] | None = None,
    verified_origins: Sequence[str] | None = None,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """DeliveryStatusView 列表形态（D1-WEB-011 前端组件契约）。

    ``items[*]`` 供 ``components/delivery-status.js`` 直接消费：
    ``recipient_name / verified / status / status_label / channel_label /
    sensitive / sent_parts / total_parts / attempts / next_retry_at /
    last_error / can_retry / can_cancel``。普通视图不含完整 UMO 与正文，
    ``id`` 只用于 data-* 属性定位，不渲染。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知投递列表视角：{viewer_role}")
    verified_set = {
        str(origin)
        for origin in verified_origins or ()
        if str(origin or "").strip()
    }
    items: list[dict[str, Any]] = []
    for raw in rows or ():
        if not isinstance(raw, Mapping):
            continue
        view = project_delivery_status_view(
            raw,
            target_labels=target_labels,
            verified=str(raw.get("origin") or "") in verified_set,
            viewer_role=role,
            include_technical_refs=include_technical_refs,
        )
        status_map = {
            "queued": "waiting",
            "sending": "waiting",
            "partially_sent": "partial",
            "delivered": "delivered",
            "failed": "failed",
            "cancelled": "cancelled",
        }
        status = status_map.get(view["state"], "waiting")
        origin = str(raw.get("origin") or "")
        origin_lower = origin.lower()
        item_verified = bool(view["verified"]) or "groupmessage" in origin_lower
        if (
            not item_verified
            and "friendmessage" in origin_lower
            and status not in {"delivered", "cancelled"}
        ):
            status = "unverified"
        kind = str(raw.get("kind") or "").lower()
        if "friendmessage" in origin_lower:
            channel_label = "私聊"
        elif "groupmessage" in origin_lower:
            channel_label = "群聊"
        else:
            channel_label = "平台消息"
        sensitive = bool(
            kind
            in {
                "whisper",
                "dm_whisper",
                "secret",
                "card_secret",
                "death_confirm",
            }
        )
        technical = view.get("technical_details") or {}
        item: dict[str, Any] = {
            "id": str(raw.get("id") or ""),
            "recipient_name": view["target_label"],
            "verified": item_verified,
            "status": status,
            "status_label": view["state_label"],
            "channel_label": channel_label,
            "sensitive": sensitive,
            "sent_parts": _as_int(raw.get("sent_parts")),
            "total_parts": _as_int(raw.get("total_parts")),
            "attempts": _as_int(raw.get("attempts")) or 0,
            "next_retry_at": str(raw.get("next_retry_at") or ""),
            "last_error": str(raw.get("last_error") or ""),
            "can_retry": "retry" in view["available_actions"],
            "can_cancel": "cancel" in view["available_actions"],
            "message": view["message"],
            "problems": view["problems"],
        }
        if include_technical_refs and _is_technical_viewer(role):
            item["technical_details"] = technical
        items.append(item)
    return {
        "schema": "tavern-delivery-status-list/1.0.0-rc10",
        "items": items,
        "technical_details": None,
    }


def project_actor_fate_view(
    actor_name: str,
    state_id: str,
    *,
    can_act: bool | None = None,
    rescue_open: bool = False,
    rescue_message: str = "",
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """D1-UX-013：ActorFateView 角色命运视图（正常/负伤/濒危/死亡）。"""

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知角色命运视角：{viewer_role}")
    identity = str(state_id or "").strip()
    problems: list[dict[str, Any]] = []
    if not identity:
        problems.append(
            {
                "code": "projection.fate_unknown",
                "message": "角色命运状态缺失，无法安全判断行动资格。",
            }
        )
        label = "命运状态未知"
        default_can_act = False
        terminal = False
    else:
        common = FATE_STATE_LABELS.get(identity)
        if common:
            label, default_can_act, terminal = common
        else:
            problems.append(
                {
                    "code": "projection.fate_unresolved",
                    "message": "角色命运状态名称解析失败",
                }
            )
            label = "命运状态未知"
            default_can_act = False
            terminal = False
    can_act_value = bool(can_act) if can_act is not None else default_can_act
    rescue_window = {
        "open": bool(rescue_open),
        "message": str(rescue_message or ""),
    }
    return {
        "schema": "tavern-actor-fate/1.0.0-rc10",
        "actor": str(actor_name or ""),
        "state": {"id": identity, "label": label},
        "can_act": can_act_value,
        "terminal": terminal,
        "rescue_window": rescue_window,
        "available_actions": (
            []
            if terminal
            else (["rescue"] if bool(rescue_open) else [])
        ),
        "problems": problems,
    }


def project_actor_fate_summary(
    rows: Sequence[Mapping[str, Any]],
    *,
    world: Mapping[str, Any] | None = None,
    permanent_death: bool = False,
    tpk_label: str = "",
    roster_count: int = 0,
    session_started: bool = False,
    read_error: bool = False,
    has_snapshot: bool = False,
    last_success_at: str = "",
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any]:
    """ActorFateView 队伍命运聚合（D1-WEB-012 / 规格 18 §6）。

    计数范围由调用方传入的合法角色记录决定；空队伍不触发任何终局语义。
    未启用 actor_fate 的世界返回 disabled；启用但缺少命运记录时明确报错，
    不伪造 0。
    """

    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知队伍命运视角：{viewer_role}")
    capability_declared = (
        world_module_declared(world, "actor_fate")
        if world is not None
        else permanent_death
    )
    fate_rows = [
        dict(item) for item in rows or () if isinstance(item, Mapping)
    ]
    problems: list[dict[str, Any]] = []
    known_states = set(FATE_STATE_LABELS)
    invalid_rows = [
        item
        for item in fate_rows
        if str(item.get("state_id") or item.get("state") or "") not in known_states
    ]
    effective_roster_count = (
        int(roster_count) if int(roster_count or 0) > 0 else len(fate_rows)
    )
    if capability_declared and len(fate_rows) < effective_roster_count:
        problems.append(
            {
                "code": "projection.fate_data_missing",
                "message": (
                    "已确认角色缺少命运记录，系统没有把缺失记录当作存活。"
                ),
            }
        )
    if capability_declared and read_error:
        problems.append(
            {
                "code": "projection.fate_read_failed",
                "message": "队伍命运记录读取失败，系统没有修改任何角色状态。",
            }
        )
    if invalid_rows:
        problems.append(
            {
                "code": "projection.fate_state_invalid",
                "message": "命运记录包含无法识别的状态，已停止计算队伍统计。",
            }
        )
    if not capability_declared:
        state = "not_applicable"
        state_label = "不适用"
    elif read_error or invalid_rows:
        state = "error"
        state_label = "读取失败"
    elif effective_roster_count <= 0:
        state = "empty"
        state_label = "暂无成员"
    elif len(fate_rows) < effective_roster_count:
        state = "repairable_missing" if session_started else "waiting"
        state_label = "需要修复" if session_started else "等待初始化"
    else:
        state = "ready"
        state_label = "正常"
    member_count = len(fate_rows) if capability_declared else 0

    def count(state_id: str) -> int:
        return sum(
            1
            for item in fate_rows
            if str(item.get("state_id") or item.get("state") or "") == state_id
        )

    living_count = count("healthy") + count("wounded")
    dead_count = count("dead")
    wounded_count = count("wounded")
    critical_count = count("critical")
    incapacitated_count = count("critical")
    rescue_windows = [
        str(item.get("actor_name") or "")
        for item in fate_rows
        if bool(item.get("rescue_open"))
    ]
    members = [
        {
            "name": str(item.get("actor_name") or ""),
            "fate_label": FATE_STATE_LABELS.get(
                str(item.get("state_id") or ""), ("状态解析失败", False, False)
            )[0],
            "can_act": FATE_STATE_LABELS.get(
                str(item.get("state_id") or ""), (None, False, False)
            )[1],
            "action_label": (
                "不可行动"
                if not FATE_STATE_LABELS.get(
                    str(item.get("state_id") or ""), (None, False, False)
                )[1]
                else "可行动"
            ),
            "rescue_label": (
                str(item.get("rescue_message") or "")
                if bool(item.get("rescue_open"))
                else ""
            ),
            "is_terminal": FATE_STATE_LABELS.get(
                str(item.get("state_id") or ""), (None, False, False)
            )[2],
        }
        for item in fate_rows
    ]
    rescue_window: dict[str, Any] | None = None
    if rescue_windows:
        first = next(
            (
                item
                for item in fate_rows
                if bool(item.get("rescue_open"))
            ),
            {},
        )
        rescue_window = {
            "active": True,
            "label": (
                str(first.get("rescue_message") or "")
                or "下一次场景推进前"
            ),
            "expires_label": "",
        }
    result: dict[str, Any] = {
        "schema": "tavern-actor-fate/1.0.0-rc10",
        "capability_declared": capability_declared,
        "state": state,
        "state_label": state_label,
        "error_message": (
            (
                "已确认的角色缺少命运记录，因此系统不能安全计算存活人数。"
                if state == "repairable_missing"
                else "命运记录损坏，系统已停止计算队伍统计。"
            )
            if state in {"repairable_missing", "error"}
            else ""
        ),
        "has_snapshot": bool(has_snapshot),
        "last_success_at": str(last_success_at or ""),
        "member_count": member_count,
        "living_count": living_count if fate_rows else None,
        "wounded_count": wounded_count if fate_rows else None,
        "critical_count": critical_count if fate_rows else None,
        "dead_count": dead_count if fate_rows else None,
        "incapacitated_count": incapacitated_count if fate_rows else None,
        "rescue_window": rescue_window,
        "rescue_windows": [name for name in rescue_windows if name],
        "permanent_death": bool(permanent_death),
        "tpk_label": str(tpk_label or ""),
        "members": members,
        "problems": problems,
        "available_actions": (
            ["repair"]
            if state == "repairable_missing"
            and include_technical_refs
            and _is_technical_viewer(role)
            else []
        ),
    }
    if include_technical_refs and _is_technical_viewer(role):
        result["technical"] = {
            "rows": [
                {
                    "actor_name": str(item.get("actor_name") or ""),
                    "state_id": str(item.get("state_id") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
                for item in fate_rows
            ],
            "problems": problems,
        }
    return result


def project_terminal_view(
    archive: Mapping[str, Any] | None,
    *,
    ending_label: str = "",
    reason_extra: str = "",
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any] | None:
    """D1-UX-013：TerminalView 终局视图（正常/失败/强制终止 + 永久归档）。"""

    if not archive or not isinstance(archive, Mapping):
        return None
    role = str(viewer_role or "player").strip().lower()
    if role not in VIEWER_ROLES:
        raise ValueError(f"未知终局视角：{viewer_role}")
    termination = str(
        archive.get("termination_type") or "completed"
    ).strip()
    termination_label, result_label = TERMINATION_LABELS.get(
        termination, ("归档", "未知结果")
    )
    readonly = bool(archive.get("readonly", True))
    reason = str(
        reason_extra or archive.get("reason") or ""
    ).strip()
    result: dict[str, Any] = {
        "schema": "tavern-terminal/1.0.0-rc10",
        "ending_label": str(ending_label or "").strip(),
        "result_label": result_label,
        "reason": reason,
        "archive_label": "永久归档" if readonly else "已归档",
        "readonly": readonly,
        "termination_label": termination_label,
        "ended_at": str(archive.get("ended_at") or ""),
        "technical_details": None,
    }
    if include_technical_refs and _is_technical_viewer(role):
        result["technical_details"] = {
            "session_id": str(archive.get("session_id") or ""),
            "final_snapshot_id": str(archive.get("final_snapshot_id") or ""),
            "ended_by": str(archive.get("ended_by") or ""),
            "termination_type": termination,
        }
    return result


def project_terminal_report_view(
    archive: Mapping[str, Any] | None,
    *,
    ending_label: str = "",
    reason_extra: str = "",
    fate_rows: Sequence[Mapping[str, Any]] | None = None,
    quest_items: Sequence[Mapping[str, Any]] | None = None,
    total_quests: int | None = None,
    clue_items: Sequence[Mapping[str, Any]] | None = None,
    total_clues: int | None = None,
    knowledge_declared: bool = False,
    viewer_role: str = "player",
    include_technical_refs: bool = False,
) -> dict[str, Any] | None:
    """失败归档报告：结局、死亡顺序、存活统计与任务/线索完成度。

    缺少数据时给出明确错误状态，不伪造 0。
    """

    terminal = project_terminal_view(
        archive,
        ending_label=ending_label,
        reason_extra=reason_extra,
        viewer_role=viewer_role,
        include_technical_refs=include_technical_refs,
    )
    if terminal is None:
        return None
    problems: list[dict[str, Any]] = list(terminal.get("problems") or [])
    fate_rows = [
        dict(item) for item in fate_rows or () if isinstance(item, Mapping)
    ]
    death_order = [
        str(item.get("actor_name") or item.get("name") or "")
        for item in sorted(
            fate_rows,
            key=lambda row: str(row.get("updated_at") or ""),
        )
        if str(item.get("state_id") or item.get("state") or "") == "dead"
    ]
    death_order = [name for name in death_order if name]
    if not fate_rows:
        problems.append(
            {
                "code": "projection.death_order_unavailable",
                "message": "角色命运记录缺失，无法生成死亡顺序。",
            }
        )
    survival_stats: dict[str, Any] = {
        "member_count": None,
        "living_count": None,
        "dead_count": None,
        "incapacitated_count": None,
    }
    if fate_rows:
        member_count = len(fate_rows)

        def count(state_id: str) -> int:
            return sum(
                1
                for item in fate_rows
                if str(item.get("state_id") or item.get("state") or "")
                == state_id
            )

        survival_stats = {
            "member_count": member_count,
            "living_count": count("healthy") + count("wounded"),
            "dead_count": count("dead"),
            "incapacitated_count": count("critical"),
        }
    else:
        problems.append(
            {
                "code": "projection.survival_stats_unavailable",
                "message": "角色命运记录缺失，无法生成存活统计。",
            }
        )
    if survival_stats["living_count"] is not None:
        survival_text = (
            f"存活 {survival_stats['living_count']} 人"
            f" · 死亡 {survival_stats['dead_count']} 人"
            f" · 濒危 {survival_stats['incapacitated_count']} 人"
        )
    else:
        survival_text = "存活统计不可用"
    quest_items = [
        dict(item) for item in quest_items or () if isinstance(item, Mapping)
    ]
    if total_quests is not None:
        completed = sum(
            1
            for item in quest_items
            if str(item.get("status") or "") == "completed"
        )
        quest_stats: dict[str, Any] = {
            "completed": completed,
            "total": max(0, int(total_quests)),
            "state": "ready",
            "message": "",
        }
    elif quest_items:
        quest_stats = {
            "completed": sum(
                1
                for item in quest_items
                if str(item.get("status") or "") == "completed"
            ),
            "total": len(quest_items),
            "state": "ready",
            "message": "",
        }
    else:
        quest_stats = {
            "completed": None,
            "total": None,
            "state": "error",
            "message": "任务完成度读取失败，暂无数据。",
        }
        problems.append(
            {
                "code": "projection.quest_progress_unavailable",
                "message": "任务完成度数据缺失",
            }
        )
    if quest_stats.get("completed") is not None:
        quest_progress = (
            f"{quest_stats['completed']}/{quest_stats['total']}"
        )
    else:
        quest_progress = "任务完成度不可用"
    clue_items = [
        dict(item) for item in clue_items or () if isinstance(item, Mapping)
    ]
    if not knowledge_declared:
        clue_stats: dict[str, Any] = {
            "discovered": None,
            "total": None,
            "state": "unavailable",
            "message": "本世界未启用知识模块，没有线索统计。",
        }
    elif total_clues is not None:
        clue_stats = {
            "discovered": len(clue_items),
            "total": max(0, int(total_clues)),
            "state": "ready",
            "message": "",
        }
    else:
        clue_stats = {
            "discovered": None,
            "total": None,
            "state": "error",
            "message": "线索完成度读取失败，暂无数据。",
        }
        problems.append(
            {
                "code": "projection.clue_progress_unavailable",
                "message": "线索完成度数据缺失",
            }
        )
    if clue_stats.get("discovered") is not None:
        clue_progress = (
            f"{clue_stats['discovered']}/{clue_stats['total']}"
        )
    elif not knowledge_declared:
        clue_progress = "本世界未启用知识模块"
    else:
        clue_progress = "线索完成度不可用"
    return {
        "schema": "tavern-terminal-report/1.0.0-rc10",
        "ending_label": terminal["ending_label"],
        "result_label": terminal["result_label"],
        "reason": terminal["reason"],
        "archive_label": terminal["archive_label"],
        "readonly": terminal["readonly"],
        "termination_label": terminal["termination_label"],
        "ended_at": terminal["ended_at"],
        "death_order": death_order,
        "survival_stats": survival_stats,
        "survival_text": survival_text,
        "quest_progress": quest_progress,
        "quest_stats": quest_stats,
        "clue_progress": clue_progress,
        "clue_stats": clue_stats,
        "problems": problems,
        "technical_details": terminal["technical_details"],
    }


__all__ = [
    "actor_definition",
    "actor_values_for_roles",
    "field_display_value",
    "field_for_role",
    "project_actor_fate_summary",
    "project_actor_fate_view",
    "project_actor_view",
    "project_delivery_status_view",
    "project_delivery_status_items",
    "project_faction_view",
    "project_module_panel_view",
    "project_module_panels",
    "project_narrative_control_view",
    "project_npc_view",
    "project_quest_view",
    "project_resource_view",
    "project_story_view",
    "project_terminal_report_view",
    "project_terminal_view",
    "project_world_summary_view",
    "project_world_state_view",
    "resolved_catalog",
    "resolve_state_label",
    "semantic_field_index",
    "world_capability_view",
    "world_has_capability",
    "world_module_declared",
    "world_module_manifest",
    "world_module_summary",
    "world_protocol_display",
]

__all__ = [name for name in globals() if not name.startswith('__')]
