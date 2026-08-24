"""Repository orchestration for lazy visual endpoints.

Business decisions remain in the existing repositories and semantic projection
functions.  This module only selects the data required by one lens, applies
permission-aware presentation projections, and composes VisualEnvelope values.
No visual data is persisted and no database schema is introduced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Awaitable, Callable

from ..protocol.projections import project_runtime
from ..projections.world import project_world_state_view
from ..narrative_modes import narrative_mode_view
from ..contracts.narrative_document import legacy_text_fallback
from .clocks import project_clocks
from .common import (
    integer,
    latest_timestamp,
    mapping,
    source_problem,
    text,
)
from .envelopes import VisualEnvelope, visual_envelope
from .keys import OpaqueKeyFactory
from .party import project_party
from .quest_tracks import project_quest_tracks
from .relations import project_relations
from .scene_path import project_scene_path
from .ui_profile import public_ui_profile
from .public_states import public_state_fields, public_state_rows
from .surface_projectors import UnsupportedSurfaceError, project_surface


def visual_permissions(
    role: str,
    *,
    readonly: bool,
    is_admin: bool,
) -> dict[str, bool]:
    privileged = role in {"dm", "admin"}
    return {
        "can_view": role in {"player", "dm", "admin"},
        "can_manage": privileged and not readonly,
        "can_view_private": privileged,
        "can_view_diagnostics": bool(is_admin),
    }


def _readonly(session: Mapping[str, Any]) -> bool:
    return text(session.get("state"), limit=30) == "finished" or bool(
        mapping(session.get("archive")).get("readonly")
    )


def _runtime_for_semantics(
    world: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    role: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_state = mapping(session.get("world_state"))
    projection = project_runtime(
        world,
        raw_state,
        viewer_role=role,
        purpose="web",
    )
    normalized_modules: dict[str, Any] = {}
    for module_id, raw in mapping(projection.get("modules")).items():
        block = mapping(raw)
        normalized_modules[str(module_id)] = mapping(block.get("state"))
    semantic_state = {
        "runtime": {
            "revision": projection.get("revision"),
            "event_sequence": projection.get("event_sequence"),
            "modules": normalized_modules,
        }
    }
    return projection, semantic_state


async def _read(
    problems: list[dict[str, Any]],
    code: str,
    message: str,
    callback: Callable[[], Awaitable[Any]],
    default: Any,
) -> Any:
    try:
        return await callback()
    except Exception:
        problems.append(source_problem(code, message))
        return default


def _choice_summary(
    choice: Mapping[str, Any] | None,
    *,
    keys: OpaqueKeyFactory,
) -> dict[str, Any] | None:
    source = mapping(choice)
    if not source:
        return None
    raw_options = source.get("choices")
    if not isinstance(raw_options, (list, tuple)):
        raw_options = source.get("options")
    options: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_options or ()):
        if not isinstance(raw, Mapping):
            continue
        label = text(raw.get("text") or raw.get("label"), limit=180)
        if not label:
            continue
        options.append(
            {
                "key": keys.key(
                    "choice", f"{index}:{raw.get('key') or raw.get('id') or label}"
                ),
                "label": label,
                "risk": text(raw.get("risk"), limit=60),
                "description": text(
                    raw.get("description")
                    or raw.get("effect")
                    or raw.get("known_consequences"),
                    limit=240,
                    default="选择后将按这项行动继续推进。",
                ),
                "limitation": (
                    text(raw.get("known_consequences"), limit=240)
                    if raw.get("known_consequences")
                    else (
                        "需要完成公开检定后才会结算。"
                        if raw.get("requires_check") or raw.get("check")
                        else "不会额外申请隐藏检定。"
                    )
                ),
                "collective": bool(raw.get("collective")),
                "requires_check": bool(
                    raw.get("requires_check") or raw.get("check")
                ),
            }
        )
    return {
        **public_state_fields(
            source.get("status"),
            family="choice",
            problem_code="visual.summary.choice_state_unknown",
        ),
        "round": integer(source.get("round_no"), 0),
        "options": options,
    }


def _vote_summary(vote: Mapping[str, Any] | None) -> dict[str, Any] | None:
    source = mapping(vote)
    if not source:
        return None
    ballots = [
        item for item in source.get("ballots") or source.get("votes") or ()
        if isinstance(item, Mapping)
    ]
    eligible = [item for item in source.get("eligible_user_ids") or () if str(item)]
    return {
        "title": text(source.get("title") or source.get("topic"), limit=100),
        **public_state_fields(
            source.get("status"),
            family="vote",
            problem_code="visual.summary.vote_state_unknown",
        ),
        "voted_count": len(ballots),
        "eligible_count": len(eligible) if eligible else None,
        "unvoted_count": max(0, len(eligible) - len(ballots)) if eligible else None,
        "deadline_at": text(source.get("deadline_at"), limit=80),
    }


_DECLARED_WORLD_LENS_MODULES = {
    "resources": ("resources", "items_inventory", "economy"),
    "challenge": ("challenge_engine",),
    "progression": ("progression",),
}
_SEMANTIC_WORLD_SURFACES = {
    "scene": "scene_path",
    "quests": "quest_tracks",
    "clocks": "clocks",
    "relations": "relations",
}


def _declared_module_lens(
    runtime: Mapping[str, Any],
    module_ids: Sequence[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Project labels from permission-scrubbed module state, never raw refs."""

    modules = mapping(runtime.get("modules"))
    selected = [mapping(modules.get(module_id)) for module_id in module_ids]
    selected = [item for item in selected if item]
    if not selected:
        return [], [
            {
                **source_problem(
                    "visual.world.declared_module_missing",
                    "世界声明了该视图，但冻结运行态缺少对应模块。",
                ),
                "retryable": False,
            }
        ]
    problems: list[dict[str, Any]] = []
    states = [text(item.get("status"), limit=30) for item in selected]
    if any(state in {"corrupt", "degraded"} for state in states):
        problems.append(
            source_problem(
                "visual.world.declared_module_unhealthy",
                "该世界视图的运行态未通过完整性检查。",
            )
        )
    items: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 3 or len(items) >= 12:
            return
        if isinstance(value, Mapping):
            label = text(
                value.get("label") or value.get("name") or value.get("title"),
                limit=100,
            )
            summary = text(
                value.get("summary")
                or value.get("description")
                or value.get("state_label")
                or value.get("phase_label"),
                limit=180,
            )
            if label:
                identity = (label, summary)
                if identity not in seen:
                    seen.add(identity)
                    items.append({"label": label, "summary": summary})
            for nested in value.values():
                if isinstance(nested, (Mapping, list, tuple)):
                    walk(nested, depth + 1)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for nested in value:
                walk(nested, depth + 1)

    for module in selected:
        walk(mapping(module.get("state")))
    return items, problems


