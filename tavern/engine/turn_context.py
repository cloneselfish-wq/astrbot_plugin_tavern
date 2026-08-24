from .shared import *
from .errors import *
from .turn_state import TurnProcessState
from ..copy.entities import decorate_entity


class TurnContextMixin:
    async def _prepare_turn_process_state(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        workflow: Mapping[str, Any] | None,
        progress: ProgressCallback | None,
        operator_id: str,
        force_actor: bool,
        item_ops: Sequence[Mapping[str, Any]] | None,
        operation_id_override: str,
        operation_request_override: Mapping[str, Any] | None,
        actor_context: Mapping[str, Any] | None,
        config: TavernConfig,
        text: str,
    ) -> TurnProcessState | EngineReply:
        session = await self.database.get_session(session_id)
        if session["state"] != "running":
            raise TavernEngineError("酒馆当前不在运行状态")
        if int(session.get("input_locked") or 0) and not force_actor:
            raise TavernEngineError("副本输入已被真人主持人锁定，请等待解锁")

        remaining = self.rate_limiter.remaining(
            session_id,
            sender_id,
            config.user_cooldown_seconds,
        )
        if remaining > 0:
            raise TavernBusyError(
                f"行动提交过快，请等待 {remaining:.1f} 秒"
            )

        if actor_context:
            actor_context = dict(actor_context)
            turn = await self.database.get_turn_status(session_id)
            if turn["current_user_id"] != sender_id:
                current_name = str(turn.get("current_name") or "").strip()
                if not current_name:
                    raise TavernTurnOrderError(
                        "当前行动者缺少可公开显示的角色名称，"
                        "系统无法安全确认 AI 队友的行动顺序。",
                        turn=turn,
                    )
                raise TavernTurnOrderError(
                    f"当前轮到 {decorate_entity('character', current_name)}，"
                    "AI 队友的迟到选择未提交。",
                    turn=turn,
                )
            player = {
                "id": str(
                    actor_context.get("actor_id")
                    or actor_context.get("id")
                    or ""
                ),
                "user_id": sender_id,
                "display_name": str(
                    actor_context.get("display_name") or sender_name
                ),
                "character_name": str(
                    actor_context.get("character_name")
                    or actor_context.get("display_name")
                    or sender_name
                ),
                "profile": dict(
                    actor_context.get("card_profile") or {}
                ),
                "enabled": True,
                "actor_kind": "ai_companion",
            }
            joined_result = {"joined": False}
        else:
            try:
                joined_result = await self.database.join_turn_order(
                    session_id,
                    sender_id,
                    sender_name,
                    sender_id,
                )
            except InvalidTransitionError as exc:
                if "玩家身份" in str(exc):
                    raise TavernPlayerDisabledError(str(exc)) from exc
                raise
            player = joined_result["player"]
            if not player["enabled"]:
                raise TavernPlayerDisabledError("该玩家已被停用")
            session = joined_result["session"]
            turn = joined_result["turn"]
        if not force_actor and turn["current_user_id"] != sender_id:
            current_name = str(turn.get("current_name") or "").strip()
            joined_note = "你已加入队尾；" if joined_result["joined"] else ""
            if not current_name:
                raise TavernTurnOrderError(
                    f"{joined_note}当前行动者缺少可公开显示的角色名称，"
                    "系统无法安全确认行动顺序；本条内容未记录。",
                    turn=turn,
                    joined=joined_result["joined"],
                )
            current = decorate_entity("character", current_name)
            raise TavernTurnOrderError(
                f"{joined_note}当前轮到 {current}，本条内容未记录。",
                turn=turn,
                joined=joined_result["joined"],
            )
        acting_round = int(turn["round_no"])
        if operator_id and operator_id != sender_id:
            try:
                await self.database.write_audit(
                    session_id,
                    operator_id,
                    "turn.forced_choose",
                    sender_id,
                    {
                        "actor_user_id": sender_id,
                        "force": True,
                        "operator_id": operator_id,
                    },
                )
            except Exception:
                logger.exception("321开团强制代选审计写入失败")

        for prefix in config.ooc_prefixes:
            if text.lower().startswith(prefix.lower()):
                await self.database.append_ooc(
                    session_id,
                    sender_id,
                    sender_name,
                    text,
                )
                self.broker.schedule(
                    {
                        "type": "ooc",
                        "session_id": session_id,
                        "actor": sender_name,
                    }
                )
                return EngineReply(
                    text=(
                        "【OOC】场外发言已记录，"
                        "本轮世界状态与行动顺序均未推进。"
                    ),
                    session=session,
                    ooc=True,
                    turn=turn,
                )

        image_caption = await self._caption_images(
            event=event,
            session_id=session_id,
            config=config,
        )
        player_input = text
        if image_caption:
            player_input = (
                f"{text}\n\n"
                "<image_descriptions>\n"
                f"{image_caption}\n"
                "</image_descriptions>"
            )

        current_world = await self.database.get_world(session["world_id"])
        instance: dict[str, Any] = {}
        try:
            instance = await self.database.get_instance_config(session_id)
            world = dict(instance["world_snapshot"])
            world.setdefault(
                "characters",
                current_world.get("characters", []),
            )
        except Exception:
            world = current_world
        players = await self.database.list_players(session_id)
        roster = await self.database.list_roster(session_id)
        ai_roster = await self.database.list_ai_turn_actors(session_id)
        roster = [*roster, *ai_roster]
        acting_participant = dict(actor_context) if actor_context else next(
            (
                item
                for item in roster
                if str(
                    item.get("group_user_id")
                    or item.get("actor_ref")
                    or ""
                )
                == sender_id
            ),
            None,
        )
        if actor_context:
            roster = [
                item
                for item in roster
                if str(item.get("actor_id") or item.get("id") or "")
                != str(
                    acting_participant.get("actor_id")
                    or acting_participant.get("id")
                    or ""
                )
            ]
            roster.append(acting_participant)
        if (
            acting_participant
            and int(acting_participant.get("action_locked") or 0)
            and not force_actor
        ):
            raise TavernEngineError(
                "该角色的行动已被真人主持人锁定，请等待解锁"
            )
        # 1.0.0-A7：重伤自动跳过（多人游玩时）。重创角色提交行动时，
        # 提示跳过并由系统推进到下一位；单人游玩不触发。
        if not force_actor and acting_participant:
            runtime = acting_participant.get("runtime_state")
            actor_statuses = (
                runtime.get("statuses")
                if isinstance(runtime, Mapping)
                else []
            )
            if (
                len(turn.get("order") or []) > 1
                and self._is_incapacitated(actor_statuses)
            ):
                await self.database.skip_turn(
                    session_id, sender_id, sender_id
                )
                return EngineReply(
                    text=(
                        "🚑 你已被重创，暂时跳过回合。\n"
                        "恢复途径：队友使用医疗道具"
                        f"（{config.trigger_prefix} 道具 医疗包）、"
                        "治疗技能"
                        f"（{config.trigger_prefix} 技能 急救包扎），"
                        "或通过剧情恢复。"
                    )
                )
        player = dict(player)
        if acting_participant:
            player.update(
                {
                    "participant_id": acting_participant.get("id"),
                    "character_name": (
                        acting_participant.get("character_name")
                        or player.get("character_name")
                    ),
                    "character_code": acting_participant.get(
                        "character_code"
                    ),
                    "profile": acting_participant.get(
                        "card_profile",
                        {},
                    ),
                    "stats": acting_participant.get("card_stats", {}),
                    "runtime_state": acting_participant.get(
                        "runtime_state",
                        {},
                    ),
                    "participation_status": acting_participant.get(
                        "participation_status"
                    ),
                }
            )
        session = dict(session)
        session["players"] = players
        session["roster"] = roster
        session["turn_status"] = turn
        session["next_actor"] = self._next_actor(turn, roster)
        session["phase_meta"] = dict(instance.get("phase_meta") or {})
        session["narrative_mode"] = narrative_mode_from_session(instance)
        session["narrative_style"] = await self.database.get_narrative_style(
            session_id,
            can_manage=True,
            include_private=True,
        )
        narrative_policy = narrative_quality_policy(
            session["narrative_mode"]
        )
        session["item_instances"] = (
            await self.database.list_item_instances(
                session_id,
                self._actor_owner_key(acting_participant),
            )
            if acting_participant
            else []
        )
        session["return_requests"] = await self.database.list_return_requests(
            session_id
        )
        rule_state = await self.database.get_session_rule_state(session_id)
        context_budget = dict(rule_state.get("context_budget") or {})
        recent_turn_limit = max(
            2,
            min(
                50,
                int(
                    context_budget.get(
                        "recent_turns",
                        config.recent_turns,
                    )
                ),
            ),
        )
        memory_limit = max(
            0,
            min(
                40,
                int(
                    context_budget.get(
                        "memories",
                        config.memory_limit,
                    )
                ),
            ),
        )
        events = await self.database.recent_events(
            session_id,
            recent_turn_limit * 2 + 6,
        )
        memories = await self.database.list_memories(
            session_id,
            player_input,
            memory_limit,
        )
        session["session_characters"] = (
            await self.database.list_session_characters(
                session_id,
                include_archived=False,
                context_only=True,
            )
        )
        ledger_limit = max(
            0,
            min(100, int(context_budget.get("ledger_items", 8))),
        )
        session["story_ledger"] = (
            await self.database.list_story_ledger(session_id)
        )[:ledger_limit]
        session["scene_clocks"] = await self.database.list_scene_clocks(
            session_id
        )
        session["content_boundaries"] = rule_state.get(
            "content_boundaries",
            {},
        )
        session["progress"] = rule_state.get("progress", {})
        session["recovery"] = rule_state.get("recovery", {})
        opening_projection = self._first_turn_opening_context(
            world,
            session,
            roster,
        )
        if opening_projection is not None:
            session["opening_scene_projection"] = opening_projection
        provider_ids = await self._story_providers(event, config)
        generation_budget = self._new_generation_budget(config)
        generation_budget.record(
            stage="prepare_context",
            result="ok",
        )
        transport_id = transport_event_id(event)
        operation_turn = (
            int(operation_request_override.get("turn_no") or 0)
            if operation_request_override
            else (0 if transport_id else int(session.get("turn_no", 0)) + 1)
        )
        turn_operation_id = str(operation_id_override or "") or operation_key(
            session_id,
            "turn",
            turn_no=operation_turn,
            actor_id=sender_id,
            source_id=str(
                transport_id
                or (workflow or {}).get("choice_set_id")
                or session.get("revision")
                or ""
            ),
            payload=(
                {"transport_event_id": transport_id}
                if transport_id
                else {
                    "input": player_input,
                    "selected_key": str((workflow or {}).get("selected_key") or ""),
                }
            ),
        )
        operation_request = (
            dict(operation_request_override)
            if operation_request_override
            else {
                "turn_no": operation_turn,
                "transport_event_id": transport_id,
                "actor_id": sender_id,
                "player_input": clean_text(player_input, max_chars=4000),
                "choice_set_id": str((workflow or {}).get("choice_set_id") or ""),
                "selected_key": str((workflow or {}).get("selected_key") or ""),
                "workflow": dict(workflow or {}),
            }
        )
        operation = await self.database.reserve_operation(
            turn_operation_id,
            session_id,
            "turn",
            operation_request,
        )
        await self.database.arm_generation_reminder(
            turn_operation_id,
            self._effective_generation_reminder(instance, config),
        )
        if not operation.get("created"):
            status = str(operation.get("status") or "pending")
            if status == "completed":
                raise TavernBusyError("该行动已经处理完成，重复事件未再次消费")
            if status == "needs_recovery":
                raise TavernBusyError(
                    "该行动需要人工恢复后才能重试，请稍后再试"
                )
            if status in {
                "pending",
                "reserved",
                "generating",
                "dice_locked",
                "ready_to_commit",
            }:
                phase = player_generation_stage_label(
                    (operation.get("result") or {}).get("phase")
                )
                raise TavernBusyError(
                    "本轮行动仍在处理中，重复提交未被记录。\n"
                    f"当前进度：{phase}。\n"
                    "自动处理：系统继续使用首次提交的行动与检定结果。\n"
                    "下一步：请等待本轮消息；如需停止，请发送 /团 取消。"
                )
            raise TavernBusyError(
                "该行动当前状态无法继续处理，请重新提交"
            )

        allow_unlocked_check = bool(
            not workflow
            and config.two_phase_checks
            and world_contract(world)["resolution"]["mode"]
            in {"dice_only", "attribute"}
        )
        capability_projection = []
        if acting_participant:
            capability_projection = await self.database.list_actor_capabilities(
                session_id,
                f"character:{acting_participant.get('id')}",
            )
        system = system_prompt(
            world,
            allow_check=allow_unlocked_check,
            capability_projection=capability_projection,
        )
        first_prompt = planning_prompt(
            world=world,
            session=session,
            player=player,
            player_input=player_input,
            events=events,
            memories=memories,
            allow_checks=(config.two_phase_checks and world_contract(world)["resolution"]["mode"] in {"dice_only", "attribute"}),
            workflow=workflow,
        )
        await self._emit_operation_progress(
            turn_operation_id,
            "context_ready",
            progress,
            PlayerMessage.dynamic(
                title="故事生成",
                summary="本轮线索与角色状态已整理完成，开始生成故事。",
                sections=(
                    "自动处理：本轮尚未提交，世界状态没有改变。",
                ),
                actions=("/团 取消",),
                source="story_generation_progress",
            ),
            acknowledge=True,
        )
        generation_notice_sent = False

        return TurnProcessState(
            event=event,
            session_id=session_id,
            sender_id=sender_id,
            sender_name=sender_name,
            workflow=workflow,
            progress=progress,
            item_ops=item_ops,
            config=config,
            acting_round=acting_round,
            acting_participant=acting_participant,
            player=dict(player),
            player_input=player_input,
            session=dict(session),
            world=dict(world),
            roster=list(roster),
            events=list(events),
            memories=list(memories),
            rule_state=rule_state,
            narrative_policy=narrative_policy,
            opening_projection=opening_projection,
            provider_ids=list(provider_ids),
            generation_budget=generation_budget,
            operation_turn=operation_turn,
            turn_operation_id=turn_operation_id,
            system=system,
            first_prompt=first_prompt,
            capability_projection=list(capability_projection),
            generation_notice_sent=generation_notice_sent,
        )
