"""Platform-neutral RC10 challenge BOT commands and direct-text drafts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from typing import Any

from ..challenge_runtime import (
    TERMINAL_OUTCOMES, advance_phase, commit, draft_from_text, end_challenge,
    preview,
)
from ..database_support import DatabaseConflictError
from ..runtime.contracts import CommandResult
from ..runtime.request import RequestContext
from ..security import ParsedCommand
from ..web.routes.gameplay_runtime import build_challenge_start_state


CHALLENGE_ACTIONS = frozenset(
    {
        "challenge_status", "challenge_start", "challenge_action", "challenge_withdraw",
        "challenge_negotiate", "challenge_confirm", "challenge_advance",
        "challenge_end",
    }
)
_ACTION_OVERRIDES = {
    "challenge_withdraw": "withdraw",
    "challenge_negotiate": "negotiate",
}
_OUTCOMES = {
    "成功": "success", "部分成功": "partial", "失败推进": "failure_forward",
    "退出": "retreat", "撤退": "retreat", "协商": "negotiated", "中止": "aborted",
}
_MODE_LABELS = {
    "investigation": "调查", "social": "交涉", "chase": "追逐", "rescue": "救援",
    "hazard": "环境风险", "infiltration": "潜入", "ritual": "仪式",
    "choice": "抉择", "tactical": "战术冲突",
}
_PHASE_LABELS = {
    "setup": "准备挑战", "declare": "声明行动", "locked": "行动已锁定",
    "resolve": "结算行动", "settle": "阶段收束", "ended": "挑战已结束",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _active(rows: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    item = next(
        (_mapping(value) for value in rows.get("items") or () if _mapping(value).get("state_key") == "active"),
        {},
    )
    return _mapping(item.get("state")), int(item.get("revision") or 0)


def _world_module(world: Mapping[str, Any], module_id: str) -> dict[str, Any]:
    return _mapping(world.get(module_id)) or _mapping(_mapping(world.get("rules")).get(module_id))


def _choose_template(argument: str, templates: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        selected = int(str(argument or "").split()[0])
    except (ValueError, IndexError):
        selected = 0
    return templates[selected - 1] if 1 <= selected <= len(templates) else {}


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
        f"{index}. {str(item.get('label') or item.get('name') or '世界挑战').strip()}\n"
        f"说明：{str(item.get('description') or item.get('summary') or item.get('objective') or '按世界作者定义执行').strip()}"
        for index, item in enumerate(templates[offset:offset + 12], offset + 1)
    )
    navigation = []
    if page > 1:
        navigation.append(f"上一页：/团 开始挑战 页 {page - 1}")
    if page < pages:
        navigation.append(f"下一页：/团 开始挑战 页 {page + 1}")
    return f"第 {page} / {pages} 页\n\n{rows}" + ("\n\n" + "\n".join(navigation) if navigation else "")


def format_challenge_status(state: Mapping[str, Any]) -> str:
    mode = str(state.get("mode") or "")
    phase = str(state.get("phase") or "setup")
    progress = max(0, int(state.get("progress") or 0))
    target = max(0, int(state.get("target") or 0))
    lines = [
        "【当前挑战】",
        f"类型：{_MODE_LABELS.get(mode, '通用挑战')} · {_PHASE_LABELS.get(phase, '等待开始')}",
        f"目标：{str(state.get('objective') or '当前公开目标尚未说明').strip()}",
        f"进度：{progress} / {target}",
    ]
    risk = str(state.get("risk") or state.get("risk_summary") or "").strip()
    failure = str(state.get("failure_forward") or "").strip()
    if risk:
        lines.append(f"公开风险：{risk}")
    if failure:
        lines.append(f"失败推进：{failure}")
    lines.extend([
        "", "可发送：", "/团 挑战行动 <目标与说明>",
        "/团 退出挑战 <出口或保护对象>", "/团 挑战谈判 <提案与担保>",
    ])
    return "\n".join(lines)


class ChallengeCommandService:
    def __init__(self) -> None:
        self._drafts: dict[tuple[str, str], dict[str, Any]] = {}

    async def _state(self, database: Any, session_id: str, role: str) -> tuple[dict[str, Any], int]:
        reader = getattr(database, "get_gameplay_states", None)
        if not session_id or not callable(reader):
            return {}, 0
        rows = await reader(session_id, "challenge_engine", viewer_role="dm" if role in {"host", "admin"} else "player")
        return _active(rows)

    async def is_active(self, database: Any, session_id: str) -> bool:
        state, _ = await self._state(database, session_id, "player")
        return bool(state and str(state.get("phase") or "") == "declare")

    async def handle_plain_text(self, ctx: RequestContext, text: str, database: Any) -> CommandResult:
        if not await self.is_active(database, ctx.session_id):
            return CommandResult.ignored()
        return await self.handle(
            ctx,
            ParsedCommand(matched=True, action="challenge_action", argument=str(text or "").strip(), raw_action="直接回复"),
            database,
        )

    async def handle(self, ctx: RequestContext, command: ParsedCommand, database: Any) -> CommandResult:
        if command.action not in CHALLENGE_ACTIONS:
            return CommandResult.ignored()
        if not ctx.roles & {"player", "host", "admin"}:
            return CommandResult.reply(
                "【当前挑战】你不是当前副本成员，无法读取或提交挑战行动。\n下一步：请先发送 /团 加入。",
                code="challenge.member_required",
            )
        state, revision = await self._state(database, ctx.session_id, "host" if ctx.roles & {"host", "admin"} else "player")
        if command.action == "challenge_start":
            if not ctx.roles & {"host", "admin"}:
                return CommandResult.reply(
                    "【建立挑战被拒绝】只有当前副本主持人可以选择世界挑战模板。\n"
                    "自动处理：系统没有写入状态。\n下一步：请等待主持人发送 /团 开始挑战。",
                    code="challenge.host_required",
                )
            reader = getattr(database, "get_instance_config", None)
            instance = _mapping(await reader(ctx.session_id)) if callable(reader) else {}
            world = _mapping(instance.get("world_snapshot"))
            manifest = _mapping(_mapping(instance.get("ui_profile")).get("ui_surface_manifest"))
            world_revision = str(manifest.get("world_revision") or instance.get("world_revision") or "").strip()
            definition = _world_module(world, "challenge_engine")
            templates = [
                _mapping(item) for item in definition.get("templates") or ()
                if isinstance(item, Mapping)
            ]
            template = _choose_template(str(command.argument or ""), templates)
            if not world or not world_revision or not templates:
                return CommandResult.reply(
                    "【挑战未建立】当前副本缺少冻结世界或挑战模板。\n"
                    "自动处理：系统没有使用聊天文本补造规则。\n下一步：请管理员检查副本世界快照。",
                    code="challenge.frozen_world_missing",
                )
            if not template:
                return CommandResult.reply(
                    "【可用世界挑战】\n" + _template_menu(templates, str(command.argument or "")) +
                    "\n\n开始命令独占一行：\n/团 开始挑战 <序号>\n例如：/团 开始挑战 1",
                    code="challenge.template_menu",
                )
            request_key = ctx.idempotency_key or "bot-challenge-start-" + hashlib.sha256(
                f"{ctx.session_id}:{ctx.user_id}:{ctx.request_id}:{command.argument}".encode()
            ).hexdigest()[:24]
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(await receipt_reader(ctx.session_id, "challenge_engine", request_key)) if callable(receipt_reader) else {}
            terminal = str(state.get("phase") or "") == "ended" or str(state.get("status") or state.get("outcome") or "") in TERMINAL_OUTCOMES
            if prior and str(prior.get("intent") or "") != "challenge.start":
                return CommandResult.reply(
                    "【挑战未建立】该防重复凭证已用于另一项操作。\n下一步：请查询原回执。",
                    code="challenge.idempotency_conflict",
                )
            if state and not terminal and not prior:
                return CommandResult.reply(
                    "【挑战未建立】当前已有进行中的挑战。\n"
                    "自动处理：系统没有覆盖现有进度与回执。\n下一步：请先结束当前挑战。",
                    code="challenge.already_started",
                )
            try:
                start_state = await build_challenge_start_state(
                    database, ctx.session_id, template, world_revision,
                    request_key=request_key,
                )
                if prior:
                    saved = _mapping(prior.get("result"))
                    frozen = _mapping(_mapping(saved.get("state")).get("start_receipt"))
                    requested = _mapping(start_state.get("start_receipt"))
                    if any(str(frozen.get(key) or "") != str(requested.get(key) or "") for key in ("template_revision", "world_revision")):
                        raise ValueError("该防重复凭证对应另一份挑战启动请求")
                    replayed = True
                else:
                    saved = await database.put_gameplay_state(
                        ctx.session_id, "challenge_engine", "active", start_state,
                        expected_revision=revision, actor_id=ctx.user_id,
                        idempotency_key=request_key, intent="challenge.start",
                        archive_current=bool(state),
                    )
                    replayed = bool(saved.get("replayed"))
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【挑战未建立】" + str(exc) + "\n"
                    "自动处理：系统没有覆盖当前挑战。\n下一步：发送 /团 挑战 后重新选择模板。",
                    code="challenge.start_rejected",
                )
            return CommandResult.reply(
                "【世界挑战已建立】\n"
                f"挑战：{str(template.get('label') or template.get('name') or '世界挑战').strip()}\n"
                f"目标：{str(start_state.get('objective') or '按公开目标推进').strip()}\n"
                "自动处理：参与者、目标、风险、失败推进和结果 effects 已冻结。\n"
                "下一步：发送 /团 推进挑战 进入声明阶段。",
                code="challenge.started",
                data={"revision": int(saved.get("revision") or 0), "replayed": replayed},
            )
        if not state:
            return CommandResult.reply(
                "【当前挑战】当前副本没有活动挑战。\n下一步：发送 /团 当前 查看可执行操作。",
                code="challenge.not_started",
            )
        if command.action == "challenge_status":
            return CommandResult.reply(format_challenge_status(state), code="challenge.status")
        argument = str(command.argument or "").strip()
        if command.action in {"challenge_advance", "challenge_end"}:
            if not ctx.roles & {"host", "admin"}:
                return CommandResult.reply(
                    "【主持操作被拒绝】只有当前副本主持人可以推进或结束挑战。\n自动处理：系统没有修改挑战状态。",
                    code="challenge.host_required",
                )
            if command.action == "challenge_end" and not argument:
                return CommandResult.reply(
                    "【挑战未结束】请填写结果和依据。\n例如：/团 结束挑战 成功 已完成公开目标",
                    code="challenge.end_reason_required",
                )
            request_key = ctx.idempotency_key or "bot-challenge-host-" + hashlib.sha256(
                f"{ctx.session_id}:{ctx.user_id}:{ctx.request_id}:{command.action}:{argument}".encode()
            ).hexdigest()[:24]
            expected_intent = "challenge.phase.advance" if command.action == "challenge_advance" else "challenge.end"
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(await receipt_reader(ctx.session_id, "challenge_engine", request_key)) if callable(receipt_reader) else {}
            if prior:
                if str(prior.get("intent") or "") != expected_intent:
                    return CommandResult.reply(
                        "【主持操作未执行】该消息凭证已用于另一项挑战操作。\n"
                        "自动处理：系统没有重复写入。\n下一步：发送 /团 挑战 查看原结果。",
                        code="challenge.idempotency_conflict",
                    )
                return CommandResult.reply(
                    "【挑战状态已恢复】\n"
                    "自动处理：系统按同一平台事件找回原回执，没有重复推进。\n"
                    "下一步：发送 /团 挑战 查看最新状态。",
                    code="challenge.host_operation_committed",
                    data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                )
            try:
                if command.action == "challenge_advance":
                    next_state, receipt = advance_phase(state, idempotency_key=request_key, reason=argument or "主持人确认推进挑战阶段")
                    intent = "challenge.phase.advance"
                else:
                    label, _, reason = argument.partition(" ")
                    outcome = _OUTCOMES.get(label)
                    if not outcome or not reason.strip():
                        raise ValueError("结束挑战必须使用已注册结果并填写依据")
                    next_state, receipt = end_challenge(state, outcome=outcome, reason=reason, idempotency_key=request_key)
                    intent = "challenge.end"
                saved = await database.put_gameplay_state(
                    ctx.session_id, "challenge_engine", "active", next_state,
                    expected_revision=revision, actor_id=ctx.user_id,
                    idempotency_key=request_key, intent=intent,
                )
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【主持操作未执行】" + str(exc) + "\n自动处理：系统没有覆盖当前挑战。\n下一步：发送 /团 挑战 刷新后重试。",
                    code="challenge.host_operation_rejected",
                )
            return CommandResult.reply(
                "【挑战状态已更新】\n"
                f"阶段：{_PHASE_LABELS.get(str(next_state.get('phase')), '挑战已更新')}\n"
                "自动处理：状态和回执已在同一事务中保存。\n下一步：发送 /团 挑战 查看最新状态。",
                code="challenge.host_operation_committed",
                data={"revision": int(saved.get("revision") or 0), "replayed": bool(receipt.get("replayed"))},
            )
        if "player" not in ctx.roles:
            return CommandResult.reply(
                "【挑战行动未建立】当前账号没有绑定可行动角色。\n下一步：请先加入并完成角色绑定。",
                code="challenge.actor_required",
            )
        draft_key = (ctx.session_id, ctx.user_id)
        if command.action == "challenge_confirm":
            stored = self._drafts.get(draft_key)
            if not stored:
                receipt_reader = getattr(database, "get_gameplay_receipt", None)
                prior = _mapping(await receipt_reader(ctx.session_id, "challenge_engine", ctx.idempotency_key)) if callable(receipt_reader) and ctx.idempotency_key else {}
                if str(prior.get("intent") or "") == "challenge.action.commit":
                    return CommandResult.reply(
                        "【挑战行动回执已恢复】\n"
                        "自动处理：系统按同一平台事件找回原提交，没有重复结算。\n"
                        "下一步：发送 /团 挑战 查看更新。",
                        code="challenge.committed",
                        data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                    )
                return CommandResult.reply("【确认挑战】当前没有待确认草稿。\n下一步：先发送 /团 挑战行动 <说明>。", code="challenge.draft_missing")
            request_key = ctx.idempotency_key or "bot-challenge-" + hashlib.sha256(
                f"{ctx.session_id}:{ctx.user_id}:{stored['expected_revision']}:{stored['draft']}".encode()
            ).hexdigest()[:24]
            receipt_reader = getattr(database, "get_gameplay_receipt", None)
            prior = _mapping(
                await receipt_reader(ctx.session_id, "challenge_engine", request_key)
            ) if callable(receipt_reader) else {}
            if prior:
                if str(prior.get("intent") or "") != "challenge.action.commit":
                    return CommandResult.reply(
                        "【挑战行动未提交】该消息凭证已用于另一项操作。\n"
                        "自动处理：系统没有重复写入。\n下一步：请查询原回执。",
                        code="challenge.idempotency_conflict",
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
                        raise ValueError("持久回执未能重放原挑战行动")
                except ValueError as exc:
                    return CommandResult.reply(
                        "【挑战行动未提交】防重复凭证与当前草稿不一致：" + str(exc) + "\n"
                        "自动处理：系统没有覆盖原回执。\n下一步：请保留草稿并使用新的确认消息。",
                        code="challenge.idempotency_conflict",
                    )
                self._drafts.pop(draft_key, None)
                return CommandResult.reply(
                    "【挑战行动回执已恢复】\n"
                    "自动处理：系统在原提交快照上核对草稿后找回回执，没有重复结算。\n"
                    "下一步：发送 /团 挑战 查看更新。",
                    code="challenge.committed",
                    data={"revision": int(prior.get("revision_after") or 0), "replayed": True},
                )
            if int(stored["expected_revision"]) != revision:
                return CommandResult.reply("【挑战行动未提交】状态已更新，系统保留了草稿。\n下一步：发送 /团 挑战 后重新确认。", code="challenge.revision_conflict")
            try:
                next_state, receipt = commit(state, _mapping(stored.get("draft")), idempotency_key=request_key)
                saved = await database.put_gameplay_state(
                    ctx.session_id, "challenge_engine", "active", next_state,
                    expected_revision=revision, actor_id=ctx.user_id,
                    idempotency_key=request_key, intent="challenge.action.commit",
                )
            except (DatabaseConflictError, ValueError) as exc:
                return CommandResult.reply(
                    "【挑战行动未提交】" + str(exc) + "\n自动处理：系统保留了草稿。\n下一步：发送 /团 挑战 后重新确认。",
                    code="challenge.commit_rejected",
                )
            self._drafts.pop(draft_key, None)
            label = {"success": "成功推进", "partial": "带代价推进", "failure_forward": "失败后推进"}.get(str(receipt.get("result_band")), "状态已更新")
            return CommandResult.reply(
                f"【挑战行动已提交】\n结果：{label}\n自动处理：规则结果、进度和回执已在同一事务中写入。\n下一步：发送 /团 挑战 查看更新。",
                code="challenge.committed",
                data={"revision": int(saved.get("revision") or 0)},
            )
        if not argument:
            return CommandResult.reply("【挑战草稿未建立】请说明目标、方法或要保护的对象。", code="challenge.description_required")
        draft = draft_from_text(argument, actor_key=ctx.user_id)
        if command.action in _ACTION_OVERRIDES:
            draft["action_kind"] = _ACTION_OVERRIDES[command.action]
        try:
            checked = preview(state, draft)
        except ValueError as exc:
            return CommandResult.reply(
                "【挑战草稿未建立】" + str(exc) + "\n自动处理：系统没有写入状态。\n下一步：发送 /团 挑战 查看当前阶段。",
                code="challenge.draft_rejected",
            )
        self._drafts[draft_key] = {"draft": checked["draft"], "expected_revision": revision}
        return CommandResult.reply(
            "【挑战行动草稿】\n"
            f"说明：{argument}\n已知影响：{'；'.join(checked['known_effects'])}\n"
            "自动处理：预览没有写入状态。\n确认提交：\n/团 确认挑战\n\n修改草稿：直接重新发送行动说明。",
            code="challenge.draft_ready",
        )


__all__ = ["CHALLENGE_ACTIONS", "ChallengeCommandService", "format_challenge_status"]
