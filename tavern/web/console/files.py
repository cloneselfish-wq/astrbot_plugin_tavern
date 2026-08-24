from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)
from ...contracts.narrative_document import (
    narrative_document_from_plain_text,
    narrative_document_to_plain_text,
)
from ...narrative_modes import narrative_mode_from_session


class ConsoleFileMethods:
    async def _send_group_text(
        self,
        session_id: str,
        origin: str,
        text: str,
        *,
        kind: str = "webui.notice",
        recipient: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """通过统一 DeliveryService 发送，并由持久化 outbox 接管失败重试。"""

        policy = "next_event"
        try:
            instance = await self.database.get_instance_config(session_id)
            policy = str(
                normalize_chat_experience(instance.get("world_snapshot") or {})
                ["delivery"]["proactive_fallback"]
            )
        except Exception:
            policy = "next_event"
        sensitive = kind == "dm.whisper"
        if policy == "webui_only":
            target = DeliveryTarget.webui_only(source="web_console_policy")
            message_kind = "webui_notice"
        else:
            target = DeliveryTarget.from_origin(
                origin,
                verified_binding=True,
                source="web_console",
            )
            message_kind = "dm_whisper" if sensitive else "group_notice"
        meta: dict[str, Any] = {
            "recipient_name": "目标角色" if sensitive else "当前群聊",
            "source_kind": kind,
        }
        if isinstance(recipient, Mapping):
            # D1-DEL-010：密语投递记录携带真实收件人身份，供投递状态与隐私过滤使用。
            recipient_name = str(
                recipient.get("character_name")
                or recipient.get("display_name")
                or ""
            ).strip()
            if recipient_name:
                meta["recipient_name"] = recipient_name
            meta["recipient_user_id"] = str(
                recipient.get("group_user_id") or ""
            )
            meta["recipient_participant_id"] = str(recipient.get("id") or "")
        if target is None:
            return {
                "ok": False,
                "status": "invalid_target",
                "reason": "当前会话缺少可用投递目标，消息未发送。",
                "queued": False,
            }
        outcome = await self.delivery_service.send(
            session_id=session_id,
            target=target,
            kind=message_kind,
            text=text,
            audience="private_owner" if sensitive else "group",
            dedupe_key=f"web:{session_id}:{kind}:{uuid.uuid4().hex}",
            projection={"kind": kind},
            meta=meta,
            actor=self._actor(),
        )
        if policy == "discard" and outcome.delivery_id and not outcome.ok:
            await self.delivery_service.cancel(
                outcome.delivery_id,
                actor=self._actor(),
                reason="world_policy_discard",
            )
            return {
                "ok": False,
                "status": "discarded",
                "delivery_id": outcome.delivery_id,
                "reason": "平台未送达；世界包策略要求不保留重试。",
                "queued": False,
            }
        payload = self._delivery_outcome_payload(outcome)
        payload["queued"] = bool(outcome.delivery_id and not outcome.ok)
        if payload["queued"]:
            await self.broker.publish(
                {
                    "type": "delivery",
                    "action": "queued",
                    "session_id": session_id,
                }
            )
        return payload

    def _build_whisper_record(
        self,
        session_id: str,
        participant: Mapping[str, Any] | None,
        text: str,
        actor: str,
    ) -> dict[str, Any]:
        """构造主持密语的待投递记录（enqueue-only，D1-DEL-010）。

        只入队不直发；有私聊来源时走 dm_whisper，缺失时降级为
        webui_only，绝不以群聊目标发送敏感密语。meta 携带真实收件人
        身份，供投递状态展示与普通玩家隐私过滤使用。
        """
        private_origin = str(
            (participant or {}).get("private_origin") or ""
        ).strip()
        target = DeliveryTarget.from_origin(
            private_origin,
            verified_binding=True,
            source="web_console_dm_whisper",
        )
        kind = "dm_whisper"
        if target is None:
            target = DeliveryTarget.webui_only(
                source="dm_whisper_no_private_origin"
            )
            kind = "webui_notice"
        meta: dict[str, Any] = {
            "recipient_name": "目标角色",
            "source_kind": "dm.whisper",
        }
        if isinstance(participant, Mapping):
            recipient_name = str(
                participant.get("character_name")
                or participant.get("display_name")
                or ""
            ).strip()
            if recipient_name:
                meta["recipient_name"] = recipient_name
            meta["recipient_user_id"] = str(
                participant.get("group_user_id") or ""
            )
            meta["recipient_participant_id"] = str(
                participant.get("id") or ""
            )
        return self.delivery_service.build_record(
            session_id=session_id,
            target=target,
            kind=kind,
            text=f"【主持密语】\n{text}",
            audience="private_owner",
            dedupe_key=f"web:{session_id}:dm.whisper:{uuid.uuid4().hex}",
            projection={"kind": "dm.whisper"},
            meta=meta,
            actor=actor,
        )

    @staticmethod
    def _delivery_outcome_payload(
        outcome: DeliveryOutcome,
    ) -> dict[str, Any]:
        return {
            "ok": bool(outcome.ok),
            "status": str(outcome.status or ""),
            "delivery_id": str(outcome.delivery_id or ""),
            "reason": str(outcome.reason or ""),
            "method": str(outcome.method or ""),
            "attempts": int(outcome.attempts or 0),
            "sent_parts": int(outcome.sent_parts or 0),
            "total_parts": int(outcome.total_parts or 0),
            "next_retry_at": str(outcome.next_retry_at or ""),
        }

    async def _delivery_status_views(
        self,
        session_id: str,
        *,
        viewer: str,
        limit: int,
        viewer_user_id: str = "",
        viewer_participant_id: str = "",
    ) -> list[dict[str, Any]]:
        """调用 DeliveryService.list_status 获取投递状态视图。

        D1-DEL-010：普通玩家只允许查看本人收件的投递状态。服务层按
        ``viewer="player:<身份>"`` 过滤收件人身份；身份优先使用群用户 ID
        （平台级身份），缺失时回退到副本参与者 ID。
        """
        viewer_arg = viewer
        if viewer == "player":
            identity = str(
                viewer_user_id or viewer_participant_id or ""
            ).strip()
            if identity:
                viewer_arg = f"player:{identity}"
        return await self.delivery_service.list_status(
            session_id,
            viewer=viewer_arg,
            limit=limit,
        )

    async def _viewer_participant_or_none(
        self,
        session_id: str,
        user: str,
    ) -> dict[str, Any] | None:
        """把 Web 登录名解析为副本参与者；无法匹配时返回 None。"""
        try:
            participant = await self.database.get_participant(
                session_id,
                user_id=user,
            )
            if isinstance(participant, Mapping):
                return dict(participant)
        except Exception:
            pass
        try:
            participant = await self.database.get_participant(
                session_id,
                participant_ref=user,
            )
            if isinstance(participant, Mapping):
                return dict(participant)
        except Exception:
            pass
        return await resolve_viewer_participant(
            self.database,
            session_id,
            user,
        )

    async def _resolve_participant_ref(
        self,
        session_id: str,
        reference: str,
    ) -> dict[str, Any]:
        """密语等 DM 操作的目标参与者解析：优先群用户 ID，再按角色名/代号。"""
        try:
            return await self.database.get_participant(
                session_id,
                user_id=reference,
            )
        except DatabaseNotFoundError:
            return await self.database.get_participant(
                session_id,
                participant_ref=reference,
            )

    async def deliveries(self):
        """返回 DeliveryStatusView，并通过 DeliveryService 重试或取消。"""

        try:
            self._username()
            principal = self._console_principal()
            method = str(getattr(request, "method", "GET") or "GET").upper()
            if method == "GET":
                session_id = str(request.query.get("session_id", "") or "")
                if not session_id:
                    raise ValueError("缺少 session_id：请先选择要查看的副本")
                viewer = "admin"
                viewer_user_id = ""
                viewer_participant_id = ""
                views = await self._delivery_status_views(
                    session_id,
                    viewer=viewer,
                    limit=int(request.query.get("limit", "100") or 100),
                    viewer_user_id=viewer_user_id,
                    viewer_participant_id=viewer_participant_id,
                )
                status_map = {
                    "pending": "waiting",
                    "leased": "waiting",
                    "partially_sent": "partial",
                    "retry_wait": "retry_wait",
                    "delivered": "delivered",
                    "permanently_failed": "failed",
                    "cancelled": "cancelled",
                    "webui_only": "waiting",
                }
                items = [
                    {
                        "id": str(item.get("delivery_id") or ""),
                        "recipient_name": str(
                            item.get("recipient_label")
                            or "收件人名称缺失"
                        ),
                        "verified": bool(item.get("verified")),
                        "status": status_map.get(
                            str(item.get("status") or ""),
                            "waiting",
                        ),
                        "status_label": str(
                            item.get("status_label") or "等待投递"
                        ),
                        "channel_label": str(
                            item.get("channel") or "平台消息"
                        ),
                        "sensitive": str(
                            item.get("message_type") or ""
                        ) in {"dm_whisper", "death_confirm"},
                        "sent_parts": int(item.get("sent_parts") or 0),
                        "total_parts": int(item.get("total_parts") or 0),
                        "attempts": int(item.get("attempts") or 0),
                        "next_retry_at": str(
                            item.get("next_retry_at") or ""
                        ),
                        "last_error": str(
                            item.get("last_error_message") or ""
                        ),
                        "can_retry": str(item.get("status") or "")
                        in {
                            "pending",
                            "partially_sent",
                            "retry_wait",
                        },
                        "can_cancel": str(item.get("status") or "")
                        in {
                            "pending",
                            "leased",
                            "partially_sent",
                            "retry_wait",
                        },
                    }
                    for item in views
                ]
                return json_response(
                    {
                        "schema": "DeliveryStatusView",
                        "items": items,
                    }
                )
            payload = await self._payload()
            delivery_id = str(payload.get("delivery_id") or "")
            action = str(payload.get("action") or "retry").strip().lower()
            if not delivery_id:
                raise ValueError("缺少 delivery_id")
            record = await self.database.get(delivery_id)
            if not record:
                raise ValueError("待投递通知不存在")
            record_session_id = str(record.get("session_id") or "")
            if not record_session_id:
                raise ValueError("待投递通知缺少所属副本，无法安全操作")
            if action == "cancel":
                outcome = await self.delivery_service.cancel(
                    delivery_id,
                    actor=self._actor(),
                    reason=str(payload.get("reason") or "web_cancelled"),
                )
            elif action == "retry":
                outcome = await self.delivery_service.deliver(
                    delivery_id,
                    actor=self._actor(),
                )
            else:
                raise ValueError("投递操作只支持 retry 或 cancel")
            await self.broker.publish(
                {
                    "type": "delivery",
                    "action": action,
                    "session_id": record_session_id,
                }
            )
            return json_response(self._delivery_outcome_payload(outcome))
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_forced_reroll(self):
        """由具备 DM 权限的后台用户代当前行动角色重整选项。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            choice_set = await self.database.active_choice_set(session_id)
            participant = (choice_set or {}).get("participant") or {}
            acting_user_id = str(participant.get("group_user_id") or "")
            acting_name = str(
                participant.get("character_name")
                or participant.get("display_name")
                or acting_user_id
            )
            if not acting_user_id:
                raise ValueError("当前没有可代重整的行动角色")
            operation_id = str(payload.get("operation_id") or "").strip() or (
                f"reroll:{session_id}:{uuid.uuid4().hex}"
            )
            claim = await self.database.claim_action_operation(
                session_id,
                operation_id,
                "forced_reroll",
                acting_user_id,
                user,
                {"choice_set_id": str((choice_set or {}).get("id") or "")},
            )
            if not claim["claimed"]:
                return json_response(
                    {
                        "ok": False,
                        "idempotent_replay": True,
                        "message": "该代重整操作已执行过，未重复提交",
                    }
                )
            session = await self.database.get_session(session_id)
            from types import SimpleNamespace

            event = SimpleNamespace(
                unified_msg_origin=str(session.get("unified_origin", "")),
                message_obj=None,
            )
            result = await self._engine().reroll_choices(
                event=event,
                session_id=session_id,
                sender_id=acting_user_id,
                operator_id=user,
                force=True,
            )
            await self.broker.publish(
                {
                    "type": "delegation",
                    "action": "forced_reroll",
                    "session_id": session_id,
                    "hook": "forced_reroll",
                }
            )
            option_lines = [
                f"{item.get('key', '')}. {item.get('text', '')}".strip()
                for item in (result.get("choices") or [])
                if isinstance(item, Mapping)
            ]
            notice = (
                f"🎲 后台代重整\n角色：{acting_name}\n操作者：{user}"
                + (("\n" + "\n".join(option_lines)) if option_lines else "")
            )
            send_result = await self._send_group_text(
                session_id,
                str(session.get("unified_origin") or ""),
                notice,
                kind="delegation.forced_reroll",
            )
            return json_response(
                {
                    "ok": True,
                    "operation_id": operation_id,
                    "choice_set": result,
                    "actor_user_id": acting_user_id,
                    "operator_id": user,
                    "notice_sent": bool(send_result.get("ok")),
                    "notice_reason": str(send_result.get("reason") or ""),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    def _engine(self):
        return getattr(self, "_tavern_engine", None)

    async def dm_command(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = str(payload.get("command") or "")
            if not session_id or not command:
                raise ValueError("缺少 session_id/command")
            await self._require_dm_capability(session_id, user)
            database = self.database
            actor = self._actor()
            instance = await database.get_instance_config(session_id)
            dm_policy = normalize_chat_experience(
                instance.get("world_snapshot") or {}
            )["dm"]
            required_policy = {
                "directive": "allow_narrative_override",
                "narrative": "allow_narrative_override",
                "whisper": "allow_secret_whispers",
                "manual_roll": "allow_manual_checks",
                "adjust_relationship": "allow_state_intervention",
                "adjust_economy": "allow_state_intervention",
                "set_next_actor": "allow_state_intervention",
                "lock_action": "allow_state_intervention",
                "lock_input": "allow_state_intervention",
                "replace_choices": "allow_state_intervention",
                "force_end_vote": "allow_state_intervention",
                "vote_as": "allow_state_intervention",
            }.get(command)
            if required_policy and not bool(dm_policy.get(required_policy, True)):
                raise PermissionError(
                    f"当前世界包已关闭该人工 DM 能力（{required_policy}）"
                )
            result: dict[str, Any] = {}
            whisper_participant: dict[str, Any] | None = None
            if command == "enable_dm":
                result = await database.enable_dm_mode(
                    session_id,
                    user,
                    actor,
                )
            elif command == "disable_dm":
                result = await database.disable_dm_mode(session_id, actor)
            elif command == "directive":
                directive = str(payload.get("directive") or "").strip()
                if not directive:
                    raise ValueError("主持指令不能为空")
                result = await database.set_dm_directive(
                    session_id,
                    directive,
                    user,
                )
            elif command == "narrative":
                narrative = str(
                    payload.get("narrative") or payload.get("text") or ""
                ).strip()
                if not narrative:
                    raise ValueError("追加叙事不能为空")
                instance = await database.get_instance_config(session_id)
                document = narrative_document_from_plain_text(
                    narrative,
                    mode=narrative_mode_from_session(instance),
                )
                narrative = narrative_document_to_plain_text(document)
                result = await database.insert_dm_narrative(
                    session_id,
                    document,
                    actor,
                    mode=str(payload.get("mode") or "append"),
                )
            elif command == "announce":
                result = await database.publish_announcement(
                    session_id, str(payload.get("text") or ""), actor
                )
            elif command == "whisper":
                whisper_ref = str(
                    payload.get("participant_id") or ""
                ).strip()
                if not whisper_ref:
                    raise ValueError("缺少密语目标角色")
                whisper_text = str(payload.get("text") or "").strip()
                # D1-DEL-010：先解析目标参与者（支持群用户 ID 与角色名），
                # 再构造待投递记录，最后与领域事件/审计在同一事务内原子
                # 入队；只入队不直发，绝不把敏感密语回退到群聊。
                whisper_participant = await self._resolve_participant_ref(
                    session_id,
                    whisper_ref,
                )
                delivery_record = self._build_whisper_record(
                    session_id,
                    whisper_participant,
                    whisper_text,
                    actor,
                )
                result = await database.whisper_to(
                    session_id,
                    whisper_text,
                    str(whisper_participant.get("id") or ""),
                    actor,
                    delivery_record=delivery_record,
                )
            elif command == "set_next_actor":
                result = await database.designate_turn(
                    session_id, str(payload.get("user_id") or ""), actor
                )
            elif command == "lock_action":
                result = await database.set_action_lock(
                    session_id,
                    str(payload.get("participant_id") or ""),
                    bool(payload.get("locked", True)),
                    actor,
                )
            elif command == "lock_input":
                result = await database.set_input_lock(
                    session_id, bool(payload.get("locked", True)), actor
                )
            elif command == "replace_choices":
                import json as _json

                raw = payload.get("choices")
                choices = raw if isinstance(raw, list) else _json.loads(
                    str(payload.get("choices_json") or "[]")
                )
                choice_set = await database.active_choice_set(session_id)
                if not choice_set:
                    raise ValueError("当前没有可替换的选项")
                result = await database.replace_active_choices(
                    session_id,
                    choice_set["participant"]["id"],
                    choices,
                    actor_id=actor,
                )
            elif command == "force_end_vote":
                result = await database.force_end_vote(
                    session_id, str(payload.get("winner_key") or ""), actor
                )
            elif command == "vote_as":
                result = await database.cast_vote(
                    session_id,
                    str(payload.get("user_id") or ""),
                    str(payload.get("key") or ""),
                )
            elif command == "manual_roll":
                result = await database.record_manual_roll(
                    session_id,
                    str(payload.get("participant_id") or ""),
                    str(payload.get("stat") or ""),
                    int(payload.get("total") or 0),
                    str(payload.get("note") or ""),
                    actor,
                )
            elif command == "adjust_relationship":
                result = await database.apply_relationship_delta(
                    session_id,
                    str(payload.get("source") or ""),
                    str(payload.get("target") or ""),
                    str(payload.get("dimension") or "信任"),
                    int(payload.get("delta") or 0),
                    actor,
                )
            elif command == "adjust_economy":
                await self._require_economy_capability(session_id, user)
                result = await database.economy_apply(
                    session_id=session_id,
                    operation_id=str(
                        payload.get("operation_id") or f"dm:{uuid.uuid4().hex}"
                    ),
                    kind=str(payload.get("kind") or "adjust"),
                    currency_id=str(payload.get("currency_id") or ""),
                    amount=payload.get("amount"),
                    from_owner=(
                        (str(payload["from_owner_type"]), str(payload["from_owner_ref"]))
                        if payload.get("from_owner_type") and payload.get("from_owner_ref")
                        else None
                    ),
                    to_owner=(
                        (str(payload["to_owner_type"]), str(payload["to_owner_ref"]))
                        if payload.get("to_owner_type") and payload.get("to_owner_ref")
                        else None
                    ),
                    reason=str(payload.get("reason") or ""),
                    source="dm",
                    actor_id=user,
                )
            elif command == "pause":
                result = await database.transition_session(
                    session_id, "paused", actor
                )
            elif command == "resume":
                result = await database.transition_session(
                    session_id, "preparing", actor
                )
            elif command == "checkpoint":
                expected_revision = payload.get("expected_revision")
                idempotency_key = str(
                    payload.get("idempotency_key")
                    or payload.get("request_id")
                    or ""
                ).strip()
                if expected_revision in {None, ""}:
                    raise ValueError("缺少 expected_revision")
                if not idempotency_key:
                    raise ValueError("缺少 idempotency_key")
                result = await database.create_snapshot(
                    session_id,
                    str(payload.get("name") or "DM检查点"),
                    actor,
                    replace=False,
                    expected_revision=int(expected_revision),
                    idempotency_key=idempotency_key,
                )
            elif command == "cancel_operation":
                result = await database.update_operation(
                    str(payload.get("operation_id") or ""),
                    status="failed",
                    phase="cancelled_by_dm",
                    result={"reason": str(payload.get("reason") or "DM 取消卡死任务")},
                    actor_id=actor,
                )
            else:
                raise ValueError(f"不支持的 DM 指令：{command}")
            delivery: dict[str, Any] | None = None
            session = await database.get_session(session_id)
            public_text = ""
            target_origin = str(session.get("unified_origin") or "")
            if command == "narrative":
                public_text = str(
                    payload.get("narrative") or payload.get("text") or ""
                )
            elif command == "announce":
                public_text = "【主持公告】\n" + str(payload.get("text") or "")
            elif command == "manual_roll":
                public_text = (
                    f"【主持检定】{payload.get('stat') or '检定'}："
                    f"{int(payload.get('total') or 0)}"
                )
            elif command == "whisper":
                status = str(result.get("status") or "")
                delivery = {
                    "ok": True,
                    "status": status,
                    "delivery_id": str(result.get("delivery_id") or ""),
                    "queued": status == "queued",
                    "reason": "",
                    "method": "",
                    "attempts": 0,
                    "sent_parts": 0,
                    "total_parts": 1,
                    "next_retry_at": "",
                }
                if status == "queued":
                    await self.broker.publish(
                        {
                            "type": "delivery",
                            "action": "queued",
                            "session_id": session_id,
                        }
                    )
            if public_text:
                delivery = await self._send_group_text(
                    session_id,
                    target_origin,
                    public_text,
                    kind=f"dm.{command}",
                    recipient=whisper_participant,
                )
            await self.broker.publish(
                {"type": "dm", "action": command, "session_id": session_id}
            )
            return json_response({"ok": True, "result": result, "delivery": delivery})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_token_reset(self):
        """A16：重置副本 Token 统计（不删除剧情；管理员/DM）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            count = await self.database.reset_token_usage(session_id, self._actor())
            return json_response({"ok": True, "count": count})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_element_reaction(self):
        """解析一次「属性元素反应」（只读干跑，不写状态）。"""
        try:
            self._username()
            payload = await self._payload()
            world_ref = str(
                payload.get("world_id") or payload.get("id") or payload.get("slug") or ""
            )
            world = (
                await self.database.get_world(world_ref)
                if world_ref
                else payload.get("world")
            )
            if not isinstance(world, dict):
                raise ValueError("请提供 world_id 或内联 world")
            parsed = parse_elemental(world)
            resolver = None
            resolver_name = str(parsed.get("resolver") or "")
            if resolver_name and self._extension_registry is not None:
                resolver = self._extension_registry.resolve(
                    "element_resolver", resolver_name
                )
            result = resolve_elemental(
                parsed,
                str(payload.get("source") or ""),
                str(payload.get("target") or ""),
                target_element=str(payload.get("target_element") or "") or None,
                context=payload.get("context") or {},
                resolver=resolver,
            )
            return json_response(
                {
                    "reaction": result,
                    "table": elemental_table(world),
                    "resolver_used": resolver_name or "table",
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_element_table(self):
        """世界元素表（元素 / 亲和 / 反应 / 解析器）。"""
        try:
            self._username()
            world_id = str(request.query.get("id", "") or "")
            if not world_id:
                raise ValueError("缺少世界标识")
            world = await self.database.get_world(world_id)
            return json_response({"table": elemental_table(world)})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_resolution_table(self):
        """世界裁定表（解析模式 / 角色卡 / 能力 / 元素 / 限制）。"""
        try:
            self._username()
            world_id = str(request.query.get("id", "") or "")
            if not world_id:
                raise ValueError("缺少世界标识")
            world = await self.database.get_world(world_id)
            contract: dict[str, Any] = {}
            try:
                from ...world_contract import validate_world_contract

                contract = validate_world_contract(world)
            except Exception:
                contract = {}
            rules = world.get("rules") if isinstance(world.get("rules"), dict) else {}
            return json_response(
                {
                    "resolution": contract.get("resolution", {}),
                    "stats": contract.get("stats", {}),
                    "capabilities": contract.get("capabilities", {}),
                    "protocol": contract.get("protocol", {}),
                    "player_limits": rules.get("player_limits", {}),
                    "elemental": elemental_table(world),
                    "check_modifiers": (world.get("initial_state") or {}).get(
                        "check_modifiers", {}
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_turn_preflight(self):
        """行动前预检（只读）：当前行动者 / 选项 / 投票 / 等待流程。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            turn = await self.database.get_turn_status(session_id)
            choice_set = await self.database.active_choice_set(session_id)
            vote = await self.database.active_vote(session_id)
            waiting_for = "vote" if vote else (
                "choice" if choice_set else (
                    "preparation" if session.get("state") == SESSION_PREPARING else (
                        "admin" if session.get("state") == SESSION_PAUSED else ""
                    )
                )
            )
            options: list[dict[str, Any]] = []
            if choice_set and isinstance(choice_set, dict):
                raw_choices = choice_set.get("choices_json") or choice_set.get("choices") or []
                if isinstance(raw_choices, list):
                    for item in raw_choices:
                        if isinstance(item, dict):
                            options.append(
                                {
                                    "key": str(item.get("key") or ""),
                                    "text": str(item.get("text") or ""),
                                    "risk": str(item.get("risk") or ""),
                                    "requires_check": bool(
                                        item.get("requires_check") or item.get("check")
                                    ),
                                }
                            )
            return json_response(
                {
                    "session": {
                        "id": session.get("id"),
                        "state": session.get("state"),
                        "turn_no": session.get("turn_no"),
                        "revision": session.get("revision"),
                        "world_name": session.get("world_name"),
                    },
                    "turn": turn,
                    "active_choices": options,
                    "active_vote": vote,
                    "waiting_for": waiting_for,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
