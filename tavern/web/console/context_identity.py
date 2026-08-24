from __future__ import annotations

import json

from ...web_console_shared import *
from ...config import safe_config_projection
from ...web_console_compat import (
    error_response,
    file_response,
    is_standalone_upload,
    json_response,
    plugin_page_json_response,
    plugin_page_surface_response,
    request,
    stream_response,
)
from ..query import QueryAdapter
from ..routes.narrative_mode import (
    narrative_mode_view as route_narrative_mode_view,
)
from ..routes.narrative_style import (
    narrative_style_view as route_narrative_style_view,
)
from ..routes.gameplay_runtime import gameplay_runtime_view as route_gameplay_runtime_view
from ..intents.dispatcher import execute_intent
from ..routes.event_stream import format_sse_event, open_event_stream
from ..surfaces.registry import resolve_surface_key




class ContextIdentityMixin:
    async def dashboard_context(self):
        """Return the safe principal used by the AstrBot plugin-page bridge."""

        try:
            principal = self._surface_principal()
            capabilities = {
                str(key): bool(value)
                for key, value in dict(
                    principal.get("capabilities") or {}
                ).items()
                if isinstance(key, str)
            }
            roles: list[str] = []
            if bool(principal.get("is_admin")):
                roles.extend(("admin", "author", "host"))
            else:
                if bool(principal.get("is_author")) or capabilities.get("author"):
                    roles.append("author")
                if bool(principal.get("is_dm")) or capabilities.get("dm"):
                    roles.append("host")
                member_role = str(principal.get("member_role") or "").lower()
                if member_role in {"player", "member"} or capabilities.get("member"):
                    roles.append("player")
            if not roles:
                roles.append("readonly")
            auth_source = str(
                principal.get("auth_source") or "astrbot_console"
            )
            return json_response(
                {
                    "authenticated": True,
                    "principalRef": f"{auth_source}:current",
                    "roles": list(dict.fromkeys(roles)),
                    "is_admin": "admin" in roles,
                    "is_author": "author" in roles,
                    "is_host": "host" in roles,
                    "is_player": "player" in roles,
                    "is_readonly": roles == ["readonly"],
                    "capabilities": capabilities,
                    "auth_source": auth_source,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_intent(self):
        try:
            payload = await request.json(default={})
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            headers = dict(getattr(request, "headers", {}) or {})
            idempotency_key = str(
                headers.get("x-idempotency-key")
                or headers.get("X-Idempotency-Key")
                or payload.pop("idempotency_key", "")
                or ""
            )
            result = await execute_intent(
                self._surface_principal(),
                self.database,
                self.application_router,
                payload=payload,
                idempotency_key=idempotency_key,
                services={
                    "backup_recovery_service": getattr(
                        self,
                        "backup_recovery_service",
                        None,
                    ),
                    "plugin_config": self.plugin_config,
                    "config_lock": getattr(self, "config_lock", None),
                    "world_twp": getattr(self, "world_twp", None),
                    "publish": getattr(getattr(self, "broker", None), "schedule", None),
                    "default_world_slug": self._config().default_world_slug,
                    "http_client": getattr(getattr(self, "context", None), "http_client", None),
                    "data_dir": getattr(self, "data_dir", None),
                },
            )
            return json_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_events(self):
        try:
            query = self._route_query("session_key", "after_seq")
            headers = dict(getattr(request, "headers", {}) or {})
            last_event_id = (
                headers.get("Last-Event-ID")
                or headers.get("last-event-id")
                or headers.get("Last-event-id")
            )
            if last_event_id not in (None, ""):
                query["last_event_id"] = last_event_id
            iterator = await open_event_stream(
                self._surface_principal(),
                self.database,
                self.broker,
                query=query,
            )

            async def stream():
                async for item in iterator:
                    yield format_sse_event(item)

            return stream_response(stream())
        except Exception as exc:
            return self._handle_error(exc)
    async def session_narrative_mode(self):
        try:
            method = str(getattr(request, "method", "GET") or "GET").upper()
            payload = (
                await request.json(default={}) if method == "POST" else {}
            )
            headers = dict(getattr(request, "headers", {}) or {})
            idempotency_key = str(
                headers.get("x-idempotency-key")
                or headers.get("X-Idempotency-Key")
                or ""
            )
            principal = self._surface_principal()
            query = self._route_query("session_key", "session_id")
            result = await route_narrative_mode_view(
                principal,
                self.database,
                query=query,
                payload=payload if isinstance(payload, Mapping) else {},
                method=method,
                idempotency_key=idempotency_key,
            )
            if method == "POST" and int(result.get("status", 500)) < 300:
                data = result.get("body", {}).get("data", {})
                object_key = str(
                    (payload or {}).get("session_key")
                    or query.get("session_key")
                    or ""
                )
                session_id = str(
                    (payload or {}).get("session_id")
                    or query.get("session_id")
                    or resolve_surface_key(
                        principal, "dashboard", object_key
                    )
                    or ""
                )
                self.broker.schedule(
                    {
                        "type": "narrative-mode",
                        "kind": "narrative-mode",
                        "action": "updated",
                        "session_id": session_id,
                        "revision": int(data.get("revision") or 0),
                    }
                )
            return json_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def session_narrative_style(self):
        try:
            method = str(getattr(request, "method", "GET") or "GET").upper()
            payload = await request.json(default={}) if method == "POST" else {}
            headers = dict(getattr(request, "headers", {}) or {})
            idempotency_key = str(
                headers.get("x-idempotency-key")
                or headers.get("X-Idempotency-Key")
                or ""
            )
            result = await route_narrative_style_view(
                self._surface_principal(),
                self.database,
                query=self._route_query("session_key", "session_id"),
                payload=payload if isinstance(payload, Mapping) else {},
                method=method,
                idempotency_key=idempotency_key,
            )
            if method == "POST" and int(result.get("status", 500)) < 300:
                data = result.get("body", {}).get("data", {})
                self.broker.schedule(
                    {
                        "type": "narrative-style",
                        "kind": "narrative-style",
                        "action": "updated",
                        "revision": int(data.get("revision") or 0),
                    }
                )
            return json_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def session_gameplay_runtime(self):
        try:
            method = str(getattr(request, "method", "GET") or "GET").upper()
            payload = await request.json(default={}) if method == "POST" else {}
            headers = dict(getattr(request, "headers", {}) or {})
            result = await route_gameplay_runtime_view(
                self._surface_principal(),
                self.database,
                query=self._route_query("session_key", "session_id", "module_id"),
                payload=payload if isinstance(payload, Mapping) else {},
                method=method,
                idempotency_key=str(
                    headers.get("x-idempotency-key")
                    or headers.get("X-Idempotency-Key")
                    or ""
                ),
            )
            return json_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
    def _visual_query(self) -> dict[str, Any]:
        return QueryAdapter(
            request.query,
            allowed_fields=(
                "session_key",
                "session_id",
                "expected_revision",
                "cursor",
                "delivery_cursor",
                "page_size",
            ),
        ).normalize().to_mapping()
    def _surface_query(self) -> dict[str, Any]:
        return QueryAdapter(
            request.query,
            allowed_fields=(
                "session_key",
                "session_id",
                "world_key",
                "world_ref",
                "object_key",
                "q",
                "status",
                "scope",
                "world",
                "author",
                "capability",
                "importance",
                "tag",
                "governance",
                "object",
                "actor",
                "action",
                "time",
                "type",
                "layer",
                "consumer",
                "round",
                "cursor",
                "page_size",
                "mode",
                "group",
                "expected_revision",
            ),
        ).normalize().to_mapping()
    def _surface_principal(self) -> dict[str, Any]:
        source = str(getattr(request, "auth_source", "") or "").strip()
        identity = {
            name: str(getattr(request, name, "") or "").strip()
            for name in (
                "participant_ref",
                "member_role",
                "binding_ref",
                "platform_user_id",
                "platform",
                "provider",
            )
        }
        has_bound_identity = bool(
            identity["participant_ref"]
            or identity["binding_ref"]
            or identity["platform_user_id"]
        )

        # AstrBot 4.27.4 authenticates plugin extension routes and injects a
        # username, but its PluginRequest has no auth_source/platform fields.
        # That exact native shape is a console principal.  Explicit platform
        # identities still take the member path and never borrow console power.
        if source in {"remote_panel", "astrbot_console"} or (
            not source and not has_bound_identity
        ):
            return self._console_principal()

        platform_sources = {"platform_binding", "miniprogram_binding"}
        source_has_required_identity = (
            source == "platform_binding"
            and bool(identity["platform_user_id"] or identity["participant_ref"])
        ) or (
            source == "miniprogram_binding" and bool(identity["binding_ref"])
        ) or (not source and has_bound_identity)
        if source not in platform_sources | {""} or not source_has_required_identity:
            return {
                "username": self._username(),
                "auth_source": "unmapped",
                "is_admin": False,
                "is_author": False,
                "is_dm": False,
                "role_source": "unmapped",
                "capabilities": {
                    "admin": False,
                    "author": False,
                    "world_install": False,
                    "economy": False,
                    "dm": False,
                    "member": False,
                },
            }

        principal = dict(self._web_principal())
        for name, value in identity.items():
            if value:
                principal[name] = value
        if source:
            principal["auth_source"] = source
        member_role = str(principal.get("member_role") or "").lower()
        if member_role in {"dm", "host", "moderator"}:
            principal["is_dm"] = True
            capabilities = dict(principal.get("capabilities") or {})
            capabilities.update({"dm": True, "member": True})
            principal["capabilities"] = capabilities
        elif member_role in {"player", "member"}:
            capabilities = dict(principal.get("capabilities") or {})
            capabilities["member"] = True
            principal["capabilities"] = capabilities
        return principal
    async def _surface_services(self) -> dict[str, Any]:
        full = TavernConfig.from_mapping(self.plugin_config).to_mapping()
        security = dict(full.get("security") or {})
        model = dict(full.get("model") or {})
        runtime = dict(full.get("runtime") or {})
        advanced = dict(full.get("advanced") or {})
        panel = dict(full.get("remote_panel") or {})
        token_quota = dict(full.get("token_quota") or {})
        # This service feeds the admin-only settings surface.  It must include
        # every editable field so a save can preserve unchanged values and the
        # subsequent PageModel read can prove what the host persisted.  Player
        # surfaces never receive this object.
        safe_settings = {
            "security": {
                "admin_ids": list(security.get("admin_ids") or ()),
                "allowed_group_ids": list(
                    security.get("allowed_group_ids") or ()
                ),
                "require_group_whitelist": bool(
                    security.get("require_group_whitelist")
                ),
                "unauthorized_command_behavior": str(
                    security.get("unauthorized_command_behavior") or "ignore"
                ),
                "public_status": bool(security.get("public_status")),
                "admin_count": len(security.get("admin_ids") or ()),
                "allowed_group_count": len(
                    security.get("allowed_group_ids") or ()
                ),
            },
            "model": {
                name: model.get(name)
                for name in (
                    "provider_id",
                    "fallback_provider_1_id",
                    "fallback_provider_2_id",
                    "fallback_provider_3_id",
                    "fallback_provider_4_id",
                    "image_caption_provider_id",
                    "image_caption_prompt",
                    "max_images_per_turn",
                    "temperature",
                    "max_tokens",
                    "request_timeout_seconds",
                    "json_repair_attempts",
                    "generation_budget_total_seconds",
                    "generation_budget_max_calls",
                    "generation_budget_per_call_seconds",
                    "generation_budget_max_fallbacks",
                )
            },
            "runtime": {
                name: runtime.get(name)
                for name in (
                    "default_world_slug",
                    "trigger_prefix",
                    "qqbot_markdown_enabled",
                    "user_cooldown_seconds",
                    "max_input_chars",
                    "max_output_chars",
                    "recent_turns",
                    "memory_limit",
                    "two_phase_checks",
                    "auto_snapshot_interval",
                    "story_generation_reminder_enabled",
                    "story_generation_reminder_interval_seconds",
                )
            },
            "advanced": {
                name: advanced.get(name)
                for name in (
                    "audit_retention_days",
                    "store_model_payloads",
                )
            },
            "remote_panel": {
                name: panel.get(name)
                for name in (
                    "enabled",
                    "host",
                    "port",
                    "allow_insecure_http",
                    "secure_cookie",
                )
            },
            "token_quota": {
                name: token_quota.get(name)
                for name in ("enabled", "window_seconds", "token_limit")
            },
        }
        config_state: dict[str, Any] = {}
        recorder = getattr(self.database, "record_configuration_revision", None)
        if callable(recorder):
            try:
                recorded = await recorder(
                    safe_config_projection(full),
                    self._actor(),
                )
                if isinstance(recorded, Mapping):
                    config_state = {
                        "revision": recorded.get("revision"),
                        "updated_at": recorded.get("saved_at") or "",
                    }
            except Exception:
                # A read-only surface remains usable when revision persistence
                # is unavailable; it simply cannot offer optimistic editing.
                config_state = {}
        return {
            "settings": {
                "settings": safe_settings,
                "config_state": config_state,
            },
            "modules": self.modules,
            "world_twp": getattr(self, "world_twp", None),
            "default_world_slug": self._config().default_world_slug,
            "about": {
                "version": PLUGIN_VERSION,
                "repository_url": (
                    "https://github.com/horizoe10/astrbot_plugin_tavern"
                ),
            },
        }

    def _surface_provider_options(self) -> list[dict[str, str]]:
        """Return provider choices without making a PageModel depend on them.

        AstrBot providers are third-party extension objects.  A provider may be
        half-initialised or may raise while exposing its metadata; that must not
        take every Tavern workspace down.  Only the settings workspace asks for
        these choices, and one broken provider is skipped independently.
        """

        runtime_context = getattr(self, "context", None)
        provider_getter = getattr(runtime_context, "get_all_providers", None)
        try:
            providers = list(
                provider_getter() if callable(provider_getter) else []
            )
        except Exception:
            return []

        options: list[dict[str, str]] = []
        seen_provider_ids: set[str] = set()
        for provider in providers:
            try:
                meta_getter = getattr(provider, "meta", None)
                meta = meta_getter() if callable(meta_getter) else None
                meta_value = meta if isinstance(meta, Mapping) else {}
                provider_id = str(
                    meta_value.get("id")
                    or getattr(meta, "id", "")
                    or getattr(provider, "id", "")
                    or ""
                ).strip()
                if not provider_id or provider_id in seen_provider_ids:
                    continue
                name = str(
                    meta_value.get("name")
                    or meta_value.get("provider_name")
                    or getattr(meta, "name", "")
                    or provider_id
                ).strip()
                model_name = str(
                    meta_value.get("model")
                    or meta_value.get("model_name")
                    or getattr(meta, "model", "")
                    or getattr(meta, "model_name", "")
                    or ""
                ).strip()
            except Exception:
                continue
            seen_provider_ids.add(provider_id)
            label = f"{name} · {model_name}" if model_name else name
            options.append({"value": provider_id, "label": label})
        return options

    async def _surface_response(self, workspace: str):
        try:
            services = await self._surface_services()
            if workspace == "settings":
                services["provider_options"] = self._surface_provider_options()
            result = await route_surface_view(
                workspace,
                self._surface_principal(),
                self.database,
                query=self._surface_query(),
                services=services,
            )
            return plugin_page_surface_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            result = surface_error_response(workspace, exc)
            return plugin_page_surface_response(
                result.get("body", {}),
                status_code=int(result.get("status", 500)),
            )
    async def dashboard_surface_dashboard(self):
        return await self._surface_response("dashboard")
    async def dashboard_surface_tendencies(self):
        return await self._surface_response("tendencies")
    async def dashboard_surface_sessions(self):
        return await self._surface_response("sessions")
    async def dashboard_surface_characters(self):
        return await self._surface_response("characters")
    async def dashboard_surface_memories(self):
        return await self._surface_response("memories")
    async def dashboard_surface_worlds(self):
        return await self._surface_response("worlds")
    async def dashboard_surface_designer(self):
        return await self._surface_response("designer")
    async def dashboard_surface_author_jobs(self):
        return await self._surface_response("author_jobs")
    async def dashboard_surface_todo(self):
        return await self._surface_response("todo")
    async def dashboard_surface_audit(self):
        return await self._surface_response("audit")
    async def dashboard_surface_health(self):
        return await self._surface_response("health")
    async def dashboard_surface_settings(self):
        return await self._surface_response("settings")
    async def dashboard_surface_modules(self):
        return await self._surface_response("modules")
    async def dashboard_surface_about(self):
        return await self._surface_response("about")
    async def _visual_response(
        self,
        route: Any,
        *,
        with_delivery: bool = False,
    ):
        try:
            kwargs: dict[str, Any] = {"query": self._visual_query()}
            if with_delivery:
                kwargs["delivery_service"] = self.delivery_service
            result = await route(
                self._surface_principal(),
                self.database,
                **kwargs,
            )
            return plugin_page_surface_response(
                result.get("body", {}),
                status_code=int(result.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_session_summary(self):
        return await self._visual_response(route_visual_session_summary)
    async def dashboard_session_party(self):
        return await self._visual_response(route_visual_session_party)
    async def dashboard_session_world_visuals(self):
        return await self._visual_response(route_visual_session_world)
    async def dashboard_session_history(self):
        return await self._visual_response(
            route_visual_session_history,
            with_delivery=True,
        )
    async def dashboard_session_generation(self):
        return await self._visual_response(route_visual_session_generation)
    async def dashboard_session(self):
        """单个副本的实时聚合（状态机 / 行动者 / 计时器 / 选项 / 投票）。"""
        try:
            user = self._username()
            principal = self._console_principal()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            can_view_private = True
            viewer_role = "admin"
            payload = await build_session_dashboard(
                self.database,
                session_id,
                viewer_role=viewer_role,
                include_technical_refs=principal["is_admin"],
            )
            session = payload.get("session") or {}
            archive = payload.get("archive") or {}
            readonly = bool(archive.get("readonly")) or bool(
                session.get("readonly")
            )
            payload["preflight"] = await self.database.opening_preflight(
                session_id
            )
            capabilities = await self._session_capabilities(
                session_id,
                user,
                principal,
                can_view_private=can_view_private,
            )
            payload["permissions"] = {
                "can_admin": True,
                "can_review_cards": capabilities["review_character_cards"],
                "can_force_ready": (
                    not readonly
                    and str(session.get("state") or "") == "preparing"
                ),
                "can_view_private_card_fields": can_view_private,
                "can_manage_narrative": can_view_private and not readonly,
                "role_source": principal["role_source"],
            }
            payload["capabilities"] = capabilities
            payload["ai_companions"] = await self.database.list_ai_companions(
                session_id
            )
            payload["runtime_modules"] = [
                item for item in self.modules.catalog()
                if item.get("runtime_visible")
            ]
            # WP-11：副本事件流最新序号，作为 WebUI 增量锚点。
            payload["latest_seq"] = await self.database.latest_session_event_seq(
                session_id
            )
            return json_response(payload)
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_timeline(self):
        """事件时间线（回放视图）。"""
        try:
            self._console_principal()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            limit = int(request.query.get("limit", "30") or "30")
            payload = await build_session_timeline(
                self.database, session_id, limit=limit
            )
            return json_response(payload)
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_timers_fast(self):
        """轻量倒计时列表（供嵌入式小窗口局部刷新；支持 ?order=asc|desc）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            order = str(request.query.get("order", "desc") or "desc")
            timers = await build_session_timers(
                self.database, session_id, order=order
            )
            return json_response({"timers": timers})
        except Exception as exc:
            return self._handle_error(exc)
    async def dashboard_seed_quota(self):
        """用配置默认值给尚未配置配额策略的副本播种（F3 的控制台入口）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            config = TavernConfig.from_mapping(self.plugin_config)
            summary = await self.database.ensure_default_token_quota(
                session_id,
                window_seconds=config.token_quota_window_seconds,
                token_limit=config.token_quota_token_limit,
                enabled=config.token_quota_enabled,
                actor_id=self._actor(),
            )
            return json_response(
                {
                    "seeded": config.token_quota_enabled,
                    "quota": summary.get("quotas", []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    def _handle_error(self, exc: Exception):
        """统一把所有 Web 异常映射为不含诊断标识的公开错误信封。

        不再让 PolicyRejection / 数据库异常走宿主裸 ``error_response``，
        否则前端只能拿到 Axios 通用文案，无法展示 code/message/recovery 与
        恢复建议。关联编号与异常原文只写内部日志；普通用户与管理员通过
        通用 Console 路由得到相同的安全投影，诊断必须走独立授权接口。
        """
        status = 500
        code = "internal.error"
        message = "服务器内部错误。"
        recovery = "请稍后重试；若持续失败，请联系管理员并说明发生时间与操作。"
        report_unknown = False
        if isinstance(exc, sqlite3.IntegrityError):
            status, code = 409, "data.conflict"
            message = "数据冲突：标识、群会话或名称可能已存在"
            recovery = "请刷新页面后重试；若仍失败，请联系管理员。"
        elif isinstance(exc, WebApiError):
            status = int(exc.status_code)
            code = str(exc.code)
            message = str(exc.message)
            recovery = str(exc.recovery)
        elif isinstance(exc, PermissionError):
            status, code = 401, "auth.login_required"
            message = "登录状态无效或已过期。"
            recovery = "请重新登录 AstrBot 管理后台后重试。"
        elif isinstance(exc, PolicyRejection):
            status, code = 403, "auth.forbidden"
            message = "权限不足，无法执行此操作。"
            recovery = "请联系插件管理员授予权限，或切换到主持人账号后重试。"
        elif isinstance(exc, DatabaseNotFoundError):
            status, code = 404, "resource.not_found"
            message = "请求的内容不存在。"
            recovery = "请刷新列表后重新选择。"
        elif isinstance(exc, DatabaseConflictError):
            status, code = 409, "data.conflict"
            message = "数据冲突，操作无法完成。"
            recovery = "请刷新页面后重试；若仍失败，请联系管理员。"
        elif isinstance(exc, InvalidTransitionError):
            status, code = 400, "request.invalid"
            message = "当前状态不允许执行此操作。"
            recovery = "请检查当前副本状态后重试。"
        elif isinstance(exc, (ValueError, TypeError)):
            status, code = 400, "request.invalid"
            message = "请求参数不正确。"
            recovery = "请检查输入后重试。"
        else:
            report_unknown = True
        correlation_id = uuid.uuid4().hex
        if report_unknown:
            # 未知异常统一经 report_failure 上报（含请求上下文），
            # 避免被静默吞掉；同时保留 500 响应语义。
            report_failure(
                self.logger,
                stage="webui",
                operation=str(getattr(request, "path", "unknown")),
                exc=exc,
                context={"correlation_id": correlation_id},
            )
        else:
            self.logger.warning(
                "321开团 WebUI 请求失败：correlation_id=%s status=%s code=%s",
                correlation_id,
                status,
                code,
            )
        retryable = status in {409, 500, 502, 503, 504}
        return self._route_json_response(
            {
                "status": status,
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "recovery": recovery,
                    "retryable": retryable,
                },
            }
        )
    async def _payload(self) -> dict[str, Any]:
        value = await request.json(default={})
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value
    @staticmethod
    def _route_json_response(envelope: Mapping[str, Any]):
        """Convert a pure-route envelope into AstrBot's HTTP response."""

        status = int(envelope.get("status") or 200)
        body = envelope.get("body")
        if isinstance(body, Mapping):
            payload = dict(body)
        else:
            error = envelope.get("error")
            payload = {"error": dict(error)} if isinstance(error, Mapping) else {}
            ok_value = envelope.get("ok")
            payload["ok"] = bool(ok_value) if ok_value is not None else False
            correlation_id = envelope.get("correlation_id") or (
                error.get("correlation_id")
                if isinstance(error, Mapping)
                else ""
            )
            if correlation_id:
                payload["correlation_id"] = str(correlation_id)
        try:
            return json_response(payload, status_code=status)
        except TypeError:
            response = json_response(payload)
            try:
                response.status_code = status
            except Exception:
                pass
            return response
    @staticmethod
    def _route_query(
        *allowed_fields: str,
        multi_fields: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        query = getattr(request, "query", {})
        fields = allowed_fields or (
            "session_id", "id", "participant_id", "participant_ref",
            "character", "world_ref",
            "world_id", "job_id", "operation_id", "cursor", "limit",
            "after_seq", "include_archived", "status", "component", "kind",
            "action", "page", "query", "include",
        )
        return QueryAdapter(
            query,
            allowed_fields=fields,
            multi_fields=multi_fields,
        ).normalize().to_mapping()
    async def _route_audit(
        self,
        session_id: str,
        actor_id: str,
        action: str,
        target: str,
        detail: Mapping[str, Any],
    ) -> None:
        await self.database.write_audit(
            session_id,
            actor_id,
            action,
            target,
            dict(detail),
        )
    def _route_publish(self, event: Mapping[str, Any]) -> None:
        self.broker.schedule(dict(event))
    async def session_narrative_control_view(self):
        principal = self._web_principal()
        query = self._route_query()
        session_id = str(query.get("session_id") or "").strip()
        try:
            body = await route_narrative_control_view(
                self.database,
                session_id,
                principal,
            )
            return self._route_json_response({"status": 200, "body": body})
        except Exception as exc:
            return self._route_json_response(error_from_exception(exc))
    async def session_characters_view(self):
        query = self._route_query()
        envelope = await route_character_list_view(
            self._web_principal(),
            self.database,
            str(query.get("session_id") or ""),
            query=query,
        )
        return self._route_json_response(envelope)
