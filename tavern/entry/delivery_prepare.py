from __future__ import annotations

from .plugin_shared import *




class DeliveryPrepareMixin:
    async def _run_due_ai_turns(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        if not session_id:
            return None
        runner = getattr(self, "ai_turn_runner", None)
        if runner is None:
            return None
        try:
            return await runner.run_due(session_id, max_steps=8)
        except Exception:
            logger.exception(
                "321开团 AI 队友调度失败：session=%s",
                session_id,
            )
            return None
    async def _send_event_text(
        self,
        event: AstrMessageEvent,
        text: PlayerMessage | str,
    ) -> bool:
        """Send one event-bound BOT message through the Markdown result path.

        This must use ``event.send``. Routing through ``context.send_message``
        turns a reply into a proactive send; QQOfficial's proactive adapter in
        the supported AstrBot host does not consume the Markdown marker.
        """

        result = await self._message_result(event, text)
        if result is None:
            return False
        sender = getattr(event, "send", None)
        if not callable(sender):
            logger.warning("321开团事件 Markdown 发送不可用：event.send 缺失")
            return False
        try:
            await sender(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "321开团事件 Markdown 发送失败：%s: %s",
                type(exc).__name__,
                str(exc)[:160],
            )
            return False
        return True
    async def _send_event_parts(
        self,
        event: AstrMessageEvent,
        parts: Sequence[PlayerMessage | str],
    ) -> list[PlayerMessage | str]:
        """Send ordered parts and return only parts that could not be sent."""

        delivered = getattr(self, "_turn_message_dedupes", None)
        if not isinstance(delivered, set):
            delivered = set()
            self._turn_message_dedupes = delivered
        unsent, receipts, delivered = await send_ordered_parts(
            parts,
            send=lambda part: self._send_event_text(event, part),
            delivered_dedupes=delivered,
        )
        self._turn_message_dedupes = delivered
        self._last_turn_delivery_receipts = receipts
        return unsent
    @staticmethod
    def _render_unsent_parts(
        parts: Sequence[PlayerMessage | str],
    ) -> str:
        return "\n\n".join(
            rendered
            for rendered in (
                render_player_message(part)
                if isinstance(part, PlayerMessage)
                else str(part or "").strip()
                for part in parts
            )
            if rendered
        )
    async def _send_engine_reply(
        self,
        event: AstrMessageEvent,
        reply: Any,
    ) -> list[PlayerMessage | str]:
        bundle = getattr(reply, "message_bundle", None)
        if not isinstance(bundle, TurnMessageBundle) or not bundle.parts:
            return await self._send_event_parts(event, reply_message_parts(reply))
        return await self._send_turn_bundle(event, bundle)
    async def _send_turn_bundle(
        self,
        event: AstrMessageEvent,
        bundle: TurnMessageBundle,
    ) -> list[PlayerMessage | str]:
        """Persist an ordered bundle before the first platform send."""

        delivery_platform = (
            "qq_official"
            if qqbot_markdown_for_event(event)
            else (self._event_origin(event) or self._platform_id(event))
        )
        bundle = split_turn_bundle_for_delivery(bundle, delivery_platform)
        stored = await self.database.prepare_turn_delivery(
            session_id=bundle.session_id,
            operation_id=bundle.operation_id,
            actor_id=str(event.get_sender_id() or ""),
            state_revision=bundle.state_revision,
            origin=self._event_origin(event),
            parts=[
                {
                    "kind": part.kind,
                    "message_type": part.message.message_type,
                    "dedupe_key": part.dedupe_key,
                    "payload": serialize_player_message(part.message),
                    "rendered_text": render_player_text(part.message),
                }
                for part in bundle.parts
            ],
        )
        return await self._send_persisted_turn_run(event, stored)
    async def _send_committed_narrative(
        self,
        event: AstrMessageEvent,
        result: Mapping[str, Any],
        *,
        title: str,
        source: str,
        notice: str = "",
    ) -> list[PlayerMessage | str]:
        """Deliver only the NarrativeDocument already committed by the engine.

        The document remains attached until the platform-specific block-boundary
        split is complete.  The persisted run then makes retries resume from the
        first unconfirmed physical part without invoking the model again.
        """

        document = result.get("narrative_document")
        if not isinstance(document, Mapping):
            raise ValueError(
                "已提交故事缺少 NarrativeDocument，已停止投递"
            )
        story = PlayerMessage.dynamic(
            title=title,
            summary=str(result.get("narrative") or ""),
            source=source,
        )
        story = replace(
            story,
            data={"delivery_narrative_document": dict(document)},
        )
        messages: list[tuple[str, PlayerMessage]] = [("story", story)]
        if str(notice or "").strip():
            messages.append(
                (
                    "notice",
                    PlayerMessage.from_text(
                        str(notice).strip(),
                        default_title="主持状态",
                    ),
                )
            )
        bundle = TurnMessageBundle.build(
            session_id=str(result.get("session_id") or ""),
            operation_id=str(
                result.get("operation_id") or result.get("event_id") or ""
            ),
            actor_id=str(event.get_sender_id() or ""),
            state_revision=str(
                result.get("revision") or result.get("turn_no") or ""
            ),
            messages=messages,
        )
        return await self._send_turn_bundle(event, bundle)
    async def _send_persisted_turn_run(
        self,
        event: AstrMessageEvent,
        run: Mapping[str, Any],
    ) -> list[PlayerMessage | str]:
        """Resume a persisted ordered sequence from its first unconfirmed part."""

        run_id = str(run.get("run_id") or "")
        raw_parts = [
            item for item in (run.get("parts") or ()) if isinstance(item, Mapping)
        ]
        messages = [
            deserialize_player_message(item.get("payload") or {})
            for item in raw_parts
        ]
        delivered = set(run.get("delivered_dedupes") or ())

        async def before_send(receipt: dict[str, Any]) -> None:
            await self.database.mark_turn_delivery_sending(
                run_id,
                int(receipt.get("part_index") or 0),
            )

        async def receipt_sink(receipt: dict[str, Any]) -> None:
            await self.database.record_turn_delivery_receipt(
                run_id,
                int(receipt.get("part_index") or 0),
                str(receipt.get("status") or "failed"),
                error=(
                    "平台没有确认本段消息送达"
                    if receipt.get("status") == "failed"
                    else ""
                ),
            )

        unsent, receipts, delivered = await send_ordered_parts(
            messages,
            send=lambda part: self._send_event_text(event, part),
            delivered_dedupes=delivered,
            before_send=before_send,
            receipt_sink=receipt_sink,
        )
        self._turn_message_dedupes = delivered
        self._last_turn_delivery_receipts = receipts
        return unsent
    async def _resume_latest_turn_delivery(
        self,
        event: AstrMessageEvent,
        *,
        session_id: str,
        sender_id: str,
        roles: set[str],
        is_admin: bool,
    ) -> str | None:
        runs = await self.database.list_turn_delivery_runs(session_id, limit=20)
        active = {
            "pending",
            "sending",
            "partially_sent",
            "retry_wait",
        }
        origin = self._event_origin(event)
        run = next(
            (
                item
                for item in runs
                if str(item.get("status") or "") in active
                and str(item.get("origin") or "") == origin
            ),
            None,
        )
        if run is None:
            return None
        authorized = (
            is_admin
            or bool(set(roles) & {"host", "moderator"})
            or str(run.get("actor_id") or "") == str(sender_id or "")
        )
        if not authorized:
            return (
                "【续发本轮失败】\n\n"
                "失败操作：发送本轮尚未确认的消息。\n\n"
                "原因：只有原发起者、主持人或管理员可以续发。\n\n"
                "自动处理：系统保留已送达回执和剩余位置，没有重复结算。\n\n"
                "下一步：请联系原发起者或主持人发送 /团 重试本轮。"
            )
        unsent = await self._send_persisted_turn_run(event, run)
        if unsent:
            return (
                "【本轮消息仍未全部送达】\n\n"
                "失败操作：续发本轮剩余消息。\n\n"
                "原因：平台仍未确认当前消息段送达。\n\n"
                "自动处理：逐段回执和剩余位置已经保存，世界状态没有重复结算。\n\n"
                "下一步：请稍后再次发送 /团 重试本轮。"
            )
        return (
            "【本轮消息已续发】\n\n"
            "此前未确认的消息已按原顺序发送；已送达内容没有重复发送。"
        )
    def _card_request_context(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id() or "")
        roles = (
            ("admin",)
            if self.runtime_config().is_admin(sender_id)
            else ()
        )
        return from_astrbot_event(
            event,
            roles=roles,
            metadata={"transport_event_id": transport_event_id(event)},
            platform_id=self._platform_id(event),
            user_id=sender_id,
            is_private=True,
        )
    async def _dispatch_card_intents(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        result: CommandResult,
    ) -> tuple[bool, str | None]:
        """Execute platform side effects declared by ``CardCommandService``."""

        for intent in result.delivery:
            payload = (
                dict(intent.payload)
                if isinstance(intent.payload, Mapping)
                else {}
            )
            if intent.kind == INTENT_PRIVATE_REPLY:
                text = str(payload.get("text") or "").strip()
                if text and not await self._send_event_text(event, text):
                    return False, (
                        "【选择已记录，但确认消息发送失败】\n"
                        "系统已经保存你的选择；以下是下一步内容。\n\n"
                        + str(result.text or "")
                    )
            elif intent.kind == INTENT_CANDIDATE_BUNDLE:
                handled, fallback = await self._deliver_card_candidate_bundle(
                    event,
                    str(result.text or ""),
                    command,
                )
                if handled or fallback:
                    return handled, fallback
            elif intent.kind == INTENT_PERSIST_VERIFIED_TARGET:
                target = payload.get("target")
                if isinstance(target, DeliveryTarget):
                    await self._persist_delivery_target(
                        target,
                        session_id=str(payload.get("session_id") or ""),
                        verified=True,
                        source="verified_private",
                    )
            elif intent.kind == INTENT_GROUP_NOTICE:
                await self._send_or_queue(
                    session_id=str(payload.get("session_id") or ""),
                    origin=str(payload.get("origin") or ""),
                    text=str(payload.get("text") or ""),
                    kind=str(payload.get("kind") or "card.created"),
                    dedupe_key=str(payload.get("dedupe_key") or ""),
                )
            elif intent.kind == INTENT_REVOKE_PRIVATE_TARGET:
                await self._revoke_private_delivery_target(
                    platform_id=str(payload.get("platform_id") or ""),
                    user_id=str(payload.get("user_id") or ""),
                    reason=str(payload.get("reason") or "card_service"),
                )
        return False, None
    async def _dispatch_session_intents(
        self,
        event: AstrMessageEvent,
        result: CommandResult,
    ) -> None:
        """Execute side effects declared by ``SessionCommandService``."""

        for intent in result.delivery:
            payload = (
                dict(intent.payload)
                if isinstance(intent.payload, Mapping)
                else {}
            )
            if intent.kind == INTENT_BROKER_EVENT:
                await self.broker.publish(payload)
            elif intent.kind == INTENT_SYNC_GROUP_TARGET:
                await self._sync_group_delivery_target(
                    event=event,
                    session_id=str(payload.get("session_id") or ""),
                )
            elif intent.kind == INTENT_REVOKE_PRIVATE_TARGET:
                await self._revoke_private_delivery_target(
                    platform_id=str(payload.get("platform_id") or ""),
                    user_id=str(payload.get("user_id") or ""),
                    reason=str(payload.get("reason") or "session_service"),
                )
    async def _handle_session_command_service(
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
        """Run the D1 session application service for its owned actions."""

        if command.action not in SESSION_ACTIONS or command.action == "worlds":
            return False, None
        if command.action == "retry_turn" and session:
            resumed = await self._resume_latest_turn_delivery(
                event,
                session_id=str(session.get("id") or ""),
                sender_id=sender_id,
                roles=roles,
                is_admin=is_admin,
            )
            if resumed is not None:
                return True, resumed
        role_names = set(roles)
        if is_admin:
            role_names.add("admin")
        if session and any(
            str(item.get("group_user_id") or "") == sender_id
            for item in await self.database.list_roster(str(session["id"]))
        ):
            role_names.add("player")
        ctx = from_astrbot_event(
            event,
            session_id=str((session or {}).get("id") or ""),
            roles=role_names,
            metadata={"transport_event_id": transport_event_id(event)},
            platform_id=platform_id,
            group_id=group_id,
            user_id=sender_id,
            is_private=False,
        )
        service = SessionCommandService(
            _SessionCommandGateway(self, event)
        )
        result = await service.handle(ctx, command)
        if result is None or not result.handled:
            return False, None
        await self._dispatch_session_intents(event, result)
        if command.action in {"perform", "resume"} and session:
            await self._run_due_ai_turns(str(session.get("id") or ""))
        return True, result.text
    async def _handle_admin_command_service(
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
        """Run the D1 platform-neutral administration service."""

        if command.action not in self.admin_commands.handles():
            return False, None
        role_names = set(roles)
        if is_admin:
            role_names.add("admin")
        ctx = from_astrbot_event(
            event,
            session_id=str((session or {}).get("id") or ""),
            roles=role_names,
            metadata={"transport_event_id": transport_event_id(event)},
            platform_id=platform_id,
            group_id=group_id,
            user_id=sender_id,
            is_private=False,
        )
        result = await self.admin_commands.handle(ctx, command)
        if not result.handled:
            return False, None
        return True, result.text
    async def _handle_growth_command_service(
        self,
        event: AstrMessageEvent,
        command: ParsedCommand,
        *,
        session_id: str = "",
        roles: Sequence[str] = (),
        is_private: bool,
    ) -> tuple[bool, str | None]:
        """Run the D1 player-facing growth command service."""

        if command.action != "growth":
            return False, None
        sender_id = str(event.get_sender_id() or "")
        ctx = from_astrbot_event(
            event,
            session_id=session_id,
            roles=roles,
            metadata={"transport_event_id": transport_event_id(event)},
            platform_id=self._platform_id(event),
            group_id="" if is_private else self._group_id(event),
            user_id=sender_id,
            is_private=is_private,
        )
        result = await self.growth_commands.handle(ctx, command)
        if not result.handled:
            return False, None
        event.stop_event()
        return True, result.text
    async def _finalize_pending_terminal(
        self,
        session_id: str,
        receipt: Mapping[str, Any],
        request: DMRequest,
    ) -> dict[str, Any]:
        """Consume one manual terminal receipt through the atomic finalizer."""

        raw_payload = receipt.get("payload_json")
        if isinstance(raw_payload, Mapping):
            payload = dict(raw_payload)
        else:
            try:
                payload = json.loads(str(raw_payload or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
        match = (
            dict(payload.get("match"))
            if isinstance(payload.get("match"), Mapping)
            else {}
        )
        termination_type = str(
            receipt.get("termination_type") or "completed"
        ).strip().lower()
        if termination_type not in {"completed", "failed", "aborted"}:
            termination_type = "completed"
        reason = str(
            payload.get("reason")
            or receipt.get("condition_label")
            or "世界终局条件已由主持确认"
        ).strip()
        result = await self.database.finalize_session(
            session_id,
            request.actor,
            termination_type=termination_type,
            reason=reason,
            terminal_match=match,
            trigger_revision=int(receipt.get("trigger_revision") or 0),
        )
        await self.engine.release_session_lock(session_id)
        label = str(receipt.get("condition_label") or "世界终局").strip()
        return {
            "ok": True,
            "status": "finalized",
            "message": (
                f"【终局已确认】《{label}》已完成最终保护存档并永久只读归档。"
            ),
            "result": result,
        }
    def _turn_request_context(
        self,
        event: AstrMessageEvent,
        *,
        platform_id: str,
        group_id: str,
        sender_id: str,
        is_admin: bool,
        is_moderator: bool,
        is_active_dm: bool,
    ) -> TurnRequestContext:
        sender_name = str(event.get_sender_name() or "").strip()
        return TurnRequestContext(
            platform=platform_id,
            group_id=group_id,
            user_id=sender_id,
            user_name=sender_name,
            roles=frozenset(
                role
                for role, enabled in (
                    ("admin", is_admin),
                    ("moderator", is_moderator),
                    ("host", is_active_dm),
                )
                if enabled
            ),
            metadata={
                "sender_name": sender_name,
            },
            trigger_prefix=self.runtime_config().trigger_prefix,
        )
    @staticmethod
    def _turn_command_result_text(result: TurnCommandResult) -> str | None:
        if result.error is not None:
            return result.error.message + (
                "\n" + result.error.recovery
                if result.error.recovery
                else ""
            )
        if result.text:
            return result.text
        if result.parts:
            return "\n\n".join(part for part in result.parts if part)
        return None
    async def _execute_turn_engine_request(
        self,
        event: AstrMessageEvent,
        request: Any,
        *,
        sender_id: str,
    ) -> str:
        params = dict(request.params)
        method = str(request.method or "")
        if method in {
            "process_choice",
            "reroll_choices",
            "process_team_proposal",
            "use_skill",
            "process_vote_resolution",
        }:
            params["event"] = event
        if method in {"process_choice", "use_skill", "process_vote_resolution"}:
            params["progress"] = lambda text: self._send_event_text(event, text)
        engine_method = getattr(self.engine, method, None)
        if engine_method is None or not callable(engine_method):
            raise TavernEngineError(f"不支持的回合引擎操作：{method}")
        reply = await engine_method(**params)
        session_id = str(params.get("session_id") or "")
        if method in {"process_choice", "use_skill"} and session_id:
            await self._open_supplements_after_progress(session_id)
        if method in {
            "process_choice",
            "use_skill",
            "process_vote_resolution",
        } and session_id:
            await self._run_due_ai_turns(session_id)
        if request.render == "choice_set":
            participant = reply.get("participant") or (
                await self.database.get_participant(
                    session_id,
                    user_id=str(params.get("sender_id") or sender_id),
                )
            )
            return format_choices(
                participant.get("character_name")
                or participant.get("display_name"),
                reply["choices"],
                rerolls_left=max(0, 1 - int(reply["reroll_count"])),
                trigger_prefix=str(
                    request.render_kwargs.get("trigger_prefix")
                    or self.runtime_config().trigger_prefix
                ),
            )
        if request.render == "skip_result":
            turn = reply
            if turn.get("current_user_id"):
                turn = await self.database.designate_turn(
                    session_id,
                    turn["current_user_id"],
                    sender_id,
                )
            headline = str(request.render_kwargs.get("headline") or "【回合已推进】")
            return headline + "\n" + format_turn_status(turn)
        unsent = await self._send_engine_reply(event, reply)
        if not unsent:
            return ""
        return (
            "【故事已结算，但消息未全部送达】\n\n"
            "失败操作：发送本轮剩余消息。\n\n"
            "原因：平台未确认当前消息段送达。\n\n"
            "自动处理：已经送达的消息不会重复发送，世界状态不会重复结算。\n\n"
            "下一步：请稍后发送 /团 重试本轮。\n\n"
            + self._render_unsent_parts(unsent)
        )
