from .shared import *
from .errors import *
from .turn_state import TurnProcessState


class TurnDeliveryMixin:
    async def _deliver_turn_result(
        self,
        state: TurnProcessState,
    ) -> EngineReply:
        session_id = state.session_id
        sender_name = state.sender_name
        config = state.config
        acting_round = state.acting_round
        roster = state.roster
        session = state.session
        world = state.world
        generation_budget = state.generation_budget
        turn_operation_id = state.turn_operation_id
        dice = state.dice
        check_event = state.check_event
        narrative_document = state.narrative_document
        commit_workflow = state.commit_workflow
        updated_session = state.updated_session

        if dice is not None and check_event is not None:
            self.broker.schedule(check_event)
        self.broker.schedule(
            {
                "type": "turn",
                "hook": "story_generated",
                "session_id": session_id,
                "group_id": updated_session["group_id"],
                "turn_no": updated_session["turn_no"],
                "actor": sender_name,
                "checked": bool(dice),
            }
        )
        story_event = await self.database.latest_public_story_event(
            session_id
        )
        logger.info(
            "321开团整回合生成阶段 session=%s stages=%s",
            session_id,
            generation_budget.safe_records(),
        )
        self.broker.schedule(
            {
                "type": "story",
                "action": "updated",
                "session_id": session_id,
                "event_id": updated_session.get(
                    "narrator_event_id", ""
                ),
                "story_revision": int(
                    (story_event or {}).get("meta", {}).get(
                        "story_revision", 0
                    )
                ),
            }
        )
        next_turn = await self.database.get_turn_status(session_id)
        if not isinstance(narrative_document, NarrativeDocument):
            raise TavernEngineError(
                "已提交故事缺少结构化 NarrativeDocument，已停止投递；"
                "请从已提交故事记录恢复后重试"
            )
        story_body = narrative_document_to_plain_text(narrative_document)
        story_output = "【故事推进】\n\n" + story_body
        stall_output = ""
        stall_intervention = updated_session.get("stall_intervention")
        if isinstance(stall_intervention, Mapping) and bool(
            stall_intervention.get("stalled")
        ):
            guidance = self._format_stall_guidance(stall_intervention)
            if guidance:
                stall_output = guidance
        current_name = (
            next_turn["current_name"]
            or "等待确认行动者"
        )
        workflow_result = (
            updated_session.get("workflow", {})
            if commit_workflow
            else {}
        )
        fate_result = (
            workflow_result.get("fate")
            if isinstance(workflow_result, Mapping)
            else {}
        )
        fate_result = (
            fate_result if isinstance(fate_result, Mapping) else {}
        )
        terminal_result = fate_result.get("terminal")
        terminal_result = (
            terminal_result
            if isinstance(terminal_result, Mapping)
            else {}
        )
        terminal_match = terminal_result.get("match")
        terminal_match = (
            terminal_match
            if isinstance(terminal_match, Mapping)
            else {}
        )
        terminal_finished = (
            str(updated_session.get("state") or "") == SESSION_FINISHED
        )
        vote_pending = bool(workflow_result.get("vote_id"))
        if vote_pending:
            turn_footer = (
                "【全队行动待表决】\n\n"
                f"行动者：「{current_name}」\n"
                "当前情况：行动权已挂起，集体投票不消耗个人行动机会。"
            )
        elif len(next_turn["order"]) > 1:
            if next_turn["round_no"] > acting_round:
                turn_footer = (
                    "【行动权已交接】\n\n"
                    f"第 {acting_round} 轮已经结束。\n"
                    f"当前轮次：第 {next_turn['round_no']} 轮\n"
                    f"当前行动者：「{current_name}」"
                )
            else:
                turn_footer = (
                    "【行动权已交接】\n\n"
                    f"当前轮次：第 {acting_round} 轮\n"
                    f"下一位行动者：「{current_name}」"
                )
        else:
            turn_footer = (
                "【当前行动】\n\n"
                f"当前轮次：第 {next_turn['round_no']} 轮\n"
                f"当前行动者：「{current_name}」"
            )
        turn_output = turn_footer
        actor_status_output = ""
        bundle_actor: Mapping[str, Any] = {}
        if terminal_result.get("matched"):
            ending_label = str(
                terminal_match.get("label")
                or terminal_match.get("condition_label")
                or "世界终局"
            )
            terminal_reason = str(
                terminal_match.get("reason")
                or terminal_result.get("reason")
                or ""
            )
            if str(terminal_result.get("decision") or "") == "manual":
                turn_output = (
                    f"🏁 【终局待确认】{ending_label}\n"
                    "世界终局条件已经满足，系统已记录待办，但没有自动归档。\n"
                    "下一步：请主持人在 WebUI 或主持命令中确认终局。"
                )
            elif terminal_finished:
                turn_output = (
                    f"🏁 【终局】{ending_label}\n"
                    + (
                        f"{terminal_reason}\n"
                        if terminal_reason
                        else ""
                    )
                    + "副本已按世界终局规则完成永久只读归档。"
                )
        if commit_workflow and not terminal_finished:
            world_event = workflow_result.get("world_event")
            if world_event:
                turn_output += (
                    "\n\n🌐 【世界脉冲】"
                    f"{world_event.get('title') or '局势变化'}\n"
                    f"{world_event.get('description')}"
                )
            vote_id = workflow_result.get("vote_id")
            if vote_id:
                vote = await self.database.active_vote(session_id)
                if vote:
                    vote_lines = [
                        "🗳️ 【集体决策】",
                        vote["question"],
                        *[
                            f"{item.get('key')}. {item.get('text')}"
                            for item in vote["options"]
                        ],
                        "",
                        "💬 发送：/团 投票 A",
                        "投票期间不消耗当前玩家的行动机会。",
                    ]
                    turn_output += "\n\n" + "\n".join(vote_lines)
            else:
                next_choice = await self.database.active_choice_set(
                    session_id
                )
                if next_choice and (
                    next_choice.get("participant")
                    or next_choice.get("actor")
                ):
                    choice_recovery = workflow_result.get(
                        "choice_recovery_receipt"
                    )
                    if isinstance(choice_recovery, Mapping) and str(
                        choice_recovery.get("message") or ""
                    ):
                        turn_output += (
                            "\n\n【行动选项已恢复】\n\n"
                            f"结果：{choice_recovery.get('message')}\n"
                            "自动处理：系统没有跳过风险校验，也没有改变已提交事实。\n\n"
                            "下一步：可以直接选择；如需更换选项，请发送\n"
                            "/团 重整选项"
                        )
                    next_actor = (
                        next_choice.get("actor")
                        or next_choice.get("participant")
                    )
                    if not isinstance(next_actor, Mapping):
                        raise TavernEngineError(
                            "下一行动角色投影缺失，本轮事实已提交，"
                            "请刷新副本状态后继续"
                        )
                    bundle_actor = next_actor
                    try:
                        context = await self._build_turn_context(
                            next_actor=next_actor,
                            roster=roster,
                            world=world,
                            session=session,
                            session_id=session_id,
                        )
                        if context:
                            actor_status_output = context
                    except Exception:
                        logger.exception("321开团回合信息小节生成失败")
                    turn_output += "\n\n" + format_choices(
                        str(
                            next_actor.get("character_name")
                            or next_actor.get("display_name")
                            or "当前行动角色"
                        ),
                        next_choice["choices"],
                        rerolls_left=(
                            1 - int(next_choice["reroll_count"])
                        ),
                        trigger_prefix=config.trigger_prefix,
                    )
        message_rows: list[tuple[Any, PlayerMessage]] = []
        next_actor_id = str(
            bundle_actor.get("id")
            or bundle_actor.get("participant_id")
            or ""
        )
        next_actor_name = str(
            bundle_actor.get("character_name")
            or bundle_actor.get("display_name")
            or current_name
        )
        if actor_status_output:
            status_sections = tuple(
                line.strip()
                for line in actor_status_output.splitlines()
                if line.strip()
            )
            usage_actions: list[str] = []
            if any("⚡" in line for line in status_sections):
                usage_actions.append(
                    f"{config.trigger_prefix} 技能 <名称> 对 <目标>"
                )
            if any("🎒" in line for line in status_sections):
                usage_actions.append(
                    f"{config.trigger_prefix} 道具 <名称> 对 <目标>"
                )
            message_rows.append(
                (
                    "actor_status",
                    PlayerMessage.dynamic(
                        title=f"「{next_actor_name}」的状态",
                        sections=status_sections,
                        actions=usage_actions,
                        source="turn.actor_status",
                        privacy="private",
                        delivery_policy="private_or_group_safe",
                    ),
                )
            )
        if story_output:
            story_message = PlayerMessage.dynamic(
                title="故事推进",
                summary=story_body,
                source="turn.story",
            )
            story_message = replace(
                story_message,
                data={
                    "delivery_narrative_document": (
                        narrative_document.to_dict()
                    )
                },
            )
            message_rows.append(
                (
                    "story",
                    story_message,
                )
            )
        if stall_output:
            message_rows.append(
                (
                    "notice",
                    PlayerMessage.dynamic(
                        title="推进建议",
                        summary=stall_output,
                        source="turn.stall_intervention",
                    ),
                )
            )
        if turn_output:
            message_rows.append(
                (
                    "choices",
                    PlayerMessage.from_text(
                        turn_output,
                        default_title="行动选择",
                    ),
                )
            )
        bundle = TurnMessageBundle.build(
            session_id=session_id,
            operation_id=turn_operation_id,
            actor_id=next_actor_id,
            state_revision=str(updated_session.get("revision") or ""),
            messages=message_rows,
        )
        return EngineReply(
            text="\n\n".join(
                item for item in (story_output, stall_output, turn_output)
                if item
            ),
            session=updated_session,
            dice=dice,
            turn=next_turn,
            story_text=story_output,
            turn_text=turn_output,
            messages=bundle.messages,
            message_bundle=bundle,
        )
