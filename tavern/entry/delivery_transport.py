from __future__ import annotations

from .plugin_shared import *




class DeliveryTransportMixin:
    async def _handle_tactical_command_service(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        *,
        session: Mapping[str, Any] | None,
        roles: set[str],
        is_admin: bool,
        platform_id: str,
        group_id: str,
        sender_id: str,
    ) -> tuple[bool, str | None]:
        if command.action not in TACTICAL_ACTIONS | CHALLENGE_ACTIONS:
            return False, None
        if not session:
            return True, "【战术冲突】当前群没有活动副本。\n下一步：请先发送 /团 状态。"
        role_names = set(roles)
        if is_admin:
            role_names.add("admin")
        roster = await self.database.list_roster(str(session["id"]))
        if any(
            str(item.get("group_user_id") or "") == sender_id
            and str(item.get("participation_status") or "") not in {"retired", "archived"}
            for item in roster
        ):
            role_names.add("player")
        ctx = from_astrbot_event(
            event,
            session_id=str(session["id"]),
            roles=role_names,
            metadata={"transport_event_id": transport_event_id(event)},
            platform_id=platform_id,
            group_id=group_id,
            user_id=sender_id,
            is_private=False,
        )
        service = self.tactical_commands if command.action in TACTICAL_ACTIONS else self.challenge_commands
        result = await service.handle(ctx, command, self.database)
        return result.handled, result.text

    async def _execute_vote_cast(
        self,
        event: AstrMessageEvent,
        request: Any,
        *,
        sender_id: str,
    ) -> str:
        outcome = await self.database.cast_vote(
            request.session_id,
            request.user_id,
            request.option_key,
        )
        render = render_vote_outcome(outcome)
        if render.text is not None:
            return render.text
        if render.broker_event is not None:
            await self.broker.publish(render.broker_event)
        if render.engine_request is None:
            return "【投票已记录】系统已保存你的选择。"
        vote = render.engine_request.params.get("vote") or {}
        winner = str(vote.get("winner_key") or "")
        await self._send_event_text(
            event,
            PlayerMessage.dynamic(
                title="表决通过",
                summary=f"多数选择 {winner}，正在落实故事结果。",
                sections=(
                    "自动处理：本次决定已经锁定，世界状态尚未改变。",
                ),
                actions=("/团 取消",),
                source="story_generation_progress",
            ),
        )
        try:
            reply = await self.engine.process_vote_resolution(
                event=event,
                session_id=str(render.engine_request.params["session_id"]),
                vote=render.engine_request.params["vote"],
                progress=lambda text: self._send_event_text(event, text),
            )
        except TavernOperationCancelled as exc:
            return str(exc)
        except (TavernEngineError, ValueError) as exc:
            operation_id = str(
                vote.get("resolution_operation_id")
                or f"vote-resolution:{vote.get('id') or ''}"
            )
            try:
                state = await self.database.get_operation_state(operation_id)
                if str((state or {}).get("status") or "") not in {
                    "cancelled",
                    "completed",
                    "needs_recovery",
                }:
                    await self.database.update_operation(
                        operation_id,
                        status="failed_retryable",
                        phase="vote_resolution_failed",
                        result={
                            "error_type": type(exc).__name__,
                            "error": str(exc)[:500],
                        },
                    )
                    await self.database.update_vote_resolution_status(
                        str(vote.get("id") or ""),
                        "failed_retryable",
                    )
            except Exception as receipt_exc:
                logger.warning("321开团表决失败回执更新异常：%s", receipt_exc)
            fallback = render.fallback
            await self.database.write_audit(
                request.session_id,
                sender_id,
                fallback.audit_action if fallback else "vote.resolution_failed",
                "",
                {"error": str(exc)[:500]},
            )
            logger.warning("321开团表决推进失败：%s", exc)
            if fallback is not None and fallback.kind == "restore_actor_choices":
                try:
                    await self.database.restore_actor_choices(**fallback.params)
                except Exception as restore_exc:
                    logger.warning(
                        "321开团表决失败恢复兜底选项异常：%s",
                        restore_exc,
                    )
            if isinstance(exc, TavernStoryGenerationError):
                return render_player_message(
                    story_generation_failure_message(
                        exc,
                        operation="落实表决并生成故事",
                    )
                )
            template = render.failure_text or (
                "【表决推进失败】故事推进暂未完成：{error}"
            )
            return template.format(error=exc)
        await self._run_due_ai_turns(request.session_id)
        unsent = await self._send_engine_reply(event, reply)
        if not unsent:
            return ""
        return (
            "【表决已经结算，但消息未全部送达】\n"
            "系统不会重复结算投票；请稍后发送 /团 重试本轮。\n\n"
            + self._render_unsent_parts(unsent)
        )
    async def _handle_turn_vote_command_service(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        *,
        platform_id: str,
        group_id: str,
        sender_id: str,
        is_admin: bool,
        is_moderator: bool,
        is_active_dm: bool,
    ) -> tuple[bool, str | None]:
        if command.action not in TurnCommandHandler.ACTIONS | {"vote"}:
            return False, None
        ctx = self._turn_request_context(
            event,
            platform_id=platform_id,
            group_id=group_id,
            sender_id=sender_id,
            is_admin=is_admin,
            is_moderator=is_moderator,
            is_active_dm=is_active_dm,
        )
        handler = self.vote_commands if command.action == "vote" else self.turn_commands
        try:
            result = await handler.handle(ctx, command, self.database)
        except (DatabaseNotFoundError, PermissionError, ValueError) as exc:
            return True, (
                "【开团】回合查询失败：" + str(exc) + "\n"
                "系统没有执行本次操作，也没有修改世界状态。\n"
                "下一步：查看 /团 顺序 后重试。"
            )
        if result is None:
            return False, None
        immediate = self._turn_command_result_text(result)
        if immediate is not None:
            return True, immediate
        try:
            outputs: list[str] = []
            for vote_cast in result.vote_casts or []:
                outputs.append(
                    await self._execute_vote_cast(
                        event,
                        vote_cast,
                        sender_id=sender_id,
                    )
                )
            for engine_request in result.engine_requests or []:
                outputs.append(
                    await self._execute_turn_engine_request(
                        event,
                        engine_request,
                        sender_id=sender_id,
                    )
                )
            for broker_event in result.broker_events or []:
                await self.broker.publish(broker_event)
            return True, "\n\n".join(item for item in outputs if item) or None
        except TavernTurnOrderError as exc:
            return True, f"【回合秩序】{exc}"
        except TavernBusyError as exc:
            return True, f"【开团】{exc}"
        except TavernPlayerDisabledError:
            return True, "【开团】你的玩家身份当前不可用。"
        except (TavernEngineError, DatabaseNotFoundError, ValueError) as exc:
            if isinstance(exc, TavernStoryGenerationError):
                return True, render_player_message(
                    story_generation_failure_message(
                        exc,
                        operation="结算回合操作",
                    )
                )
            return True, (
                "【开团】回合操作失败：" + str(exc) + "\n"
                "系统没有完成本次推进；已有世界状态保持不变。\n"
                "下一步：查看 /团 顺序 后重试，或发送 /团 帮助 回合。"
            )
    def _dm_command_service(self) -> DMCommandService:
        return DMCommandService(
            database=self.database,
            config=self.runtime_config(),
            delivery_service=self.delivery_service,
            terminal_finalizer=self._finalize_pending_terminal,
        )
    @staticmethod
    def _dm_result_text(result: Any) -> str:
        if result.ok:
            return str(result.message or "")
        error = result.error
        return (
            "【主持操作失败】\n"
            f"原因：{error.message if error else result.message}\n"
            "自动处理：系统没有完成本次主持操作。\n"
            f"下一步：{error.recovery if error else result.next_action}"
        )
    async def _handle_dm_command_service(
        self,
        event: AstrMessageEvent,
        *,
        session: Mapping[str, Any],
        sender_id: str,
        is_admin: bool,
        argument: str,
    ) -> str:
        raw = str(argument or "").strip()
        normalized = raw.replace(" ", "")
        request = DMRequest(
            session_id=str(session["id"]),
            user_id=sender_id,
            actor=sender_id,
            is_admin=is_admin,
            correlation_id=transport_event_id(event),
            group_origin=str(session.get("unified_origin") or ""),
        )
        service = self._dm_command_service()
        if normalized in {"终局确认", "确认终局"}:
            return self._dm_result_text(
                await service.execute(session["id"], request, "terminal_confirm")
            )
        sub, _, value = raw.partition(" ")
        sub = sub.strip()
        value = value.strip()
        if sub in {"", "状态"}:
            return self._dm_result_text(
                await service.execute(session["id"], request, "status")
            )
        if sub == "开启":
            result = await service.execute(
                session["id"],
                request,
                "enable_dm",
                {"dm_user_id": value or sender_id},
            )
            if result.ok:
                await self.broker.publish({
                    "type": "dm_control",
                    "hook": "dm_mode_enabled",
                    "session_id": session["id"],
                    "actor": str(result.data.get("active_dm_user_id") or sender_id),
                })
            return self._dm_result_text(result)
        if sub == "接管":
            return self._dm_result_text(
                await service.execute(session["id"], request, "takeover")
            )

        state_result = await service.execute(session["id"], request, "status")
        if not state_result.ok:
            return self._dm_result_text(state_result)
        if str(state_result.data.get("mode") or "") != "dm":
            return (
                "【主持操作失败】\n"
                "原因：当前未开启主持模式。\n"
                "自动处理：系统没有修改副本状态。\n"
                "下一步：请先发送 /团 主持 开启。"
            )
        if sub == "指引":
            return self._dm_result_text(
                await service.execute(
                    session["id"],
                    request,
                    "directive",
                    {"directive": value},
                )
            )
        if sub == "推进":
            try:
                result = await self.engine.process_dm_beat(
                    event=event,
                    session_id=session["id"],
                    dm_user_id=sender_id,
                    instruction=value,
                    progress=lambda text: self._send_event_text(event, text),
                )
                await self._open_supplements_after_progress(session["id"])
                unsent = await self._send_committed_narrative(
                    event,
                    result,
                    title=f"主持推进 · 第 {result['beat_no']} 段",
                    source="dm.story",
                )
                return (
                    self._render_unsent_parts(unsent)
                    if unsent
                    else ""
                )
            except (TavernEngineError, ValueError) as exc:
                return (
                    "【主持推进失败】\n"
                    f"原因：{exc}\n"
                    "自动处理：本段叙事未提交，现有世界状态保持不变。\n"
                    "下一步：检查主持指引后重试 /团 主持 推进。"
                )
        if sub == "直述":
            result = await service.execute(
                session["id"],
                request,
                "direct_narrative",
                {"narrative": value, "group_mode": "none"},
            )
            if result.ok:
                await self._open_supplements_after_progress(session["id"])
                delivery_result = {
                    **dict(result.data),
                    "session_id": str(session["id"]),
                    "narrative": value,
                }
                unsent = await self._send_committed_narrative(
                    event,
                    delivery_result,
                    title=(
                        "主持直述 · 第 "
                        f"{int(result.data.get('beat_no') or 0)} 段"
                    ),
                    source="dm.direct_story",
                )
                return (
                    self._render_unsent_parts(unsent)
                    if unsent
                    else ""
                )
            return self._dm_result_text(result)
        if sub == "交棒":
            result = await service.execute(
                session["id"],
                request,
                "handoff",
                {"target_ref": value},
            )
            if not result.ok or result.status != "handoff_npc":
                return self._dm_result_text(result)
            try:
                beat = await self.engine.process_dm_beat(
                    event=event,
                    session_id=session["id"],
                    dm_user_id=sender_id,
                    instruction=str(result.data.get("instruction") or ""),
                    progress=lambda text: self._send_event_text(event, text),
                )
                await self._open_supplements_after_progress(session["id"])
                npc_name = str(result.data.get("npc_name") or "非玩家角色")
                unsent = await self._send_committed_narrative(
                    event,
                    beat,
                    title=f"非玩家角色演出 · {npc_name}",
                    source="dm.npc_story",
                    notice="已回到等待真人主持人推进的状态。",
                )
                return (
                    self._render_unsent_parts(unsent)
                    if unsent
                    else ""
                )
            except (TavernEngineError, ValueError) as exc:
                return (
                    "【非玩家角色交棒失败】\n"
                    f"原因：{exc}\n"
                    "自动处理：交棒目标已记录，但演出叙事尚未提交。\n"
                    "下一步：发送 /团 主持 推进 继续该角色的行动。"
                )
        if sub == "自动":
            if not value:
                return (
                    "【主持操作失败】\n"
                    "原因：缺少要恢复行动的玩家角色。\n"
                    "自动处理：系统没有修改主持模式。\n"
                    "下一步：发送 /团 主持 自动 <玩家角色>。"
                )
            try:
                target = await self.database.get_participant(
                    session["id"],
                    participant_ref=value,
                )
                turn = await self.database.designate_turn(
                    session["id"],
                    target["group_user_id"],
                    sender_id,
                )
            except (DatabaseNotFoundError, ValueError) as exc:
                return (
                    "【主持操作失败】\n"
                    f"原因：{exc}\n"
                    "自动处理：系统没有关闭主持模式。\n"
                    "下一步：发送 /团 阵容 核对角色名称后重试。"
                )
            result = await service.execute(session["id"], request, "disable_dm")
            if not result.ok:
                return self._dm_result_text(result)
            await self.broker.publish({
                "type": "dm_control",
                "hook": "dm_mode_disabled",
                "session_id": session["id"],
                "actor": sender_id,
            })
            return "【已恢复 AI 自动模式】\n" + format_turn_status(turn)
        return (
            "【主持操作失败】\n"
            "原因：无法识别该主持命令。\n"
            "自动处理：系统没有修改副本状态。\n"
            "下一步：可用命令为开启、状态、指引、推进、直述、交棒、自动、接管、终局确认。"
        )
    async def _handle_dm_terminal_confirm(
        self,
        event: AstrMessageEvent,
        *,
        session: Mapping[str, Any],
        sender_id: str,
        is_admin: bool,
        argument: str,
    ) -> tuple[bool, str | None]:
        normalized = str(argument or "").strip().replace(" ", "")
        if normalized not in {"终局确认", "确认终局"}:
            return False, None
        request = DMRequest(
            session_id=str(session["id"]),
            user_id=sender_id,
            actor=sender_id,
            is_admin=is_admin,
            correlation_id=transport_event_id(event),
            group_origin=str(session.get("unified_origin") or ""),
        )
        result = await self._dm_command_service().execute(
            str(session["id"]),
            request,
            "terminal_confirm",
        )
        if result.ok:
            return True, result.message
        error = result.error
        return True, (
            "【终局确认失败】\n"
            f"原因：{error.message if error else result.message}\n"
            "自动处理：系统没有归档副本，待确认终局仍被保留。\n"
            f"下一步：{error.recovery if error else result.next_action}"
        )
    async def _handle_private_card_service(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        message: str,
        *,
        active_draft: Mapping[str, Any] | None,
    ) -> tuple[bool, str | None]:
        """Run the platform-neutral card application service."""

        if not active_draft and not (
            command.matched and command.action in PRIVATE_CARD_ACTIONS
        ):
            return False, None
        ctx = self._card_request_context(event)
        if active_draft:
            preflight = await self.card_commands.preflight_candidate_delivery(
                ctx,
                command,
            )
            if preflight is not None:
                event.stop_event()
                return True, preflight.text
        result = await self.card_commands.handle_private(
            ctx,
            command,
            message,
        )
        if not result.handled:
            return False, None
        event.stop_event()
        handled, fallback = await self._dispatch_card_intents(
            event,
            command,
            result,
        )
        if handled:
            return True, None
        return True, fallback or result.text
