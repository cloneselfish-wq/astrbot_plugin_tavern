from __future__ import annotations

from .plugin_shared import *


class WebhookMethods:
    async def _handle_legacy_command_part_4(
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
        if command.action == "countdown":
            labels = {
                "card_code": "建卡码",
                "card_completion": "角色卡完成",
                "preparation": "准备大厅",
                "ready": "准备确认",
                "turn": "行动回合",
                "vote": "集体投票",
                "standby": "候补等待",
            }
            aliases = {
                "总": "all",
                "全部": "all",
                "全局": "all",
                "建卡码": "card_code",
                "建卡": "card_completion",
                "角色卡": "card_completion",
                "准备阶段": "preparation",
                "准备大厅": "preparation",
                "准备": "ready",
                "回合": "turn",
                "行动": "turn",
                "投票": "vote",
                "候补": "standby",
            }
            argument = str(command.argument or "").strip()
            compact = argument.replace(" ", "")
            if not argument or argument in {"状态", "status"}:
                policy = await self.database.get_timer_policy(
                    session["id"]
                )
            else:
                setting: bool | None = None
                target_text = ""
                for suffix, value in (
                    ("开启", True),
                    ("打开", True),
                    ("开", True),
                    ("关闭", False),
                    ("关", False),
                ):
                    if compact.endswith(suffix):
                        target_text = compact[: -len(suffix)]
                        setting = value
                        break
                if setting is None or target_text not in aliases:
                    return (
                        "【倒计时】格式：\n"
                        "/团 倒计时 状态\n"
                        "/团 倒计时 总关\n"
                        "/团 倒计时 回合 关\n"
                        "/团 倒计时 投票 开"
                    )
                policy = await self.database.set_timer_policy(
                    session["id"],
                    aliases[target_text],
                    setting,
                    sender_id,
                )
            lines = [
                "【倒计时开关】",
                "总开关："
                + (
                    "开启"
                    if policy["global_enabled"]
                    else "关闭（全部冻结）"
                ),
            ]
            for key, label in labels.items():
                switch = policy["switches"][key]
                effective = policy["effective"][key]
                lines.append(
                    f"· {label}："
                    + (
                        "开启"
                        if effective
                        else (
                            "分类关闭"
                            if not switch
                            else "随总开关冻结"
                        )
                    )
                )
            lines.append("关闭后保留真实剩余时间，不执行超时处罚。")
            return "\n".join(lines)

        if command.action == "usage":
            usage = await self.database.token_usage_summary(
                session["id"]
            )
            lines = [
                "【Token 用量】",
                (
                    "当前副本："
                    f"1小时 {usage['session']['hour']} · "
                    f"24小时 {usage['session']['day']} · "
                    f"累计 {usage['session']['all']}"
                ),
                (
                    "当前群："
                    f"1小时 {usage['group']['hour']} · "
                    f"24小时 {usage['group']['day']} · "
                    f"累计 {usage['group']['all']}"
                ),
            ]
            if usage["quotas"]:
                lines.append("滚动限额：")
                for item in usage["quotas"]:
                    scope = (
                        "群"
                        if item["scope_type"] == "group"
                        else "副本"
                    )
                    lines.append(
                        f"· {scope}：{item['used']}/"
                        f"{item['token_limit']}，"
                        f"剩余 {item['remaining']}，"
                        f"窗口 {_format_remaining_time(item['window_seconds'])}"
                        + ("" if item["enabled"] else "（已关闭）")
                    )
            else:
                lines.append("滚动限额：未设置")
            return "\n".join(lines)

        if command.action == "quota":
            parts = str(command.argument or "").strip().split()
            if not parts or parts[0] not in {"群", "副本"}:
                return (
                    "【Token 限额】格式：\n"
                    "/团 限额 群 24小时 500000\n"
                    "/团 限额 副本 1小时 100000\n"
                    "/团 限额 群 关"
                )
            scope_type = "group" if parts[0] == "群" else "session"
            if len(parts) == 2 and parts[1] in {"关", "关闭"}:
                current_usage = await self.database.token_usage_summary(
                    session["id"]
                )
                current = next(
                    (
                        item for item in current_usage["quotas"]
                        if item["scope_type"] == scope_type
                    ),
                    None,
                )
                if not current:
                    return "【Token 限额】该范围尚未设置限额。"
                await self.database.set_token_quota(
                    session["id"],
                    scope_type,
                    window_seconds=current["window_seconds"],
                    token_limit=current["token_limit"],
                    enabled=False,
                    actor_id=sender_id,
                )
                return f"【Token 限额已关闭】{parts[0]}范围不再拦截请求。"
            if len(parts) != 3:
                return (
                    "【Token 限额】请提供时间窗口和 Token 上限，"
                    "例如：/团 限额 副本 1小时 100000"
                )
            window_seconds = parse_duration(parts[1])
            token_limit = int(parts[2])
            result = await self.database.set_token_quota(
                session["id"],
                scope_type,
                window_seconds=window_seconds,
                token_limit=token_limit,
                enabled=True,
                actor_id=sender_id,
            )
            item = next(
                entry for entry in result["quotas"]
                if entry["scope_type"] == scope_type
            )
            return (
                f"【Token 限额已设置】{parts[0]}\n"
                f"窗口：{_format_remaining_time(item['window_seconds'])}\n"
                f"上限：{item['token_limit']} Token\n"
                f"当前已用：{item['used']} · 剩余：{item['remaining']}"
            )

        if command.action == "delete_session":
            argument = str(command.argument or "").strip()
            prefix = "确认 "
            if not argument.startswith(prefix):
                return (
                    "【确认删除整个副本】只能删除已关闭或已归档副本；"
                    "角色、剧情、Token 流水、存档和独立数据库会一并移入"
                    "回收目录。\n请发送：/团 删除副本 确认 "
                    f"{session['instance_name']}"
                )
            confirm_name = argument[len(prefix) :].strip()
            result = await self.database.delete_session(
                session["id"],
                sender_id,
                confirm_name,
            )
            await self.engine.release_session_lock(session["id"])
            suffix = (
                "\n副本文件已移入回收目录，可由服务器管理员恢复。"
                if result.get("trash_path")
                else "\n目录中没有残留的副本文件。"
            )
            if result.get("trash_error"):
                suffix = (
                    "\n数据库记录已删除，但文件移入回收目录失败："
                    + result["trash_error"]
                )
            return (
                f"【副本已删除】{result['instance_name']}" + suffix
            )

        if command.action == "pause":
            await self.database.pause_session_timers(
                session["id"],
                sender_id,
            )
            session = await self.database.transition_session(
                session["id"],
                SESSION_PAUSED,
                sender_id,
            )
            return (
                "【开团已暂停】现场、未完成选项、投票和剩余时间"
                "均已持久化；暂停期间默认不计时。"
                "\n恢复时请先发送 /团 恢复。"
            )
        if command.action == "safety_pause":
            if session["state"] == SESSION_PAUSED:
                return "【安全暂停】故事与全部计时已经处于冻结状态。"
            if not is_host:
                participant = await self.database.get_participant(
                    session["id"],
                    user_id=sender_id,
                )
                if participant.get("participation_status") != "active":
                    raise PermissionError("只有当前出场玩家可以发起安全暂停")
            cancellation = await _SessionCommandGateway(
                self,
                event,
            ).cancel_and_wait_operation(
                session["id"],
                sender_id,
            )
            await self.database.pause_session_timers(
                session["id"],
                sender_id,
            )
            await self.database.transition_session(
                session["id"],
                SESSION_PAUSED,
                sender_id,
            )
            await self.database.write_audit(
                session["id"],
                sender_id,
                "session.safety_pause",
                session["id"],
                {"reason_disclosed": False},
            )
            return (
                "【安全暂停】故事、行动、投票与全部计时已立即冻结。"
                + (
                    "\n" + str(cancellation.get("message") or "")
                    if cancellation.get("found")
                    else ""
                )
                + "\n无需在群内说明原因；由主持人与参与者确认边界后，"
                "由主持人发送 /团 恢复。"
            )
        if command.action == "recover":
            if session["state"] in {
                SESSION_PAUSED,
                SESSION_MAINTENANCE,
            }:
                session = await self.database.transition_session(
                    session["id"],
                    SESSION_PREPARING,
                    sender_id,
                )
                return (
                    "【恢复准备大厅】剧情尚未继续，计时仍暂停。\n"
                    f"上次位置：{session['world_state'].get('location', '未记录')}\n"
                    f"剧情回合：{session['turn_no']}\n\n"
                    + format_roster(
                        await self.database.list_roster(session["id"])
                    )
                    + "\n\n全员重新发送 /团 准备；"
                    "完成后主持人发送 /团 继续。"
                )
            if session["state"] == SESSION_PREPARING:
                if int(session.get("turn_no") or 0) > 0:
                    return (
                        "【开团】已经位于恢复准备大厅。"
                        "请等待全员发送 /团 准备；"
                        "全部完成后由主持人发送 /团 继续。"
                    )
                return (
                    "【开团】当前是新故事准备大厅，"
                    "准备完成后请使用 /团 开演。"
                )
            if session["state"] == SESSION_RUNNING:
                return (
                    "【开团】故事当前正在运行，无需恢复。"
                    "如需停团，请先发送 /团 暂停。"
                )
            if session["state"] == SESSION_CLOSED:
                return (
                    "【开团】副本当前已关闭；"
                    "请使用 /团 开启 <副本标识> 重新进入。"
                )
            raise InvalidTransitionError("当前副本状态不能进入恢复准备大厅")
        if command.action == "resume":
            if session["state"] in {
                SESSION_PAUSED,
                SESSION_MAINTENANCE,
            }:
                return (
                    "【开团】副本仍处于暂停状态。"
                    "请先发送 /团 恢复 进入恢复准备大厅；"
                    "本次没有切换状态，也没有恢复任何计时。"
                )
            if session["state"] != SESSION_PREPARING:
                raise InvalidTransitionError(
                    "只有恢复准备大厅中的副本可以继续"
                )
            if int(session.get("turn_no") or 0) <= 0:
                return (
                    "【开团】该副本尚未产生剧情，"
                    "新故事请使用 /团 开演。"
                )
            instance_config = await self.database.get_instance_config(
                session["id"]
            )
            self.engine.validate_world_runtime(
                instance_config["world_snapshot"]
            )
            result = await self.database.activate_story(
                session["id"],
                sender_id,
                resume=True,
            )
            if not result["started"]:
                return (
                    "【暂时无法继续】\n· "
                    + "\n· ".join(
                        result.get("blocker_messages")
                        or ["准备尚未完成"]
                    )
                )
            await self.database.resume_session_timers(
                session["id"],
                sender_id,
            )
            session = result["session"]
            current = result["current_participant"]
            active_vote = await self.database.active_vote(session["id"])
            recent_events = await self.database.recent_events(
                session["id"],
                80,
            )
            last_story = next(
                (
                    str(item.get("content") or "")
                    for item in reversed(recent_events)
                    if item.get("role") == "narrator"
                ),
                str(
                    session["world_state"].get("scene_summary")
                    or "暂无剧情正文"
                ),
            )
            workflow_text = (
                format_vote(active_vote)
                if active_vote
                else format_choices(
                    current.get("character_name")
                    or current.get("display_name"),
                    result["choice_set"]["choices"],
                    rerolls_left=max(
                        0,
                        1
                        - int(
                            result["choice_set"].get(
                                "reroll_count",
                                0,
                            )
                        ),
                    ),
                    trigger_prefix=config.trigger_prefix,
                )
            )
            timer_text = format_recovered_timer(
                await self.database.list_timers(session["id"]),
                vote_active=bool(active_vote),
            )
            return (
                f"📜 【故事继续】{session['instance_name']}\n"
                f"🎭 当前行动者："
                f"{current.get('character_name') or current.get('display_name')}"
                f"\n\n📖 【恢复时的最后剧情】\n{last_story}"
                "\n\n"
                + workflow_text
                + "\n\n"
                + timer_text
            )
        if command.action == "close":
            await self.database.pause_session_timers(
                session["id"],
                sender_id,
            )
            session = await self.database.transition_session(
                session["id"],
                SESSION_CLOSED,
                sender_id,
            )
            await self.engine.release_session_lock(session["id"])
            return "【开团已关闭】关闭期间不处理消息、不调用模型。"
        if command.action == "finish":
            if command.argument not in {"确认", "CONFIRM", "confirm"}:
                return (
                    "【确认完结】完结后只允许查看与导出。"
                    "请发送 /团 完结 确认。"
                )
            await self.database.finalize_session(
                session["id"],
                sender_id,
                termination_type="completed",
                reason="正常完结",
            )
            await self.engine.release_session_lock(session["id"])
            return (
                "【故事已完结】已创建最终保护存档并永久归档。"
                "\n角色、NPC、长期记忆、剧情账本、时间线和存档均只读保留；"
                "如需续作，请从最终存档克隆新副本。"
            )
        if command.action == "abort":
            parts = command.argument.split(maxsplit=1)
            confirmed = bool(
                parts
                and parts[0] in {"确认", "CONFIRM", "confirm"}
            )
            reason = parts[1].strip() if len(parts) > 1 else ""
            if not confirmed or not reason:
                return (
                    "【确认强制终止】此操作会创建最终保护存档并永久只读归档。"
                    "\n请发送：/团 强制终止 确认 <原因>"
                )
            await self.database.finalize_session(
                session["id"],
                sender_id,
                termination_type="aborted",
                reason=reason,
            )
            await self.engine.release_session_lock(session["id"])
            return (
                "【故事已强制终止】已保存最终保护存档并永久归档。"
                f"\n终止原因：{reason}"
            )
        if command.action == "maintenance":
            session = await self.database.transition_session(
                session["id"],
                SESSION_MAINTENANCE,
                sender_id,
            )
            return "【维护模式】仅保留管理操作，剧情不会推进。"
        if command.action == "save_list":
            snapshots = await self.database.list_snapshots(session["id"])
            if not snapshots:
                return "【存档列表】当前没有存档。"
            return "【存档列表】\n" + "\n".join(
                (
                    f"· {item['name']} · 第 {item['turn_no']} 回合"
                    f" · {item['kind']} · {item['created_at']}"
                )
                for item in snapshots
            )
        if command.action == "recap":
            return await build_recap(
                self.database,
                session,
                sender_id,
                command.argument,
            )
        if command.action == "save":
            if session["state"] != SESSION_RUNNING:
                raise InvalidTransitionError(
                    "只有正式运行中的故事可以创建新剧情存档"
                )
            if not command.argument:
                return "【开团】请提供存档名：/团 存档 <名称>"
            name = str(command.argument).strip()
            replace = False
            if name.endswith(" 覆盖确认"):
                name = name[: -len(" 覆盖确认")].strip()
                replace = True
            existing = next(
                (
                    item
                    for item in await self.database.list_snapshots(
                        session["id"]
                    )
                    if item["name"] == name
                ),
                None,
            )
            if existing and not replace:
                return (
                    "【发现同名存档】\n"
                    f"存档：{existing['name']}\n"
                    f"位置：第 {existing['turn_no']} 回合\n"
                    f"创建时间：{existing['created_at']}\n\n"
                    f"确认覆盖请发送：/团 存档 {name} 覆盖确认"
                )
            snapshot_result = await self.database.create_snapshot(
                session["id"],
                name,
                sender_id,
                replace=replace,
                expected_revision=int(snapshot.get("revision") or 0),
                expected_snapshot_revision=(
                    int(existing.get("revision") or 0)
                    if replace and existing is not None
                    else None
                ),
                idempotency_key=(
                    f"bot:snapshot:{transport_event_id(event)}:"
                    f"{'replace' if replace else 'create'}"
                ),
            )
            snapshot = snapshot_result["snapshot"]
            return (
                (
                    f"【覆盖成功】{snapshot['name']}"
                    if replace
                    else f"【存档完成】{snapshot['name']}"
                )
                + f"\n记录于第 {snapshot['turn_no']} 回合。"
            )
        if command.action == "delete_save":
            if not command.argument:
                return "【开团】格式：/团 删档 <存档名>"
            snapshots = await self.database.list_snapshots(
                session["id"]
            )
            snapshot = next(
                (
                    item for item in snapshots
                    if item["id"] == command.argument
                    or item["name"] == command.argument
                ),
                None,
            )
            if not snapshot:
                raise DatabaseNotFoundError("存档不存在")
            await self.database.delete_snapshot(
                session["id"],
                snapshot["id"],
                sender_id,
                expected_revision=int(session.get("revision") or 0),
                idempotency_key=(
                    f"bot:snapshot:{transport_event_id(event)}:delete"
                ),
            )
            return f"【删档完成】已删除存档「{snapshot['name']}」。"
        if command.action == "load":
            if not command.argument:
                return "【开团】请提供存档名：/团 读档 <名称>"
            snapshots = await self.database.list_snapshots(session["id"])
            snapshot = next(
                (
                    item for item in snapshots
                    if item["id"] == command.argument
                    or item["name"] == command.argument
                ),
                None,
            )
            if snapshot is None:
                raise DatabaseNotFoundError("存档不存在")
            restored_result = await self.database.restore_snapshot(
                session["id"],
                snapshot["id"],
                sender_id,
                expected_revision=int(snapshot.get("revision") or 0),
                idempotency_key=(
                    f"bot:snapshot:{transport_event_id(event)}:restore"
                ),
            )
            restored = restored_result["snapshot"]
            await self.database.pause_session_timers(
                session["id"],
                sender_id,
            )
            return (
                f"【读档完成】已恢复至第 {restored['turn_no']} 回合。"
                "\n会话已暂停；发送 /团 恢复 进入恢复准备大厅。"
            )
        if command.action == "rollback":
            restored_result = await self.database.restore_latest_auto(
                session["id"],
                sender_id,
            )
            restored = restored_result["snapshot"]
            await self.database.pause_session_timers(
                session["id"],
                sender_id,
            )
            return (
                f"【回滚完成】已恢复至第 {restored['turn_no']} 回合。"
                "\n会话已暂停；发送 /团 恢复 进入恢复准备大厅。"
            )
        return _COMMAND_UNHANDLED

    async def _handle_command_impl(
        self,
        *,
        event: AstrMessageEvent,
        command: ParsedCommand,
        config: TavernConfig,
        group_id: str,
        platform_id: str,
        sender_id: str,
    ) -> str | None:
        is_admin = config.is_admin(sender_id)

        if not config.admin_ids:
            await self._write_security_audit(
                sender_id=sender_id,
                action=command.action,
                group_id=group_id,
                platform_id=platform_id,
                reason="admin_not_configured",
            )
            return (
                "【开团尚未初始化】请先在开团控制台填写至少一个"
                "管理员账号；随后由该账号在目标群发送 /团 开启，"
                "系统会自动完成当前群授权。"
            )

        session = await self.database.get_session_by_group(
            platform_id,
            group_id,
        )
        roles = (
            await self.database.permission_roles(session["id"], sender_id)
            if session
            else set()
        )
        is_host = is_admin or "host" in roles
        is_moderator = is_host or "moderator" in roles
        control_state = (
            await self.database.get_control_state(session["id"])
            if session
            else {"mode": "auto", "active_dm_user_id": ""}
        )
        is_active_dm = bool(
            control_state.get("mode") == "dm"
            and str(control_state.get("active_dm_user_id") or "") == sender_id
        )
        growth_roles = set(roles)
        if is_admin:
            growth_roles.add("admin")
        handled, response = await self._handle_growth_command_service(
            event,
            command,
            session_id=str((session or {}).get("id") or ""),
            roles=tuple(growth_roles),
            is_private=False,
        )
        if handled:
            return response
        handled, response = await self._handle_session_command_service(
            event,
            command,
            session=session,
            roles=set(roles),
            is_admin=is_admin,
            platform_id=platform_id,
            group_id=group_id,
            sender_id=sender_id,
        )
        if handled:
            return response
        handled, response = await self._handle_admin_command_service(
            event,
            command,
            session=session,
            roles=set(roles),
            is_admin=is_admin,
            platform_id=platform_id,
            group_id=group_id,
            sender_id=sender_id,
        )
        if handled:
            return response
        host_actions = {
            "start",
            "perform",
            "pause",
            "recover",
            "resume",
            "close",
            "finish",
            "abort",
            "save",
            "delete_save",
            "load",
            "rollback",
            "save_list",
            "review",
            "force_ready",
            "extend",
            "countdown",
            "usage",
            "quota",
            "delete_session",
            "instances",
            "worlds",
            "dm",
            "tactical_lock",
            "tactical_advance",
            "tactical_correct",
            "tactical_end",
            "challenge_advance",
            "challenge_end",
        }
        moderator_actions = {
            "next",
            "move",
            "designate",
            "ban",
            "unban",
            "ban_list",
        }

        group_allowed = config.is_group_allowed(group_id)
        public_action = group_allowed and (
            command.action in PLAYER_ACTIONS
            or (
                command.action == "status"
                and config.public_status
            )
        )
        privileged_action = (
            command.action in host_actions and (is_host or (command.action == "dm" and is_active_dm))
        ) or (
            command.action in moderator_actions and is_moderator
        )
        harmless_action = command.action in {"help", "unknown"}
        if not is_admin and not public_action and not privileged_action and not harmless_action:
            await self._write_security_audit(
                sender_id=sender_id,
                action=command.action,
                group_id=group_id,
                platform_id=platform_id,
                reason="sender_not_authorized",
            )
            if config.unauthorized_command_behavior == "deny":
                return "【开团】该命令只允许授权管理员使用。"
            return None

        auto_bound = False
        if not group_allowed:
            if is_admin and command.action == "start":
                try:
                    auto_bound = await self._allow_group(
                        group_id=group_id,
                        platform_id=platform_id,
                        actor_id=sender_id,
                        source="authorized_group_command",
                    )
                    group_allowed = True
                except ValueError as exc:
                    return f"【开团】无法识别当前群：{exc}"
                except Exception:
                    logger.exception("321开团自动绑定群失败")
                    return (
                        "【开团】当前群授权失败，系统没有创建副本。"
                        "\n原因：无法保存当前群的授权信息。"
                        "\n下一步：请在控制台检查群授权配置后重试 /团 开启。"
                    )
            elif command.action not in {
                "help",
                "unknown",
                "worlds",
                "instances",
            }:
                return (
                    "【开团】本群尚未授权。请由管理员发送 /团 开启，"
                    "系统会自动完成授权并显示可用世界。"
                )

        if command.action in {"help", "unknown"}:
            help_text = _help_text(config.trigger_prefix)
            if command.action == "unknown":
                return (
                    f"【开团】未知命令：{command.raw_action}\n\n{help_text}"
                )
            turn = (
                await self.database.get_turn_status(session["id"])
                if session
                else {}
            )
            contextual = contextual_help(
                command.argument,
                session=session or {},
                turn=turn,
                user_id=sender_id,
                is_admin=is_admin,
            )
            return contextual if command.argument else contextual + "\n\n" + help_text

        # ── 暂停态守卫 ─────────────────────────────────────────────
        # 会话处于「已暂停」时，任何会改变剧情 / 选项 / 投票 / 队列 /
        # 角色卡的玩法指令都必须拦截，避免“已暂停却能重整选项、推进游戏”。
        # 仅放行主持人 / 管理员的会话管理类指令（恢复、续演、关闭、存档、
        # 状态查询等），这些指令在上方权限层已做 host 校验。
        if session and session["state"] == SESSION_PAUSED:
            paused_blocked_actions = {
                # 选项 / 行动 / 投票（核心玩法循环）
                "choose", "reroll", "inspiration", "inspiration_reroll",
                "vote",
                "rescue",
                "tactical_action", "tactical_guard", "tactical_aid",
                "tactical_retreat", "tactical_parley", "tactical_confirm",
                "tactical_lock", "tactical_advance", "tactical_correct", "tactical_end",
                "challenge_action", "challenge_withdraw", "challenge_negotiate",
                "challenge_confirm", "challenge_advance", "challenge_end",
                # 队列 / 回合控制
                "join", "ready", "away", "return_queue", "return_request",
                "delegate", "delegate_revoke", "leave", "order", "skip",
                "next", "move", "designate", "perform", "force_ready",
                # 角色卡建立
                "card", "card_fill", "card_preview", "card_stats_reset",
                "card_timer_notice", "card_confirm", "card_cancel",
                "dm",
            }
            if command.action in paused_blocked_actions:
                return (
                    "【开团】剧情已暂停，暂不可进行选项 / 投票 / 行动 / "
                    "建卡等玩法操作。\n请先由主持人发送 /团 恢复 "
                    "进入恢复准备大厅，再发送 /团 继续 续演。"
                )

        handled, response = await self._handle_tactical_command_service(
            event,
            command,
            session=session,
            roles=set(roles),
            is_admin=is_admin,
            platform_id=platform_id,
            group_id=group_id,
            sender_id=sender_id,
        )
        if handled:
            return response

        if command.action == "dm":
            if session is None:
                return "【开团】当前群尚未开馆，请先发送 /团 开启。"
            return await self._handle_dm_command_service(
                event,
                session=session,
                sender_id=sender_id,
                is_admin=is_admin,
                argument=command.argument,
            )
        handled, response = await self._handle_turn_vote_command_service(
            event,
            command,
            platform_id=platform_id,
            group_id=group_id,
            sender_id=sender_id,
            is_admin=is_admin,
            is_moderator=is_moderator,
            is_active_dm=is_active_dm,
        )
        if handled:
            return response

        try:
            for handler in (
                self._handle_legacy_command_part_1,
                self._handle_legacy_command_part_2,
                self._handle_legacy_command_part_3,
                self._handle_legacy_command_part_4,
            ):
                response = await handler(event=event, command=command, config=config, group_id=group_id, platform_id=platform_id, sender_id=sender_id, session=session, roles=roles, is_admin=is_admin, is_host=is_host, is_moderator=is_moderator, is_active_dm=is_active_dm, control_state=control_state)
                if response is not _COMMAND_UNHANDLED:
                    return response
        except (
            DatabaseNotFoundError,
            InvalidTransitionError,
            PermissionError,
            ValueError,
        ) as exc:
            return f"【开团】{exc}"
        except Exception:
            logger.exception("321开团管理命令失败")
            return (
                "【管理操作失败】\n"
                "失败操作：处理本次管理命令。\n"
                "原因：系统发生未预期错误。\n"
                "自动处理：本次操作没有继续执行，已有副本状态保持不变。\n"
                "下一步：请稍后重试；若持续失败，请管理员在 WebUI 健康检查中确认服务状态。"
            )
        return _help_text(config.trigger_prefix)
