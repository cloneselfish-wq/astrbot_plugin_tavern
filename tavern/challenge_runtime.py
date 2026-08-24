"""Deterministic RC10 challenge drafts, progress, phases, and receipts."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


CHALLENGE_MODES = frozenset(
    {
        "investigation", "social", "chase", "rescue", "hazard",
        "infiltration", "ritual", "choice", "tactical",
    }
)
ACTION_KINDS = frozenset(
    {"act", "investigate", "persuade", "pursue", "rescue", "mitigate", "infiltrate", "perform", "choose", "withdraw", "negotiate"}
)
PHASE_TRANSITIONS = {
    "setup": "declare",
    "declare": "locked",
    "locked": "resolve",
    "resolve": "settle",
    "settle": "declare",
}
TERMINAL_OUTCOMES = frozenset(
    {"success", "partial", "failure_forward", "retreat", "negotiated", "aborted"}
)


def _clone(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(value), ensure_ascii=False))


def _event(kind: str, label: str, summary: str, **details: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "summary": summary,
        "visibility": "party",
        "details": {key: value for key, value in details.items() if isinstance(value, (str, int, float, bool))},
    }


def _restore_replay(current: dict[str, Any], receipt: Mapping[str, Any]) -> None:
    events = [dict(item) for item in receipt.get("semantic_events") or () if isinstance(item, Mapping)]
    effects = [dict(item) for item in receipt.get("effect_updates") or () if isinstance(item, Mapping)]
    if events:
        current["_semantic_events"] = events
    if effects:
        current["_effect_updates"] = effects


def draft_from_text(text: object, *, actor_key: str, action_kind: str = "act") -> dict[str, Any]:
    description = str(text or "").strip()
    if not description:
        raise ValueError("挑战行动说明不能为空")
    kind = str(action_kind or "act").strip()
    if kind not in ACTION_KINDS:
        hints = {
            "调查": "investigate", "核验": "investigate", "说服": "persuade",
            "追赶": "pursue", "救援": "rescue", "加固": "mitigate",
            "潜入": "infiltrate", "仪式": "perform", "选择": "choose",
            "退出": "withdraw", "撤退": "withdraw", "谈判": "negotiate",
        }
        kind = next((value for word, value in hints.items() if word in description), "act")
    return {
        "actor_key": str(actor_key or "").strip(),
        "action_kind": kind,
        "description": description[:500],
        "target_key": "",
    }


def validate_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    draft = dict(value)
    actor = str(draft.get("actor_key") or "").strip()
    kind = str(draft.get("action_kind") or "").strip()
    if not actor or kind not in ACTION_KINDS:
        raise ValueError("挑战草稿缺少行动者或使用了未知行动")
    draft["actor_key"] = actor
    draft["action_kind"] = kind
    draft["description"] = str(draft.get("description") or "").strip()[:500]
    draft["target_key"] = str(draft.get("target_key") or "").strip()[:160]
    return draft


def preview(state: Mapping[str, Any], draft: Mapping[str, Any]) -> dict[str, Any]:
    current = dict(state)
    normalized = validate_draft(draft)
    participants = current.get("participants")
    if isinstance(participants, Mapping) and normalized["actor_key"] not in participants:
        raise ValueError("行动者不属于本场挑战的冻结参与者")
    if str(current.get("phase") or "setup") != "declare":
        raise ValueError("当前挑战阶段不能修改行动；系统保留草稿")
    mode = str(current.get("mode") or "")
    if mode not in CHALLENGE_MODES:
        raise ValueError("当前挑战 mode 未注册")
    objective = str(current.get("objective") or "").strip()
    if not objective:
        raise ValueError("当前挑战没有可执行的公开目标")
    return {
        "draft": normalized,
        "known_effects": [
            "成功时推进公开目标",
            "未成功时应用作者声明的失败推进，不删除主线",
        ],
        "boundary": "预览不写状态；最终检定与效果只在确认事务中锁定。",
    }


def _input_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _prior_receipt(state: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    return next(
        (dict(item) for item in state.get("receipts") or () if item.get("idempotency_key") == key),
        None,
    )


def _outcome_effects(state: Mapping[str, Any], outcome: str) -> list[dict[str, Any]]:
    source = state.get("outcome_effects") or []
    if isinstance(source, Mapping):
        source = source.get(outcome) or source.get("default") or []
    return [dict(item) for item in source if isinstance(item, Mapping)][:16]


def _advance_reusable_effect_preimages(
    state: dict[str, Any],
    outcome: str,
    applied: list[dict[str, Any]],
) -> None:
    """Advance only the stored next-use CAS preimage for a non-terminal effect.

    ``applied`` keeps the revision that the repository must compare now.  The
    frozen plan retained in the active challenge moves to the revision that
    will exist after this transaction, so another failure-forward can be
    attempted without disabling optimistic concurrency.
    """

    plans = state.get("outcome_effects")
    if not isinstance(plans, Mapping):
        return
    updated_plans = _clone(plans)
    branch = updated_plans.get(outcome)
    if not isinstance(branch, list):
        return
    revisions = {
        (str(item.get("module_id") or ""), str(item.get("state_key") or "")):
        int(item["expected_revision"]) + 1
        for item in applied
        if isinstance(item, Mapping) and item.get("expected_revision") is not None
    }
    for item in branch:
        if not isinstance(item, dict):
            continue
        identity = (str(item.get("module_id") or ""), str(item.get("state_key") or ""))
        if identity in revisions:
            item["expected_revision"] = revisions[identity]
    state["outcome_effects"] = updated_plans


def commit(
    state: Mapping[str, Any],
    draft: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    # Repository writes strip these transaction-only fields before persistence,
    # but pure runtime callers may feed the returned state straight back in.
    # Never let a previous commit's effects/events leak into the next receipt.
    current.pop("_effect_updates", None)
    current.pop("_semantic_events", None)
    normalized = validate_draft(draft)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("提交挑战行动需要防重复凭证")
    digest = _input_hash(normalized)
    prior = _prior_receipt(current, key)
    if prior is not None:
        if prior.get("input_sha256") != digest or prior.get("intent") != "challenge.action.commit":
            raise ValueError("相同防重复凭证已用于另一项挑战行动")
        _restore_replay(current, prior)
        return current, {**prior, "replayed": True}
    preview(current, normalized)
    roll = int(hashlib.sha256(f"{key}:{digest}".encode()).hexdigest()[:8], 16) % 20 + 1
    band = "success" if roll >= 11 else "partial" if roll >= 6 else "failure_forward"
    progress_before = max(0, int(current.get("progress") or 0))
    target = max(1, int(current.get("target") or 3))
    progress_after = min(target, progress_before + (1 if band in {"success", "partial"} else 0))
    current["progress"] = progress_after
    outcome = "active"
    if normalized["action_kind"] == "withdraw":
        outcome = "retreat"
    elif normalized["action_kind"] == "negotiate" and band != "failure_forward":
        outcome = "negotiated"
    elif progress_after >= target:
        outcome = "success" if band == "success" else "partial"
    if outcome != "active":
        current["phase"] = "ended"
        current["status"] = outcome
        current["outcome"] = outcome
        current["_effect_updates"] = _outcome_effects(current, outcome)
    elif band == "failure_forward":
        current.setdefault("failure_events", []).append(
            str(current.get("failure_forward") or "出现新的风险，但主线仍可继续。")[:300]
        )
        # A failed check keeps the challenge active, but its declared cost is
        # still a real outcome and must be committed in the same transaction.
        failure_effects = _outcome_effects(current, "failure_forward")
        if failure_effects:
            current["_effect_updates"] = failure_effects
            _advance_reusable_effect_preimages(
                current, "failure_forward", failure_effects,
            )
    receipt = {
        "receipt_id": "challenge_" + hashlib.sha256(f"{key}:{digest}".encode()).hexdigest()[:20],
        "idempotency_key": key,
        "input_sha256": digest,
        "request_sha256": digest,
        "intent": "challenge.action.commit",
        "actor_key": normalized["actor_key"],
        "action_kind": normalized["action_kind"],
        "roll": roll,
        "result_band": band,
        "progress_before": progress_before,
        "progress_after": progress_after,
        "outcome": outcome,
        "effects": [str(item.get("label") or "关联状态已更新")[:120] for item in current.get("_effect_updates") or ()],
        "effect_updates": [dict(item) for item in current.get("_effect_updates") or () if isinstance(item, Mapping)],
        "semantic_events": [
            _event(
                "challenge_action_submitted",
                "挑战行动已结算",
                "行动检定、进度和失败推进已写入同一回执。",
                actor=normalized["actor_key"], result_band=band, outcome=outcome,
            )
        ],
        "replayed": False,
    }
    current.setdefault("receipts", []).append(receipt)
    current["receipts"] = current["receipts"][-100:]
    current["_semantic_events"] = list(receipt["semantic_events"])
    return current, receipt


def advance_phase(
    state: Mapping[str, Any],
    *,
    idempotency_key: str,
    reason: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValueError("主持挑战推进需要防重复凭证")
    prior = _prior_receipt(current, key)
    explanation = str(reason or "").strip()[:300]
    digest = _input_hash({"intent": "challenge.phase.advance", "reason": explanation})
    if prior is not None:
        if prior.get("intent") != "challenge.phase.advance" or prior.get("request_sha256") != digest:
            raise ValueError("相同防重复凭证已用于另一项挑战操作")
        _restore_replay(current, prior)
        return current, {**prior, "replayed": True}
    before = str(current.get("phase") or "setup")
    if before == "ended":
        raise ValueError("挑战已经结束，不能继续推进")
    after = PHASE_TRANSITIONS.get(before)
    if not after:
        raise ValueError("当前挑战阶段没有已注册的推进路径")
    round_before = max(1, int(current.get("round") or 1))
    current["phase"] = after
    current["round"] = round_before + 1 if before == "settle" else round_before
    payload = {"phase_before": before, "phase_after": after, "reason": explanation}
    receipt = {
        "receipt_id": "challenge_" + hashlib.sha256(f"{key}:{digest}".encode()).hexdigest()[:20],
        "idempotency_key": key,
        "input_sha256": digest,
        "request_sha256": digest,
        "intent": "challenge.phase.advance",
        **payload,
        "semantic_events": [_event("challenge_phase_changed", "挑战阶段已推进", f"阶段由 {before} 推进到 {after}。", phase_before=before, phase_after=after)],
        "replayed": False,
    }
    current.setdefault("receipts", []).append(receipt)
    current["receipts"] = current["receipts"][-100:]
    current["_semantic_events"] = list(receipt["semantic_events"])
    return current, receipt


def end_challenge(
    state: Mapping[str, Any],
    *,
    outcome: str,
    reason: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = _clone(state)
    terminal = str(outcome or "").strip()
    explanation = str(reason or "").strip()[:500]
    key = str(idempotency_key or "").strip()
    if terminal not in TERMINAL_OUTCOMES:
        raise ValueError("挑战结束结果未注册")
    if not explanation or not key:
        raise ValueError("结束挑战需要结果依据和防重复凭证")
    prior = _prior_receipt(current, key)
    request_digest = _input_hash({"outcome": terminal, "reason": explanation})
    if prior is not None:
        if prior.get("intent") != "challenge.end" or prior.get("request_sha256") != request_digest:
            raise ValueError("相同防重复凭证已用于另一项挑战操作")
        _restore_replay(current, prior)
        return current, {**prior, "replayed": True}
    before = str(current.get("phase") or "setup")
    if before == "ended" or str(current.get("status") or current.get("outcome") or "") in TERMINAL_OUTCOMES:
        raise ValueError("挑战已经结束，不能用新凭证改写结果")
    current["phase"] = "ended"
    current["status"] = terminal
    current["outcome"] = terminal
    current["_effect_updates"] = _outcome_effects(current, terminal)
    payload = {"phase_before": before, "outcome": terminal, "reason": explanation}
    digest = _input_hash(payload)
    receipt = {
        "receipt_id": "challenge_" + hashlib.sha256(f"{key}:{digest}".encode()).hexdigest()[:20],
        "idempotency_key": key,
        "input_sha256": digest,
        "request_sha256": request_digest,
        "intent": "challenge.end",
        **payload,
        "effects": [str(item.get("label") or "关联状态已更新")[:120] for item in current.get("_effect_updates") or ()],
        "effect_updates": [dict(item) for item in current.get("_effect_updates") or () if isinstance(item, Mapping)],
        "semantic_events": [_event("challenge_ended", "挑战已结束", explanation, outcome=terminal)],
        "replayed": False,
    }
    current.setdefault("receipts", []).append(receipt)
    current["receipts"] = current["receipts"][-100:]
    current["_semantic_events"] = list(receipt["semantic_events"])
    return current, receipt


__all__ = [
    "ACTION_KINDS", "CHALLENGE_MODES", "PHASE_TRANSITIONS", "TERMINAL_OUTCOMES",
    "advance_phase", "commit", "draft_from_text", "end_challenge", "preview",
    "validate_draft",
]
