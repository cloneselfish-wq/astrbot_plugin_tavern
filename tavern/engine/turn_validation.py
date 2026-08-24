from .shared import *
from .errors import *
from .turn_state import TurnProcessState


class TurnValidationMixin:
    async def _validate_turn_result(self, state: TurnProcessState) -> None:
        session_id = state.session_id
        sender_id = state.sender_id
        workflow = state.workflow
        progress = state.progress
        config = state.config
        acting_participant = state.acting_participant
        player = state.player
        session = state.session
        world = state.world
        roster = state.roster
        events = state.events
        narrative_policy = state.narrative_policy
        opening_projection = state.opening_projection
        provider_ids = state.provider_ids
        generation_budget = state.generation_budget
        turn_operation_id = state.turn_operation_id
        system = state.system
        resolution = state.resolution
        used_provider_id = state.used_provider_id
        dice = state.dice
        check_request = state.check_request
        first_mode = state.first_mode
        operation_id = state.operation_id

        normalized_patch = self._normalize_state_patch_relationships(
            resolution.state_patch, roster
        )
        new_state = (
            dict(session.get("world_state") or {})
            if resolution.group_decision
            else apply_state_patch(
                session.get("world_state"),
                normalized_patch,
                fact_round=int(session.get("turn_no") or 0),
                fact_time=_session_game_time(session),
            )
        )
        # C6：模型阶段只生成计划，不提前写资产。
        # 经济操作与回合同一事务提交；任一失败整轮回滚。
        if resolution.group_decision:
            entity_recovery: dict[str, Any] = {}
            staged_item_ops: list[dict[str, Any]] = []
            staged_economy_ops: list[dict[str, Any]] = []
        else:
            entity_recovery = {}
            staged_item_ops = await self._stage_item_ops(
                session_id=session_id,
                ops=resolution.raw.get("item_ops"),
                operation_prefix=f"story:{session.get('revision')}",
                participant=acting_participant,
                actor_id=sender_id,
                recovery=entity_recovery,
            )
            staged_economy_ops = await self._stage_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"story:{session.get('revision')}",
                actor_id=sender_id,
            )
        entity_receipt = self._entity_recovery_receipt(
            entity_recovery,
            turn_operation_id,
        )
        if entity_receipt:
            resolution = replace(
                resolution,
                raw={
                    **dict(resolution.raw),
                    "entity_recovery_receipt": entity_receipt,
                },
            )
        if workflow:
            if workflow.get("requires_check") and first_mode != "check":
                raise TavernEngineError(
                    "该选项标记为必须检定，但模型未申请检定；"
                    "为避免越权结果，本轮没有提交"
                )
            if (
                workflow.get("collective")
                and not resolution.group_decision
            ):
                # 0.11.3：本轮未提交 → 作废已锁骰值，避免重试复用旧骰。
                if operation_id:
                    try:
                        await self.database.revoke_operation_receipt(
                            operation_id
                        )
                    except Exception:
                        pass
                raise TavernEngineError(
                    "该选项影响全队，但模型没有生成集体表决；"
                    "为避免单人越权，本轮没有提交"
                )
            if resolution.group_decision:
                resolution = replace(
                    resolution,
                    next_choices=(),
                )
            else:
                expected_id = str(
                    session["next_actor"].get("id")
                    or session["next_actor"].get("participant_id")
                    or ""
                )
                next_participant = next(
                    (
                        item
                        for item in roster
                        if str(item.get("id") or "") == expected_id
                    ),
                    session["next_actor"],
                )
                resolution = await self._ensure_next_choices(
                    resolution=resolution,
                    provider_ids=self._provider_order(
                        used_provider_id,
                        tuple(provider_ids),
                    ),
                    world=world,
                    session=session,
                    participant=next_participant,
                    roster=roster,
                    events=events,
                    candidate_state=new_state,
                    config=config,
                    budget=generation_budget,
                    operation_id=turn_operation_id,
                )
                await self._raise_if_operation_cancelled(turn_operation_id)
            if not resolution.next_choices and not resolution.group_decision:
                raise TavernEngineError(
                    "模型未生成下一位玩家的 A/B/C/D 选项；"
                    "本轮没有提交"
                )
        normalized_memories = []
        for memory in resolution.memories:
            entry = dict(memory)
            if entry["scope"] == "player" and not entry["scope_id"]:
                entry["scope_id"] = player["id"]
            normalized_memories.append(entry)

        await self._emit_operation_progress(
            turn_operation_id,
            "validating",
            progress,
            PlayerMessage.dynamic(
                title="故事校验",
                summary="故事正文已返回，正在检查事实、长度与行动选项。",
                sections=(
                    "自动处理：全部校验通过后才会一次性提交本轮。",
                ),
                source="story_generation_progress",
            ),
        )
        await self._raise_if_operation_cancelled(turn_operation_id)
        document = resolution.narrative_document
        if not isinstance(document, NarrativeDocument):
            quality = {
                "passed": False,
                "findings": [
                    {
                        "level": "error",
                        "message": "故事缺少结构化 NarrativeDocument",
                    }
                ],
            }
        else:
            document = self._decorate_story_document(
                document,
                resolution=resolution,
                world=world,
                roster=roster,
                session_characters=session.get("session_characters", []),
            )
            transition_patch = (
                {}
                if resolution.group_decision
                else _effective_narrative_transition_patch(
                    normalized_patch,
                    session.get("world_state"),
                )
            )
            quality = inspect_narrative_document(
                document,
                dialogue_expected=False,
                opening=opening_projection is not None,
                max_output_chars=config.max_output_chars,
                previous_narrative=str(
                    next(
                        (
                            item.get("content")
                            for item in reversed(events)
                            if item.get("role") == "narrator"
                        ),
                        "",
                    )
                    or ""
                ),
                state_patch=transition_patch,
            )
        if not quality["passed"]:
            await self.database.update_operation(
                turn_operation_id,
                status="failed_retryable",
                phase="quality_rejected",
                result={"quality": quality},
            )
            first_finding = next(iter(quality.get("findings") or ()), {})
            raise TavernEngineError(
                "叙事质量检查未通过："
                f"{first_finding.get('message') or '故事结构不符合当前模式'}；"
                "本轮没有提交"
            )
        narrative = narrative_document_to_plain_text(document)
        resolution = replace(
            resolution,
            narrative=narrative,
            narrative_document=document,
            raw={
                **dict(resolution.raw),
                "narrative_document": document.to_dict(),
            },
        )

        check_payload = asdict(dice) if dice else None
        if dice and check_request:
            check_payload = {
                **check_payload,
                "check_id": (
                    "check:"
                    + str(
                        workflow.get("choice_set_id")
                        if workflow
                        else session["revision"]
                    )
                ),
                "stat": check_request.stat,
                "reason": check_request.reason,
                "known_consequences": (
                    check_request.known_consequences
                ),
            }
        commit_workflow = {
            **dict(workflow or {}),
            "next_choices": [
                dict(item) for item in resolution.next_choices
            ],
            "group_decision": resolution.group_decision,
            "return_progress": resolution.return_progress,
            "npc_ops": [
                dict(item) for item in resolution.npc_ops
            ],
            "clock_ops": [
                dict(item) for item in resolution.clock_ops
            ],
            "ledger_ops": [
                dict(item) for item in resolution.ledger_ops
            ],
            "status_ops": [
                dict(item) for item in resolution.status_ops
            ],
            "fate_consequences": [
                dict(item) for item in resolution.fate_consequences
            ],
            "assist_ops": [
                dict(item) for item in resolution.assist_ops
            ],
            "operation_id": turn_operation_id,
            "choice_recovery_receipt": (
                dict(resolution.raw.get("choice_recovery_receipt") or {})
                if isinstance(
                    resolution.raw.get("choice_recovery_receipt"),
                    Mapping,
                )
                else {}
            ),
        }
        if not any(
            value
            for key, value in commit_workflow.items()
            if key not in {"group_decision", "return_progress"}
        ) and not (
            commit_workflow.get("group_decision")
            or commit_workflow.get("return_progress")
        ):
            commit_workflow = None
        await self.database.update_operation(
            turn_operation_id,
            status="ready_to_commit",
            phase="ready_to_commit",
            result={
                "generation_stages": generation_budget.safe_records(),
            },
        )

        state.resolution = resolution
        state.new_state = new_state
        state.staged_item_ops = staged_item_ops
        state.staged_economy_ops = staged_economy_ops
        state.normalized_memories = tuple(normalized_memories)
        state.narrative = narrative
        state.narrative_document = document
        state.quality = dict(quality)
        state.check_payload = check_payload
        state.commit_workflow = commit_workflow
