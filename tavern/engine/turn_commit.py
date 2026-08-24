from .shared import *
from .errors import *
from .turn_state import TurnProcessState


class TurnCommitMixin:
    async def _commit_turn_result(self, state: TurnProcessState) -> None:
        session_id = state.session_id
        sender_id = state.sender_id
        item_ops = state.item_ops
        config = state.config
        acting_participant = state.acting_participant
        player = state.player
        player_input = state.player_input
        session = state.session
        world = state.world
        generation_budget = state.generation_budget
        operation_turn = state.operation_turn
        turn_operation_id = state.turn_operation_id
        resolution = state.resolution
        new_state = state.new_state
        staged_item_ops = state.staged_item_ops
        staged_economy_ops = state.staged_economy_ops
        normalized_memories = state.normalized_memories
        narrative = state.narrative
        narrative_document = state.narrative_document
        quality = state.quality
        check_payload = state.check_payload
        commit_workflow = state.commit_workflow

        try:
            await self._raise_if_operation_cancelled(turn_operation_id)
            generation_budget.record(
                stage="commit_and_deliver",
                result="ready",
            )
            experience = normalize_chat_experience(world)
            checkpoint_interval = int(
                experience["continuity"].get("checkpoint_every_turns") or 0
            )
            updated_session = await self.database.commit_turn(
                session_id=session_id,
                expected_revision=session["revision"],
                player_id=player["id"],
                player_user_id=sender_id,
                player_name=(
                    player["character_name"] or player["display_name"]
                ),
                player_input=player_input,
                narrative=narrative,
                narrative_document=narrative_document,
                world_state=new_state,
                memories=normalized_memories,
                check_payload=check_payload,
                model_payload={**dict(resolution.raw), "_quality": quality},
                director_note=resolution.director_note,
                auto_snapshot_interval=(
                    checkpoint_interval
                    if experience.get("enabled") and checkpoint_interval > 0
                    else config.auto_snapshot_interval
                ),
                store_model_payload=config.store_model_payloads,
                workflow=commit_workflow,
                actor_kind=str(player.get("actor_kind") or "human"),
                actor_id=str(
                    acting_participant.get("actor_id")
                    if acting_participant
                    else ""
                ),
                # 0.11.1：回执完成并入提交事务，避免已提交回合
                # 因跨事务崩溃被永久误判为“处理中”。
                operation_id=turn_operation_id,
                operation_result={
                    "turn_no": operation_turn,
                    "quality": quality,
                    "generation_stages": (
                        generation_budget.safe_records()
                    ),
                },
                item_ops=[
                    *[dict(op) for op in (item_ops or ())],
                    *staged_item_ops,
                ],
                economy_ops=staged_economy_ops,
            )
        except DatabaseConflictError as exc:
            try:
                await self.database.update_operation(
                    turn_operation_id,
                    status="failed_retryable",
                    phase="revision_conflict",
                    result={"error": "revision_conflict"},
                )
            except Exception:
                pass
            raise TavernBusyError(
                "本轮状态刚被其他操作更新，请重新提交行动"
            ) from exc
        except Exception as exc:
            receipt = await self.database.get_operation_receipt(
                turn_operation_id
            )
            if receipt is not None:
                updated_session = await self.database.get_session(session_id)
                logger.warning(
                    "321开团提交响应不确定，已通过幂等回执确认提交完成："
                    "session=%s operation=%s",
                    session_id,
                    turn_operation_id,
                )
            else:
            # C6：任何提交路径异常都更新回执，避免停留在 ready_to_commit
            # 被误判为“处理中”；资产已随事务回滚，可安全重试。
                try:
                    await self.database.update_operation(
                        turn_operation_id,
                        status="failed_retryable",
                        phase="turn_commit_failed",
                        result={
                            "error_type": type(exc).__name__,
                            "error": clean_text(str(exc), max_chars=500),
                        },
                    )
                except Exception:
                    pass
                raise
        state.updated_session = dict(updated_session)
