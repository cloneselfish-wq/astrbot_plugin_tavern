"""Platform-neutral RC10 tactical BOT command and plain-text handler."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any

from ..database_support import DatabaseConflictError
from ..runtime.contracts import CommandResult
from ..runtime.request import RequestContext
from ..security import ParsedCommand
from ..tactical_runtime import (
    TERMINAL_PHASES, advance_phase, apply_correction, commit, draft_from_text,
    end_conflict, preview,
)
from ..web.routes.gameplay_runtime import build_tactical_start_state


TACTICAL_ACTIONS = frozenset({
    "tactical_status", "tactical_start", "tactical_action", "tactical_guard", "tactical_aid",
    "tactical_retreat", "tactical_parley", "tactical_confirm",
    "tactical_lock", "tactical_advance", "tactical_correct", "tactical_end",
})

_ACTION_OVERRIDES = {
    "tactical_guard": "guard",
    "tactical_aid": "aid",
    "tactical_retreat": "retreat",
    "tactical_parley": "parley",
}

_ACTION_LABELS = {
    "strike": "攻击", "guard": "防守", "maneuver": "移动", "cast": "施展能力",
    "interact": "处理目标", "aid": "援助", "retreat": "撤退", "parley": "谈判",
}
_OUTCOMES = {
    "胜利": "victory", "达成": "victory", "部分成功": "partial_success",
    "撤退": "retreat", "停战": "negotiated", "谈判": "negotiated",
    "失败推进": "defeat_forward", "中止": "aborted_by_host",
}
_INTENSITIES = {
    "议政": "political", "政治": "political", "political": "political",
    "均衡": "balanced", "balanced": "balanced",
    "冒险": "adventure", "adventure": "adventure",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _active(rows: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    items = [_mapping(item) for item in rows.get("items") or ()]
    item = next((value for value in items if value.get("state_key") == "active"), {})
    return _mapping(item.get("state")), int(item.get("revision") or 0)


def _labels(value: Any, *, limit: int = 5) -> list[str]:
    rows = list(value.values()) if isinstance(value, Mapping) else list(value or ()) if isinstance(value, list) else []
    return [
        str(_mapping(item).get("label") or _mapping(item).get("name") or "").strip()
        for item in rows
        if str(_mapping(item).get("label") or _mapping(item).get("name") or "").strip()
    ][:limit]


def _visible_choices(value: Any, *, ref_names: tuple[str, ...]) -> list[tuple[str, str]]:
    if isinstance(value, Mapping):
        rows = [(str(key), _mapping(item)) for key, item in value.items()]
    else:
        rows = [("", _mapping(item)) for item in value or () if isinstance(item, Mapping)]
    result: list[tuple[str, str]] = []
    for fallback_ref, item in rows:
        ref = next((str(item.get(name) or "").strip() for name in ref_names if item.get(name)), fallback_ref)
        label = str(item.get("label") or item.get("name") or "").strip()
        if ref and label:
            result.append((ref, label))
    return result


def _match_visible_choice(
    description: str,
    choices: list[tuple[str, str]],
    *,
    subject: str,
) -> str:
    matches = [(ref, label) for ref, label in choices if label in description]
    unique = list(dict.fromkeys(ref for ref, _label in matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise ValueError(f"行动说明同时命中多个{subject}，请只填写一个面板中显示的完整名称")
    if len(choices) == 1:
        return choices[0][0]
    if len(choices) > 1:
        labels = "、".join(label for _ref, label in choices[:5])
        raise ValueError(f"行动说明未唯一对应{subject}，请填写面板中显示的名称：{labels}")
    return ""


def _bind_visible_references(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
    description: str,
) -> dict[str, Any]:
    bound = dict(draft)
    action = str(bound.get("action_kind") or "")
    if action in {"strike", "parley"}:
        choices = _visible_choices(
            state.get("known_threats") or state.get("threats") or (),
            ref_names=("threat_id", "id", "ref"),
        )
        ref = _match_visible_choice(description, choices, subject="已知威胁")
        if ref:
            bound["target_refs"] = [ref]
    elif action in {"guard", "aid"}:
        choices = _visible_choices(
            state.get("participants") or {},
            ref_names=("participant_id", "actor_key", "id", "ref"),
        )
        ref = _match_visible_choice(description, choices, subject="参与者")
        if ref:
            bound["target_refs"] = [ref]
    elif action in {"maneuver", "retreat"}:
        source = state.get("escape_routes") if action == "retreat" else state.get("zones")
        choices = _visible_choices(
            source or (),
            ref_names=("zone_ref", "zone_id", "id", "ref"),
        )
        ref = _match_visible_choice(description, choices, subject="区域")
        if ref:
            bound["zone_ref"] = ref
    elif action == "interact":
        choices = _visible_choices(
            state.get("objectives") or (),
            ref_names=("objective_id", "id", "objective_ref", "ref"),
        )
        ref = _match_visible_choice(description, choices, subject="公开目标")
        if ref:
            bound["objective_ref"] = ref
    elif action == "cast":
        choices = _visible_choices(
            list(state.get("available_capabilities") or ()) + list(state.get("available_items") or ()),
            ref_names=("capability_id", "item_id", "id", "ref"),
        )
        ref = _match_visible_choice(description, choices, subject="能力或物品")
        if ref:
            bound["capability_or_item_ref"] = ref
    return bound


def _world_module(world: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    return _mapping(world.get(module_id)) or _mapping(_mapping(world.get("rules")).get(module_id))


def _choose_numbered(argument: str, templates: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    parts = str(argument or "").split()
    try:
        selected = int(parts[0]) if parts else 0
    except ValueError:
        selected = 0
    intensity = _INTENSITIES.get(parts[1].lower() if len(parts) > 1 else "", "")
    if 1 <= selected <= len(templates):
        return templates[selected - 1], intensity
    return {}, intensity


def _template_menu(templates: list[dict[str, Any]], argument: str = "") -> str:
    parts = str(argument or "").split()
    try:
        requested = int(parts[1]) if parts and parts[0] in {"页", "列表"} and len(parts) > 1 else 1
    except ValueError:
        requested = 1
    pages = max(1, (len(templates) + 11) // 12)
    page = max(1, min(pages, requested))
    offset = (page - 1) * 12
    rows = "\n\n".join(
        f"{index}. {str(item.get('label') or item.get('name') or '战术冲突').strip()}\n"
        f"说明：{str(item.get('summary') or item.get('objective') or '按世界作者定义执行').strip()}"
        for index, item in enumerate(templates[offset:offset + 12], offset + 1)
    )
    navigation = []
    if page > 1:
        navigation.append(f"上一页：/团 开始战术 页 {page - 1}")
    if page < pages:
        navigation.append(f"下一页：/团 开始战术 页 {page + 1}")
    return f"第 {page} / {pages} 页\n\n{rows}" + ("\n\n" + "\n".join(navigation) if navigation else "")


def format_tactical_status(state: Mapping[str, Any], *, user_id: str) -> str:
    phase_labels = {
        "setup": "准备冲突", "declare": "声明行动", "locked": "行动已锁定",
        "resolve_players": "结算玩家行动", "resolve_opposition": "结算敌方行动",
        "environment": "推进环境", "settle_round": "回合收束",
        "victory": "目标达成", "partial_success": "带代价达成", "retreat": "已撤退",
        "negotiated": "已停战", "defeat_forward": "失败后继续推进",
    }
    phase = str(state.get("phase") or "setup")
    objective = str(state.get("objective") or "当前公开目标尚未说明").strip()
    zones = _labels(state.get("zones"))
    threats = _labels(state.get("known_threats") or state.get("threats"))
    telegraphs = [str(item).strip() for item in state.get("telegraphs") or () if str(item).strip()][:3]
    participants = _mapping(state.get("participants"))
    actor = _mapping(participants.get(user_id))
    budget = _mapping(actor.get("action_budget"))
    lines = [
        "【战术冲突】",
        f"第 {max(1, int(state.get('round') or 1))} 轮 · {phase_labels.get(phase, '等待开始')}",
        f"目标：{objective}",
        "区域：" + ("、".join(zones) if zones else "当前没有可见区域资料"),
        "已知威胁：" + ("、".join(threats) if threats else "当前没有已识别威胁"),
    ]
    if telegraphs:
        lines.append("敌方预兆：" + "；".join(telegraphs))
    if budget:
        lines.append(
            "你的额度：主要行动 "
            f"{max(0, int(budget.get('major') or 0))}，移动 "
            f"{max(0, int(budget.get('maneuver') or 0))}，反应 "
            f"{max(0, int(budget.get('reaction') or 0))}"
        )
    lines.extend([
        "",
        "可发送：",
        "/团 行动 <目标或说明>",
        "/团 防守 <对象>",
        "/团 援助 <对象>",
        "/团 撤退 <出口>",
        "/团 谈判 <提案>",
    ])
    return "\n".join(lines)


class TacticalCommandService:
    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], dict[str, Any]] = {}

    async def _state(self, database: Any, session_id: str, role: str) -> tuple[dict[str, Any], int]:
        if not session_id:
            return {}, 0
        reader = getattr(database, "get_gameplay_states", None)
        if not callable(reader):
            return {}, 0
        rows = await reader(
            session_id,
            "tactical_conflict",
            viewer_role="dm" if role in {"host", "admin"} else "player",
        )
        return _active(rows)

    async def is_active(self, database: Any, session_id: str) -> bool:
        state, _revision = await self._state(database, session_id, "player")
        return bool(state and str(state.get("phase") or "") == "declare")

    async def handle_plain_text(self, ctx: RequestContext, text: str, database: Any) -> CommandResult:
        if not await self.is_active(database, ctx.session_id):
            return CommandResult.ignored()
        command = ParsedCommand(
            matched=True,
            action="tactical_action",
            argument=str(text or "").strip(),
            raw_action="直接回复",
        )
        return await self.handle(ctx, command, database)

    async def handle(self, ctx: RequestContext, command: ParsedCommand, database: Any) -> CommandResult:
        if command.action not in TACTICAL_ACTIONS:
            return CommandResult.ignored()
        if not ctx.roles & {"player", "host", "admin"}:
            return CommandResult.reply(
                "【战术冲突】你不是当前副本成员，无法读取或提交战术行动。\n"
                "下一步：请先发送 /团 加入，或联系主持人确认席位。",
                code="tactical.member_required",
            )
        state, revision = await self._state(
            database,
            ctx.session_id,
            "host" if ctx.roles & {"host", "admin"} else "player",
        )
        if command.action == "tactical_start":
            if not ctx.roles & {"host", "admin"}:
                return CommandResult.reply(
                    "【建立战术冲突被拒绝】只有当前副本主持人可以选择世界模板。\n"
                    "自动处理：系统没有写入战况。\n下一步：请等待主持人发送 /团 开始战术。",
                    code="tactical.host_required",
                )
            reader = getattr(database, "get_instance_config", None)
            instance = _mapping(await reader(ctx.session_id)) if callable(reader) else {}
            world = _mapping(instance.get("world_snapshot"))
            manifest = _mapping(_mapping(instance.get("ui_profile")).get("ui_surface_manifest"))
            world_revision = str(manifest.get("world_revision") or instance.get("world_revision") or "").strip()
            definition = _world_module(world, "tactical_conflict")
            templates = [
                _mapping(item) for item in definition.get("conflicts") or definition.get("templates") or ()
                if isinstance(item, Mapping)
            ]
            template, intensity = _choose_numbered(str(command.argument or ""), templates)
            if not world or not world_revision or not templates:
                return CommandResult.reply(
                    "【战术冲突未建立】当前副本缺少冻结世界或战术模板。\n"
                    "自动处理：系统没有使用聊天文本补造规则。\n下一步：请管理员检查副本世界快照。",
                    code="tactical.frozen_world_missing",
                )
            if not template:
                return CommandResult.reply(
                    "【可用战术冲突】\n" + _template_menu(templates, str(command.argument or "")) +
                    "\n\n开始命令独占一行：\n/团 开始战术 <序号> [议政|均衡|冒险]\n"
                    "例如：/团 开始战术 1 均衡",
                    code="tactical.template_menu",
                )
            request_key = ctx.idempotency_key or "bot-tactical-start-" + hashlib.sha256(
                f"{ctx.session_id}:{ctx.user_id}:{ctx.request_id}:{command.argument}".encode()
            ).hexdigest()[:24]
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(await receipt_reader(ctx.session_id, "tactical_conflict", request_key)) if callable(receipt_reader) else {}
            if prior and str(prior.get("intent") or "") != "tactical.conflict.start":
                return CommandResult.reply(
                    "【战术冲突未建立】该防重复凭证已用于另一项操作。\n下一步：请查询原回执。",
                    code="tactical.idempotency_conflict",
                )
            if state and str(state.get("phase") or state.get("status") or "") not in TERMINAL_PHASES and not prior:
                return CommandResult.reply(
                    "【战术冲突未建立】当前已有进行中的冲突。\n"
                    "自动处理：系统没有覆盖现有行动与回执。\n下一步：请先结束当前冲突。",
                    code="tactical.already_started",
                )
            try:
                start_state = await build_tactical_start_state(
                    database, ctx.session_id, template, world, world_revision,
                    intensity_id=intensity, request_key=request_key,
                )
                if prior:
                    saved = _mapping(prior.get("result"))
                    frozen = _mapping(_mapping(saved.get("state")).get("start_receipt"))
                    requested = _mapping(start_state.get("start_receipt"))
                    if any(str(frozen.get(key) or "") != str(requested.get(key) or "") for key in ("template_revision", "world_revision", "intensity")):
                        raise ValueError("该防重复凭证对应另一份战术启动请求")
                    replayed = True
                else:
                    saved = await database.put_gameplay_state(
                        ctx.session_id, "tactical_conflict", "active", start_state,
                        expected_revision=revision, actor_id=ctx.user_id,
                        idempotency_key=request_key, intent="tactical.conflict.start",
                        archive_current=bool(state),
                    )
                    replayed = bool(saved.get("replayed"))
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【战术冲突未建立】" + str(exc) + "\n"
                    "自动处理：系统没有覆盖当前战况。\n下一步：发送 /团 战况 后重新选择模板。",
                    code="tactical.start_rejected",
                )
            return CommandResult.reply(
                "【战术冲突已建立】\n"
                f"冲突：{str(template.get('label') or '世界战术冲突').strip()}\n"
                f"强度：{str(_mapping(start_state.get('intensity')).get('label') or '均衡').strip()}\n"
                "自动处理：参与者、区域、目标、威胁、人数缩放和结果 effects 已冻结。\n"
                "下一步：发送 /团 推进战术 进入声明阶段。",
                code="tactical.started",
                data={"revision": int(saved.get("revision") or 0), "replayed": replayed},
            )
        if not state:
            return CommandResult.reply(
                "【战术冲突】当前副本没有活动冲突。\n下一步：发送 /团 当前 查看可执行操作。",
                code="tactical.not_started",
            )
        if command.action == "tactical_status":
            return CommandResult.reply(
                format_tactical_status(state, user_id=ctx.user_id),
                code="tactical.status",
            )
        if command.action in {"tactical_lock", "tactical_advance", "tactical_correct", "tactical_end"}:
            if not ctx.roles & {"host", "admin"}:
                return CommandResult.reply(
                    "【主持操作被拒绝】只有当前副本主持人可以锁定、推进、纠错或结束战术冲突。\n"
                    "自动处理：系统没有修改战况。\n下一步：请保留草稿并等待主持人处理。",
                    code="tactical.host_required",
                )
            argument = str(command.argument or "").strip()
            if command.action in {"tactical_correct", "tactical_end"} and not argument:
                return CommandResult.reply(
                    "【主持操作未执行】请填写纠错内容或结束结果与依据。\n"
                    "例如：/团 纠正战术 公开目标改为保护证人\n"
                    "或：/团 结束战术 胜利 证人已安全抵达出口",
                    code="tactical.host_reason_required",
                )
            request_key = ctx.idempotency_key or "bot-host-" + hashlib.sha256(
                f"{ctx.session_id}:{ctx.user_id}:{ctx.request_id}:{command.action}:{argument}".encode()
            ).hexdigest()[:24]
            expected_intent = (
                "tactical.correction.apply" if command.action == "tactical_correct"
                else "tactical.conflict.end" if command.action == "tactical_end"
                else "tactical.phase.advance"
            )
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(await receipt_reader(ctx.session_id, "tactical_conflict", request_key)) if callable(receipt_reader) else {}
            if prior:
                if str(prior.get("intent") or "") != expected_intent:
                    return CommandResult.reply(
                        "【主持操作未执行】该消息凭证已用于另一项战术操作。\n"
                        "自动处理：系统没有重复写入。\n下一步：发送 /团 战况 查看原结果。",
                        code="tactical.idempotency_conflict",
                    )
                return CommandResult.reply(
                    "【战术状态已恢复】\n"
                    "自动处理：系统按同一平台事件找回原回执，没有重复推进。\n"
                    "下一步：发送 /团 战况 查看最新公开状态。",
                    code="tactical.host_operation_committed",
                    data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                )
            try:
                if command.action in {"tactical_lock", "tactical_advance"}:
                    phase = str(state.get("phase") or "setup")
                    if command.action == "tactical_lock" and phase != "declare":
                        raise ValueError("锁定行动只允许在声明阶段执行；请先推进到声明阶段或查看当前战况")
                    if command.action == "tactical_advance" and phase == "declare":
                        raise ValueError("声明阶段必须先使用 /团 锁定行动，不能直接跳过行动锁定")
                    next_state, receipt = advance_phase(
                        state,
                        idempotency_key=request_key,
                        reason=argument or ("主持人确认锁定玩家行动" if command.action == "tactical_lock" else "主持人确认推进战术阶段"),
                    )
                elif command.action == "tactical_correct":
                    next_state, receipt = apply_correction(
                        state,
                        {"field": "objective", "value": argument},
                        idempotency_key=request_key,
                        reason=argument,
                    )
                else:
                    outcome_label, _, reason = argument.partition(" ")
                    outcome = _OUTCOMES.get(outcome_label)
                    if not outcome or not reason.strip():
                        raise ValueError("结束战术必须使用已注册结果并填写依据")
                    next_state, receipt = end_conflict(
                        state,
                        outcome=outcome,
                        idempotency_key=request_key,
                        reason=reason,
                    )
                saved = await database.put_gameplay_state(
                    ctx.session_id,
                    "tactical_conflict",
                    "active",
                    next_state,
                    expected_revision=revision,
                    actor_id=ctx.user_id,
                    idempotency_key=request_key,
                    intent=str(receipt.get("intent") or (
                        "tactical.correction.apply" if command.action == "tactical_correct"
                        else "tactical.conflict.end" if command.action == "tactical_end"
                        else "tactical.phase.advance"
                    )),
                )
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【主持操作未执行】" + str(exc) + "\n"
                    "自动处理：系统保留当前战况，没有静默覆盖旧回执。\n"
                    "下一步：发送 /团 战况 刷新后重新确认。",
                    code="tactical.host_operation_rejected",
                )
            return CommandResult.reply(
                "【战术状态已更新】\n"
                f"阶段：{format_tactical_status(next_state, user_id=ctx.user_id).splitlines()[1]}\n"
                "自动处理：阶段、纠错或结果与回执已在同一事务中保存。\n"
                "下一步：发送 /团 战况 查看最新公开状态。",
                code="tactical.host_operation_committed",
                data={"revision": int(saved.get("revision") or 0)},
            )
        if "player" not in ctx.roles:
            return CommandResult.reply(
                "【行动未建立】当前账号没有绑定可行动角色。\n"
                "自动处理：系统没有代替任何玩家建立草稿。\n"
                "下一步：请先加入并完成角色绑定；主持推进请使用主持入口。",
                code="tactical.actor_required",
            )
        draft_key = (ctx.session_id, ctx.user_id)
        if command.action == "tactical_confirm":
            stored = self._drafts.get(draft_key)
            if not stored:
                receipt_reader = getattr(database, "get_gameplay_receipt", None)
                prior = _mapping(await receipt_reader(ctx.session_id, "tactical_conflict", ctx.idempotency_key)) if callable(receipt_reader) and ctx.idempotency_key else {}
                if str(prior.get("intent") or "") == "tactical.action.commit":
                    return CommandResult.reply(
                        "【行动回执已恢复】\n"
                        "自动处理：系统按同一平台事件找回原提交，没有重复建立行动。\n"
                        "下一步：发送 /团 战况 查看更新。",
                        code="tactical.committed",
                        data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                    )
                return CommandResult.reply(
                    "【确认行动】当前没有待确认草稿。\n下一步：先发送 /团 行动 <目标或说明>。",
                    code="tactical.draft_missing",
                )
            request_key = (
                ctx.idempotency_key
                or "bot-" + hashlib.sha256(
                    f"{ctx.session_id}:{ctx.user_id}:{stored['expected_revision']}:{stored['draft']}".encode()
                ).hexdigest()[:24]
            )
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(
                await receipt_reader(ctx.session_id, "tactical_conflict", request_key)
            ) if callable(receipt_reader) else {}
            if prior:
                if str(prior.get("intent") or "") != "tactical.action.commit":
                    return CommandResult.reply(
                        "【行动未提交】该消息凭证已用于另一项操作。\n"
                        "自动处理：系统没有重复写入。\n下一步：请查询原回执。",
                        code="tactical.idempotency_conflict",
                    )
                try:
                    original_state = _mapping(_mapping(prior.get("result")).get("state"))
                    if not original_state:
                        raise ValueError("持久回执缺少原始状态快照")
                    _state, replay = commit(
                        original_state,
                        _mapping(stored.get("draft")),
                        idempotency_key=request_key,
                    )
                    if not replay.get("replayed"):
                        raise ValueError("持久回执未能重放原战术行动")
                except ValueError as exc:
                    return CommandResult.reply(
                        "【行动未提交】防重复凭证与当前草稿不一致：" + str(exc) + "\n"
                        "自动处理：系统没有覆盖原回执。\n下一步：请保留草稿并使用新的确认消息。",
                        code="tactical.idempotency_conflict",
                    )
                self._drafts.pop(draft_key, None)
                return CommandResult.reply(
                    "【行动回执已恢复】\n"
                    "自动处理：系统在原提交快照上核对草稿后找回回执，没有重复建立行动。\n"
                    "下一步：发送 /团 战况 查看更新。",
                    code="tactical.committed",
                    data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                )
            if int(stored["expected_revision"]) != revision:
                return CommandResult.reply(
                    "【行动未提交】战况已更新，系统保留了草稿但没有覆盖新状态。\n下一步：发送 /团 战况，再重新发送行动说明。",
                    code="tactical.revision_conflict",
                )
            try:
                next_state, receipt = commit(
                    state,
                    _mapping(stored.get("draft")),
                    idempotency_key=request_key,
                )
                saved = await database.put_gameplay_state(
                    ctx.session_id,
                    "tactical_conflict",
                    "active",
                    next_state,
                    expected_revision=revision,
                    actor_id=ctx.user_id,
                    idempotency_key=receipt["idempotency_key"],
                    intent="tactical.action.commit",
                )
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【行动未提交】" + str(exc) + "\n"
                    "自动处理：系统保留了草稿，没有覆盖当前战况。\n"
                    "下一步：发送 /团 战况 后重新确认。",
                    code="tactical.revision_conflict",
                )
            self._drafts.pop(draft_key, None)
            return CommandResult.reply(
                "【行动已提交】\n"
                f"行动：{_ACTION_LABELS.get(receipt['action_kind'], '执行行动')}\n"
                "结果：等待主持人锁定\n"
                "自动处理：行动只进入待锁定区，尚未掷骰或扣除最终额度。\n"
                "下一步：可重新发送行动说明替换自己的声明；主持人发送 /团 锁定行动 后结算。",
                code="tactical.committed",
                data={"revision": int(saved.get("revision") or 0)},
            )
        argument = str(command.argument or "").strip()
        if not argument:
            return CommandResult.reply(
                "【行动草稿未建立】请说明目标、区域或要保护的对象。\n"
                "例如：/团 援助 站台出口的证人",
                code="tactical.description_required",
            )
        draft = draft_from_text(argument, actor_key=ctx.user_id)
        if command.action in _ACTION_OVERRIDES:
            draft["action_kind"] = _ACTION_OVERRIDES[command.action]
        try:
            draft = _bind_visible_references(state, draft, argument)
            checked = preview(state, draft)
        except ValueError as exc:
            return CommandResult.reply(
                "【行动草稿未建立】" + str(exc) + "\n"
                "自动处理：系统没有写入状态，也没有消耗行动额度。\n"
                "下一步：发送 /团 战况 查看当前阶段和可用行动。",
                code="tactical.draft_rejected",
            )
        self._drafts[draft_key] = {
            "draft": checked["draft"],
            "expected_revision": revision,
        }
        cost = next(iter(checked["cost"]))
        cost_label = "移动" if cost == "maneuver" else "主要行动"
        return CommandResult.reply(
            "【行动草稿】\n"
            f"行动：{_ACTION_LABELS.get(draft['action_kind'], '处理目标')}\n"
            f"说明：{argument}\n"
            f"已知消耗：{cost_label} 1 次\n"
            "已知影响：" + "；".join(checked["known_effects"]) + "\n"
            "自动处理：预览没有写入状态，也没有锁定骰值。\n"
            "确认提交：\n/团 确认行动\n\n"
            "修改草稿：直接重新发送行动说明。",
            code="tactical.draft_ready",
        )


__all__ = ["TACTICAL_ACTIONS", "TacticalCommandService", "format_tactical_status"]