def _safe_narrative_document(value: Any) -> dict[str, Any] | None:
    source = mapping(value)
    mode = text(source.get("mode"), limit=20).lower()
    if mode not in {"minimal", "balanced", "epic"}:
        return None
    blocks = []
    for raw in source.get("blocks") or ():
        block = mapping(raw)
        kind = text(block.get("kind"), limit=30)
        body = text(block.get("text"), limit=500)
        if kind not in {"narration", "action", "dialogue", "reaction", "transition", "reveal", "system_note"} or not body:
            return None
        item = {"kind": kind, "text": body, "tone": text(block.get("tone"), limit=80)}
        speaker = text(block.get("speaker_label"), limit=100)
        if speaker:
            item["speaker_label"] = speaker
        blocks.append(item)
    if not blocks:
        return None
    continuity = mapping(source.get("continuity"))
    return {
        "mode": mode,
        "title": text(source.get("title"), limit=120),
        "blocks": blocks,
        "continuity": {
            "scene_changed": bool(continuity.get("scene_changed")),
            "time_advanced": bool(continuity.get("time_advanced")),
            "revealed_fact_count": max(0, integer(continuity.get("revealed_fact_count"), 0)),
        },
    }


def _public_reminder(value: Any) -> dict[str, Any]:
    source = mapping(value)
    return {
        "enabled": bool(source.get("enabled", True)),
        "interval_seconds": max(30, min(600, integer(source.get("interval_seconds"), 60))),
        "source_label": {"global_default": "全局默认", "session_override": "副本覆盖", "implicit_default": "安全默认"}.get(text(source.get("source"), limit=40), "安全默认"),
        "updated_at": text(source.get("updated_at"), limit=80),
        "applies_to": "next_generation",
    }


