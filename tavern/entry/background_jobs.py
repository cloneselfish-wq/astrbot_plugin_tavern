from __future__ import annotations

from .plugin_shared import *


class BackgroundJobMethods:
    async def _handle_legacy_command_part_3(
        self,
        *,
        event,
        command,
        config,
        group_id,
        platform_id,
        sender_id,
        session,
        roles,
        is_admin,
        is_host,
        is_moderator,
        is_active_dm,
        control_state,
    ):
        if command.action == "give_item":
            argument = command.argument
            parts = argument.split(maxsplit=1)
            if len(parts) < 2:
                return (
                    "🎁 用法：/团 赠予 <道具> <目标>"
                    "（例如 /团 赠予 火把 卡密）"
                )
            text = await self.engine.give_item(
                session_id=session["id"],
                sender_id=sender_id,
                item_name=parts[0],
                target_ref=parts[1],
            )
            return text
        if command.action == "shop":
            return await self.engine.shop_list(
                session_id=session["id"]
            )
        if command.action == "buy":
            return await self.engine.buy_item(
                session_id=session["id"],
                sender_id=sender_id,
                item_ref=command.argument,
            )
        if command.action == "away":
            participant = await self.database.set_participant_away(
                session["id"],
                sender_id,
            )
            narrative = participant.get("away_narrative") or ""
            return (
                f"【已暂离】{participant.get('character_name') or participant.get('display_name')}"
                + ("\n" + narrative if narrative else "")
                + "\n席位仍为你保留；返回时发送 /团 返回队列。"
            )
        if command.action == "return_queue":
            participant = await self.database.return_to_queue(
                session["id"],
                sender_id,
            )
            narrative = participant.get("return_narrative") or ""
            return (
                f"【返回申请已确认】将在第 "
                f"{participant.get('effective_round')} 轮队尾生效。"
                + ("\n" + narrative if narrative else "")
            )
        if command.action == "return_request":
            result = await self.database.request_return(
                session["id"],
                sender_id,
            )
            vote = await self.database.active_vote(session["id"])
            return (
                f"【返场申请】{result['character_name']}\n"
                f"剧情条件：{result['objective']}\n\n"
                + (format_vote(vote) if vote else "")
            )
        if command.action == "delegate":
            parts = command.argument.split(maxsplit=1)
            if not parts:
                return (
                    "【开团】格式：/团 授权代控 <真实用户ID> "
                    "[时长，例如 2小时]"
                )
            duration = (
                parse_duration(parts[1])
                if len(parts) > 1
                else None
            )
            grant = await self.database.grant_delegation(
                session["id"],
                sender_id,
                parts[0],
                sender_id,
                duration_seconds=duration,
            )
            return (
                f"【代控已授权】用户 {grant['delegate_user_id']}\n"
                f"到期：{grant['expires_at'] or '不限时'}\n"
                "角色本人再次行动、主动撤销、退场或封禁时立即失效。"
            )
        if command.action == "delegate_revoke":
            count = await self.database.revoke_delegation(
                session["id"],
                sender_id,
                sender_id,
            )
            return f"【代控已撤销】共撤销 {count} 条有效授权。"
        if command.action == "delegate_status":
            grants = await self.database.list_delegations(session["id"])
            if not is_admin:
                grants = [
                    item
                    for item in grants
                    if str(item.get("owner_user_id") or "")
                    in {sender_id, ""}
                    or str(item.get("delegate_user_id") or "")
                    == sender_id
                ]
            if not grants:
                return "【托管状态】当前没有托管记录。"
            lines = ["【托管状态】"]
            for item in grants:
                lines.append(
                    f"- {item.get('participant_character') or item.get('participant_id')}"
                    f"：{item.get('owner_user_id')} → {item.get('delegate_user_id')}"
                    f" [{item.get('status')}]"
                    f" 权限 {','.join(item.get('permissions') or [])}"
                    + (
                        f" 到期 {item.get('expires_at')}"
                        if item.get("expires_at")
                        else ""
                    )
                )
            return "\n".join(lines)
        if command.action == "delegate_force":
            if not (is_admin or is_moderator or is_active_dm):
                raise PermissionError("只有管理员或真人主持人可以强制托管")
            parts = command.argument.split(maxsplit=1)
            if len(parts) < 2:
                return (
                    "【开团】格式：/团 强制托管 <角色拥有者ID> "
                    "<代理人ID>"
                )
            grant = await self.database.grant_delegation(
                session["id"],
                parts[0],
                parts[1],
                sender_id,
                source="admin",
            )
            return (
                f"【已强制托管】{parts[0]} 的角色现由 {parts[1]} 控制\n"
                f"来源：{grant.get('source')}"
            )
        if command.action == "delegate_restore":
            if not (is_admin or is_moderator or is_active_dm):
                raise PermissionError("只有管理员或真人主持人可以恢复控制权")
            if not command.argument:
                return "【开团】格式：/团 恢复控制 <角色拥有者ID>"
            participant = await self.database.get_participant(
                session["id"],
                user_id=command.argument,
            )
            count = await self.database.restore_owner_control(
                session["id"],
                participant["id"],
                sender_id,
            )
            return f"【控制权已恢复】撤销 {count} 条托管，交由原玩家本人。"
        if command.action == "leave":
            if command.argument:
                if not is_moderator:
                    raise PermissionError("只有主持人或秩序管理员能让他人退场")
                result = await self.database.retire_participant(
                    session["id"],
                    command.argument,
                    sender_id,
                    forced=True,
                    reason="moderator_exit",
                )
                try:
                    exited = await self.database.get_participant(
                        session["id"],
                        participant_ref=command.argument,
                    )
                    exit_user_id = str(exited.get("group_user_id") or "")
                except Exception:
                    exit_user_id = ""
            else:
                result = await self.database.retire_self(
                    session["id"],
                    sender_id,
                )
                exit_user_id = sender_id
            if exit_user_id:
                await self._revoke_private_delivery_target(
                    platform_id=self._platform_id(event),
                    user_id=exit_user_id,
                    reason="retired",
                )
            return (
                "【角色已正式退场】席位已经释放，角色历史仍被归档。\n"
                + result["narrative"]
            )
        if command.action == "move":
            target_text, separator, position_text = (
                command.argument.rpartition(" ")
            )
            if not separator:
                return "【开团】格式：/团 移至 <角色名或代号> <顺序>"
            target = await self.database.get_participant(
                session["id"],
                participant_ref=target_text,
            )
            position = int(position_text)
            turn = await self.database.get_turn_status(session["id"])
            order = [
                str(item["user_id"]) for item in turn["order"]
            ]
            user_id = target["group_user_id"]
            if user_id not in order:
                raise ValueError("该角色当前不在回合队列")
            order.remove(user_id)
            order.insert(
                max(0, min(len(order), position - 1)),
                user_id,
            )
            turn = await self.database.set_turn_order(
                session["id"],
                order,
                sender_id,
            )
            return "【未来行动顺序已调整】\n" + format_turn_status(turn)
        if command.action == "designate":
            target = await self.database.get_participant(
                session["id"],
                participant_ref=command.argument,
            )
            turn = await self.database.designate_turn(
                session["id"],
                target["group_user_id"],
                sender_id,
            )
            return "【当前行动者已指定】\n" + format_turn_status(turn)
        if command.action == "ban":
            parts = command.argument.split()
            if not parts:
                return (
                    "【开团】格式：/团 封禁 <角色> "
                    "[时长] [原因]"
                )
            ref = parts.pop(0)
            scope = "instance"
            scope_map = {
                "副本": "instance",
                "群": "group",
                "全局": "global",
            }
            if parts and parts[0] in scope_map:
                scope = scope_map[parts.pop(0)]
                if scope == "global" and not is_admin:
                    raise PermissionError("全局封禁只允许插件管理员")
            duration = None
            if parts:
                try:
                    duration = parse_duration(parts[0])
                    parts.pop(0)
                except ValueError:
                    duration = None
            result = await self.database.create_ban(
                session["id"],
                ref,
                sender_id,
                scope=scope,
                duration_seconds=duration,
                reason=" ".join(parts),
            )
            return (
                "【封禁已生效】已原子移出队列、撤销授权并释放席位。\n"
                + result["narrative"]
                + f"\n范围：{result['ban']['scope']}"
                f" · 到期：{result['ban']['expires_at'] or '永久'}"
            )
        if command.action == "unban":
            if not command.argument:
                return "【开团】格式：/团 解封 <角色名或代号>"
            count = await self.database.revoke_ban(
                session["id"],
                command.argument,
                sender_id,
            )
            return f"【解封完成】撤销 {count} 条有效封禁记录。"
        if command.action == "ban_list":
            bans = await self.database.list_bans(session["id"])
            if not bans:
                return "【黑名单】当前没有有效封禁。"
            return "【黑名单】\n" + "\n".join(
                (
                    f"· {item['user_id']} · {item['scope']}"
                    f" · {item['reason'] or '未注明'}"
                    f" · 至 {item['expires_at'] or '永久'}"
                )
                for item in bans
            )
        if command.action == "extend":
            target, separator, duration_text = (
                command.argument.rpartition(" ")
            )
            if not separator:
                return (
                    "【开团】格式：/团 延时 "
                    "<角色名、代号或准备阶段> <30分钟>"
                )
            timer = await self.database.extend_active_timer(
                session["id"],
                target,
                parse_duration(duration_text),
                sender_id,
            )
            return (
                "【计时已延长】"
                f"{timer.get('character_name') or timer['timer_type']}"
                f" · 新截止：{timer.get('deadline_at') or '暂停中'}"
            )
        return _COMMAND_UNHANDLED
