"""Audience-safe PageModel for RC10 gameplay surface modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...challenge_runtime import (
    TERMINAL_OUTCOMES as CHALLENGE_TERMINAL_OUTCOMES,
    advance_phase as advance_challenge_phase,
    commit as commit_challenge,
    draft_from_text as challenge_draft_from_text,
    end_challenge,
    preview as preview_challenge,
    validate_draft as validate_challenge_draft,
)
from ...gameplay_runtime import GAMEPLAY_RUNTIME_MODULES
from ...visualization import visual_envelope
from ...visualization.surface_registry import MODULE_LABELS as SURFACE_LABELS
from ...tactical_runtime import (
    TERMINAL_PHASES as TACTICAL_TERMINAL_PHASES,
    advance_phase as advance_tactical_phase,
    apply_correction as apply_tactical_correction,
    commit as commit_tactical,
    draft_from_text,
    end_conflict,
    preview as preview_tactical,
    validate_draft,
)
from ..errors import bad_request, forbidden
from . import WebRouteError, mapping, require_login, text, to_int
from .sessions import require_member
from ..surfaces.registry import resolve_surface_key
from .gameplay_replay import (
    _active_item,
    _canonical_actor_key,
    _challenge_request_draft,
    _finish_persistent_replay,
    _has_idempotency_receipt,
    _persistent_replay_source,
    _safe_draft,
    _safe_receipt,
    _stored_receipt,
    _tactical_request_draft,
)
from .gameplay_start import (
    _resolve_frozen_template,
    _resolve_intensity_profile,
    _template_revision,
    build_challenge_start_state,
    build_tactical_start_state,
)

MODULE_LABELS = {
    "elemental_interactions": "元素交互",
    "evidence_ledger": "证据账本",
    "accords": "承诺与协定",
    "assembly": "听证与会盟",
    "rumor_network": "传闻网络",
    "scene_environment": "场景环境",
}


def _request_key(value: object) -> str:
    key = text(value)
    if not key:
        raise WebRouteError(
            400,
            "gameplay.idempotency_required",
            "本次操作缺少防重复凭证。",
            "请保留当前输入并重新提交一次；系统尚未写入状态。",
        )
    return key


def _is_tactical_terminal(state: Mapping[str, Any]) -> bool:
    return text(state.get("phase") or state.get("status")) in TACTICAL_TERMINAL_PHASES


def _is_challenge_terminal(state: Mapping[str, Any]) -> bool:
    return (
        text(state.get("phase")) == "ended"
        or text(state.get("status") or state.get("outcome")) in CHALLENGE_TERMINAL_OUTCOMES
    )


def _public_gameplay_rows(module_id: str, rows: Mapping[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for raw in rows.get("items") or ():
        item = mapping(raw)
        state = mapping(item.get("state"))
        public = {
            "state": text(state.get("status") or state.get("state") or state.get("phase")),
            "phase": text(state.get("phase")),
            "label": text(state.get("label") or state.get("title")),
            "summary": text(state.get("summary")),
            "revision": int(item.get("revision") or 0),
        }
        if module_id in {"challenge_engine", "tactical_conflict"}:
            public.update(
                {
                    "mode": text(state.get("mode")),
                    "round": max(0, int(state.get("round") or 0)),
                    "objective": text(state.get("objective")),
                    "progress": max(0, int(state.get("progress") or 0)),
                    "target": max(0, int(state.get("target") or 0)),
                    "risk": text(state.get("risk") or state.get("risk_summary")),
                    "failure_forward": text(state.get("failure_forward")),
                    "receipt_count": len(state.get("receipts") or state.get("locked_receipts") or ()),
                }
            )
        items.append(public)
    return {"label": MODULE_LABELS.get(module_id, SURFACE_LABELS.get(module_id, ("运行玩法", ""))[0]), "items": items, "count": len(items)}


async def gameplay_runtime_view(
    principal: Mapping[str, Any],
    database: Any,
    *,
    query: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    method: str = "GET",
    idempotency_key: str = "",
) -> dict[str, Any]:
    require_login(principal)
    query_map = mapping(query)
    body = mapping(payload)
    session_key = text(query_map.get("session_key") or body.get("session_key"))
    session_id = text(query_map.get("session_id") or body.get("session_id"))
    if session_key:
        session_id = text(resolve_surface_key(principal, "dashboard", session_key))
    module_id = text(query_map.get("module_id") or body.get("module_id"))
    requested_intent = text(body.get("intent"))
    if not module_id and requested_intent.startswith("tactical."):
        module_id = "tactical_conflict"
    elif not module_id and requested_intent.startswith("challenge."):
        module_id = "challenge_engine"
    if not session_id or module_id not in GAMEPLAY_RUNTIME_MODULES:
        raise bad_request(
            "缺少有效副本或玩法板块。",
            recovery="请返回跑团现场后重新选择要查看的板块。",
        )
    role = await require_member(database, session_id, principal)
    can_manage = role in {"dm", "admin"}
    if str(method or "GET").upper() == "POST":
        semantic_intent = text(body.get("intent"))
        tactical_player_intents = {
            "tactical.action.draft",
            "tactical.action.preview",
            "tactical.action.commit",
            "tactical.withdraw.commit",
            "tactical.negotiate.commit",
        }
        tactical_host_intents = {
            "tactical.conflict.start",
            "tactical.phase.advance",
            "tactical.correction.apply",
            "tactical.conflict.end",
        }
        challenge_player_intents = {
            "challenge.action.draft",
            "challenge.action.preview",
            "challenge.action.commit",
            "challenge.withdraw.commit",
            "challenge.negotiate.commit",
        }
        challenge_host_intents = {"challenge.start", "challenge.phase.advance", "challenge.end"}
        if module_id == "tactical_conflict" and semantic_intent in tactical_player_intents | tactical_host_intents:
            current_rows = await database.get_gameplay_states(
                session_id, module_id, viewer_role=role,
            )
            current_state, current_revision = _active_item(current_rows)
            if semantic_intent == "tactical.conflict.start":
                if not can_manage:
                    raise forbidden("只有当前副本主持人可以开始战术冲突。", recovery="请等待主持人从当前世界的战术模板开始冲突。")
                request_key = _request_key(idempotency_key or body.get("idempotency_key"))
                prior_start = await _stored_receipt(database, session_id, module_id, request_key)
                if prior_start and text(prior_start.get("intent")) != semantic_intent:
                    raise WebRouteError(409, "tactical.idempotency_conflict", "该防重复凭证已用于另一项战术操作。", "请查询原回执；不要用同一凭证提交不同操作。")
                if current_state and not _is_tactical_terminal(current_state) and not prior_start:
                    raise WebRouteError(409, "tactical.already_started", "当前已有活动战术冲突。", "请先结束当前冲突，不能覆盖进行中的回执。")
                expected = to_int(body.get("expected_revision"))
                if expected is None:
                    raise WebRouteError(400, "tactical.revision_required", "开始冲突缺少当前状态版本。", "请刷新战术面板后重新选择模板。")
                template, world, frozen_revision = await _resolve_frozen_template(
                    database, session_id, principal, role,
                    module_id=module_id,
                    template_key=text(body.get("template_key")),
                    expected_world_revision=text(body.get("world_revision")),
                )
                profile, selected_intensity = _resolve_intensity_profile(
                    world, text(body.get("intensity")),
                )
                requested_fingerprint = {
                    "template_revision": _template_revision(template),
                    "world_revision": frozen_revision,
                    "intensity": text(profile.get("id") or selected_intensity),
                }
                if prior_start:
                    prior_result = mapping(prior_start.get("result"))
                    prior_fingerprint = mapping(mapping(prior_result.get("state")).get("start_receipt"))
                    if not prior_result or not prior_fingerprint:
                        raise _receipt_integrity_error()
                    try:
                        prior_revision = int(prior_start["revision_before"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        raise _receipt_integrity_error() from None
                    if (
                        prior_revision != int(expected)
                        or text(prior_fingerprint.get("template_revision")) != text(requested_fingerprint.get("template_revision"))
                        or text(prior_fingerprint.get("world_revision")) != text(requested_fingerprint.get("world_revision"))
                        or text(prior_fingerprint.get("intensity")) != text(requested_fingerprint.get("intensity"))
                    ):
                        raise WebRouteError(409, "tactical.idempotency_conflict", "该防重复凭证对应另一份冲突启动请求。", "请查询原回执，或为新模板生成新的防重复凭证。")
                    result = {**prior_result, "replayed": True}
                else:
                    start_state = await build_tactical_start_state(
                        database, session_id, template, world, frozen_revision,
                        intensity_id=text(body.get("intensity")), request_key=request_key,
                    )
                    archive_current = bool(current_state and _is_tactical_terminal(current_state))
                    result = await database.put_gameplay_state(
                        session_id, module_id, "active", start_state,
                        expected_revision=int(expected), actor_id=text(principal.get("username")),
                        idempotency_key=request_key, intent=semantic_intent,
                        archive_current=archive_current,
                    )
                data = {"label": "战术冲突", "items": [{"revision": int(result.get("revision") or 0), "tactical_receipt": {"intent": semantic_intent, "phase_after": "setup", "replayed": bool(result.get("replayed"))}}], "count": 1}
                revision = int(result.get("revision") or 0)
            elif semantic_intent in tactical_host_intents:
                if not can_manage:
                    raise forbidden(
                        "只有当前副本主持人可以推进、纠错或结束战术冲突。",
                        recovery="请保留行动草稿并等待主持人处理。",
                    )
                expected = to_int(body.get("expected_revision"))
                request_key = _request_key(idempotency_key or body.get("idempotency_key"))
                replay_source = await _persistent_replay_source(
                    database,
                    session_id=session_id,
                    module_id=module_id,
                    semantic_intent=semantic_intent,
                    request_key=request_key,
                    expected_revision=expected,
                )
                if replay_source:
                    if semantic_intent == "tactical.phase.advance":
                        validator = lambda state: advance_tactical_phase(
                            state, idempotency_key=request_key, reason=body.get("reason", ""),
                        )
                    elif semantic_intent == "tactical.correction.apply":
                        validator = lambda state: apply_tactical_correction(
                            state,
                            mapping(body.get("correction")),
                            idempotency_key=request_key,
                            reason=text(body.get("reason")),
                        )
                    else:
                        validator = lambda state: end_conflict(
                            state,
                            outcome=text(body.get("outcome")),
                            idempotency_key=request_key,
                            reason=text(body.get("reason")),
                        )
                    result, receipt, revision = _finish_persistent_replay(
                        module_id, replay_source, validator,
                    )
                else:
                    if not current_state:
                        raise WebRouteError(
                            409, "tactical.not_started", "当前副本没有活动战术冲突。",
                            "返回跑团现场选择其他挑战，或由主持人开始冲突。",
                        )
                    if expected is None or (
                        expected != current_revision
                        and not _has_idempotency_receipt(current_state, request_key)
                    ):
                        raise WebRouteError(
                            409, "tactical.revision_conflict", "战况已更新，本次主持操作没有覆盖新状态。",
                            "请刷新战况后重新确认；相同防重复凭证可安全查询原回执。",
                        )
                    if semantic_intent == "tactical.phase.advance":
                        next_state, receipt = advance_tactical_phase(
                            current_state, idempotency_key=request_key, reason=body.get("reason", ""),
                        )
                    elif semantic_intent == "tactical.correction.apply":
                        next_state, receipt = apply_tactical_correction(
                            current_state, mapping(body.get("correction")),
                            idempotency_key=request_key, reason=text(body.get("reason")),
                        )
                    else:
                        next_state, receipt = end_conflict(
                            current_state, outcome=text(body.get("outcome")),
                            idempotency_key=request_key, reason=text(body.get("reason")),
                        )
                    result = await database.put_gameplay_state(
                        session_id, module_id, "active", next_state,
                        expected_revision=int(expected), actor_id=text(principal.get("username")),
                        idempotency_key=request_key, intent=semantic_intent,
                    )
                data = {"label": MODULE_LABELS.get(module_id, "战术冲突"), "items": [{"revision": int(result.get("revision") or 0), "tactical_receipt": _safe_receipt(receipt)}], "count": 1}
                if not replay_source:
                    revision = int(result.get("revision") or 0)
            else:
                principal_key = text(principal.get("username"))
                write_intents = {
                    "tactical.action.commit",
                    "tactical.withdraw.commit",
                    "tactical.negotiate.commit",
                }
                expected = to_int(body.get("expected_revision")) if semantic_intent in write_intents else None
                request_key = (
                    _request_key(idempotency_key or body.get("idempotency_key"))
                    if semantic_intent in write_intents
                    else ""
                )
                replay_source = (
                    await _persistent_replay_source(
                        database,
                        session_id=session_id,
                        module_id=module_id,
                        semantic_intent=semantic_intent,
                        request_key=request_key,
                        expected_revision=expected,
                    )
                    if semantic_intent in write_intents
                    else {}
                )
                action_state = mapping(replay_source.get("state")) if replay_source else current_state
                if not action_state:
                    raise WebRouteError(
                        409, "tactical.not_started", "当前副本没有活动战术冲突。",
                        "返回跑团现场选择其他挑战，或由主持人开始冲突。",
                    )
                actor_key = await _canonical_actor_key(
                    database,
                    session_id,
                    principal,
                    action_state,
                    requested_actor=text(body.get("actor_key")),
                    can_manage=can_manage,
                )
                try:
                    draft = _tactical_request_draft(
                        action_state,
                        body,
                        semantic_intent=semantic_intent,
                        actor_key=actor_key,
                        role=role,
                        principal=principal,
                        session_id=session_id,
                    )
                except (ValueError, WebRouteError):
                    if replay_source:
                        raise _idempotency_conflict(module_id) from None
                    raise
                if semantic_intent == "tactical.action.draft":
                    checked_draft = validate_draft(draft)
                    data = {"label": "战术行动", "items": [{"draft": _safe_draft(checked_draft), "written": False}], "count": 1}
                    revision = current_revision
                elif semantic_intent == "tactical.action.preview":
                    tactical_result = preview_tactical(current_state, draft)
                    data = {"label": "战术行动", "items": [{"draft": _safe_draft(mapping(tactical_result.get("draft"))), "known_effects": tactical_result.get("known_effects") or [], "cost": tactical_result.get("cost") or {}, "boundary": tactical_result.get("boundary"), "written": False}], "count": 1}
                    revision = current_revision
                else:
                    if replay_source:
                        result, tactical_receipt, revision = _finish_persistent_replay(
                            module_id,
                            replay_source,
                            lambda state: commit_tactical(
                                state, draft, idempotency_key=request_key,
                            ),
                        )
                    else:
                        if expected is None or (
                            expected != current_revision
                            and not _has_idempotency_receipt(current_state, request_key)
                        ):
                            raise WebRouteError(
                                409, "tactical.revision_conflict", "战况已更新，系统没有提交旧草稿。",
                                "已保留草稿；请刷新战况、比较目标后重新确认。",
                            )
                        next_state, tactical_receipt = commit_tactical(
                            current_state, draft, idempotency_key=request_key,
                        )
                        result = await database.put_gameplay_state(
                            session_id, module_id, "active", next_state,
                            expected_revision=int(expected), actor_id=principal_key,
                            idempotency_key=request_key, intent=semantic_intent,
                        )
                        revision = int(result.get("revision") or 0)
                    data = {"label": "战术行动", "items": [{"revision": int(result.get("revision") or 0), "tactical_receipt": _safe_receipt(tactical_receipt)}], "count": 1}
        elif module_id == "challenge_engine" and semantic_intent in challenge_player_intents | challenge_host_intents:
            current_rows = await database.get_gameplay_states(session_id, module_id, viewer_role=role)
            current_state, current_revision = _active_item(current_rows)
            if semantic_intent == "challenge.start":
                if not can_manage:
                    raise forbidden("只有当前副本主持人可以开始挑战。", recovery="请等待主持人从当前世界的挑战模板开始。")
                request_key = _request_key(idempotency_key or body.get("idempotency_key"))
                prior_start = await _stored_receipt(database, session_id, module_id, request_key)
                if prior_start and text(prior_start.get("intent")) != semantic_intent:
                    raise WebRouteError(409, "challenge.idempotency_conflict", "该防重复凭证已用于另一项挑战操作。", "请查询原回执；不要用同一凭证提交不同操作。")
                if current_state and not _is_challenge_terminal(current_state) and not prior_start:
                    raise WebRouteError(409, "challenge.already_started", "当前已有活动挑战。", "请先结束当前挑战，不能覆盖进行中的回执。")
                expected = to_int(body.get("expected_revision"))
                if expected is None:
                    raise WebRouteError(400, "challenge.revision_required", "开始挑战缺少当前状态版本。", "请刷新挑战面板后重新选择模板。")
                template, world, frozen_revision = await _resolve_frozen_template(
                    database, session_id, principal, role,
                    module_id=module_id,
                    template_key=text(body.get("template_key")),
                    expected_world_revision=text(body.get("world_revision")),
                )
                requested_fingerprint = {
                    "template_revision": _template_revision(template),
                    "world_revision": frozen_revision,
                }
                if prior_start:
                    prior_result = mapping(prior_start.get("result"))
                    prior_fingerprint = mapping(mapping(prior_result.get("state")).get("start_receipt"))
                    if not prior_result or not prior_fingerprint:
                        raise _receipt_integrity_error()
                    try:
                        prior_revision = int(prior_start["revision_before"])
                    except (KeyError, TypeError, ValueError, OverflowError):
                        raise _receipt_integrity_error() from None
                    if (
                        prior_revision != int(expected)
                        or text(prior_fingerprint.get("template_revision")) != text(requested_fingerprint.get("template_revision"))
                        or text(prior_fingerprint.get("world_revision")) != text(requested_fingerprint.get("world_revision"))
                    ):
                        raise WebRouteError(409, "challenge.idempotency_conflict", "该防重复凭证对应另一份挑战启动请求。", "请查询原回执，或为新模板生成新的防重复凭证。")
                    result = {**prior_result, "replayed": True}
                else:
                    start_state = await build_challenge_start_state(
                        database, session_id, template, frozen_revision,
                        request_key=request_key, world=world,
                    )
                    archive_current = bool(current_state and _is_challenge_terminal(current_state))
                    result = await database.put_gameplay_state(
                        session_id, module_id, "active", start_state,
                        expected_revision=int(expected), actor_id=text(principal.get("username")),
                        idempotency_key=request_key, intent=semantic_intent,
                        archive_current=archive_current,
                    )
                data = {"label": "当前挑战", "items": [{"revision": int(result.get("revision") or 0), "challenge_receipt": {"intent": semantic_intent, "phase_after": "setup", "replayed": bool(result.get("replayed"))}}], "count": 1}
                revision = int(result.get("revision") or 0)
            elif semantic_intent in challenge_host_intents:
                request_key = _request_key(idempotency_key or body.get("idempotency_key"))
                if not can_manage:
                    raise forbidden("只有当前副本主持人可以推进或结束挑战。", recovery="请保留草稿并等待主持人处理。")
                expected = to_int(body.get("expected_revision"))
                replay_source = await _persistent_replay_source(
                    database,
                    session_id=session_id,
                    module_id=module_id,
                    semantic_intent=semantic_intent,
                    request_key=request_key,
                    expected_revision=expected,
                )
                if replay_source:
                    if semantic_intent == "challenge.phase.advance":
                        validator = lambda state: advance_challenge_phase(
                            state,
                            idempotency_key=request_key,
                            reason=text(body.get("reason")),
                        )
                    else:
                        validator = lambda state: end_challenge(
                            state,
                            outcome=text(body.get("outcome")),
                            reason=text(body.get("reason")),
                            idempotency_key=request_key,
                        )
                    result, receipt, revision = _finish_persistent_replay(
                        module_id, replay_source, validator,
                    )
                else:
                    if not current_state:
                        raise WebRouteError(409, "challenge.not_started", "当前副本没有活动挑战。", "请返回跑团现场选择其他行动，或由主持人开始挑战。")
                    if expected is None or (expected != current_revision and not _has_idempotency_receipt(current_state, request_key)):
                        raise WebRouteError(409, "challenge.revision_conflict", "挑战状态已更新，本次操作没有覆盖新状态。", "请刷新挑战后重新确认。")
                    if semantic_intent == "challenge.phase.advance":
                        next_state, receipt = advance_challenge_phase(current_state, idempotency_key=request_key, reason=text(body.get("reason")))
                    else:
                        next_state, receipt = end_challenge(current_state, outcome=text(body.get("outcome")), reason=text(body.get("reason")), idempotency_key=request_key)
                    result = await database.put_gameplay_state(session_id, module_id, "active", next_state, expected_revision=int(expected), actor_id=text(principal.get("username")), idempotency_key=request_key, intent=semantic_intent)
                    revision = int(result.get("revision") or 0)
                data = {"label": "当前挑战", "items": [{"revision": int(result.get("revision") or 0), "challenge_receipt": _safe_receipt(receipt)}], "count": 1}
            else:
                principal_key = text(principal.get("username"))
                write_intents = {
                    "challenge.action.commit",
                    "challenge.withdraw.commit",
                    "challenge.negotiate.commit",
                }
                expected = to_int(body.get("expected_revision")) if semantic_intent in write_intents else None
                request_key = (
                    _request_key(idempotency_key or body.get("idempotency_key"))
                    if semantic_intent in write_intents
                    else ""
                )
                replay_source = (
                    await _persistent_replay_source(
                        database,
                        session_id=session_id,
                        module_id=module_id,
                        semantic_intent=semantic_intent,
                        request_key=request_key,
                        expected_revision=expected,
                    )
                    if semantic_intent in write_intents
                    else {}
                )
                action_state = mapping(replay_source.get("state")) if replay_source else current_state
                if not action_state:
                    raise WebRouteError(409, "challenge.not_started", "当前副本没有活动挑战。", "请返回跑团现场选择其他行动，或由主持人开始挑战。")
                actor_key = await _canonical_actor_key(
                    database,
                    session_id,
                    principal,
                    action_state,
                    requested_actor=text(body.get("actor_key")),
                    can_manage=can_manage,
                )
                try:
                    draft = _challenge_request_draft(
                        body,
                        semantic_intent=semantic_intent,
                        actor_key=actor_key,
                    )
                except (ValueError, WebRouteError):
                    if replay_source:
                        raise _idempotency_conflict(module_id) from None
                    raise
                if semantic_intent == "challenge.action.draft":
                    data = {"label": "挑战行动", "items": [{"draft": _safe_draft(validate_challenge_draft(draft)), "written": False}], "count": 1}
                    revision = current_revision
                elif semantic_intent == "challenge.action.preview":
                    checked = preview_challenge(current_state, draft)
                    data = {"label": "挑战行动", "items": [{"draft": _safe_draft(mapping(checked.get("draft"))), "known_effects": checked.get("known_effects") or [], "boundary": checked.get("boundary"), "written": False}], "count": 1}
                    revision = current_revision
                else:
                    if replay_source:
                        result, receipt, revision = _finish_persistent_replay(
                            module_id,
                            replay_source,
                            lambda state: commit_challenge(
                                state, draft, idempotency_key=request_key,
                            ),
                        )
                    else:
                        if expected is None or (expected != current_revision and not _has_idempotency_receipt(current_state, request_key)):
                            raise WebRouteError(409, "challenge.revision_conflict", "挑战状态已更新，系统没有提交旧草稿。", "已保留草稿；请刷新挑战后重新确认。")
                        next_state, receipt = commit_challenge(current_state, draft, idempotency_key=request_key)
                        result = await database.put_gameplay_state(session_id, module_id, "active", next_state, expected_revision=int(expected), actor_id=principal_key, idempotency_key=request_key, intent=semantic_intent)
                        revision = int(result.get("revision") or 0)
                    data = {"label": "挑战行动", "items": [{"revision": int(result.get("revision") or 0), "challenge_receipt": _safe_receipt(receipt)}], "count": 1}
        elif module_id in {"challenge_engine", "tactical_conflict"}:
            raise bad_request(
                "该挑战或战术动作未注册。",
                recovery="请刷新跑团现场并使用当前 PageModel 提供的操作。",
            )
        else:
            raise bad_request(
                "该玩法动作未注册，普通业务接口不接受原始状态写入。",
                recovery="请刷新当前页面并使用服务端提供的语义操作；诊断恢复不在此接口执行。",
            )
    else:
        receipt_key = text(query_map.get("receipt_key") or query_map.get("idempotency_key"))
        receipt_reader = getattr(database, "get_gameplay_receipt", None)
        if receipt_key:
            receipt = await receipt_reader(session_id, module_id, receipt_key) if callable(receipt_reader) else None
            if not receipt:
                raise WebRouteError(
                    404,
                    "gameplay.receipt_not_found",
                    "没有找到这次玩法操作的回执。",
                    "请确认使用提交时的防重复凭证；不要自动重复写操作。",
                )
            result = mapping(receipt.get("result"))
            state = mapping(result.get("state"))
            embedded = list(state.get("locked_receipts") or state.get("receipts") or ())
            matched = next(
                (mapping(item) for item in reversed(embedded) if text(mapping(item).get("idempotency_key")) == receipt_key),
                {},
            )
            if not matched and text(mapping(state.get("start_receipt")).get("idempotency_key")) == receipt_key:
                matched = mapping(state.get("start_receipt"))
            safe_embedded = _safe_receipt(matched)
            safe_embedded["replayed"] = True
            revision = int(receipt.get("revision_after") or 0)
            data = {
                "label": "玩法操作回执",
                "items": [{
                    "intent": text(receipt.get("intent")),
                    "revision_before": int(receipt.get("revision_before") or 0),
                    "revision_after": revision,
                    "created_at": text(receipt.get("created_at")),
                    "receipt": safe_embedded,
                }],
                "count": 1,
            }
        else:
            raw_data = await database.get_gameplay_states(
                session_id,
                module_id,
                viewer_role=role,
            )
            data = _public_gameplay_rows(module_id, raw_data)
            revision = max(
                (int(item.get("revision") or 0) for item in raw_data.get("items") or []),
                default=0,
            )
    envelope = visual_envelope(
        kind="gameplay_runtime",
        data=data,
        revision=revision,
        summary={
            "label": MODULE_LABELS.get(
                module_id,
                SURFACE_LABELS.get(module_id, ("运行玩法", ""))[0],
            ),
            "summary": "显示当前副本中服务端裁剪后的真实状态、变化来源与下一步。",
            "count": int(data.get("count") or 0),
        },
        permissions={"can_view": True, "can_manage": can_manage},
        state="ready" if data.get("items") else "empty",
        readonly=not can_manage,
    )
    return {"status": 200, "body": envelope.to_dict()}


__all__ = [
    "MODULE_LABELS", "build_challenge_start_state",
    "build_tactical_start_state", "gameplay_runtime_view",
]
