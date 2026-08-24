from __future__ import annotations

from .plugin_shared import *
from .startup import StartupMethods
from .delivery import DeliveryMethods
from .messages import MessageMethods
from .commands import CommandMethods
from .legacy_commands import LegacyCommandMethods
from .background_jobs import BackgroundJobMethods
from .webhooks import WebhookMethods
from .shutdown import ShutdownMethods

# Dynamic help title contract: 【321开团 v{PLUGIN_VERSION}｜



class GroupMessagesMixin:
    async def on_group_message(self, event: AstrMessageEvent):
        message = str(event.message_str or "")
        await self._deliver_pending(self._event_origin(event))
        command = await self._parse_command_relaxed(
            event,
            message,
            str(event.get_sender_id() or ""),
            self._group_id(event),
        )
        config = self.runtime_config()
        group_id = self._group_id(event)
        sender_id = str(event.get_sender_id() or "")
        platform_id = self._platform_id(event)
        content = (
            None
            if command.matched
            else parse_story_trigger(message, config.trigger_prefix)
        )
        if config.debug and (command.matched or content is not None):
            logger.debug(
                "321开团事件：platform=%s group=%s sender=%s command=%s",
                platform_id,
                group_id,
                sender_id,
                command.action if command.matched else "story",
            )

        if command.matched:
            event.stop_event()
            try:
                response = await self._handle_command(
                    event=event,
                    command=command,
                    config=config,
                    group_id=group_id,
                    platform_id=platform_id,
                    sender_id=sender_id,
                )
            except Exception:
                logger.exception("321开团管理命令发生未处理异常")
                can_respond = config.is_admin(sender_id) or (
                    command.action == "status" and config.public_status
                )
                response = (
                    "【管理命令处理失败】\n"
                    "操作：执行本次开团管理命令。\n"
                    "原因：服务暂时未能完成处理。\n"
                    "自动处理：系统已中止本次命令，未确认的结果不会作为成功发送。\n"
                    "下一步：请确认当前状态后再重试：\n\n"
                    "/团 状态"
                    if can_respond
                    else None
                )
            if response:
                yield await self._message_result(event, response)
            return

        session = None
        if not command.matched and config.is_group_allowed(group_id):
            session = await self.database.get_session_by_group(platform_id, group_id)
            roster_reader = getattr(self.database, "list_roster", None)
            tactical_service = getattr(self, "tactical_commands", None)
            if (
                session
                and session.get("state") == SESSION_RUNNING
                and callable(roster_reader)
                and tactical_service is not None
            ):
                roster = await roster_reader(str(session["id"]))
                member = any(
                    str(item.get("group_user_id") or "") == sender_id
                    and str(item.get("participation_status") or "") not in {"retired", "archived"}
                    for item in roster
                )
                if member:
                    roles = set(
                        await self.database.permission_roles(
                            str(session["id"]), sender_id
                        )
                    )
                    roles.add("player")
                    ctx = from_astrbot_event(
                        event,
                        session_id=str(session["id"]),
                        roles=roles,
                        metadata={"transport_event_id": transport_event_id(event)},
                        platform_id=platform_id,
                        group_id=group_id,
                        user_id=sender_id,
                        is_private=False,
                    )
                    tactical_result = await tactical_service.handle_plain_text(
                        ctx,
                        content if content is not None else message,
                        self.database,
                    )
                    if tactical_result.handled:
                        event.stop_event()
                        if tactical_result.text:
                            yield await self._message_result(event, tactical_result.text)
                        return
                    challenge_service = getattr(self, "challenge_commands", None)
                    if challenge_service is not None:
                        challenge_result = await challenge_service.handle_plain_text(
                            ctx,
                            content if content is not None else message,
                            self.database,
                        )
                        if challenge_result.handled:
                            event.stop_event()
                            if challenge_result.text:
                                yield await self._message_result(event, challenge_result.text)
                            return
        if content is None:
            return
        if not config.is_group_allowed(group_id):
            return
        if session is None:
            session = await self.database.get_session_by_group(
                platform_id,
                group_id,
            )

        event.stop_event()
        if not session or session["state"] == SESSION_CLOSED:
            yield await self._message_result(
                event, "【开团】当前群尚未开馆，请由管理员发送 /团 开启。"
            )
            return
        if session["state"] == SESSION_PREPARING:
            next_command = (
                "/团 继续"
                if int(session.get("turn_no") or 0) > 0
                else "/团 开演"
            )
            yield await self._message_result(
                event,
                "【故事尚未开演】当前处于角色准备阶段。"
                "请先完成角色卡并发送 /团 准备，"
                f"由主持人发送 {next_command}。",
            )
            return
        if session["state"] == SESSION_PAUSED:
            yield await self._message_result(
                event, "【开团】剧情已暂停，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_FINISHED:
            yield await self._message_result(
                event, "【开团】故事已经完结，本条内容未记录。"
            )
            return
        if session["state"] == SESSION_MAINTENANCE:
            yield await self._message_result(
                event, "【开团】当前处于维护模式，本条内容未记录。"
            )
            return

        try:
            # 0.11.3：定时器结束的表决（已通过但尚未落实叙事）自动推进。
            pending_vote = await self.database.pending_vote_resolution(
                session["id"]
            )
            if pending_vote:
                try:
                    reply = await self.engine.process_vote_resolution(
                        event=event,
                        session_id=session["id"],
                        vote=pending_vote,
                    )
                    await self.database.clear_vote_resolution_pending(
                        pending_vote["id"]
                    )
                    body = "\n\n".join(
                        render_player_message(part)
                        if isinstance(part, PlayerMessage)
                        else str(part or "").strip()
                        for part in reply_message_parts(reply)
                    )
                    yield await self._message_result(
                        event, f"🌐 【表决通过 · 自动推进】\n{body}"
                    )
                except (TavernEngineError, ValueError) as exc:
                    yield await self._message_result(
                        event,
                        (
                            story_generation_failure_message(
                                exc,
                                operation="落实表决并生成故事",
                            )
                            if isinstance(exc, TavernStoryGenerationError)
                            else f"🌐 【表决通过】故事推进暂未完成：{exc}"
                        ),
                    )
                return
            control = await self.database.get_control_state(session["id"])
            if control.get("mode") == "dm" and control.get("phase") != "player_handoff":
                if sender_id != str(control.get("active_dm_user_id") or ""):
                    yield await self._message_result(
                        event,
                        "【主持人模式】当前剧情由真人主持人接管；普通玩家输入暂不记录。",
                    )
                    return
                result = await self.engine.process_dm_beat(
                    event=event,
                    session_id=session["id"],
                    dm_user_id=sender_id,
                    instruction=content,
                    progress=lambda text: self._send_event_text(event, text),
                )
                unsent = await self._send_committed_narrative(
                    event,
                    result,
                    title=f"主持推进 · 第 {result['beat_no']} 段",
                    source="dm.story",
                )
                if unsent:
                    yield await self._message_result(
                        event,
                        self._render_unsent_parts(unsent),
                    )
                return
            vote = await self.database.active_vote(session["id"])
            if vote:
                yield await self._message_result(
                    event,
                    "【集体投票进行中】请使用 /团 投票 A；"
                    "投票不会消耗个人行动机会。",
                )
                return
            sender_name = str(event.get_sender_name() or "").strip()
            if not sender_name:
                yield await self._message_result(
                    event,
                    "【行动提交失败】\n\n"
                    "失败操作：提交本轮角色行动。\n\n"
                    "原因：平台没有提供可公开显示的玩家名称。\n\n"
                    "自动处理：本次输入没有写入，系统也没有用账号标识代替名称。\n\n"
                    "下一步：请先设置平台昵称，然后查看当前行动：\n\n"
                    "/团 当前",
                )
                return
            # 0.11.3：t 全队 / 提议全队 —— 便捷发起全队行动表决。
            team_text = content.strip()
            team_words = ("全队", "提议全队")
            if any(
                team_text == word or team_text.startswith(word + " ")
                for word in team_words
            ):
                argument = (
                    team_text.split(maxsplit=1)[1]
                    if " " in team_text
                    else ""
                )
                index = _team_index_from_argument(argument)
                try:
                    reply = await self.engine.process_team_proposal(
                        event=event,
                        session_id=session["id"],
                        sender_id=sender_id,
                        sender_name=sender_name,
                        index=index,
                    )
                    unsent = await self._send_engine_reply(event, reply)
                    if unsent:
                        yield await self._message_result(
                            event,
                            self._render_unsent_parts(unsent),
                        )
                except TavernEngineError as exc:
                    yield await self._message_result(
                        event,
                        (
                            story_generation_failure_message(
                                exc,
                                operation="结算全队行动",
                            )
                            if isinstance(exc, TavernStoryGenerationError)
                            else f"【开团】{exc}"
                        ),
                    )
                return
            # 1.0.0-A7：t 道具 / t 技能 —— 结构化前缀（使用道具/技能，生成故事后再给选项）。
            item_text = content.strip()
            if item_text == "道具" or item_text.startswith("道具 "):
                argument = (
                    item_text.split(maxsplit=1)[1]
                    if " " in item_text
                    else ""
                ).strip()
                if not argument:
                    yield await self._message_result(
                        event,
                        f"✋ 用法：{config.trigger_prefix} 道具 <名称>"
                        f"（例如 {config.trigger_prefix} 道具 绷带，"
                        f"可加目标：{config.trigger_prefix} 道具 医疗包 对 卡密）",
                    )
                    return
                parts = argument.split(maxsplit=1)
                item_name = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                target_ref = ""
                for prefix in ("对 ", "给 ", "医治 ", "治疗 "):
                    if rest.startswith(prefix):
                        target_ref = rest[len(prefix):].strip()
                        break
                try:
                    reply = await self.engine.use_item(
                        event=event,
                        session_id=session["id"],
                        sender_id=sender_id,
                        sender_name=sender_name,
                        item_name=item_name,
                        target_ref=target_ref,
                        progress=lambda text: self._send_event_text(event, text),
                    )
                    unsent = await self._send_engine_reply(event, reply)
                    if unsent:
                        yield await self._message_result(
                            event,
                            self._render_unsent_parts(unsent),
                        )
                except TavernTurnOrderError as exc:
                    yield await self._message_result(event, f"【回合秩序】{exc}")
                except TavernBusyError as exc:
                    yield await self._message_result(event, f"【开团】{exc}")
                except TavernPlayerDisabledError:
                    yield await self._message_result(
                        event, "【开团】你的玩家身份当前不可用。"
                    )
                except (TavernEngineError, ValueError) as exc:
                    yield await self._message_result(
                        event,
                        (
                            story_generation_failure_message(
                                exc,
                                operation="使用道具并生成故事",
                            )
                            if isinstance(exc, TavernStoryGenerationError)
                            else f"【开团】{exc}"
                        ),
                    )
                return
            if item_text == "技能" or item_text.startswith("技能 "):
                argument = (
                    item_text.split(maxsplit=1)[1]
                    if " " in item_text
                    else ""
                ).strip()
                if not argument:
                    yield await self._message_result(
                        event,
                        f"⚡ 用法：{config.trigger_prefix} 技能 <名称>"
                        f"（例如 {config.trigger_prefix} 技能 急救包扎 对 卡密，"
                        f"或 {config.trigger_prefix} 技能 短剑连击 攻击变异体）",
                    )
                    return
                parts = argument.split(maxsplit=1)
                skill_name = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                target_ref = ""
                action_note = rest
                for prefix in ("对 ", "给 ", "医治 ", "治疗 "):
                    if rest.startswith(prefix):
                        target_ref = rest[len(prefix):].strip()
                        action_note = ""
                        break
                try:
                    reply = await self.engine.use_skill(
                        event=event,
                        session_id=session["id"],
                        sender_id=sender_id,
                        sender_name=sender_name,
                        skill_name=skill_name,
                        target_ref=target_ref,
                        action_note=action_note,
                        progress=lambda text: self._send_event_text(event, text),
                    )
                    await self._open_supplements_after_progress(
                        session["id"]
                    )
                    unsent = await self._send_engine_reply(event, reply)
                    if unsent:
                        yield await self._message_result(
                            event,
                            self._render_unsent_parts(unsent),
                        )
                except TavernTurnOrderError as exc:
                    yield await self._message_result(event, f"【回合秩序】{exc}")
                except TavernBusyError as exc:
                    yield await self._message_result(event, f"【开团】{exc}")
                except TavernPlayerDisabledError:
                    yield await self._message_result(
                        event, "【开团】你的玩家身份当前不可用。"
                    )
                except (TavernEngineError, ValueError) as exc:
                    yield await self._message_result(
                        event,
                        (
                            story_generation_failure_message(
                                exc,
                                operation="使用能力并生成故事",
                            )
                            if isinstance(exc, TavernStoryGenerationError)
                            else f"【开团】{exc}"
                        ),
                    )
                return
            choice_key, flavor_text = parse_choice_input(content)
            reply = await self.engine.process_choice(
                event=event,
                session_id=session["id"],
                sender_id=sender_id,
                sender_name=sender_name,
                choice_key=choice_key,
                flavor_text=flavor_text,
                progress=lambda text: self._send_event_text(event, text),
            )
            await self._open_supplements_after_progress(session["id"])
            if control.get("mode") == "dm" and control.get("phase") == "player_handoff":
                await self.database.finish_dm_handoff(session["id"])
                handoff = PlayerMessage.dynamic(
                    title="本次交棒行动已完成",
                    summary="已回到等待真人主持人推进的状态。",
                    source="turn.dm_handoff",
                )
                reply = replace(
                    reply,
                    messages=(*reply.messages, handoff),
                    message_bundle=None,
                    turn_text=(
                        "【本次交棒行动已完成】"
                        "已回到等待真人主持人推进的状态。"
                    ),
                )
            unsent = await self._send_engine_reply(event, reply)
            if unsent:
                yield await self._message_result(
                    event,
                    self._render_unsent_parts(unsent),
                )
        except TavernTurnOrderError as exc:
            yield await self._message_result(
                event,
                    "【无法提交行动】\n\n"
                    "失败操作：提交本轮行动。\n\n"
                    f"原因：{exc}\n\n"
                    "自动处理：本次输入没有写入，也没有改变行动顺序。\n\n"
                    "下一步\n\n"
                    "/团 当前",
            )
        except TavernBusyError as exc:
            yield await self._message_result(
                event,
                    "【本轮仍在处理中】\n\n"
                    "失败操作：重复提交本轮行动。\n\n"
                    f"原因：{exc}\n\n"
                    "自动处理：系统没有重复执行，也没有重复消耗资源。\n\n"
                    "下一步\n\n"
                    "/团 当前",
            )
        except TavernPlayerDisabledError:
            yield await self._message_result(
                event,
                    "【玩家身份暂不可用】\n\n"
                    "失败操作：提交本轮行动。\n\n"
                    "原因：你的当前席位已暂停、退出或失去行动权限。\n\n"
                    "自动处理：系统没有写入本次输入。\n\n"
                    "下一步\n\n"
                    "/团 当前",
            )
        except (TavernEngineError, ValueError) as exc:
            await self.database.write_audit(
                session["id"],
                sender_id,
                "turn.failed",
                "",
                {"error": str(exc)[:500]},
            )
            logger.warning("321开团本轮失败：%s", exc)
            yield await self._message_result(
                event,
                (
                    story_generation_failure_message(
                        exc,
                        operation="结算本轮行动",
                    )
                    if isinstance(exc, TavernStoryGenerationError)
                    else (
                        "【本轮裁定未完成】\n\n"
                        "失败操作：结算本轮行动。\n\n"
                        f"原因：{exc}\n\n"
                        "自动处理：世界状态没有改变；当前行动和可恢复内容仍然保留。\n\n"
                        "下一步\n\n"
                        "/团 当前"
                    )
                ),
            )
        except Exception as exc:
            await self.database.write_audit(
                session["id"],
                sender_id,
                "turn.failed",
                "",
                {"error_type": type(exc).__name__},
            )
            logger.exception("321开团处理群消息时发生异常")
            yield await self._message_result(
                event,
                    "【本轮暂时无法继续】\n\n"
                    "失败操作：生成并提交本轮故事。\n\n"
                    "原因：叙事服务没有返回可安全保存的完整结果。\n\n"
                    "自动处理：世界状态没有改变；系统已丢弃不完整结果。\n\n"
                    "下一步\n\n"
                    "/团 当前",
            )