async def build_session_summary(
    database: Any,
    session: Mapping[str, Any],
    *,
    role: str,
    is_admin: bool,
    keys: OpaqueKeyFactory,
) -> VisualEnvelope:
    problems: list[dict[str, Any]] = []
    session_id = text(session.get("id"), limit=180)
    turn = await _read(
        problems,
        "visual.summary.turn_read_failed",
        "当前行动状态读取失败。",
        lambda: database.get_turn_status(session_id),
        {},
    )
    story = await _read(
        problems,
        "visual.summary.story_read_failed",
        "当前故事摘要读取失败。",
        lambda: database.latest_public_story_event(session_id),
        None,
    )
    choice = await _read(
        problems,
        "visual.summary.choice_read_failed",
        "当前行动选项读取失败。",
        lambda: database.active_choice_set(session_id),
        None,
    )
    vote = await _read(
        problems,
        "visual.summary.vote_read_failed",
        "当前投票读取失败。",
        lambda: database.active_vote(session_id),
        None,
    )
    timers = await _read(
        problems,
        "visual.summary.timer_read_failed",
        "当前倒计时读取失败。",
        lambda: database.list_timers(session_id),
        [],
    )
    latest_sequence = await _read(
        problems,
        "visual.summary.sequence_read_failed",
        "实时更新位置读取失败。",
        lambda: database.latest_session_event_seq(session_id),
        0,
    )
    mode_reader = getattr(database, "get_narrative_mode", None)
    narrative_mode = (
        await _read(
            problems,
            "visual.summary.narrative_mode_read_failed",
            "正文模式读取失败。",
            lambda: mode_reader(session_id),
            narrative_mode_view("balanced"),
        )
        if callable(mode_reader)
        else narrative_mode_view("balanced")
    )
    reminder_reader = getattr(database, "get_session_generation_reminder", None)
    reminder = (
        await _read(
            problems,
            "visual.summary.generation_reminder_read_failed",
            "故事生成提醒设置读取失败。",
            lambda: reminder_reader(session_id),
            {"enabled": True, "interval_seconds": 60, "source": "implicit_default", "revision": 0},
        )
        if callable(reminder_reader)
        else {"enabled": True, "interval_seconds": 60, "source": "implicit_default", "revision": 0}
    )
    instance_reader = getattr(database, "get_instance_config", None)
    instance = (
        await _read(
            problems,
            "visual.summary.world_profile_read_failed",
            "世界展示规则读取失败。",
            lambda: instance_reader(session_id),
            {},
        )
        if callable(instance_reader)
        else {}
    )
    ui_profile = public_ui_profile(mapping(instance).get("ui_profile"))
    token_usage: Mapping[str, Any] = {}
    token_reader = getattr(database, "token_usage_summary", None)
    if (is_admin or role == "dm") and callable(token_reader):
        token_usage = mapping(
            await _read(
                problems,
                "visual.summary.token_usage_read_failed",
                "Token 摘要读取失败。",
                lambda: token_reader(session_id),
                {},
            )
        )
    session_token_usage = mapping(token_usage.get("session"))
    quota_label = ""
    for raw_quota in token_usage.get("quotas") or ():
        quota = mapping(raw_quota)
        if not bool(quota.get("enabled")):
            continue
        used = max(0, integer(quota.get("used"), 0))
        limit = max(0, integer(quota.get("token_limit"), 0))
        quota_label = f"{used:,} / {limit:,} tokens"
        break
    story_map = mapping(story)
    story_meta = mapping(story_map.get("meta"))
    narrative_document = _safe_narrative_document(story_map.get("narrative_document"))
    legacy_projection: dict[str, Any] = {}
    legacy_problem: dict[str, Any] | None = None
    if narrative_document is None and story_map.get("legacy_record") is True:
        try:
            legacy_projection = legacy_text_fallback(
                story_map.get("text"), legacy_record=True
            ).to_dict()
        except Exception:
            legacy_problem = {
                **source_problem(
                    "visual.summary.legacy_record_invalid",
                    "明确标记的旧故事正文未通过安全检查。",
                ),
                "retryable": False,
            }
    explicit_legacy = bool(legacy_projection)
    story_body = text(story_map.get("content"), limit=600)
    if narrative_document and not story_body:
        story_body = text("\n\n".join(item["text"] for item in narrative_document["blocks"]), limit=600)
    narrative_problem = mapping(story_map.get("narrative_problem"))
    story_problems: list[dict[str, Any]] = []
    public_narrative_problem: dict[str, Any] | None = None
    if story_map and narrative_document is None and not explicit_legacy:
        story_problems.append(
            legacy_problem
            or source_problem(
                "visual.summary.narrative_document_invalid",
                "当前故事的结构化正文缺失或已损坏。",
            )
        )
    if narrative_problem:
        public_narrative_problem = {
            "code": text(narrative_problem.get("code"), limit=100, default="visual.summary.narrative_document_problem"),
            "message": text(narrative_problem.get("message"), limit=240, default="当前故事正文无法完整读取。"),
            "recovery": text(narrative_problem.get("recovery"), limit=240, default="请由主持人刷新后检查最近故事。"),
            "retryable": bool(narrative_problem.get("retryable", True)),
        }
        story_problems.append(public_narrative_problem)
    turn_map = mapping(turn)
    current_name = text(turn_map.get("current_name"), limit=100)
    turn_order = []
    for item in turn_map.get("order") or ():
        member = mapping(item)
        name = text(
            member.get("character_name")
            or member.get("display_name")
            or member.get("current_name")
            or member.get("name"),
            limit=100,
        )
        if name:
            turn_item: dict[str, Any] = {
                "label": name,
                "current": bool(current_name and name == current_name),
            }
            member_state = member.get("state") or member.get(
                "participation_status"
            )
            if member_state not in (None, ""):
                turn_item.update(
                    public_state_fields(
                        member_state,
                        family="participation",
                        problem_code="visual.summary.turn_member_state_unknown",
                    )
                )
            turn_order.append(turn_item)
    timer_labels = {
        "turn": "当前回合",
        "vote": "集体表决",
        "preparation": "开演准备",
        "ready": "角色准备",
        "standby": "暂离保留",
        "card_completion": "角色填写",
    }
    pressure_items = []
    for timer in timers or ():
        item = mapping(timer)
        state = text(item.get("status"), limit=30, default="active")
        if state not in {"active", "paused"}:
            continue
        remaining = max(0, integer(item.get("remaining_seconds"), 0))
        timer_state = public_state_fields(
            state,
            family="timer",
            problem_code="visual.summary.timer_state_unknown",
        )
        pressure_items.append(
            {
                "label": timer_labels.get(
                    text(item.get("timer_type"), limit=50),
                    "公开倒计时",
                ),
                **timer_state,
                "remaining_seconds": remaining,
                "remaining_label": f"剩余 {remaining} 秒",
            }
        )
    readonly = _readonly(session)
    permissions = visual_permissions(role, readonly=readonly, is_admin=is_admin)
    data = {
        "session": {
            "label": text(
                session.get("instance_name") or session.get("name"),
                limit=100,
                default="副本名称不可用",
            ),
            "world_label": text(session.get("world_name"), limit=100),
            **public_state_fields(
                session.get("state"),
                family="session",
                problem_code="visual.summary.session_state_unknown",
            ),
            "turn": integer(session.get("turn_no"), 0),
            "readonly": readonly,
            "input_locked": bool(session.get("input_locked")),
        },
        "turn": {
            "round": integer(turn_map.get("round_no"), 0),
            "current_name": current_name,
            "order": turn_order,
            "remaining_seconds": (
                integer(turn_map.get("remaining_seconds"), 0)
                if turn_map.get("remaining_seconds") is not None
                else None
            ),
        },
        "story": (
            {
                "title": "故事推进",
                "summary": story_body,
                "turn": integer(story_map.get("turn_no"), 0),
                "source_label": {
                    "ai": "AI 叙事",
                    "dm": "人工 DM 修订",
                    "system": "系统叙事",
                }.get(text(story_meta.get("source"), limit=30), "系统叙事"),
                "updated_at": text(story_map.get("created_at"), limit=80),
                "narrative_document": narrative_document,
                "narrative_problem": public_narrative_problem,
                "problems": story_problems,
                **legacy_projection,
            }
            if story_map
            else None
        ),
        "decision": _choice_summary(mapping(choice), keys=keys),
        "vote": _vote_summary(mapping(vote)),
        "pressure": {
            "active_timers": len(pressure_items),
            "items": pressure_items,
        },
        "latest_sequence": max(0, integer(latest_sequence, 0)),
        "narrative_mode": {
            key: value
            for key, value in mapping(narrative_mode).items()
            if key
            in {
                "mode",
                "label",
                "minimum",
                "maximum",
                "description",
                "updated_at",
                "applies_to",
                "options",
            }
        },
        "generation_reminder": _public_reminder(reminder),
        "ui_profile": ui_profile,
        "token_summary": (
            {
                "hour": max(0, integer(session_token_usage.get("hour"), 0)),
                "day": max(0, integer(session_token_usage.get("day"), 0)),
                "all": max(0, integer(session_token_usage.get("all"), 0)),
                "quota_label": quota_label,
            }
            if token_usage
            else None
        ),
    }
    if permissions.get("can_manage") and not readonly:
        mode_revision = integer(mapping(narrative_mode).get("revision"), 0)
        reminder_revision = integer(mapping(reminder).get("revision"), 0)
        actions = [
            {"action_id": "session-narrative-mode-save", "intent": "session.narrative_mode.save", "label": "调整正文模式", "target_kind": "session", "expected_revision": mode_revision, "description": "新模式从下一次故事生成开始生效。", "transportReady": True, "focus_return": "opener", "fields": [{"name": "mode", "type": "select", "labelKey": "action.field.narrative_mode", "required": True, "value": text(mapping(narrative_mode).get("mode"), limit=20), "options": [{"value": item.get("mode"), "label": item.get("label")} for item in mapping(narrative_mode).get("options") or () if isinstance(item, Mapping)]}]},
            {"action_id": "session-generation-reminder-save", "intent": "session.generation_reminder.save", "label": "调整生成提醒", "target_kind": "session", "expected_revision": reminder_revision, "description": "副本设置快照只影响下一次故事生成。", "transportReady": True, "focus_return": "opener", "fields": [{"name": "enabled", "type": "checkbox", "labelKey": "action.field.generation_reminder_enabled", "value": bool(mapping(reminder).get("enabled", True))}, {"name": "interval_seconds", "type": "number", "labelKey": "action.field.generation_reminder_interval", "required": True, "value": integer(mapping(reminder).get("interval_seconds"), 60), "min": 30, "max": 600, "step": 15, "unit": "秒"}, {"name": "inherit_global", "type": "checkbox", "labelKey": "action.field.generation_reminder_inherit_global", "value": text(mapping(reminder).get("source"), limit=40) == "global_default"}]},
        ]
        data["configuration_actions"] = actions
        data["available_actions"] = actions
    summary_parts = [data["session"]["world_label"]]
    if data["turn"]["round"] > 0:
        summary_parts.append(f"第 {data['turn']['round']} 轮")
    if data.get("story"):
        summary_parts.append(data["story"]["title"])
    return visual_envelope(
        kind="session_summary",
        data=data,
        revision=session.get("revision"),
        updated_at=latest_timestamp(
            session.get("updated_at"), story_map.get("created_at")
        ),
        summary={
            "label": data["session"]["label"],
            "summary": " · ".join(summary_parts),
            "count": 1,
        },
        permissions=permissions,
        problems=problems,
        readonly=readonly,
        stale=bool(session.get("stale")),
    )


