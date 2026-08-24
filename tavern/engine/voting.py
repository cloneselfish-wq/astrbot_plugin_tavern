from .shared import *
from .errors import *

class VotingMixin:
    async def process_vote_resolution(
        self,
        *,
        event: Any,
        session_id: str,
        vote: Mapping[str, Any],
        progress: ProgressCallback | None = None,
    ) -> EngineReply:
        """0.11.2：集体表决通过后，把表决结果作为已定事实推进剧情并生成新选项。

        修复：旧实现中 `/团 投票` 的“表决通过”分支只发送确认文本，
        从不生成后续叙事与新选项（WebUI 只读展示所以“看起来正常”）。
        """
        config = self.config_provider()
        lock = await self._session_lock(session_id)
        async with lock:
            operation_id = str(
                vote.get("resolution_operation_id")
                or f"vote-resolution:{vote.get('id') or ''}"
            )
            reservation = await self.database.reserve_operation(
                operation_id,
                session_id,
                "vote_resolution",
                {
                    "vote_id": str(vote.get("id") or ""),
                    "winner_key": str(vote.get("winner_key") or ""),
                    "decision_revision": int(
                        vote.get("decision_revision") or 0
                    ),
                    "suspended_user_id": str(
                        vote.get("suspended_user_id") or ""
                    ),
                },
            )
            operation_status = str(reservation.get("status") or "")
            if operation_status == "completed":
                raise TavernBusyError("该表决结果已经落实，未重复生成故事")
            if operation_status in {"cancelled", "needs_recovery"}:
                raise TavernBusyError(
                    "该表决落实已取消或需要恢复核对，不能直接重新生成"
                )
            await self.database.update_operation(
                operation_id,
                status="generating",
                phase="context_ready",
                result={"vote_id": str(vote.get("id") or "")},
            )
            await self.database.update_vote_resolution_status(
                str(vote.get("id") or ""),
                "generating",
            )
            session = await self.database.get_session(session_id)
            if session["state"] != "running":
                raise TavernEngineError("酒馆当前不在运行状态")
            instance: dict[str, Any] = {}
            try:
                instance = await self.database.get_instance_config(
                    session_id
                )
                world = dict(instance["world_snapshot"])
            except Exception:
                world = await self.database.get_world(session["world_id"])
            await self.database.arm_generation_reminder(
                operation_id,
                self._effective_generation_reminder(instance, config),
            )
            events = await self.database.recent_events(
                session_id,
                config.recent_turns * 2 + 6,
            )
            roster = await self.database.list_roster(session_id)
            turn = await self.database.get_turn_status(session_id)
            session = dict(session)
            session["roster"] = roster
            session["turn_status"] = turn
            session["next_actor"] = self._next_actor(turn, roster)
            session["phase_meta"] = dict(instance.get("phase_meta") or {})
            session["narrative_mode"] = narrative_mode_from_session(instance)
            session["narrative_style"] = await self.database.get_narrative_style(
                session_id, can_manage=True, include_private=True
            )
            narrative_policy = narrative_quality_policy(
                session["narrative_mode"]
            )
            memories = await self.database.list_memories(
                session_id,
                "",
                config.recent_turns * 2 + 6,
            )
            await self._emit_operation_progress(
                operation_id,
                "context_ready",
                progress,
                PlayerMessage.dynamic(
                    title="故事生成",
                    summary="表决结果已锁定，正在整理本轮故事。",
                    sections=(
                        "自动处理：世界状态尚未改变，表决结果可以安全恢复。",
                    ),
                    actions=("/团 取消",),
                    source="story_generation_progress",
                ),
                acknowledge=True,
            )
            await self._raise_if_operation_cancelled(operation_id)
            winner_key = str(vote.get("winner_key") or "")
            winning_text = ""
            declines_action = False
            for option in (vote.get("options") or []):
                if (
                    isinstance(option, Mapping)
                    and str(option.get("key")) == winner_key
                ):
                    winning_text = str(option.get("text") or "")
                    declines_action = bool(option.get("declines_action"))
                    break
            if not winning_text:
                winning_text = winner_key
            if declines_action:
                # 队伍选择「暂缓」→ 不调用模型、不推进回合、
                # 不消耗行动机会。兜底选项已在 cast_vote 落库时恢复，
                # 这里再幂等确认一次，防止任何边界导致「无投票+无选项」软锁。
                suspended_user_id = str(
                    vote.get("suspended_user_id") or ""
                )
                try:
                    restored = await self.database.restore_actor_choices(
                        session_id,
                        suspended_user_id,
                        reason=f"vote-decline:{vote.get('id') or ''}",
                    )
                except Exception:
                    restored = {"created": False, "reason": "restore_failed"}
                suspended_name = ""
                if suspended_user_id:
                    member = next(
                        (
                            item
                            for item in roster
                            if str(item.get("group_user_id") or "")
                            == suspended_user_id
                        ),
                        None,
                    )
                    if member:
                        suspended_name = str(
                            member.get("character_name")
                            or member.get("display_name")
                            or ""
                        )
                restored_text = (
                    "已恢复行动选项，可继续选择 A/B/C/D"
                    if restored.get("created")
                    else "行动选项已就绪"
                )
                await self.database.complete_vote_without_narrative(
                    str(vote.get("id") or "")
                )
                return EngineReply(
                    text=(
                        "🌐 【集体决定】队伍选择暂缓："
                        f"{winning_text}\n"
                        f"行动权归还 {suspended_name or '当前行动玩家'}，"
                        "本次不消耗行动机会。\n"
                        f"{restored_text}。"
                    ),
                    session=await self.database.get_session(session_id),
                    turn=await self.database.get_turn_status(session_id),
                )
            vote_input = f"队伍已表决通过：{winning_text}"
            # 0.11.4：全队行动若在投票时声明了检定（如 魔力 DC17），
            # 表决通过后先执行该检定，再把结果作为权威输入生成落实叙事。
            check_definition: dict[str, Any] | None = None
            for option in (vote.get("options") or []):
                if (
                    isinstance(option, Mapping)
                    and isinstance(option.get("check"), Mapping)
                ):
                    check_definition = dict(option["check"])
                    break
            vote_check: CheckRequest | None = None
            vote_dice: DiceResult | None = None
            if check_definition:
                vote_check = self._check_request_from_payload(
                    check_definition
                )
                acting_user_id = str(
                    vote.get("suspended_user_id") or ""
                )
                check_type = str(
                    vote_check.check_type or "standard"
                ).lower()
                if check_type in {"group", "resistance"}:
                    actors: list[dict[str, Any]] = []
                    for member in roster:
                        if (
                            member.get("participation_status") != "active"
                            or member.get("card_status") != "approved"
                        ):
                            continue
                        member_user_id = str(
                            member.get("group_user_id") or ""
                        )
                        member_modifier = (
                            await self.database.authoritative_modifier(
                                session_id,
                                member_user_id,
                                vote_check.stat,
                            )
                        )
                        member_context = (
                            await self.database.check_context(
                                session_id,
                                member_user_id,
                                str(member_modifier["stat"]),
                                proposed_advantages=(
                                    vote_check.advantage_sources
                                ),
                                proposed_disadvantages=(
                                    vote_check.disadvantage_sources
                                ),
                            )
                        )
                        actors.append(
                            {
                                "actor_id": member["id"],
                                "name": (
                                    member.get("character_name")
                                    or member.get("display_name")
                                    or member_user_id
                                ),
                                "modifier": member_modifier["modifier"],
                                "advantage_sources": member_context[
                                    "advantages"
                                ],
                                "disadvantage_sources": member_context[
                                    "disadvantages"
                                ],
                            }
                        )
                    if not actors:
                        raise TavernEngineError(
                            "集体检定没有有效参与角色"
                        )
                    vote_dice = await self._roll_with_registered_system(
                        world, vote_check, actors=actors
                    )
                else:
                    if not acting_user_id:
                        raise TavernEngineError("表决检定缺少执行玩家")
                    authoritative = (
                        await self.database.authoritative_modifier(
                            session_id,
                            acting_user_id,
                            vote_check.stat,
                        )
                    )
                    if (
                        not authoritative.get("matched")
                        and world_contract(world)["resolution"]["mode"]
                        == "attribute"
                    ):
                        raise TavernEngineError(
                            f"检定属性“{vote_check.stat}”不属于当前"
                            "世界或角色卡，表决检定无法执行"
                        )
                    await self.database.check_context(
                        session_id,
                        acting_user_id,
                        str(authoritative["stat"]),
                        proposed_advantages=(
                            vote_check.advantage_sources
                        ),
                        proposed_disadvantages=(
                            vote_check.disadvantage_sources
                        ),
                    )
                    vote_dice = await self._roll_with_registered_system(
                        world, vote_check
                    )
                # 检定凭证落库（幂等，可回放）
                dice_op_id = operation_key(
                    session_id,
                    "dice",
                    turn_no=int(session.get("turn_no") or 0) + 1,
                    actor_id=acting_user_id,
                    source_id=str(vote.get("id") or ""),
                    payload={
                        "selected_key": "team",
                        "stat": str(vote_check.stat or "").casefold(),
                        "check_type": str(
                            vote_check.check_type or ""
                        ).casefold(),
                    },
                )
                locked_receipt = await self.database.lock_check_result(
                    dice_op_id,
                    session_id,
                    asdict(vote_check),
                    asdict(vote_dice),
                )
                # v1.0-A2（缺陷修复）：与单人检定路径保持一致——
                # 复用凭证中已锁定的检定与骰面。此前表决路径丢弃返回的
                # 凭证，若模型调用失败后重试，会以「新骰面」生成叙事而
                # 凭证仍保留「旧骰面」，导致回放与展示不一致。
                vote_check = self._check_request_from_payload(
                    locked_receipt["request"]
                )
                vote_dice = self._dice_result_from_payload(
                    locked_receipt["result"]
                )
                dice_line = self._format_dice_result(
                    vote_dice,
                    vote_check.stat,
                )
                await self._emit_operation_progress(
                    operation_id,
                    "check_resolved",
                    progress,
                    dice_line or "【检定结果】集体检定结果已经锁定。",
                )
                await self.database.update_operation(
                    operation_id,
                    status="dice_locked",
                    phase="dice_locked",
                    result={"dice_operation_id": dice_op_id},
                )
                await self._raise_if_operation_cancelled(operation_id)
            providers = await self._story_providers(event, config)
            system = system_prompt(
                world,
                allow_check=False,
                capability_projection=[],
            )
            if vote_check is not None and vote_dice is not None:
                prompt = checked_resolution_prompt(
                    world=world,
                    session=session,
                    player={},
                    player_input=vote_input,
                    events=events,
                    memories=memories,
                    check=asdict(vote_check),
                    dice=asdict(vote_dice),
                )
            else:
                prompt = planning_prompt(
                    world=world,
                    session=session,
                    player={},
                    player_input=vote_input,
                    events=events,
                    memories=memories,
                    allow_checks=False,
                    workflow={},
                )
            await self.database.update_operation(
                operation_id,
                status="generating",
                phase="generating",
            )
            await self._emit_operation_progress(
                operation_id,
                "generating",
                progress,
                None,
            )
            generation_budget = self._new_generation_budget(config)
            await self._raise_if_operation_cancelled(operation_id)
            resolution, used_provider_id = await self._generate_resolution(
                session_id=session_id,
                request_type="vote_resolution",
                world=world,
                provider_ids=providers,
                system=system,
                prompt=prompt,
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
                budget=generation_budget,
            )
            await self._raise_if_operation_cancelled(operation_id)
            if resolution.mode != "resolve":
                raise TavernEngineError("模型未完成表决后的最终裁定")
            if resolution.check is not None:
                raise TavernEngineError(
                    "表决结果落实不应产生新的检定"
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
            normalized_patch = self._normalize_state_patch_relationships(
                resolution.state_patch,
                roster,
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
                operation_prefix=f"vote:{session.get('revision')}",
                participant=current_actor,
                actor_id=str((vote or {}).get("actor_id") or "system"),
                source="vote",
                recovery=entity_recovery,
            )
            entity_receipt = self._entity_recovery_receipt(
                entity_recovery,
                operation_id,
            )
            if entity_receipt:
                resolution = replace(
                    resolution,
                    raw={
                        **dict(resolution.raw),
                        "entity_recovery_receipt": entity_receipt,
                    },
                )
            # C6：表决落实的经济操作不再提前扣款；先校验生成计划，
            # 与表决叙事在同一数据库事务内落账，失败整笔回滚。
            staged_economy_ops = await self._stage_economy_ops(
                session_id=session_id,
                ops=resolution.raw.get("economy_ops"),
                operation_prefix=f"vote:{session.get('revision')}",
                actor_id=str((vote or {}).get("actor_id") or ""),
            )
            next_participant = next(
                (
                    item
                    for item in roster
                    if str(item.get("id") or "")
                    == str(session["next_actor"].get("id") or "")
                ),
                session["next_actor"],
            )
            resolution = await self._ensure_next_choices(
                resolution=resolution,
                provider_ids=self._provider_order(
                    used_provider_id,
                    tuple(providers),
                ),
                world=world,
                session=session,
                participant=next_participant,
                roster=roster,
                events=events,
                candidate_state=new_state,
                config=config,
                budget=generation_budget,
                operation_id=operation_id,
            )
            await self._raise_if_operation_cancelled(operation_id)
            await self._emit_operation_progress(
                operation_id,
                "validating",
                progress,
                PlayerMessage.dynamic(
                    title="故事校验",
                    summary="故事正文已返回，正在检查事实与表决结果。",
                    sections=(
                        "自动处理：校验完成前不会写入世界状态。",
                    ),
                    source="story_generation_progress",
                ),
            )
            document = resolution.narrative_document
            if not isinstance(document, NarrativeDocument):
                quality = {
                    "passed": False,
                    "findings": [
                        {
                            "level": "error",
                            "message": "表决故事缺少结构化 NarrativeDocument",
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
                    "表决故事正文未通过质量校验："
                    f"{first_finding.get('message') or '故事结构不符合当前模式'}；"
                    "世界尚未改变"
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
            await self.database.update_operation(
                operation_id,
                status="ready_to_commit",
                phase="ready_to_commit",
                result={"quality": quality},
            )
            await self._raise_if_operation_cancelled(operation_id)
            updated = await self.database.commit_vote_resolution(
                session_id=session_id,
                expected_revision=session["revision"],
                narrative=narrative,
                narrative_document=document,
                world_state=new_state,
                memories=resolution.memories,
                model_payload={**dict(resolution.raw), "_quality": quality},
                workflow={
                    "vote_id": str(vote.get("id") or ""),
                    "next_choices": [
                        dict(item) for item in resolution.next_choices
                    ],
                    "operation_id": operation_id,
                    "choice_recovery_receipt": (
                        dict(
                            resolution.raw.get(
                                "choice_recovery_receipt"
                            )
                            or {}
                        )
                        if isinstance(
                            resolution.raw.get(
                                "choice_recovery_receipt"
                            ),
                            Mapping,
                        )
                        else {}
                    ),
                },
                vote_id=str(vote.get("id") or ""),
                item_ops=staged_item_ops,
                economy_ops=staged_economy_ops,
            )
            story_event = await self.database.latest_public_story_event(
                session_id
            )
            await self.broker.publish(
                {
                    "type": "story",
                    "action": "updated",
                    "session_id": session_id,
                    "event_id": updated.get("event_id", ""),
                    "story_revision": int(
                        (story_event or {}).get("meta", {}).get(
                            "story_revision", 0
                        )
                    ),
                }
            )
            story_body = narrative_document_to_plain_text(document)
            story_output = f"【集体决定】\n\n{story_body}"
            dice_line = ""
            if vote_dice is not None and vote_check is not None:
                dice_line = self._format_dice_result(
                    vote_dice, vote_check.stat
                )
                if dice_line:
                    story_output = f"{dice_line}\n\n{story_output}"
            next_turn = await self.database.get_turn_status(session_id)
            next_name = (
                str(next_turn.get("current_name") or "")
                or "等待确认行动者"
            )
            turn_output = (
                "【行动权已交接】\n\n"
                f"当前轮次：第 {next_turn.get('round_no', 1)} 轮\n"
                f"当前行动者：「{next_name}」"
            )
            if resolution.next_choices:
                turn_output += "\n\n" + format_choices(
                    next_participant.get("character_name")
                    or next_participant.get("display_name")
                    or next_name,
                    resolution.next_choices,
                    rerolls_left=1,
                    trigger_prefix=config.trigger_prefix,
                )
            actor_status_output = ""
            try:
                actor_status_output = await self._build_turn_context(
                    next_actor=next_participant,
                    roster=roster,
                    world=world,
                    session=updated,
                    session_id=session_id,
                )
            except Exception:
                logger.exception("321开团表决完成后的角色状态小节生成失败")
            message_rows: list[tuple[Any, PlayerMessage]] = []
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
                            title=f"「{next_name}」的状态",
                            sections=status_sections,
                            actions=usage_actions,
                            source="vote.actor_status",
                            privacy="private",
                            delivery_policy="private_or_group_safe",
                        ),
                    )
                )
            if dice_line:
                message_rows.append(
                    (
                        "result",
                        PlayerMessage.from_text(
                            dice_line,
                            default_title="检定结果",
                        ),
                    )
                )
            story_message = PlayerMessage.dynamic(
                title="集体决定",
                summary=story_body,
                source="vote.story",
            )
            story_message = replace(
                story_message,
                data={
                    "delivery_narrative_document": document.to_dict(),
                },
            )
            message_rows.extend(
                (
                    ("story", story_message),
                    (
                        "choices",
                        PlayerMessage.from_text(
                            turn_output,
                            default_title="行动选择",
                        ),
                    ),
                )
            )
            bundle = TurnMessageBundle.build(
                session_id=session_id,
                operation_id=operation_id,
                actor_id=str(
                    next_participant.get("id")
                    or next_participant.get("participant_id")
                    or ""
                ),
                state_revision=str(updated.get("revision") or ""),
                messages=message_rows,
            )
            return EngineReply(
                text=f"{story_output}\n\n{turn_output}",
                session=updated,
                dice=vote_dice,
                turn=next_turn,
                story_text=story_output,
                turn_text=turn_output,
                messages=bundle.messages,
                message_bundle=bundle,
            )

    async def process_team_proposal(
        self,
        *,
        event: Any,
        session_id: str,
        sender_id: str,
        sender_name: str,
        index: int = 0,
    ) -> EngineReply:
        """0.11.3：通过 t 全队 / /团 全队 便捷指令发起全队行动表决。

        全队行动以 A—D 字母选项呈现（🛡全队），选中字母即由
        engine.choose 发起表决；本便捷指令保留为兼容入口（按候选项下标）。
        本方法与 process_choice 的 collective 分支共用同一发起逻辑。
        """
        choice_set = await self.database.active_choice_set(session_id)
        if not choice_set:
            raise TavernEngineError("当前没有可选择的行动选项")
        participant = choice_set.get("participant")
        if not participant:
            raise TavernEngineError("当前选项没有有效的行动角色")
        team_choices = [
            item
            for item in choice_set["choices"]
            if bool(item.get("collective"))
        ]
        if not team_choices:
            raise TavernEngineError("当前没有全队行动候选项")
        if index < 0 or index >= len(team_choices):
            raise TavernEngineError(
                f"全队行动编号无效，当前有 {len(team_choices)} 项"
            )
        control = await self.database.authorize_participant_control(
            session_id,
            participant["id"],
            sender_id,
            "choose",
        )
        if not control["authorized"]:
            owner = (
                participant.get("character_name")
                or participant.get("display_name")
                or participant.get("group_user_id")
            )
            raise TavernTurnOrderError(
                f"当前行动属于 {owner}，本条内容未记录。",
                turn=await self.database.get_turn_status(session_id),
            )
        selected = team_choices[index]
        return await self._start_team_vote(
            session_id=session_id,
            participant=participant,
            selected=selected,
            sender_id=sender_id,
        )

    async def _start_team_vote(
        self,
        *,
        session_id: str,
        participant: Mapping[str, Any],
        selected: Mapping[str, Any],
        sender_id: str,
        flavor: str = "",
    ) -> EngineReply:
        """发起「全队行动」的集体表决，不经过模型、不消耗行动机会。"""
        # 0.11.4：若该全队行动声明需要检定（如 魔力 DC17），把检定定义
        # 随「同意执行」选项写入投票，表决通过后据此执行检定。
        team_options: list[dict[str, Any]] = [
            {"key": "A", "text": "同意执行（推进）"}
        ]
        if selected.get("requires_check") and isinstance(
            selected.get("check"), Mapping
        ):
            chk = selected["check"]
            team_options[0]["check"] = {
                "stat": str(
                    chk.get("attribute_label")
                    or chk.get("attribute_id")
                    or chk.get("stat")
                    or "通用"
                ),
                "reason": str(
                    chk.get("reason") or "全队行动存在不确定性"
                ),
                "difficulty": chk.get("difficulty") or 12,
                "risk": str(selected.get("risk") or "controlled"),
                "check_type": str(chk.get("type") or "standard"),
                "advantage_sources": list(
                    chk.get("advantage_sources") or []
                ),
                "disadvantage_sources": list(
                    chk.get("disadvantage_sources") or []
                ),
                "known_consequences": str(
                    chk.get("known_consequences") or ""
                ),
            }
        team_options.append({"key": "B", "text": "暂缓，先处理当前局面", "declines_action": True})
        await self.database.create_group_vote(
            session_id,
            group_decision={
                "question": (
                    f"是否执行全队行动：{selected['text']}"
                    + (f"\n补充说明：{flavor}" if flavor else "")
                ),
                "options": team_options,
            },
            suspended_user_id=str(
                participant.get("group_user_id")
                or participant.get("actor_ref")
                or sender_id
            ),
            actor_id=sender_id,
        )
        check_note = ""
        if team_options[0].get("check"):
            chk = team_options[0]["check"]
            stat = str(chk.get("stat") or "通用")
            dc = chk.get("difficulty")
            check_note = (
                f"\n⚠️ 表决通过后将执行检定：{stat}检定"
                + (f" DC{dc}" if dc else "")
            )
        return EngineReply(
            text=(
                "🌐 【集体表决】已发起全员投票，等待全体成员表决。\n"
                f"表决事项：{selected['text']}{check_note}\n\n"
                "💬 请全体成员发送：/团 投票 A（同意执行）"
                "或 B（暂缓）。\n"
                "投票不消耗个人行动机会。"
            ),
            session=await self.database.get_session(session_id),
            turn=await self.database.get_turn_status(session_id),
        )
