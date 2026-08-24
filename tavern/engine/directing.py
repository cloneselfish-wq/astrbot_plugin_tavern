from .shared import *
from .errors import *

class DirectingMixin:
    async def process_dm_beat(
        self,
        *,
        event: Any,
        session_id: str,
        dm_user_id: str,
        instruction: str,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Generate and atomically commit a DM beat without consuming a player turn."""
        config = self.config_provider()
        text = clean_text(instruction, max_chars=config.max_input_chars)
        if not text:
            raise TavernEngineError("主持推进方向不能为空")
        lock = await self._session_lock(session_id)
        async with lock:
            session = await self.database.get_session(session_id)
            control = await self.database.get_control_state(session_id)
            if session["state"] != "running":
                raise TavernEngineError("暂停或非运行状态不能主持推进")
            if control["mode"] != "dm":
                raise TavernEngineError("当前未开启主持模式")
            if str(control["active_dm_user_id"]) != str(dm_user_id):
                raise TavernEngineError("只有当前真人主持人可以推进剧情")
            instance: dict[str, Any] = {}
            try:
                instance = await self.database.get_instance_config(session_id)
                world = dict(instance["world_snapshot"])
            except Exception:
                world = await self.database.get_world(session["world_id"])
            operation_id = operation_key(
                session_id,
                "dm_beat",
                turn_no=int(session.get("revision") or 0),
                actor_id=dm_user_id,
                source_id=str(transport_event_id(event) or ""),
                payload={"instruction": text},
            )
            reservation = await self.database.reserve_operation(
                operation_id,
                session_id,
                "dm_beat",
                {
                    "instruction": text,
                    "session_revision": int(session.get("revision") or 0),
                },
            )
            if not reservation.get("created"):
                status = str(reservation.get("status") or "")
                if status == "completed":
                    raise TavernBusyError("该主持推进已经完成，未重复生成故事")
                raise TavernBusyError("该主持推进仍在处理或等待恢复，未重复提交")
            await self.database.arm_generation_reminder(
                operation_id,
                self._effective_generation_reminder(instance, config),
            )
            await self.database.update_operation(
                operation_id,
                status="generating",
                phase="context_ready",
            )
            roster = await self.database.list_roster(session_id)
            turn = await self.database.get_turn_status(session_id)
            session = dict(session)
            session["roster"] = roster
            session["turn_status"] = turn
            session["next_actor"] = {}
            session["phase_meta"] = dict(instance.get("phase_meta") or {})
            session["narrative_mode"] = narrative_mode_from_session(instance)
            session["narrative_style"] = await self.database.get_narrative_style(
                session_id, can_manage=True, include_private=True
            )
            session["return_requests"] = await self.database.list_return_requests(session_id)
            session["session_characters"] = await self.database.list_session_characters(
                session_id, include_archived=False, context_only=True
            )
            session["story_ledger"] = await self.database.list_story_ledger(session_id)
            session["scene_clocks"] = await self.database.list_scene_clocks(session_id)
            rule_state = await self.database.get_session_rule_state(session_id)
            session["content_boundaries"] = rule_state.get("content_boundaries", {})
            events = await self.database.recent_events(
                session_id, config.recent_turns * 2 + 6
            )
            memories = await self.database.list_memories(
                session_id, text, config.memory_limit
            )
            providers = await self._story_providers(event, config)
            await self._emit_operation_progress(
                operation_id,
                "context_ready",
                progress,
                PlayerMessage.dynamic(
                    title="主持推进",
                    summary="导演指令已收到，正在生成下一段故事。",
                    sections=("自动处理：生成完成前不会改变世界状态。",),
                    source="story_generation_progress",
                ),
                acknowledge=True,
            )
            try:
                resolution, provider_id = await self._generate_resolution(
                    session_id=session_id,
                    request_type="dm_beat",
                    world=world,
                    provider_ids=providers,
                    system=system_prompt(world, allow_check=False),
                    prompt=dm_beat_prompt(
                        world=world,
                        session=session,
                        instruction=text,
                        directive=str(control.get("directive") or ""),
                        events=events,
                        memories=memories,
                    ),
                    config=config,
                    narrative_mode=session.get("narrative_mode"),
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
                    roster=roster,
                )
            except Exception as exc:
                await self.database.update_operation(
                    operation_id,
                    status="failed_retryable",
                    phase="dm_beat_generation_failed",
                    result={"error_type": type(exc).__name__},
                )
                raise
            if resolution.mode != "resolve" or resolution.check is not None:
                raise TavernEngineError("主持推进不得申请检定")
            if resolution.group_decision:
                raise TavernEngineError("主持推进不得直接创建集体投票")
            document = resolution.narrative_document
            if not isinstance(document, NarrativeDocument):
                raise TavernEngineError(
                    "主持推进缺少结构化 NarrativeDocument"
                )
            document = self._decorate_story_document(
                document,
                resolution=resolution,
                world=world,
                roster=roster,
                session_characters=session.get("session_characters", []),
            )
            normalized_patch = self._normalize_state_patch_relationships(
                resolution.state_patch,
                roster,
            )
            quality = inspect_narrative_document(
                document,
                dialogue_expected=False,
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
                state_patch=_effective_narrative_transition_patch(
                    normalized_patch,
                    session.get("world_state"),
                ),
            )
            if not quality["passed"]:
                await self.database.update_operation(
                    operation_id,
                    status="failed_retryable",
                    phase="quality_rejected",
                    result={"quality": quality},
                )
                first_finding = next(iter(quality.get("findings") or ()), {})
                raise TavernEngineError(
                    str(first_finding.get("message") or "主持故事结构无效")
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
            current_actor = next(
                (
                    item
                    for item in roster
                    if str(item.get("group_user_id") or "")
                    == str(turn.get("current_user_id") or "")
                ),
                None,
            )
            new_state = apply_state_patch(
                session.get("world_state"),
                normalized_patch,
                fact_round=int(session.get("turn_no") or 0),
                fact_time=_session_game_time(session),
            )
            entity_recovery: dict[str, Any] = {}
            staged_item_ops = await self._stage_item_ops(
                session_id=session_id,
                ops=resolution.raw.get("item_ops"),
                operation_prefix=f"dm:{session.get('revision')}",
                participant=current_actor,
                actor_id=dm_user_id,
                source="dm",
                recovery=entity_recovery,
            )
            entity_receipt = self._entity_recovery_receipt(
                entity_recovery,
                f"dm:{session_id}:{session.get('revision')}",
            )
            if entity_receipt:
                resolution = replace(
                    resolution,
                    raw={
                        **dict(resolution.raw),
                        "entity_recovery_receipt": entity_receipt,
                    },
                )
            # C6：主持推进的经济操作不再提前扣款；先校验生成计划，
            # 与主持剧情在同一数据库事务内落账，失败整笔回滚。
            staged_economy_ops = await self._stage_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"dm:{session.get('revision')}",
                actor_id=dm_user_id,
                source="dm",
            )
            workflow = {
                "npc_ops": [dict(item) for item in resolution.npc_ops],
                "clock_ops": [dict(item) for item in resolution.clock_ops],
                "ledger_ops": [dict(item) for item in resolution.ledger_ops],
                "status_ops": [dict(item) for item in resolution.status_ops],
                "fate_consequences": [
                    dict(item) for item in resolution.fate_consequences
                ],
                "assist_ops": [dict(item) for item in resolution.assist_ops],
            }
            await self.database.update_operation(
                operation_id,
                status="ready_to_commit",
                phase="ready_to_commit",
                result={"quality": quality},
            )
            result = await self.database.commit_dm_beat(
                session_id=session_id,
                expected_revision=int(session["revision"]),
                dm_user_id=dm_user_id,
                instruction=text,
                narrative=narrative,
                narrative_document=document,
                world_state=new_state,
                memories=[dict(item) for item in resolution.memories],
                model_payload={**dict(resolution.raw), "_provider": provider_id},
                workflow=workflow,
                item_ops=staged_item_ops,
                economy_ops=staged_economy_ops,
                operation_id=operation_id,
            )
            story_event = await self.database.latest_public_story_event(
                session_id
            )
            await self.broker.publish(
                {
                    "type": "dm_control",
                    "hook": "dm_beat_committed",
                    "session_id": session_id,
                    "beat_no": result["beat_no"],
                    "actor": dm_user_id,
                }
            )
            await self.broker.publish(
                {
                    "type": "story",
                    "action": "updated",
                    "session_id": session_id,
                    "event_id": result.get("event_id", ""),
                    "story_revision": int(
                        (story_event or {}).get("meta", {}).get(
                            "story_revision", 0
                        )
                    ),
                }
            )
            return result

    async def reroll_choices(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        operator_id: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set or not choice_set.get("participant"):
            raise TavernEngineError("当前没有可重整的个人行动选项")
        participant = choice_set["participant"]
        if not force:
            control = await self.database.authorize_participant_control(
                session_id,
                participant["id"],
                sender_id,
                "reroll",
            )
            if not control["authorized"]:
                raise TavernEngineError("只能重整自己当前回合的选项")
        if int(choice_set["reroll_count"]) >= 1:
            raise TavernEngineError("本回合的免费重整次数已经用完")
        config = self.config_provider()
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态，无法重整选项")
        roster = await self.database.list_roster(session_id)
        rich_participant = next(
            (
                item
                for item in roster
                if item.get("id") == participant.get("id")
            ),
            None,
        )
        if rich_participant:
            participant = rich_participant
        # C6：模型调用前以 operation receipt 预占一次重整额度。
        # BEGIN IMMEDIATE 原子抢占 + lease，两个并发重整只有一个能进入
        # 模型调用，另一个立即得到“正在重整”提示；失败/超时可恢复重试。
        try:
            await self.database.recover_expired_operations()
        except Exception:
            pass
        reroll_operation_id = operation_key(
            session_id,
            "reroll",
            turn_no=int(choice_set.get("round_no") or 0),
            actor_id=str(participant.get("id") or ""),
            source_id=str(choice_set.get("id") or ""),
            payload={
                "choice_set_id": str(choice_set.get("id") or ""),
                "session_revision": int(
                    choice_set.get("session_revision") or 0
                ),
                "participant_id": str(participant.get("id") or ""),
            },
        )
        reservation = await self.database.reserve_operation(
            reroll_operation_id,
            session_id,
            "reroll",
            {
                "choice_set_id": str(choice_set.get("id") or ""),
                "session_revision": int(
                    choice_set.get("session_revision") or 0
                ),
                "participant_id": str(participant.get("id") or ""),
            },
        )
        if not reservation.get("created"):
            if reservation.get("status") == "completed":
                raise TavernEngineError(
                    "本回合选项已经重整完成，请查看最新选项"
                )
            raise TavernEngineError(
                "本回合选项正在重整中，请稍候再试"
            )
        try:
            instance = await self.database.get_instance_config(session_id)
            world = dict(instance["world_snapshot"])
        except Exception:
            world = await self.database.get_world(session["world_id"])
        events = await self.database.recent_events(
            session_id,
            config.recent_turns * 2 + 6,
        )
        providers = await self._story_providers(event, config)
        try:
            choices, _choice_meta = await self._generate_choices(
                provider_ids=providers,
                world=world,
                session=session,
                participant=participant,
                events=events,
                config=config,
                avoid=choice_set["choices"],
                roster=roster,
            )
        except Exception as exc:
            await self.database.update_operation(
                reroll_operation_id,
                status="failed_retryable",
                phase="reroll_generation_failed",
                result={
                    "error_type": type(exc).__name__,
                    "error": clean_text(str(exc), max_chars=500),
                },
            )
            raise
        try:
            result = await self.database.replace_active_choices(
                session_id,
                participant["id"],
                choices,
                actor_id=operator_id or sender_id,
            )
        except Exception as exc:
            await self.database.update_operation(
                reroll_operation_id,
                status="failed_retryable",
                phase="reroll_commit_failed",
                result={
                    "error_type": type(exc).__name__,
                    "error": clean_text(str(exc), max_chars=500),
                },
            )
            raise
        await self.database.update_operation(
            reroll_operation_id,
            status="completed",
            phase="committed",
            result={"choice_set_id": result.get("id")},
        )
        result["participant"] = participant
        return result