async def build_session_party(
    database: Any,
    session: Mapping[str, Any],
    *,
    role: str,
    is_admin: bool,
    keys: OpaqueKeyFactory,
) -> VisualEnvelope:
    problems: list[dict[str, Any]] = []
    session_id = text(session.get("id"), limit=180)
    roster = await _read(
        problems,
        "visual.party.roster_read_failed",
        "小队成员读取失败。",
        lambda: database.list_roster(session_id),
        [],
    )
    turn = await _read(
        problems,
        "visual.party.turn_read_failed",
        "小队行动状态读取失败。",
        lambda: database.get_turn_status(session_id),
        {},
    )
    instance = await _read(
        problems,
        "visual.party.world_read_failed",
        "世界角色定义读取失败。",
        lambda: database.get_instance_config(session_id),
        {},
    )
    world = mapping(mapping(instance).get("world_snapshot"))
    ui_profile = mapping(instance).get("ui_profile")
    visual_companion_reader = getattr(
        database,
        "list_ai_companion_visual_states",
        None,
    )
    companion_reader = (
        visual_companion_reader
        if callable(visual_companion_reader)
        else getattr(database, "list_ai_companions")
    )
    companions_payload = await _read(
        problems,
        "visual.party.ai_read_failed",
        "AI 队友状态读取失败。",
        lambda: companion_reader(session_id),
        {},
    )
    companions = [
        dict(item)
        for item in mapping(companions_payload).get("items") or ()
        if isinstance(item, Mapping)
    ]
    pending = mapping(mapping(companions_payload).get("pending_decision"))
    pending_ref = text(pending.get("actor_ref"), limit=180)
    context_method = getattr(database, "ai_companion_decision_context", None)
    if not callable(visual_companion_reader) and callable(context_method):
        for item in companions:
            actor_ref = text(item.get("actor_ref"), limit=180)
            if actor_ref:
                try:
                    context = await context_method(
                        session_id=session_id,
                        actor_ref=actor_ref,
                    )
                except Exception:
                    context = {}
                item["profile"] = mapping(mapping(context).get("profile"))
            item["awaiting_confirmation"] = bool(
                actor_ref and actor_ref == pending_ref
            )
    elif not callable(visual_companion_reader):
        for item in companions:
            item["awaiting_confirmation"] = bool(
                text(item.get("actor_ref"), limit=180) == pending_ref
            )
    list_items = getattr(database, "list_session_item_instances", None)
    if callable(list_items):
        item_instances = await _read(
            problems,
            "visual.party.inventory_read_failed",
            "小队背包摘要读取失败。",
            lambda: list_items(session_id),
            None,
        )
    else:
        item_instances = []
        for member in roster or ():
            if not isinstance(member, Mapping):
                continue
            owner_ref = text(member.get("id"), limit=180)
            if owner_ref and callable(getattr(database, "list_item_instances", None)):
                rows = await _read(
                    problems,
                    "visual.party.inventory_read_failed",
                    "小队背包摘要读取失败。",
                    lambda owner_ref=owner_ref: database.list_item_instances(
                        session_id, owner_ref
                    ),
                    [],
                )
                item_instances.extend(rows or ())
    projected = project_party(
        roster=roster,
        companions=companions,
        turn=mapping(turn),
        world=world,
        item_instances=item_instances,
        inventory_available=item_instances is not None,
        viewer_role=role,
        keys=keys,
        ui_profile=ui_profile,
    )
    projected["ui_profile"] = public_ui_profile(ui_profile)
    problems.extend(projected.pop("problems", []))
    for raw_item in projected.get("items") or ():
        if not isinstance(raw_item, dict):
            continue
        raw_item.update(
            public_state_fields(
                raw_item.get("participation_state"),
                family="participation",
                problem_code="visual.party.participation_state_unknown",
                value_key="participation_state",
                prefix="participation",
            )
        )
    readonly = _readonly(session)
    return visual_envelope(
        kind="party",
        data=projected,
        revision=session.get("revision"),
        updated_at=latest_timestamp(
            session.get("updated_at"),
            *(item.get("updated_at") for item in projected.get("items") or ()),
        ),
        summary={"label": "当前小队", "count": projected["total_items"]},
        permissions=visual_permissions(role, readonly=readonly, is_admin=is_admin),
        problems=problems,
        empty=projected["total_items"] == 0,
        readonly=readonly,
        stale=bool(session.get("stale")),
    )


