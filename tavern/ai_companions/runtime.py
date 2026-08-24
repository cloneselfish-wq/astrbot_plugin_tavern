"""Production scheduler for AI-owned turns.

The runner is host-independent: it reads the public dashboard projection,
submits through ``ChoiceCommand`` and writes notices to the durable delivery
outbox. Platform delivery remains the host adapter's responsibility.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from ..choice_command import ChoiceCommand
from ..projections.session_timeline import session_dashboard
from ..messaging import render_message_type
from .service import (
    AiCompanionDecisionService,
    DecisionProvider,
    PolicyDecisionProvider,
)


@dataclass(slots=True)
class AiCompanionTurnRunner:
    database: Any
    engine: Any
    providers: Sequence[DecisionProvider] = ()
    lease_owner: str = "inprocess-ai-turn-runner"

    async def run_due(
        self,
        session_id: str,
        *,
        max_steps: int = 8,
    ) -> dict[str, Any]:
        limit = max(1, min(8, int(max_steps)))
        receipts: list[dict[str, Any]] = []
        stop_reason = "human_turn"
        for _index in range(limit):
            session = await self.database.get_session(session_id)
            if str(session.get("state") or "") != "running":
                stop_reason = "session_not_running"
                break
            choice_set = await self.database.active_choice_set(session_id)
            actor = (
                choice_set.get("actor")
                if isinstance(choice_set, dict)
                else None
            )
            if not isinstance(actor, dict) or str(
                actor.get("actor_kind") or ""
            ) != "ai_companion":
                stop_reason = (
                    "missing_choice"
                    if not choice_set
                    else "human_turn"
                )
                break
            actor_ref = str(actor.get("actor_ref") or "")
            choice_set_id = str(choice_set.get("id") or "")
            expected_revision = int(
                choice_set.get("session_revision") or 0
            )
            actor_context = (
                await self.database.ai_companion_decision_context(
                    session_id=session_id,
                    actor_ref=actor_ref,
                )
            )
            stable = (
                f"{session_id}:{choice_set_id}:{actor_ref}:"
                f"{expected_revision}:{actor_context.get('revision') or 0}"
            )
            digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()
            operation_id = f"ai-turn:{digest[:24]}"
            trace_id = digest[:8].upper()

            async def submit_choice(command):
                return await self.engine.process_choice_command(
                    command,
                    event=SimpleNamespace(
                        unified_msg_origin=str(
                            session.get("unified_origin") or ""
                        ),
                        message_id="",
                    ),
                )

            async def deliver_notice(notice):
                await self.database.queue_delivery(
                    session_id=session_id,
                    origin=str(session.get("unified_origin") or ""),
                    kind=str(notice.get("kind") or "ai_actor"),
                    text=str(notice.get("text") or ""),
                    reason="ai_companion_turn",
                    dedupe_key=str(notice.get("dedupe_key") or ""),
                    audience="group",
                )

            dashboard = await session_dashboard(
                self.database,
                session_id,
                viewer_role="player",
                include_technical_refs=False,
            )
            service = AiCompanionDecisionService(
                repository=self.database,
                providers=(
                    tuple(self.providers)
                    if self.providers
                    else (PolicyDecisionProvider(),)
                ),
                submit_choice=submit_choice,
                deliver_notice=deliver_notice,
            )
            receipt = await service.decide(
                session_id=session_id,
                actor_ref=actor_ref,
                choice_set_id=choice_set_id,
                choices=list(choice_set.get("choices") or []),
                expected_session_revision=expected_revision,
                operation_id=operation_id,
                idempotency_key=f"ai-choice:{digest}",
                lease_owner=self.lease_owner,
                trace_id=trace_id,
                world_state_view=dict(
                    dashboard.get("world_state_view") or {}
                ),
            )
            receipts.append(dict(receipt))
            status = str(receipt.get("status") or "")
            if status == "awaiting_confirmation":
                decision = receipt.get("decision")
                decision = decision if isinstance(decision, dict) else {}
                await self.database.queue_delivery(
                    session_id=session_id,
                    origin=str(session.get("unified_origin") or ""),
                    kind="ai_actor_confirmation",
                    text=render_message_type(
                        "ai_companion.awaiting_confirmation",
                        {
                            "actor": decision.get("actor") or "AI 队友",
                            "choice": decision.get("choice") or "当前行动",
                            "reason": decision.get("reason")
                            or "请主持人确认本次行动是否符合当前局势。",
                        },
                        audience="dm",
                    ),
                    reason="ai_companion_confirmation",
                    dedupe_key=f"ai-confirm:{operation_id}",
                    audience="group",
                )
                stop_reason = "awaiting_confirmation"
                break
            if status != "submitted":
                stop_reason = status or "decision_not_submitted"
                break
        else:
            stop_reason = "step_limit"
        return {
            "schema": "tavern-ai-turn-run/1.0.0-rc10",
            "session_id": session_id,
            "processed": len(receipts),
            "stop_reason": stop_reason,
            "receipts": receipts,
        }

    async def confirm_pending(
        self,
        *,
        session_id: str,
        operation_ref: str,
        expected_session_revision: int,
    ) -> dict[str, Any]:
        claim = await self.database.claim_ai_confirmation(
            session_id=session_id,
            operation_ref=operation_ref,
            expected_session_revision=expected_session_revision,
            lease_owner=self.lease_owner,
        )
        if claim.get("replayed"):
            return claim
        command = ChoiceCommand(
            session_id=session_id,
            actor_ref=str(claim["actor_ref"]),
            choice_set_id=str(claim["choice_set_id"]),
            choice_key=str(claim["choice_key"]),
            expected_session_revision=int(claim["session_revision"]),
            idempotency_key=(
                "ai-confirm:"
                + hashlib.sha256(
                    operation_ref.encode("utf-8")
                ).hexdigest()[:24]
            ),
        )
        session = await self.database.get_session(session_id)
        try:
            await self.engine.process_choice_command(
                command,
                event=SimpleNamespace(
                    unified_msg_origin=str(
                        session.get("unified_origin") or ""
                    ),
                    message_id="",
                ),
            )
        except Exception:
            return await self.database.finish_ai_decision(
                operation_id=str(claim["operation_id"]),
                lease_owner=self.lease_owner,
                decision={"choice_key": str(claim["choice_key"])},
                public_projection={
                    **dict(claim.get("decision") or {}),
                    "message": render_message_type(
                        "ai_companion.failed",
                        {
                            "actor": (
                                claim.get("decision", {}).get("actor")
                                if isinstance(claim.get("decision"), dict)
                                else "AI 队友"
                            ),
                            "reason": "确认时副本状态已经变化，原选择无法安全写入。",
                        },
                        audience="dm",
                    ),
                },
                status="failed",
            )
        receipt = await self.database.finish_ai_decision(
            operation_id=str(claim["operation_id"]),
            lease_owner=self.lease_owner,
            decision={"choice_key": str(claim["choice_key"])},
            public_projection=dict(claim.get("decision") or {}),
            status="submitted",
        )
        decision = receipt.get("decision")
        decision = decision if isinstance(decision, dict) else {}
        await self.database.queue_delivery(
            session_id=session_id,
            origin=str(session.get("unified_origin") or ""),
            kind="ai_actor",
            text=render_message_type(
                "ai_companion.submitted",
                {
                    "actor": decision.get("actor") or "AI 队友",
                    "choice": decision.get("choice") or "当前行动",
                    "reason": decision.get("reason")
                    or "主持人已确认该行动。",
                },
                audience="public",
            ),
            reason="ai_companion_confirmation_submitted",
            dedupe_key=f"ai-confirmed:{operation_ref}",
            audience="group",
        )
        followup = await self.run_due(session_id, max_steps=8)
        return {**receipt, "followup": followup}

    async def reselect_pending(
        self,
        *,
        session_id: str,
        operation_ref: str,
    ) -> dict[str, Any]:
        discarded = await self.database.discard_ai_decision(
            session_id=session_id,
            operation_ref=operation_ref,
            pause_actor=False,
        )
        followup = await self.run_due(session_id, max_steps=8)
        return {
            "schema": "tavern-ai-reselect/1.0.0-rc10",
            "discarded": discarded,
            "followup": followup,
        }

    async def pause_pending(
        self,
        *,
        session_id: str,
        operation_ref: str,
    ) -> dict[str, Any]:
        discarded = await self.database.discard_ai_decision(
            session_id=session_id,
            operation_ref=operation_ref,
            pause_actor=True,
        )
        return {
            "schema": "tavern-ai-pause/1.0.0-rc10",
            "discarded": discarded,
            "status": "paused",
        }


__all__ = ["AiCompanionTurnRunner"]
