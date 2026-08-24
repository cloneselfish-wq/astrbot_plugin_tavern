"""Host-independent AI companion intent selection and recovery service."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..choice_command import ChoiceCommand
from ..messaging import render_message_type


class DecisionProvider(Protocol):
    async def choose(self, context: Mapping[str, Any]) -> Mapping[str, Any]: ...


class PolicyDecisionProvider:
    """Deterministic local policy used when no external decision provider exists."""

    async def choose(self, context: Mapping[str, Any]) -> Mapping[str, Any]:
        policy = context.get("decision_policy")
        policy = dict(policy) if isinstance(policy, Mapping) else {}
        prefer = {str(item) for item in policy.get("prefer_tags", [])}
        avoid = {str(item) for item in policy.get("avoid_tags", [])}
        ranked: list[tuple[float, str, Mapping[str, Any]]] = []
        risk_score = {
            "safe": 5.0,
            "controlled": 3.0,
            "dangerous": 1.0,
            "desperate": -1.0,
            "lethal": -3.0,
        }
        for item in context.get("choices") or []:
            if not isinstance(item, Mapping):
                continue
            key = _choice_key(item.get("key"))
            if not key:
                continue
            tags = {str(tag) for tag in item.get("tags", [])}
            score = risk_score.get(
                str(item.get("risk") or "controlled").lower(),
                0.0,
            )
            score += 2.0 * len(tags & prefer)
            score -= 4.0 * len(tags & avoid)
            if str(item.get("resolution_kind") or "") == "check":
                score += 0.25
            ranked.append((score, key, item))
        if not ranked:
            raise ValueError("no legal choices")
        ranked.sort(key=lambda row: (-row[0], row[1]))
        _score, key, selected = ranked[0]
        return {
            "choice_key": key,
            "reason": (
                "这项行动符合她的公开职责与风险偏好，"
                "也能让队伍继续推进而不依赖隐藏信息。"
            ),
            "selected_text": str(selected.get("text") or ""),
        }


_PRIVATE_KEYS = {
    "private",
    "private_memory",
    "private_memories",
    "hidden",
    "dm",
    "dm_notes",
    "host_notes",
    "provider",
    "provider_config",
    "raw_world",
    "world_json",
    "database",
}


def _public_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public_tree(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_KEYS
            and not str(key).lower().endswith("_private")
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_public_tree(item) for item in value]
    return value


def _choice_key(value: object) -> str:
    return str(value or "").strip().upper()


@dataclass(slots=True)
class AiCompanionDecisionService:
    repository: Any
    providers: Sequence[DecisionProvider]
    submit_choice: Callable[[ChoiceCommand], Awaitable[Any]]
    deliver_notice: Callable[[Mapping[str, Any]], Awaitable[Any] | Any]

    async def decide(
        self,
        *,
        session_id: str,
        actor_ref: str,
        choice_set_id: str,
        choices: Sequence[Mapping[str, Any]],
        expected_session_revision: int,
        operation_id: str,
        idempotency_key: str,
        lease_owner: str,
        trace_id: str,
        world_state_view: Mapping[str, Any],
    ) -> dict[str, Any]:
        context = await self.repository.ai_companion_decision_context(
            session_id=session_id,
            actor_ref=actor_ref,
        )
        claim = await self.repository.claim_ai_decision(
            session_id=session_id,
            actor_ref=actor_ref,
            choice_set_id=choice_set_id,
            operation_id=operation_id,
            expected_actor_revision=int(context["revision"]),
            expected_session_revision=int(expected_session_revision),
            lease_owner=lease_owner,
            lease_seconds=90,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        if claim.get("replayed"):
            return claim
        legal = {
            _choice_key(item.get("key")): dict(item)
            for item in choices
            if isinstance(item, Mapping) and _choice_key(item.get("key"))
        }
        if not legal:
            return await self.repository.finish_ai_decision(
                operation_id=operation_id,
                lease_owner=lease_owner,
                decision={},
                public_projection={
                    "message": "当前没有可执行的合法选项。",
                },
                status="failed",
            )
        provider_context = _public_tree(
            {
                "schema": "tavern-ai-decision-request/1.0.0-rc10",
                "actor": context["actor"],
                "profile": context["profile"],
                "decision_policy": context["decision_policy"],
                "world_state_view": dict(world_state_view),
                "choices": list(legal.values()),
                "output_schema": {
                    "choice_key": "A|B|C|D",
                    "reason": "public natural language",
                },
            }
        )
        selected: dict[str, Any] | None = None
        provider_failures = 0
        for provider in self.providers[:2]:
            try:
                candidate = dict(await provider.choose(provider_context))
                key = _choice_key(candidate.get("choice_key"))
                if key not in legal:
                    raise ValueError("invalid choice")
                selected = {
                    "choice_key": key,
                    "reason": str(candidate.get("reason") or "").strip(),
                    "source": "provider",
                }
                break
            except Exception:
                provider_failures += 1
        if selected is None:
            safe = next(
                (
                    item
                    for item in legal.values()
                    if str(item.get("risk") or "").lower()
                    in {"safe", "安全"}
                    or str(item.get("resolution_kind") or "")
                    in {"no_check", "safe"}
                ),
                next(iter(legal.values())),
            )
            selected = {
                "choice_key": _choice_key(safe.get("key")),
                "reason": "模型暂时不可用，系统按角色公开策略选择了安全可继续的行动。",
                "source": "safe_fallback",
            }
        choice = legal[selected["choice_key"]]
        actor_name = str(context["actor"].get("display_name") or "AI 队友")
        public = {
            "actor": actor_name,
            "choice": str(choice.get("text") or selected["choice_key"]),
            "reason": selected["reason"],
            "provider_failures": provider_failures,
            "trace_id": trace_id,
        }
        mode = str(context.get("mode") or "confirm")
        if mode == "confirm":
            return await self.repository.finish_ai_decision(
                operation_id=operation_id,
                lease_owner=lease_owner,
                decision=selected,
                public_projection=public,
                status="awaiting_confirmation",
            )
        latest = await self.repository.ai_companion_decision_context(
            session_id=session_id,
            actor_ref=actor_ref,
        )
        if (
            str(latest.get("mode") or "") == "paused"
            or int(latest.get("session_revision") or 0)
            != int(expected_session_revision)
        ):
            return await self.repository.finish_ai_decision(
                operation_id=operation_id,
                lease_owner=lease_owner,
                decision=selected,
                public_projection={
                    **public,
                    "message": "AI 队友已暂停或副本已更新，迟到结果未提交。",
                },
                status="discarded",
            )
        command = ChoiceCommand(
            session_id=session_id,
            actor_ref=actor_ref,
            choice_set_id=choice_set_id,
            choice_key=selected["choice_key"],
            expected_session_revision=expected_session_revision,
            idempotency_key=idempotency_key,
        )
        try:
            await self.submit_choice(command)
        except Exception:
            return await self.repository.finish_ai_decision(
                operation_id=operation_id,
                lease_owner=lease_owner,
                decision=selected,
                public_projection={
                    **public,
                    "message": render_message_type(
                        "ai_companion.failed",
                        {
                            "actor": actor_name,
                            "reason": "副本状态在提交前发生变化，原选择已经失效。",
                        },
                        audience="dm",
                    ),
                },
                status="failed",
            )
        receipt = await self.repository.finish_ai_decision(
            operation_id=operation_id,
            lease_owner=lease_owner,
            decision=selected,
            public_projection=public,
            status="submitted",
        )
        if receipt.get("status") == "submitted":
            notice = {
                "kind": "ai_actor",
                "dedupe_key": (
                    f"ai-choice:{session_id}:{actor_ref}:"
                    f"{choice_set_id}"
                ),
                "text": render_message_type(
                    "ai_companion.submitted",
                    {
                        "actor": actor_name,
                        "choice": public["choice"],
                        "reason": public["reason"],
                    },
                    audience="public",
                ),
            }
            try:
                delivered = self.deliver_notice(notice)
                if inspect.isawaitable(delivered):
                    await delivered
            except Exception:
                pass
        return receipt


__all__ = [
    "AiCompanionDecisionService",
    "DecisionProvider",
    "PolicyDecisionProvider",
]
