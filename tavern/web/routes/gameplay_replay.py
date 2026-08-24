"""Persistent gameplay receipt replay and canonical action identity helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...challenge_runtime import draft_from_text as challenge_draft_from_text
from ...tactical_runtime import draft_from_text
from ...visualization.keys import OpaqueKeyFactory
from ..errors import forbidden
from . import WebRouteError, mapping, text
from .sessions import resolve_viewer_participant


def _choice_label(value: Mapping[str, Any], fallback: str) -> str:
    return text(value.get("label") or value.get("name") or value.get("title"), fallback)


async def _stored_receipt(
    database: Any,
    session_id: str,
    module_id: str,
    request_key: str,
) -> dict[str, Any]:
    reader = getattr(database, "get_gameplay_receipt", None)
    if not callable(reader) or not request_key:
        return {}
    return mapping(await reader(session_id, module_id, request_key))


def _idempotency_conflict(module_id: str) -> WebRouteError:
    label = "战术" if module_id == "tactical_conflict" else "挑战"
    code = "tactical.idempotency_conflict" if module_id == "tactical_conflict" else "challenge.idempotency_conflict"
    return WebRouteError(
        409,
        code,
        f"该防重复凭证已用于另一项{label}操作或另一份输入。",
        "请查询原回执；修改动作、版本或输入时必须使用新的防重复凭证。",
    )


def _receipt_integrity_error() -> WebRouteError:
    return WebRouteError(
        409,
        "gameplay.receipt_integrity_error",
        "原玩法操作回执不完整，系统没有重复执行该操作。",
        "请停止自动重试并联系管理员核验持久回执；不要更换版本后复用原凭证。",
    )


def _embedded_receipt(state: Mapping[str, Any], request_key: str) -> dict[str, Any]:
    token = text(request_key)
    for collection in (state.get("locked_receipts") or (), state.get("receipts") or ()):
        matched = next(
            (
                mapping(item)
                for item in reversed(list(collection))
                if isinstance(item, Mapping)
                and text(mapping(item).get("idempotency_key")) == token
            ),
            {},
        )
        if matched:
            return matched
    start_receipt = mapping(state.get("start_receipt"))
    return start_receipt if text(start_receipt.get("idempotency_key")) == token else {}


async def _persistent_replay_source(
    database: Any,
    *,
    session_id: str,
    module_id: str,
    semantic_intent: str,
    request_key: str,
    expected_revision: int | None,
) -> dict[str, Any]:
    stored = await _stored_receipt(database, session_id, module_id, request_key)
    if not stored:
        return {}
    try:
        revision_before = int(stored["revision_before"])
        revision_after = int(stored["revision_after"])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise _receipt_integrity_error() from None
    if (
        text(stored.get("intent")) != semantic_intent
        or expected_revision is None
        or revision_before != int(expected_revision)
    ):
        raise _idempotency_conflict(module_id)
    result = mapping(stored.get("result"))
    state = mapping(result.get("state"))
    receipt = _embedded_receipt(state, request_key)
    if (
        not result
        or not state
        or not receipt
        or not text(receipt.get("receipt_id"))
        or not text(receipt.get("request_sha256"))
        or text(receipt.get("idempotency_key")) != request_key
        or revision_after <= int(expected_revision)
    ):
        raise _receipt_integrity_error()
    return {
        "result": result,
        "state": state,
        "receipt": receipt,
        "revision_after": revision_after,
    }


def _finish_persistent_replay(
    module_id: str,
    source: Mapping[str, Any],
    validator: Any,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    original = mapping(source.get("receipt"))
    try:
        _state, checked = validator(mapping(source.get("state")))
    except (ValueError, WebRouteError):
        raise _idempotency_conflict(module_id) from None
    except (KeyError, TypeError, OverflowError):
        raise _receipt_integrity_error() from None
    verified = mapping(checked)
    if (
        not bool(verified.get("replayed"))
        or text(verified.get("receipt_id")) != text(original.get("receipt_id"))
        or text(verified.get("request_sha256")) != text(original.get("request_sha256"))
    ):
        raise _receipt_integrity_error()
    revision_after = int(source.get("revision_after") or 0)
    result = {
        **mapping(source.get("result")),
        "revision": revision_after,
        "replayed": True,
    }
    receipt = {**original, "replayed": True}
    return result, receipt, revision_after


async def _canonical_actor_key(
    database: Any,
    session_id: str,
    principal: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    requested_actor: str = "",
    can_manage: bool = False,
) -> str:
    participants = mapping(state.get("participants"))
    requested = text(requested_actor)
    if can_manage and requested:
        if requested in participants:
            return requested
        raise WebRouteError(
            409,
            "gameplay.actor_not_in_runtime",
            "所选行动者不在本场玩法冻结的参与者中。",
            "请刷新当前玩法并从服务端提供的参与者选项中重新选择。",
        )

    participant = await resolve_viewer_participant(
        database,
        session_id,
        text(principal.get("username")),
        text(principal.get("participant_ref")),
    )
    if participant is None:
        if can_manage and text(principal.get("username")) in participants:
            return text(principal.get("username"))
        raise forbidden(
            "当前账号没有可用于本场玩法的参与者身份。",
            recovery="请确认账号仍绑定当前副本成员；主持操作请使用主持入口。",
        )

    participant_ref = text(participant.get("id"))
    matches = [
        text(key)
        for key, value in participants.items()
        if participant_ref
        and text(mapping(value).get("participant_ref")) == participant_ref
    ]
    if len(matches) > 1:
        raise _receipt_integrity_error()
    if matches:
        actor_key = matches[0]
    else:
        roster_keys = [
            text(participant.get(name))
            for name in ("group_user_id", "user_id", "private_user_id")
            if text(participant.get(name))
        ]
        actor_key = next((key for key in roster_keys if key in participants), "")
    if not actor_key:
        raise WebRouteError(
            409,
            "gameplay.actor_not_in_runtime",
            "你不在本场玩法冻结的参与者中。",
            "请等待当前玩法结束，或由主持人重新开始包含你的挑战或冲突。",
        )
    if requested and requested != actor_key:
        raise forbidden(
            "玩家只能提交自己的玩法行动。",
            recovery="系统已按当前成员绑定识别行动者；请移除客户端行动者字段后重试。",
        )
    return actor_key


def _decode_tactical_draft(
    state: Mapping[str, Any],
    value: Mapping[str, Any],
    *,
    role: str,
    principal: Mapping[str, Any],
    session_id: str,
    actor_key: str,
) -> dict[str, Any]:
    """Resolve principal-scoped UI handles without accepting stable refs from the browser."""

    draft = dict(value)
    keys = OpaqueKeyFactory(scope=f"console:{role}:{text(principal.get('username'))}:{session_id}")

    def rows(source: Any) -> list[tuple[str, dict[str, Any]]]:
        if isinstance(source, Mapping):
            return [(text(key), mapping(item)) for key, item in source.items() if isinstance(item, Mapping)]
        return [("", mapping(item)) for item in source or () if isinstance(item, Mapping)]

    def resolve(
        handle: object,
        *,
        kind: str,
        source: Any,
        ref_names: tuple[str, ...],
        allow_missing: bool = False,
    ) -> str:
        token = text(handle)
        if not token:
            return ""
        for index, (source_ref, item) in enumerate(rows(source)):
            label = _choice_label(item, "可选目标")
            if keys.key(kind, f"{index}:{label}") == token:
                return next((text(item.get(name)) for name in ref_names if text(item.get(name))), source_ref)
        if allow_missing:
            return ""
        raise WebRouteError(
            409,
            "tactical.choice_key_invalid",
            "所选战术目标已失效或不属于当前冲突。",
            "系统保留了说明；请刷新战况并重新选择目标。",
        )

    supplied_stable = any(
        draft.get(name)
        for name in ("target_refs", "zone_ref", "objective_ref", "capability_or_item_ref")
    )
    if supplied_stable:
        raise WebRouteError(
            400,
            "tactical.opaque_key_required",
            "战术页面不能提交内部引用。",
            "请刷新战况并从当前可见选项中选择目标。",
        )
    target_key = draft.pop("target_key", "")
    zone_key = draft.pop("zone_key", "")
    objective_key = draft.pop("objective_key", "")
    capability_key = draft.pop("capability_or_item_key", "")
    if target_key:
        target_ref = resolve(
            target_key, kind="tactical-threat",
            source=state.get("known_threats") or state.get("threats"),
            ref_names=("threat_id", "id", "ref"),
            allow_missing=True,
        )
        if not target_ref:
            target_ref = resolve(
                target_key, kind="tactical-actor", source=state.get("participants"),
                ref_names=("actor_key", "user_id", "group_user_id", "id", "ref"),
            )
        draft["target_refs"] = [target_ref]
    if zone_key:
        draft["zone_ref"] = resolve(
            zone_key, kind="tactical-zone", source=state.get("zones"),
            ref_names=("zone_id", "zone_ref", "id", "ref"),
        )
    if objective_key:
        draft["objective_ref"] = resolve(
            objective_key, kind="tactical-objective", source=state.get("objectives"),
            ref_names=("id", "objective_id", "objective_ref", "ref"),
        )
    if capability_key:
        owner_ref = text(actor_key)
        capabilities = [
            item for item in (state.get("available_capabilities") or ())
            if text(mapping(item).get("owner_ref")) == owner_ref
        ]
        items = [
            item for item in (state.get("available_items") or ())
            if text(mapping(item).get("owner_ref")) == owner_ref
        ]
        try:
            draft["capability_or_item_ref"] = resolve(
                capability_key, kind="tactical-capability", source=capabilities,
                ref_names=("id", "capability_id", "ref"),
            )
        except WebRouteError:
            draft["capability_or_item_ref"] = resolve(
                capability_key, kind="tactical-item", source=items,
                ref_names=("id", "item_id", "ref"),
            )
    return draft


def _tactical_request_draft(
    state: Mapping[str, Any],
    body: Mapping[str, Any],
    *,
    semantic_intent: str,
    actor_key: str,
    role: str,
    principal: Mapping[str, Any],
    session_id: str,
) -> dict[str, Any]:
    draft = mapping(body.get("draft"))
    if draft:
        draft = _decode_tactical_draft(
            state,
            draft,
            role=role,
            principal=principal,
            session_id=session_id,
            actor_key=actor_key,
        )
    else:
        draft = draft_from_text(body.get("text"), actor_key=actor_key)
    draft["actor_key"] = actor_key
    if semantic_intent == "tactical.withdraw.commit":
        draft["action_kind"] = "retreat"
    elif semantic_intent == "tactical.negotiate.commit":
        draft["action_kind"] = "parley"
    return draft


def _challenge_request_draft(
    body: Mapping[str, Any],
    *,
    semantic_intent: str,
    actor_key: str,
) -> dict[str, Any]:
    draft = mapping(body.get("draft")) or challenge_draft_from_text(
        body.get("text"),
        actor_key=actor_key,
        action_kind=text(body.get("action_kind") or "act"),
    )
    draft["actor_key"] = actor_key
    if semantic_intent == "challenge.withdraw.commit":
        draft["action_kind"] = "withdraw"
    elif semantic_intent == "challenge.negotiate.commit":
        draft["action_kind"] = "negotiate"
    return draft


def _active_item(rows: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    item = next(
        (
            mapping(value)
            for value in rows.get("items") or ()
            if mapping(value).get("state_key") == "active"
        ),
        {},
    )
    return mapping(item.get("state")), int(item.get("revision") or 0)


def _has_idempotency_receipt(state: Mapping[str, Any], key: str) -> bool:
    token = text(key)
    if not token:
        return False
    return any(
        text(item.get("idempotency_key")) == token
        for collection in (state.get("locked_receipts") or (), state.get("receipts") or ())
        for item in collection
        if isinstance(item, Mapping)
    )


def _safe_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "receipt_id", "action_kind", "result_band", "outcome", "effects", "progress_before",
        "progress_after", "phase_before", "phase_after", "round_before",
        "round_after", "field", "changes", "reason", "replayed", "intent",
        "roll", "rolls", "advantage", "advantage_source_count",
        "disadvantage_source_count", "modifier", "difficulty", "total",
        "replaced", "resolved_receipts",
        "stat_ref", "stat_label", "capability_mode",
        "capability_mechanical_change", "capability_resource_change_count",
        "capability_effect_change_count",
        "element_ref", "element_target_label", "element_layers_before",
        "element_layers_after", "element_matched_reaction",
        "element_matched_interaction", "element_public_copy",
        "element_settlement_order",
    }
    safe = {key: value for key, value in receipt.items() if key in allowed and key != "resolved_receipts"}
    if receipt.get("resolved_receipts"):
        safe["resolved_receipts"] = [
            _safe_receipt(mapping(item)) for item in receipt.get("resolved_receipts") or ()
            if isinstance(item, Mapping)
        ]
    return safe


def _safe_draft(draft: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_kind": text(draft.get("action_kind")),
        "description": text(draft.get("description")),
    }


__all__ = [name for name in globals() if name.startswith("_")]