async def build_session_world_visuals(
    database: Any,
    session: Mapping[str, Any],
    *,
    role: str,
    is_admin: bool,
    viewer_participant: Mapping[str, Any] | None,
    keys: OpaqueKeyFactory,
    requested_surface_key: str = "",
    placement: str = "live_session",
    principal_ref: str = "",
) -> VisualEnvelope:
    problems: list[dict[str, Any]] = []
    session_id = text(session.get("id"), limit=180)
    privileged = role in {"dm", "admin"}
    instance = await _read(
        problems,
        "visual.world.snapshot_read_failed",
        "冻结世界定义读取失败。",
        lambda: database.get_instance_config(session_id),
        {},
    )
    world = mapping(mapping(instance).get("world_snapshot"))
    compiled_ui_profile = mapping(mapping(instance).get("ui_profile"))
    ui_profile = public_ui_profile(compiled_ui_profile)
    surface_manifest = mapping(compiled_ui_profile.get("ui_surface_manifest"))
    audience_scope = "host" if role in {"dm", "admin"} else "player"
    declared_surfaces = [
        mapping(item)
        for item in surface_manifest.get("surfaces") or ()
        if isinstance(item, Mapping)
        and placement in (mapping(item).get("placements") or ())
        and audience_scope in (mapping(item).get("audience_scopes") or ())
    ]
    declared_surfaces.sort(
        key=lambda item: (integer(item.get("order"), 0), text(item.get("surface_key")))
    )
    surface_index = [
        {
            "surface_key": text(item.get("surface_key"), limit=96),
            "component_kind": text(item.get("component_kind"), limit=60),
            "data_kind": text(item.get("data_kind"), limit=80),
            "label": text(item.get("label"), limit=100),
            "summary": text(item.get("summary"), limit=300),
            "required": bool(item.get("required")),
            "order": integer(item.get("order"), 0),
            "visual_recipe": text(item.get("visual_recipe"), limit=80),
            "mobile_presentation": text(item.get("mobile_presentation"), limit=64),
            "read_action": {
                "target": "dashboard/session-world-visuals",
                "surface_key": text(item.get("surface_key"), limit=96),
                "expected_revision": integer(session.get("revision"), 0),
            },
        }
        for item in declared_surfaces
    ]
    if requested_surface_key:
        declared_surfaces = [
            item
            for item in declared_surfaces
            if text(item.get("surface_key"), limit=96) == requested_surface_key
        ]
        if not declared_surfaces:
            raise ValueError("所选世界板块未声明、已失效或当前身份无权查看")
    declared_keys: list[str] = []
    declared_labels: dict[str, str] = {}
    for raw_lens in ui_profile.get("live_lenses") or ():
        lens = mapping(raw_lens)
        key = text(lens.get("key"), limit=60)
        if key in {"party", "replay", "generation"} or key in declared_labels:
            continue
        if key:
            declared_keys.append(key)
            declared_labels[key] = text(lens.get("label"), limit=80, default=key)
    if requested_surface_key and declared_surfaces:
        semantic_lens = {
            "quest_board": "quests",
            "clock_board": "clocks",
            "route_map": "scene",
            "relation_graph": "relations",
        }.get(text(declared_surfaces[0].get("component_kind"), limit=60), "")
        declared_keys = [semantic_lens] if semantic_lens else []
        if semantic_lens:
            declared_labels = {
                semantic_lens: text(
                    declared_surfaces[0].get("label"),
                    limit=80,
                    default=semantic_lens,
                )
            }
    readonly = _readonly(session)
    permissions = visual_permissions(role, readonly=readonly, is_admin=is_admin)
    if not declared_keys and not declared_surfaces:
        return visual_envelope(
            kind="world_visuals",
            data={"surfaces": {}, "surface_index": surface_index, "ui_profile": ui_profile},
            revision=session.get("revision"),
            updated_at=text(session.get("updated_at"), limit=80),
            summary={"label": "世界状态", "count": 0},
            permissions=permissions,
            problems=problems,
            empty=not problems,
            readonly=readonly,
            stale=bool(session.get("stale")),
        )
    try:
        runtime, semantic_state = _runtime_for_semantics(
            world, session, role=role
        )
    except Exception:
        runtime, semantic_state = {}, {}
        problems.append(
            source_problem(
                "visual.world.runtime_projection_failed",
                "世界运行态投影失败。",
            )
        )
    requested = set(declared_keys)
    ledger: Any = []
    if "quests" in requested:
        try:
            ledger = await database.list_story_ledger(
                session_id, include_host=privileged
            )
        except TypeError:
            ledger = await _read(
                problems, "visual.world.ledger_read_failed",
                "任务与故事账本读取失败。",
                lambda: database.list_story_ledger(session_id), []
            )
        except Exception:
            ledger = []
            problems.append(source_problem(
                "visual.world.ledger_read_failed", "任务与故事账本读取失败。"
            ))
    clocks: Any = []
    if "clocks" in requested:
        try:
            clocks = await database.list_scene_clocks(
                session_id, include_hidden=privileged
            )
        except TypeError:
            clocks = await _read(
                problems, "visual.world.clock_read_failed",
                "场景时钟读取失败。",
                lambda: database.list_scene_clocks(session_id), []
            )
        except Exception:
            clocks = []
            problems.append(source_problem(
                "visual.world.clock_read_failed", "场景时钟读取失败。"
            ))
    npcs = (
        await _read(
            problems, "visual.world.npc_read_failed", "当前 NPC 读取失败。",
            lambda: database.list_session_characters(session_id), []
        )
        if requested & {"quests", "relations"} else []
    )
    roster = (
        await _read(
            problems, "visual.world.roster_read_failed", "关系参与者读取失败。",
            lambda: database.list_roster(session_id), []
        )
        if "relations" in requested else []
    )
    semantic_payloads: dict[str, tuple[str, dict[str, Any], int]] = {}
    if "quests" in requested:
        content = project_world_state_view(
            world, semantic_state, ledger=ledger, session_npcs=npcs,
            viewer_role=role, include_technical_refs=False
        )
        data = public_state_rows(
            project_quest_tracks(content.get("quest_view"), keys=keys),
            collection="items", family="quest",
            problem_code="visual.world.quest_state_unknown",
        )
        semantic_payloads["quests"] = (
            "quest_tracks", data, len(data.get("items") or ())
        )
    if "clocks" in requested:
        data = public_state_rows(
            project_clocks(clocks, keys=keys, privileged=privileged),
            collection="items", family="clock",
            problem_code="visual.world.clock_state_unknown",
        )
        semantic_payloads["clocks"] = (
            "clocks", data, len(data.get("items") or ())
        )
    if "scene" in requested:
        data = project_scene_path(world, runtime, keys=keys, privileged=privileged)
        semantic_payloads["scene"] = (
            "scene_path", data, len(data.get("nodes") or ())
        )
    participant = mapping(viewer_participant)
    viewer_refs = {
        text(participant.get(key), limit=180)
        for key in (
            "id",
            "group_user_id",
            "user_id",
            "private_user_id",
            "character_name",
        )
        if text(participant.get(key), limit=180)
    }
    if "relations" in requested:
        data = project_relations(
            mapping(session.get("world_state")).get("relationships"),
            world=world, roster=roster, npcs=npcs, viewer_refs=viewer_refs,
            privileged=privileged, keys=keys
        )
        data = public_state_rows(
            data, collection="nodes", family="relation",
            problem_code="visual.world.relation_state_unknown",
        )
        semantic_payloads["relations"] = (
            "relations", data, len(data.get("nodes") or ())
        )
    nested: dict[str, dict[str, Any]] = {}
    for lens in declared_keys:
        if lens in semantic_payloads:
            kind, data, count = semantic_payloads[lens]
            child_problems = list(data.pop("problems", []))
        elif lens in _DECLARED_WORLD_LENS_MODULES:
            kind = lens
            items, child_problems = _declared_module_lens(
                runtime, _DECLARED_WORLD_LENS_MODULES[lens]
            )
            data, count = {"items": items}, len(items)
        else:
            kind, data, count = lens, {}, 0
            child_problems = [source_problem(
                "visual.world.declared_lens_unsupported",
                "世界声明了当前服务无法安全投影的现场视图。",
            )]
        nested[kind] = visual_envelope(
            kind=kind,
            data=data,
            revision=session.get("revision"),
            updated_at=latest_timestamp(
                session.get("updated_at"),
                *(item.get("updated_at") for item in data.get("items") or ()),
            ),
            summary={"label": declared_labels[lens], "count": count},
            permissions=permissions,
            problems=child_problems,
            empty=count == 0 and not child_problems,
            readonly=readonly,
            stale=bool(session.get("stale")),
        ).to_dict()
    gameplay_cache: dict[str, dict[str, Any]] = {}
    gameplay_reader = getattr(database, "get_gameplay_states", None)
    for surface in declared_surfaces:
        surface_key = text(surface.get("surface_key"), limit=96)
        module_id = text(surface.get("module_id"), limit=80)
        child_problems: list[dict[str, Any]] = []
        gameplay_data: dict[str, Any] = {}
        if module_id and callable(gameplay_reader):
            if module_id not in gameplay_cache:
                try:
                    gameplay_cache[module_id] = mapping(
                        await gameplay_reader(
                            session_id,
                            module_id,
                            viewer_role=role,
                        )
                    )
                except Exception:
                    gameplay_cache[module_id] = {}
                    child_problems.append(
                        source_problem(
                            "visual.surface.runtime_read_failed",
                            "当前板块的活动状态读取失败。",
                        )
                    )
            gameplay_data = gameplay_cache[module_id]
        try:
            payload = project_surface(
                {
                    **surface,
                    "_world_revision": surface_manifest.get("world_revision"),
                    "_manifest_revision": surface_manifest.get("manifest_revision"),
                },
                world=world,
                runtime=runtime,
                gameplay=gameplay_data,
                role=role,
                keys=keys,
                principal_ref=principal_ref,
            )
            component_kind = text(surface.get("component_kind"), limit=60)
            semantic_key = {
                "quest_board": "quests",
                "clock_board": "clocks",
                "route_map": "scene",
                "relation_graph": "relations",
            }.get(component_kind, "")
            if semantic_key and semantic_key in semantic_payloads:
                _kind, semantic_data, _count = semantic_payloads[semantic_key]
                payload["data"] = semantic_data
            component_data = mapping(payload.get("data"))
            count = max(
                len(component_data.get("items") or ()),
                len(component_data.get("nodes") or ()),
                len(component_data.get("zones") or ()),
                len(component_data.get("objectives") or ()),
                1 if any(value not in (None, "", [], {}) for value in component_data.values()) else 0,
            )
            state = None
        except UnsupportedSurfaceError:
            payload = {
                "surface_key": surface_key,
                "component_kind": text(surface.get("component_kind"), limit=60),
                "data_kind": text(surface.get("data_kind"), limit=80),
                "data": {},
                "actions": [],
            }
            count = 0
            state = "unsupported"
            child_problems.append(
                {
                    **source_problem(
                        "visual.surface.component_unsupported",
                        "世界声明了当前插件未注册的可视化组件。",
                    ),
                    "recovery": "请安装包含该 RC10 组件的插件版本后刷新；系统没有显示原始数据。",
                    "retryable": False,
                }
            )
        nested[surface_key] = visual_envelope(
            kind="declared_surface",
            data=payload,
            revision=integer(mapping(payload.get("contract")).get("state_revision"), integer(session.get("revision"), 0)),
            updated_at=text(session.get("updated_at"), limit=80),
            summary={"label": text(surface.get("label"), limit=100), "count": count},
            permissions=permissions,
            problems=child_problems,
            state=state,
            empty=count == 0 and not child_problems,
            readonly=readonly,
            stale=bool(session.get("stale")),
        ).to_dict()
    child_states = {item["state"] for item in nested.values()}
    top_state = None
    if readonly:
        top_state = "readonly"
    elif "error" in child_states or "partial" in child_states or problems:
        top_state = "partial" if any(
            item.get("data") not in ({}, [], None) for item in nested.values()
        ) else "error"
    return visual_envelope(
        kind="world_visuals",
        data={
            "surfaces": nested,
            "surface_index": surface_index,
            "ui_profile": ui_profile,
            "contract": {
                "world_revision": text(surface_manifest.get("world_revision"), limit=100),
                "profile_revision": text(surface_manifest.get("profile_revision"), limit=100),
                "surface_manifest_revision": text(surface_manifest.get("manifest_revision"), limit=100),
                "state_revision": integer(session.get("revision"), 0),
            },
        },
        revision=session.get("revision"),
        updated_at=text(session.get("updated_at"), limit=80),
        summary={
            "label": "世界状态",
            "count": sum(
                integer(item.get("summary", {}).get("count"), 0)
                for item in nested.values()
            ),
        },
        permissions=permissions,
        problems=problems,
        state=top_state,
        empty=not nested and not problems,
        readonly=readonly,
        stale=bool(session.get("stale")),
    )


__all__ = [
    "build_session_party",
    "build_session_summary",
    "build_session_world_visuals",
    "visual_permissions",
]
