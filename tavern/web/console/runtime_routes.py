from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


class ConsoleRuntimeRouteMethods:
    async def character_import(self):
        """导入常驻角色/NPC JSON：校验后批量写入指定世界。"""
        try:
            self._require_admin()
            payload = await self._payload()
            world_ref = (
                payload.get("world_id")
                or payload.get("world_slug")
                or payload.get("worldRef")
                or ""
            )
            if not str(world_ref).strip():
                raise ValueError(
                    "请指定目标世界：world_id 或 world_slug 不能为空"
                )
            world = await self.database.get_world(str(world_ref).strip())
            template_version = int(payload.get("template_version", 1) or 1)
            if template_version not in {1, 2}:
                raise ValueError("只接受 NPC 导入模板 v1 或 v2")
            items = (
                payload.get("items")
                or payload.get("npcs")
                or payload.get("characters")
            )
            if not isinstance(items, list) or not items:
                raise ValueError(
                    "常驻角色数据必须是非空数组（字段 items / npcs）"
                )
            registry = EntityRegistry(world)
            input_slugs: set[str] = set()
            normalized_items: list[dict[str, Any]] = []
            for index, raw in enumerate(items):
                if not isinstance(raw, dict):
                    raise ValueError(f"第 {index + 1} 个角色项必须是对象")
                name = str(raw.get("name") or "").strip()
                if not name:
                    raise ValueError(f"第 {index + 1} 个角色缺少 name（名称）")
                role = str(raw.get("role") or "npc").strip() or "npc"
                npc_profile = raw.get("profile")
                if npc_profile is not None and not isinstance(npc_profile, dict):
                    raise ValueError(
                        f"角色「{name}」的 profile 必须是 JSON 对象"
                    )
                profile = dict(npc_profile) if isinstance(npc_profile, dict) else {}
                raw_slug = str(raw.get("slug") or "").strip()
                if raw_slug:
                    if raw_slug in input_slugs:
                        raise ValueError(f"NPC 导入包含重复 slug：{raw_slug}")
                    input_slugs.add(raw_slug)
                    validate_slug(raw_slug)
                ref_fields = {
                    "capability_refs": "capability",
                    "resource_refs": "resource",
                    "runtime_effect_refs": "runtime_effect",
                    "object_refs": "object",
                }
                for field, expected_type in ref_fields.items():
                    values = profile.get(field, [])
                    if not isinstance(values, list):
                        raise ValueError(f"角色「{name}」的 {field} 必须是数组")
                    for ref in values:
                        registry.resolve(ref, expected_type)
                resources = profile.get("resources", {})
                if resources and not isinstance(resources, dict):
                    raise ValueError(f"角色「{name}」的 resources 必须是对象")
                if isinstance(resources, dict):
                    for ref in resources:
                        registry.resolve(ref, "resource")
                private = raw.get("private_direction") or raw.get("prompt") or ""
                if private:
                    profile.setdefault("private_direction", private)
                character_payload = {
                    "slug": raw_slug or f"npc_{uuid.uuid4().hex[:12]}",
                    "name": name,
                    "role": role,
                    "profile": profile,
                    "prompt": str(private),
                    "enabled": 1,
                }
                if "sort_order" in raw:
                    character_payload["sort_order"] = int(raw["sort_order"])
                normalized_items.append(character_payload)
            result = await self.database.import_characters(
                world["id"],
                normalized_items,
                self._actor(),
                conflict_policy=str(payload.get("conflict_policy") or "skip"),
            )
            await self.broker.publish(
                {
                    "type": "world",
                    "action": "character_import",
                    "world_id": world["id"],
                    "created": result["created"],
                    "updated": result["updated"],
                    "skipped": result["skipped"],
                }
            )
            return json_response(
                {
                    **result,
                    "world_id": world["id"],
                    "world_name": world.get("name"),
                    "transactional": True,
                    "same_name_different_slug": "allowed",
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def sessions(self):
        try:
            self._console_principal()
            result = await self.database.search_sessions(
                str(request.query.get("q", "") or ""),
                str(request.query.get("scope", "all") or "all"),
                request.query.get("page", 1, type=int),
                request.query.get("page_size", 20, type=int),
            )
            from ...session_lifecycle import lifecycle_capabilities

            for item in result.get("items", []):
                if not isinstance(item, dict):
                    continue
                context = await self.database.session_lifecycle_context(
                    str(item.get("id") or "")
                )
                item.update(
                    lifecycle_capabilities(
                        item,
                        context,
                        authorized=True,
                    )
                )
            result["items"] = await enrich_session_display_labels(
                self.database,
                result.get("items", []),
            )
            result["options"] = await self.database.list_session_options()
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def console_session_detail(self):
        """Controller-only detail route; never reinterpret a console login as QQ."""

        try:
            principal = self._console_principal()
            session_id = str(request.query.get("id", "") or "")
            result = await semantic_session_detail_view(
                self.database,
                session_id,
                principal,
            )
            from ...session_lifecycle import lifecycle_capabilities

            session = result.get("session")
            if isinstance(session, dict):
                projected = await enrich_session_display_labels(
                    self.database,
                    [session],
                )
                if projected:
                    session.clear()
                    session.update(projected[0])
                context = await self.database.session_lifecycle_context(
                    session_id
                )
                result.update(
                    lifecycle_capabilities(
                        session,
                        context,
                        authorized=True,
                    )
                )
                session["capabilities"] = result["capabilities"]
                session["lifecycle_context"] = result[
                    "lifecycle_context"
                ]
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def console_session_shell(self):
        """Return only the first-paint session header, permissions and tabs."""

        try:
            principal = self._console_principal()
            session_id = str(request.query.get("id", "") or "")
            result = await semantic_session_shell_view(
                self.database,
                session_id,
                principal,
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def session_detail(self):
        try:
            user = self._username()
            principal = self._web_principal()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            can_manage_narrative = False
            try:
                await self._require_dm_capability(session_id, user)
                can_manage_narrative = True
            except Exception:
                can_manage_narrative = False
            viewer_role = (
                "admin"
                if principal["is_admin"]
                else ("dm" if can_manage_narrative else "player")
            )
            is_privileged = viewer_role in {"dm", "admin"}
            if not is_privileged:
                query = {"session_id": session_id}
                character_envelope = await route_character_list_view(
                    principal,
                    self.database,
                    session_id,
                    query=query,
                )
                world_envelope = await route_world_state_view(
                    principal,
                    self.database,
                    session_id,
                    query=query,
                )
                asset_envelope = await route_assets_view(
                    principal,
                    self.database,
                    query=query,
                )
                operation_envelope = await route_operations_view(
                    principal,
                    self.database,
                    query=query,
                )
                narrative = await route_narrative_control_view(
                    self.database,
                    session_id,
                    principal,
                )
                growth_role, participant_id = await self._growth_viewer(
                    principal,
                    session_id,
                )
                growth = await self.database.list_growth_profiles(
                    session_id,
                    participant_id=participant_id,
                    viewer_role=growth_role,
                    include_technical_refs=False,
                )
                growth = (
                    dict(growth)
                    if isinstance(growth, Mapping)
                    else {}
                )

                def body(envelope: Mapping[str, Any]) -> dict[str, Any]:
                    if int(envelope.get("status") or 500) >= 400:
                        error = envelope.get("error")
                        if isinstance(error, Mapping):
                            raise PolicyRejection(
                                str(error.get("message") or "无法读取副本视图")
                            )
                        raise PolicyRejection("无法读取副本视图")
                    value = envelope.get("body")
                    return dict(value) if isinstance(value, Mapping) else {}

                public_session = {
                    "state": str(session.get("state") or ""),
                    "instance_name": str(session.get("instance_name") or ""),
                    "world_name": str(session.get("world_name") or ""),
                    "turn_no": int(session.get("turn_no") or 0),
                    "location": str(
                        (session.get("world_state") or {}).get("location") or ""
                        if isinstance(session.get("world_state"), Mapping)
                        else ""
                    ),
                }
                return json_response(
                    {
                        "session": public_session,
                        "viewer_role": "player",
                        "readonly": str(session.get("state") or "")
                        == SESSION_FINISHED,
                        "permissions": {
                            "can_admin": False,
                            "can_manage_narrative": False,
                            "can_view_private": False,
                            "role_source": principal["role_source"],
                        },
                        "narrative_control": narrative,
                        "characters": body(character_envelope),
                        "world_state": body(world_envelope),
                        "assets": body(asset_envelope),
                        "operations": body(operation_envelope),
                        "growth": {
                            "tracks": list(growth.get("tracks") or [])
                        },
                        "latest_seq": (
                            await self.database.latest_session_event_seq(
                                session_id
                            )
                        ),
                    }
                )
            # D1-DEL-002：普通玩家视图不暴露副本 UMO 等内部连接信息。
            if isinstance(session, Mapping) and not is_privileged:
                session = {
                    key: value
                    for key, value in session.items()
                    if key != "unified_origin"
                }
            dashboard_view = await build_session_dashboard(
                self.database,
                session_id,
                viewer_role=viewer_role,
                include_technical_refs=principal["is_admin"],
            )
            instance_config = await self.database.get_instance_config(session_id)
            world = (
                instance_config.get("world_snapshot")
                if isinstance(instance_config, Mapping)
                and isinstance(instance_config.get("world_snapshot"), Mapping)
                else {}
            )
            roster_rows = await self.database.list_roster(session_id)
            roster = [
                _actor_projection_row(world, item)
                for item in roster_rows
                if isinstance(item, Mapping)
            ]
            revision_rows = await self.database.list_card_revisions(session_id)
            card_revisions = _revision_projection_rows(
                world,
                revision_rows,
                roster_rows,
            )
            session_characters = await self.database.list_session_characters(
                session_id
            )
            story_ledger = await self.database.list_story_ledger(session_id)
            world_state_view = dashboard_view.get("world_state_view", {})
            permission_grants = (
                await self.database.list_permission_grants(session_id)
                if is_privileged
                else []
            )
            archive_view = dashboard_view.get("archive")
            readonly = bool(
                isinstance(archive_view, Mapping)
                and archive_view.get("readonly")
            )
            memories = await self.database.list_memories(
                session_id,
                "",
                500,
                include_invalidated=is_privileged,
            )
            if not is_privileged:
                # D1-DEL-002：普通玩家只能看到公开记忆；host/private 仅 DM/管理员。
                memories = [
                    item
                    for item in memories
                    if str(item.get("visibility") or "public") == "public"
                ]
            return json_response(
                {
                    "session": session,
                    "players": await self.database.list_players(session_id),
                    "roster": roster,
                    "turn": await self.database.get_turn_status(session_id),
                    "narrative_control": dashboard_view.get(
                        "narrative_control", {}
                    ),
                    "events": await self.database.recent_events(session_id, 80),
                    "snapshots": await self.database.list_snapshots(session_id),
                    "instance_config": instance_config,
                    "timers": await self.database.list_timers(session_id),
                    "timer_policy": (
                        await self.database.get_timer_policy(session_id)
                    ),
                    "token_usage": _with_token_context(
                        await self.database.token_usage_summary(session_id),
                        instance_config,
                    ),
                    "choice": await self.database.active_choice_set(session_id),
                    "vote": await self.database.active_vote(session_id),
                    "bans": (
                        await self.database.list_bans(session_id)
                        if is_privileged
                        else []
                    ),
                    "permissions": {
                        "can_admin": principal["is_admin"],
                        "can_dm": can_manage_narrative,
                        "can_manage_narrative": (
                            can_manage_narrative and not readonly
                        ),
                        "can_review_cards": (
                            can_manage_narrative and not readonly
                        ),
                        "can_force_ready": (
                            can_manage_narrative
                            and not readonly
                            and str(session.get("state") or "") == "preparing"
                        ),
                        "can_view_private": can_manage_narrative,
                        "role_source": principal["role_source"],
                    },
                    "permission_grants": permission_grants,
                    "economy": (
                        await self.database.economy_summary(session_id)
                    ),
                    "delegations": (
                        await self.database.list_delegations(session_id)
                    ),
                    "pending_operations": (
                        await self.database.pending_operations(session_id)
                    ),
                    "return_requests": (
                        await self.database.list_return_requests(session_id)
                    ),
                    "preflight": (
                        await self.database.opening_preflight(session_id)
                    ),
                    "rule_state": (
                        await self.database.get_session_rule_state(session_id)
                    ),
                    "session_characters": session_characters,
                    "story_ledger": story_ledger,
                    "world_state_view": world_state_view,
                    "scene_clocks": (
                        await self.database.list_scene_clocks(session_id)
                    ),
                    "memories": memories,
                    "archive": archive_view,
                    "readonly": readonly,
                    "module_panels": dashboard_view.get("module_panels", {}),
                    "module_summary": dashboard_view.get("module_summary", {}),
                    "actor_fate_view": dashboard_view.get(
                        "actor_fate_view", {}
                    ),
                    "terminal_report": dashboard_view.get("terminal_report"),
                    "storage": (
                        await self.database.get_storage_info(session_id)
                    ),
                    "operations": (
                        await self.database.list_session_operations(session_id, 50)
                    ),
                    "card_revisions": card_revisions,
                    "latest_seq": await self.database.latest_session_event_seq(
                        session_id
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_changes(self):
        """读取 ``after_seq`` 之后的副本增量事件，供 WebUI 局部刷新。

        WP-11：响应携带 ``latest_seq`` 客户端锚点、``items`` 安全语义
        投影（普通用户不含内部 ref / raw payload）、``has_more``（按可见
        事件判断，正确处理 visibility 过滤导致的 seq 间隙）、
        ``affected_modules`` 汇总，以及 ``full_refresh`` /
        ``needs_full_refresh``（``after_seq=0``、客户端落后投影检查点、
        结构性事件时触发）。
        """

        try:
            principal = self._web_principal()
            is_admin = bool(principal["is_admin"])
            session_id = str(request.query.get("session_id", "") or "").strip()
            if not session_id:
                raise ValueError("缺少副本 session_id")
            after_seq = max(
                0,
                int(request.query.get("after_seq", "0") or 0),
            )
            limit = max(
                1,
                min(500, int(request.query.get("limit", "200") or 200)),
            )
            rows = await self.database.list_session_events(
                session_id,
                after_seq=after_seq,
                # 多取一行以按“可见事件”判断分页，避免 visibility 过滤
                # 造成的 seq 间隙令 has_more 恒真。
                limit=limit + 1,
                visibility="" if is_admin else "public",
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            items = [
                project_session_event(row, is_admin=is_admin)
                for row in rows
                if isinstance(row, Mapping)
            ]
            latest_seq = await self.database.latest_session_event_seq(
                session_id
            )
            checkpoint = await self.database.get_projection_checkpoint(
                session_id,
                "webui_live",
            )
            behind_checkpoint = bool(
                isinstance(checkpoint, Mapping)
                and after_seq
                < int(checkpoint.get("last_seq") or 0)
            )
            full_refresh = after_seq == 0 or behind_checkpoint
            needs_full_refresh = behind_checkpoint or has_structural_event(
                items
            )
            return json_response(
                {
                    "session_id": session_id,
                    "after_seq": after_seq,
                    "latest_seq": latest_seq,
                    "items": items,
                    "has_more": has_more,
                    "affected_modules": summarize_affected_modules(items),
                    "full_refresh": full_refresh,
                    "needs_full_refresh": needs_full_refresh,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_source(self):
        """Return raw edit data only on an explicit admin technical request."""

        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            participant_id = str(request.query.get("participant_id", "") or "")
            if not session_id or not participant_id:
                raise ValueError("缺少副本或角色引用")
            instance = await self.database.get_instance_config(session_id)
            world = (
                instance.get("world_snapshot")
                if isinstance(instance, Mapping)
                and isinstance(instance.get("world_snapshot"), Mapping)
                else {}
            )
            roster = await self.database.list_roster(session_id)
            participant = next(
                (
                    item
                    for item in roster
                    if isinstance(item, Mapping)
                    and str(item.get("id") or "") == participant_id
                ),
                None,
            )
            if participant is None:
                raise DatabaseNotFoundError("角色不存在或已离开副本")
            raw_profile = participant.get("card_profile")
            if not isinstance(raw_profile, Mapping):
                raw_profile = participant.get("draft_profile")
            raw_profile = (
                dict(raw_profile) if isinstance(raw_profile, Mapping) else {}
            )
            projected = _actor_projection_row(
                world,
                participant,
                profile=raw_profile,
            )
            return json_response(
                {
                    "schema": "tavern-card-edit-source/1.0.0-rc10",
                    "actor_view": projected["actor_view"],
                    "technical_detail": {
                        "profile": raw_profile,
                        "warning": (
                            "此处为管理员角色卡编辑源，仅用于提交修订；"
                            "普通展示接口不会下发该数据。"
                        ),
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_recovery(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            session = await self.database.get_session(session_id)
            operations = await self.database.list_session_operations(session_id, 100)
            choice = await self.database.active_choice_set(session_id)
            vote = await self.database.active_vote(session_id)
            return json_response(
                {
                    "recovery": recovery_summary(
                        operations,
                        session_state=str(session.get("state") or ""),
                        has_active_choices=bool(choice),
                        has_active_vote=bool(vote),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_diagnostics(self):
        try:
            self._username()
            session_id = str(request.query.get("id", "") or "")
            report = await build_diagnostic_report(self.database, session_id)
            export_dir = self.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            path = next_timestamped_path(export_dir, "tavern_diagnostic", ".zip")
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            payload = (
                json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            try:
                with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("diagnostic.json", payload)
                    archive.writestr(
                        "README.txt",
                        "321开团脱敏故障报告。用户 ID 已哈希，密钥、私聊来源、私人字段与完整系统提示词未导出。\n",
                    )
                replace_with_retry(temporary, path)
            finally:
                unlink_with_retry(temporary, suppress_errors=True)
            return file_response(path, filename=path.name, content_type="application/zip")
        except Exception as exc:
            return self._handle_error(exc)

    async def session_rescue(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            service = EmergencyService(self.database)
            result = await service.execute(
                session_id,
                str(payload.get("action") or ""),
                payload,
                self._actor(),
            )
            await self.broker.publish(
                {"type": "session", "action": "rescue", "session_id": session_id}
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_card_revisions(self):
        try:
            if request.method == "GET":
                self._username()
                session_id = str(request.query.get("id", "") or "")
                instance = await self.database.get_instance_config(session_id)
                world = (
                    instance.get("world_snapshot")
                    if isinstance(instance, Mapping)
                    and isinstance(instance.get("world_snapshot"), Mapping)
                    else {}
                )
                roster = await self.database.list_roster(session_id)
                rows = await self.database.list_card_revisions(session_id)
                return json_response(
                    {"items": _revision_projection_rows(world, rows, roster)}
                )
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            action = str(payload.get("action") or "request").lower()
            if action == "request":
                item = await self.database.request_card_revision(
                    session_id,
                    str(payload.get("participant_ref") or ""),
                    payload.get("profile_patch") or {},
                    payload.get("stats_patch") or {},
                    self._actor(),
                    str(payload.get("note") or ""),
                )
            elif action in {"approve", "reject"}:
                expected_candidate_version = payload.get(
                    "expected_candidate_version"
                )
                idempotency_key = str(
                    payload.get("idempotency_key") or ""
                ).strip()
                if expected_candidate_version in {None, ""}:
                    raise ValueError("缺少 expected_candidate_version")
                if not idempotency_key:
                    raise ValueError("缺少 idempotency_key")
                item = await self.database.review_card_revision(
                    str(payload.get("request_id") or ""),
                    action == "approve",
                    self._actor(),
                    str(payload.get("note") or ""),
                    int(expected_candidate_version),
                    idempotency_key,
                )
            elif action == "cancel":
                expected_candidate_version = payload.get(
                    "expected_candidate_version"
                )
                idempotency_key = str(
                    payload.get("idempotency_key") or ""
                ).strip()
                if expected_candidate_version in {None, ""}:
                    raise ValueError("缺少 expected_candidate_version")
                if not idempotency_key:
                    raise ValueError("缺少 idempotency_key")
                item = await self.database.cancel_card_revision(
                    str(payload.get("request_id") or ""),
                    self._actor(),
                    expected_candidate_version=int(
                        expected_candidate_version
                    ),
                    idempotency_key=idempotency_key,
                )
            else:
                raise ValueError("不支持的角色卡修订操作")
            session_id = str(item.get("session_id") or session_id)
            instance = await self.database.get_instance_config(session_id)
            world = (
                instance.get("world_snapshot")
                if isinstance(instance, Mapping)
                and isinstance(instance.get("world_snapshot"), Mapping)
                else {}
            )
            roster = await self.database.list_roster(session_id)
            rows = _revision_projection_rows(
                world,
                await self.database.list_card_revisions(session_id),
                roster,
            )
            projected = next(
                (row for row in rows if str(row.get("id") or "") == str(item.get("id") or "")),
                dict(item),
            )
            return json_response({"item": projected})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_remark(self):
        try:
            self._require_admin()
            payload = await self._payload()
            item = await self.database.save_group_remark(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                str(payload.get("remark") or ""),
                self._actor(),
                int(payload.get("revision") or 0),
            )
            await self.broker.publish(
                {
                    "type": "group",
                    "action": "remark",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_usage(self):
        try:
            self._username()
            item = await self.database.group_token_usage_summary(
                str(request.query.get("platform_id", "") or ""),
                str(request.query.get("group_id", "") or ""),
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def group_token_quota(self):
        try:
            self._require_admin()
            payload = await self._payload()
            item = await self.database.set_group_token_quota(
                str(payload.get("platform_id") or ""),
                str(payload.get("group_id") or ""),
                window_seconds=int(
                    payload.get("window_seconds") or 86_400
                ),
                token_limit=int(payload.get("token_limit") or 500_000),
                enabled=bool(payload.get("enabled", True)),
                actor_id=self._actor(),
            )
            await self.broker.publish(
                {
                    "type": "token",
                    "action": "group_quota",
                    "platform_id": item["platform_id"],
                    "group_id": item["group_id"],
                }
            )
            return json_response({"usage": item})
        except Exception as exc:
            return self._handle_error(exc)
