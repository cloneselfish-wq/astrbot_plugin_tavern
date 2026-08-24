from .shared import *
from .errors import *
from .turn_state import TurnProcessState


class TurnGenerationMixin:
    async def _run_turn_generation(self, state: TurnProcessState) -> None:
        session_id = state.session_id
        sender_id = state.sender_id
        sender_name = state.sender_name
        workflow = state.workflow
        progress = state.progress
        config = state.config
        player = state.player
        player_input = state.player_input
        session = state.session
        world = state.world
        roster = state.roster
        events = state.events
        memories = state.memories
        rule_state = state.rule_state
        provider_ids = state.provider_ids
        generation_budget = state.generation_budget
        operation_turn = state.operation_turn
        turn_operation_id = state.turn_operation_id
        system = state.system
        first_prompt = state.first_prompt
        capability_projection = state.capability_projection
        generation_notice_sent = state.generation_notice_sent
        operation_id = state.operation_id
        check_event = state.check_event

        if workflow and workflow.get("requires_check"):
            locked_check = self._check_request_from_locked_choice(workflow)
            resolution = Resolution(
                mode="check",
                narrative="",
                check=locked_check,
                state_patch={},
                memories=(),
                next_choices=(),
                group_decision=None,
                return_progress=None,
                npc_ops=(),
                clock_ops=(),
                ledger_ops=(),
                status_ops=(),
                fate_consequences=(),
                assist_ops=(),
                entity_mentions=(),
                director_note="",
                raw={
                    "mode": "check",
                    "source": "plugin_locked_choice",
                },
            )
            used_provider_id = ""
        else:
            await self._emit_operation_progress(
                turn_operation_id,
                "generating",
                progress,
                None,
            )
            generation_notice_sent = True
            try:
                await self._raise_if_operation_cancelled(turn_operation_id)
                resolution, used_provider_id = await self._generate_resolution(
                    session_id=session_id,
                    request_type="story_plan",
                    world=world,
                    provider_ids=provider_ids,
                    system=system,
                    prompt=first_prompt,
                    config=config,
                    narrative_mode=session.get("narrative_mode"),
                    player_input=player_input,
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
                    opening=state.opening_projection is not None,
                    expected_actor=session["next_actor"],
                    roster=roster,
                    enforce_mobile_limits=bool(
                        workflow and config.enforce_mobile_output
                    ),
                    budget=generation_budget,
                )
                await self._raise_if_operation_cancelled(turn_operation_id)
            except Exception as exc:
                if isinstance(exc, TavernOperationCancelled):
                    raise
                await self.database.update_operation(
                    turn_operation_id,
                    status="failed_retryable",
                    phase="story_plan_failed",
                    result={
                        "error_type": type(exc).__name__,
                        "error": clean_text(str(exc), max_chars=500),
                    },
                )
                raise
        await self.database.update_operation(
            turn_operation_id,
            phase="story_plan_generated",
            status="generating",
        )

        dice: DiceResult | None = None
        check_request = None
        first_mode = resolution.mode
        if (
            workflow
            and workflow.get("requires_check")
            and first_mode != "check"
        ):
            check_request = self._check_request_from_locked_choice(
                workflow
            )
            resolution = replace(
                resolution,
                mode="check",
                narrative="",
                check=check_request,
            )
            first_mode = "check"
        if (
            workflow
            and not workflow.get("requires_check")
            and first_mode == "check"
        ):
            raise TavernEngineError(
                "该选项未提前标记检定与风险，但模型临时申请投骰；"
                "为避免隐藏加码，本轮没有提交"
            )
        if resolution.mode == "check":
            if resolution.check is None:
                raise TavernEngineError("模型检定结构缺失")
            check_request = resolution.check
            # 0.11.3：记录骰值锁定键，用于“本轮未提交”时作废已锁骰值。
            operation_id: str | None = None
            selected_choice = (
                dict(workflow.get("selected_choice") or {})
                if workflow
                and isinstance(workflow.get("selected_choice"), Mapping)
                else {}
            )
            locked_advantages = tuple(
                selected_choice.get("advantage_sources") or ()
            )
            locked_disadvantages = tuple(
                selected_choice.get("disadvantage_sources") or ()
            )
            effective_stat = str(
                selected_choice.get("check_stat")
                or check_request.stat
                or "通用"
            )
            authoritative = await self.database.authoritative_modifier(
                session_id, sender_id, effective_stat,
            )
            if world_contract(world)["resolution"]["mode"] == "attribute" and not authoritative.get("matched"):
                raise TavernEngineError(f"检定属性“{effective_stat}”不属于当前世界或角色卡，本轮没有投骰")
            check_context = await self.database.check_context(
                session_id,
                sender_id,
                str(authoritative["stat"]),
                proposed_advantages=check_request.advantage_sources,
                proposed_disadvantages=check_request.disadvantage_sources,
                locked_advantages=locked_advantages,
                locked_disadvantages=locked_disadvantages,
            )
            inspiration_mode = str(
                workflow.get("inspiration_mode") if workflow else ""
            ).lower()
            if inspiration_mode:
                inspiration = await self.database.inspiration_status(
                    session_id,
                    sender_id,
                )
                if inspiration["balance"] < 1:
                    raise TavernEngineError("灵感点不足，本轮没有投骰")
            check_type = str(
                selected_choice.get("check_type")
                or check_request.check_type
                or "standard"
            )
            if (
                inspiration_mode
                and check_type in {"group", "resistance"}
            ):
                raise TavernEngineError(
                    "集体检定与独立抵抗不能由一名玩家替全队消耗灵感"
                )
            dice_visibility = str(
                (rule_state.get("dice_rules") or {}).get(
                    "visibility",
                    "public",
                )
            ).lower()
            risk = str(
                selected_choice.get("risk")
                or check_request.risk
                or "controlled"
            )
            known_consequences = str(
                selected_choice.get("known_consequences")
                or check_request.known_consequences
                or ""
            )
            if risk == "lethal" and not known_consequences:
                raise TavernEngineError(
                    "致命风险没有提前明示已知后果，本轮没有投骰"
                )
            check_request = replace(
                check_request,
                stat=str(authoritative["stat"]),
                modifier=int(authoritative["modifier"]),
                difficulty=int(
                    selected_choice.get("difficulty")
                    or check_request.difficulty
                ),
                risk=risk,
                check_type=check_type,
                advantage_sources=tuple(check_context["advantages"]),
                disadvantage_sources=tuple(
                    check_context["disadvantages"]
                ),
                known_consequences=known_consequences,
                visibility=(
                    dice_visibility
                    if dice_visibility
                    in {"public", "immersive", "hidden"}
                    else "public"
                ),
                inspiration_mode=inspiration_mode,
                opponent_modifier=0,
            )
            if workflow is not None:
                workflow = {
                    **dict(workflow),
                    "assist_token_id": check_context.get(
                        "assist_token_id",
                        "",
                    ),
                }
            operation_id = operation_key(
                session_id,
                "dice",
                turn_no=operation_turn,
                actor_id=sender_id,
                source_id=str(
                    workflow.get("choice_set_id")
                    if workflow
                    else session["revision"]
                ),
                # 0.11.2：骰值锁定键必须含检定类别与所选选项。
                # 旧实现只含 session_id+choice_set_id，导致同选项集内
                # “魅力检定”与“信仰检定”命中同一键、复用上一轮骰值。
                payload={
                    "selected_key": (
                        str(workflow.get("selected_key") or "")
                        if workflow
                        else ""
                    ),
                    "stat": str(check_request.stat or "").casefold(),
                    "check_type": str(
                        check_request.check_type or ""
                    ).casefold(),
                },
            )
            receipt = await self.database.get_operation_receipt(
                operation_id
            )
            if receipt:
                locked_request = self._check_request_from_payload(
                    receipt["request"]
                )
                same_category = (
                    str(locked_request.stat or "").casefold()
                    == str(check_request.stat or "").casefold()
                    and str(
                        locked_request.check_type or ""
                    ).casefold()
                    == str(check_request.check_type or "").casefold()
                )
                if not same_category:
                    # 0.11.2 双保险：即使键碰撞，类别不同也绝不复用旧骰值。
                    receipt = None
                else:
                    if (
                        locked_request.inspiration_mode
                        != check_request.inspiration_mode
                    ):
                        raise TavernEngineError(
                            "本次检定的骰池已经锁定，不能在重试时更换灵感用法"
                        )
                    check_request = locked_request
                    dice = self._dice_result_from_payload(receipt["result"])
            else:
                if check_type in {"group", "resistance"}:
                    requested_ids = set(check_request.participant_ids)
                    actors: list[dict[str, Any]] = []
                    for member in await self.database.list_roster(
                        session_id
                    ):
                        if (
                            member.get("participation_status") != "active"
                            or member.get("card_status") != "approved"
                        ):
                            continue
                        if requested_ids and not (
                            {
                                str(member.get("id") or ""),
                                str(member.get("group_user_id") or ""),
                            }
                            & requested_ids
                        ):
                            continue
                        member_user_id = str(
                            member.get("group_user_id") or ""
                        )
                        member_modifier = (
                            await self.database.authoritative_modifier(
                                session_id,
                                member_user_id,
                                check_request.stat,
                            )
                        )
                        member_context = (
                            await self.database.check_context(
                                session_id,
                                member_user_id,
                                str(member_modifier["stat"]),
                                proposed_advantages=(
                                    check_request.advantage_sources
                                ),
                                proposed_disadvantages=(
                                    check_request.disadvantage_sources
                                ),
                                locked_advantages=locked_advantages,
                                locked_disadvantages=locked_disadvantages,
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
                    dice = await self._roll_with_registered_system(
                        world, check_request, actors=actors
                    )
                else:
                    dice = await self._roll_with_registered_system(
                        world, check_request
                    )
                receipt = await self.database.lock_check_result(
                    operation_id,
                    session_id,
                    asdict(check_request),
                    asdict(dice),
                )
                check_request = self._check_request_from_payload(
                    receipt["request"]
                )
                dice = self._dice_result_from_payload(
                    receipt["result"]
                )
            await self.database.update_operation(
                turn_operation_id,
                phase="dice_locked",
                status="dice_locked",
                result={
                    "dice_operation_id": operation_id,
                    "outcome": dice.outcome,
                },
            )
            # C6：检定事件在回提交成功后再投递（hook 不得观察
            # 未提交状态），且投递不阻塞回合提交路径。
            check_event = {
                "type": "check",
                "hook": "check_completed",
                "session_id": session_id,
                "actor": sender_name,
                "stat": check_request.stat,
                "outcome": dice.outcome,
                "total": dice.total,
                "difficulty": dice.difficulty,
            }
            if not generation_notice_sent:
                dice_text = self._format_dice_result(
                    dice,
                    check_request.stat,
                )
                await self._emit_operation_progress(
                    turn_operation_id,
                    "check_resolved",
                    progress,
                    dice_text or "【检定结果】检定已经锁定。",
                )
                generation_notice_sent = True
            else:
                dice_text = self._format_dice_result(
                    dice, check_request.stat
                )
                if dice_text:
                    await self._emit_operation_progress(
                        turn_operation_id,
                        "check_resolved",
                        progress,
                        dice_text,
                    )
            check_prompt = checked_resolution_prompt(
                world=world,
                session=session,
                player=player,
                player_input=player_input,
                events=events,
                memories=memories,
                check=asdict(check_request),
                dice=asdict(dice),
            )
            second_stage_providers = self._provider_order(
                used_provider_id,
                tuple(provider_ids),
            )
            await self._raise_if_operation_cancelled(turn_operation_id)
            resolution, used_provider_id = (
                await self._generate_resolution(
                    session_id=session_id,
                    request_type="story_checked",
                    world=world,
                    provider_ids=second_stage_providers,
                    system=system_prompt(
                        world,
                        allow_check=False,
                        capability_projection=capability_projection,
                    ),
                    prompt=check_prompt,
                    config=config,
                    narrative_mode=session.get("narrative_mode"),
                    player_input=player_input,
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
                    opening=state.opening_projection is not None,
                    expected_actor=session["next_actor"],
                    roster=roster,
                    enforce_mobile_limits=bool(
                        workflow and config.enforce_mobile_output
                    ),
                    budget=generation_budget,
                )
            )
            await self._raise_if_operation_cancelled(turn_operation_id)
            if resolution.mode != "resolve":
                raise TavernEngineError("模型未完成检定后的最终裁定")

        state.workflow = workflow
        state.resolution = resolution
        state.used_provider_id = used_provider_id
        state.dice = dice
        state.check_request = check_request
        state.first_mode = first_mode
        state.generation_notice_sent = generation_notice_sent
        state.operation_id = operation_id
        state.check_event = check_event
