from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


async def _console_instance_time_rules(
    database: Any,
    config: TavernConfig,
    world_time: Mapping[str, Any] | None,
    actor_id: str,
) -> dict[str, Any]:
    recorded = await database.record_configuration_revision(
        config.to_mapping(),
        actor_id,
    )
    global_revision = int(recorded.get("revision") or 0)
    if global_revision < 1:
        raise RuntimeError("当前全局提醒设置没有可冻结的修订")
    merged = {
        **dict(config.time_rules),
        **(dict(world_time) if isinstance(world_time, Mapping) else {}),
    }
    reminder = dict(config.time_rules.get("story_generation_reminder") or {})
    reminder.update(
        {
            "source": "global_default",
            "revision": 0,
            "source_revision": global_revision,
        }
    )
    merged["story_generation_reminder"] = reminder
    return dict(normalize_time_rules(merged))




class OperationQueriesMixin:
    async def session_lifecycle(self):
        """Console-only lifecycle route; never impersonates a platform DM."""

        try:
            principal = self._console_principal()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            action = str(payload.get("action") or "").strip().lower()
            confirmation = payload.get("confirmation")
            confirmation = (
                confirmation if isinstance(confirmation, Mapping) else {}
            )
            if not session_id:
                raise WebApiError(
                    400,
                    "SESSION_NOT_FOUND",
                    "处理副本失败：缺少要操作的副本。",
                    "系统没有修改任何数据；请刷新副本列表后重试。",
                )
            try:
                result = await self.database.apply_session_lifecycle(
                    session_id,
                    action,
                    (
                        "console_admin:astrbot:"
                        + str(principal.get("username") or self._username())
                    ),
                    reason=str(payload.get("reason") or ""),
                    expected_revision=int(
                        payload.get("expected_revision", 0) or 0
                    ),
                    idempotency_key=str(
                        payload.get("idempotency_key") or ""
                    ),
                    confirmation_name=str(
                        confirmation.get("session_name") or ""
                    ),
                    acknowledge_archive=bool(
                        confirmation.get("acknowledge_archive", False)
                    ),
                )
            except DatabaseNotFoundError as exc:
                raise WebApiError(
                    404,
                    "SESSION_NOT_FOUND",
                    "处理副本失败：副本不存在或已经被移除。",
                    "系统没有修改任何数据；请刷新副本列表。",
                ) from exc
            except DatabaseConflictError as exc:
                detail = str(exc)
                if "尚未开演" in detail:
                    code = "SESSION_FINISH_NOT_STARTED"
                    message = (
                        "完结故事失败：故事尚未开演，"
                        "请改用“放弃本轮”。"
                    )
                elif "永久归档" in detail:
                    code = "SESSION_ALREADY_ARCHIVED"
                    message = "处理副本失败：副本已经永久归档。"
                else:
                    code = "SESSION_REVISION_CONFLICT"
                    message = "处理副本失败：页面中的副本状态已经过期。"
                raise WebApiError(
                    409,
                    code,
                    message,
                    "系统没有修改副本；请刷新后重新确认。",
                ) from exc
            except InvalidTransitionError as exc:
                raise WebApiError(
                    409,
                    "SESSION_LIFECYCLE_STATE_CONFLICT",
                    "处理副本失败：当前状态不允许执行这个动作。",
                    "系统没有修改副本；请刷新并查看当前可用操作。",
                ) from exc
            except ValueError as exc:
                detail = str(exc)
                if "确认" in detail or "归档" in detail:
                    raise WebApiError(
                        422,
                        "SESSION_CONFIRMATION_MISMATCH",
                        "处理副本失败：二次确认与当前副本不一致。",
                        "系统没有修改副本；请关闭确认窗口，刷新后重新操作。",
                    ) from exc
                code = (
                    "SESSION_ABORT_REASON_REQUIRED"
                    if action == "abort" and "原因" in detail
                    else "SESSION_LIFECYCLE_INVALID_ACTION"
                )
                raise WebApiError(
                    400,
                    code,
                    f"处理副本失败：{detail}",
                    "系统没有修改副本；请修正输入后重试。",
                ) from exc

            await self.broker.publish(
                {
                    "type": "session",
                    "action": f"lifecycle.{action}",
                    "session_id": session_id,
                }
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)
    async def session_action(self):
        try:
            user = self._username()
            payload = await self._payload()
            actor = self._actor()
            action = str(payload.get("action", "")).strip().lower()
            if action == "create":
                self._require_admin()
                platform_id = str(payload.get("platform_id") or "")
                group_id = str(payload.get("group_id") or "")
                session = await self.database.ensure_session(
                    platform_id,
                    group_id,
                    str(payload.get("unified_origin") or ""),
                    str(payload.get("world_ref") or ""),
                    actor,
                    str(payload.get("instance_slug") or ""),
                    str(payload.get("instance_name") or ""),
                )
                config = TavernConfig.from_mapping(self.plugin_config)
                world = await self.database.get_world(session["world_id"])
                world_rules = world.get("rules") or {}
                world_time = (
                    world_rules.get("time_rules")
                    if isinstance(world_rules, dict)
                    else {}
                )
                await self.database.save_instance_time_rules(
                    session["id"],
                    await _console_instance_time_rules(
                        self.database,
                        config,
                        world_time,
                        actor,
                    ),
                    actor,
                )
                await self.database.grant_permission(
                    session["id"],
                    actor,
                    "host",
                    actor,
                )
                await self.allow_group(
                    group_id=group_id,
                    platform_id=platform_id,
                    actor_id=actor,
                    source="web_session_create",
                )
            elif action == "clone":
                source_session_id = str(payload.get("session_id") or "")
                if not source_session_id:
                    raise ValueError("缺少 session_id")
                await self._require_dm_capability(source_session_id, user)
                session = await self.database.clone_session(
                    source_session_id,
                    actor,
                    instance_slug=str(
                        payload.get("instance_slug") or ""
                    ),
                    instance_name=str(
                        payload.get("instance_name") or ""
                    ),
                    snapshot_ref=str(
                        payload.get("snapshot_ref") or ""
                    ),
                    candidate_world_ref=str(
                        payload.get("candidate_world_ref") or ""
                    ),
                )
            else:
                session_id = str(payload.get("session_id") or "")
                if not session_id:
                    raise ValueError("缺少 session_id")
                await self._require_dm_capability(session_id, user)
                if action == "force_ready":
                    result = await self.database.force_all_ready(
                        session_id,
                        actor,
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "delete":
                    result = await self.database.delete_session(
                        session_id,
                        actor,
                        str(payload.get("confirm_name") or ""),
                    )
                    await self.broker.publish(
                        {
                            "type": "session",
                            "action": action,
                            "session_id": session_id,
                        }
                    )
                    return json_response({"result": result})
                if action == "perform":
                    result = await self.database.activate_story(
                        session_id,
                        actor,
                        resume=bool(payload.get("resume", False)),
                    )
                    if not result["started"]:
                        raise ValueError(
                            "；".join(
                                result.get("blocker_messages")
                                or ["准备未完成"]
                            )
                        )
                    await self.database.resume_session_timers(
                        session_id,
                        actor,
                    )
                    session = result["session"]
                else:
                    if action == "pause":
                        await self.database.pause_session_timers(
                            session_id,
                            actor,
                        )
                    if action in {"finish", "abort"}:
                        session = await self.database.finalize_session(
                            session_id,
                            actor,
                            termination_type=(
                                "aborted"
                                if action == "abort"
                                else "completed"
                            ),
                            reason=str(
                                payload.get("reason")
                                or (
                                    "正常完结"
                                    if action == "finish"
                                    else ""
                                )
                            ),
                        )
                        await self.broker.publish(
                            {
                                "type": "session",
                                "action": action,
                                "session_id": session["id"],
                            }
                        )
                        return json_response({"session": session})
                    target_map = {
                        "start": SESSION_PREPARING,
                        "resume": SESSION_PREPARING,
                        "pause": SESSION_PAUSED,
                        "close": SESSION_CLOSED,
                        "maintenance": SESSION_MAINTENANCE,
                    }
                    if action not in target_map:
                        raise ValueError("不支持的会话操作")
                    session = await self.database.transition_session(
                        session_id,
                        target_map[action],
                        actor,
                        str(payload.get("world_ref") or ""),
                    )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": action,
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_state(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            state = payload.get("world_state")
            if not isinstance(state, dict):
                raise ValueError("world_state 必须是 JSON 对象")
            session = await self.database.save_manual_state(
                session_id,
                state,
                int(payload.get("revision")),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "state_edit",
                    "session_id": session["id"],
                }
            )
            return json_response({"session": session})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_turn_order(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            order = payload.get("order")
            if not isinstance(order, list):
                raise ValueError("order 必须是用户 ID 数组")
            turn = await self.database.set_turn_order(
                session_id,
                [str(item) for item in order],
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "turn_order",
                    "session_id": session_id,
                }
            )
            return json_response({"turn": turn})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_time_rules(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_instance_time_rules(
                session_id,
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"instance_config": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_rules(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            rules = payload.get("rules")
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是 JSON 对象")
            item = await self.database.save_session_rule_state(
                session_id,
                rules,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "rules_update",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"rule_state": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_npc(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            item = await self.database.save_session_character(
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "npc_update",
                    "session_id": item["session_id"],
                }
            )
            instance = await self.database.get_instance_config(item["session_id"])
            world = (
                instance.get("world_snapshot")
                if isinstance(instance, Mapping)
                and isinstance(instance.get("world_snapshot"), Mapping)
                else {}
            )
            npc_payload = project_npc_view(
                world,
                {},
                session_npcs=[item],
                viewer_role="admin",
                include_technical_refs=True,
            )
            expected_refs = {
                str(item.get("id") or ""),
                str(item.get("stable_key") or ""),
            }
            projected = next(
                (
                    dict(view)
                    for view in npc_payload.get("items") or []
                    if isinstance(view, Mapping)
                    and (
                        str(view.get("technical_ref") or "") in expected_refs
                        or str(view.get("npc_ref") or "") in expected_refs
                    )
                ),
                {},
            )
            projected.pop("technical_ref", None)
            projected.pop("npc_ref", None)
            return json_response({"character": item, "npc_view": projected})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_timer(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            item = await self.database.control_timer(
                str(payload.get("timer_id") or ""),
                str(payload.get("action") or ""),
                self._actor(),
                seconds=int(payload.get("seconds") or 0),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": str(payload.get("action") or ""),
                    "session_id": item["session_id"],
                }
            )
            return json_response({"timer": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_timer_policy(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            item = await self.database.set_timer_policy(
                session_id,
                str(payload.get("timer_type") or "all"),
                bool(payload.get("enabled", False)),
                self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "timing",
                    "action": "policy",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"policy": item})
        except Exception as exc:
            return self._handle_error(exc)
    async def session_token_quota(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            item = await self.database.set_token_quota(
                session_id,
                str(payload.get("scope_type") or "session"),
                window_seconds=int(payload.get("window_seconds") or 3600),
                token_limit=int(payload.get("token_limit") or 100000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "quota",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)
