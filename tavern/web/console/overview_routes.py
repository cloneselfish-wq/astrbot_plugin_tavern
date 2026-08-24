from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


class ConsoleOverviewRouteMethods:
    async def overview(self):
        started_at = time.perf_counter()
        try:
            principal = self._web_principal()
            result = await self.database.overview()
            result["plugin_version"] = PLUGIN_VERSION
            protocol = self.world_twp.protocol_info()
            result["version_info"] = {
                "plugin_version": PLUGIN_VERSION,
                "protocol_name": "Tavern World Package（TWP）",
                "protocol_version": WORLD_PROTOCOL_VERSION.removeprefix("TWP "),
                "package_format": int(protocol.get("package_format", 2)),
                "database_schema": DATABASE_SCHEMA_VERSION,
                "character_template_schema": CHARACTER_CARD_SCHEMA_VERSION,
                "character_template_version": (
                    DEFAULT_CHARACTER_CARD_CONTENT_VERSION
                ),
                "support_policy": (
                    "支持当前 TWP 世界包、冻结副本与备份优先迁移；"
                    "旧副本默认保持原 revision，不会静默套用新世界定义"
                ),
            }
            result["web_permissions"] = {
                "is_admin": principal["is_admin"],
                "can_author": principal["capabilities"]["author"],
                "can_install_worlds": principal["capabilities"][
                    "world_install"
                ],
                "role_source": principal["role_source"],
            }
            result["sessions"] = await enrich_session_display_labels(
                self.database,
                (await self.database.list_sessions())[:8],
            )
            config = TavernConfig.from_mapping(self.plugin_config)
            result["security"] = {
                "admin_count": len(config.admin_ids),
                "allowed_group_count": len(config.allowed_group_ids),
                "whitelist_required": config.require_group_whitelist,
                "ready": bool(config.admin_ids),
            }
            # 0.12.0-A3：Token 用量（WebUI 总览顶部指标卡 / 关键数据）。
            window = max(60, config.token_quota_window_seconds)
            result["token_usage"] = {
                "enabled": config.token_quota_enabled,
                "used": await self.database.global_token_usage(window),
                "window_seconds": window,
                "limit": config.token_quota_token_limit,
            }
            # 0.12.0-A3：开馆前检查（WebUI 总览「开馆前检查」卡片）。
            health = await self.database.list_provider_health()
            result["provider_health"] = health
            provider_probe = self.provider_health_service.summarize(
                self.provider_health_service.configured_chain(),
                health,
            )
            result["provider_probe"] = provider_probe
            result["readiness"] = {
                "admin_ready": bool(config.admin_ids),
                "whitelisted_groups": len(config.allowed_group_ids),
                "whitelist_required": config.require_group_whitelist,
                "worlds_ready": int(result["counts"]["worlds"]),
                "card_code_ttl_seconds": int(
                    config.time_rules.get("card_code_ttl_seconds") or 1800
                ),
                "providers_ready": provider_probe["status"]
                in {"healthy", "degraded"},
                "provider_status": provider_probe["status"],
            }
            result["service_latency_ms"] = max(
                1, round((time.perf_counter() - started_at) * 1000)
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def worlds(self):
        try:
            principal = self._console_principal()
            archived_value = request.query.get("include_archived", "")
            include_archived = str(archived_value).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            config = TavernConfig.from_mapping(self.plugin_config)
            await self._ensure_builtin_worlds()
            items = project_world_catalog(
                await self.database.list_worlds(include_archived),
                default_slug=config.default_world_slug,
                viewer_role="admin" if principal["is_admin"] else "player",
                include_technical_refs=principal["is_admin"],
            )
            merge_builtin_world_statuses(
                items,
                await self._builtin_statuses(),
                default_slug=config.default_world_slug,
                viewer_role="admin" if principal["is_admin"] else "player",
                include_technical_refs=principal["is_admin"],
                can_retry=principal["is_admin"],
            )
            return json_response(
                {
                    "items": items,
                    "permissions": {
                        "can_install_worlds": True,
                        "can_manage_worlds": True,
                        "role_source": principal["role_source"],
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def builtin_world_status_api(self):
        """内置世界安装状态（GET，只读）。"""
        try:
            principal = self._console_principal()
            await self._ensure_builtin_worlds()
            return json_response(
                {
                    "items": await self._builtin_statuses(),
                    "permissions": {
                        "can_retry": principal["is_admin"],
                        "role_source": principal["role_source"],
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def builtin_world_retry_api(self):
        """重试失败的内置世界安装（POST，AstrBot 管理后台）。"""
        try:
            principal = self._require_console_admin()
            payload = await self._payload()
            key = str(payload.get("key") or "").strip()
            if not key:
                raise ValueError("缺少 key")
            retry = self.builtin_world_retry
            if retry is None:
                raise PolicyRejection("当前宿主未提供内置世界重试能力")
            result = retry(key)
            if inspect.isawaitable(result):
                result = await result
            try:
                await self.database.write_audit(
                    "",
                    f"web:{principal['username']}",
                    "builtin_world.retry",
                    key,
                    {},
                )
            except Exception:
                pass
            item = (
                dict(result)
                if isinstance(result, Mapping)
                else {"key": key, "state": "requested"}
            )
            await self.broker.publish(
                {
                    "type": "world",
                    "action": "builtin_retry",
                    "key": key,
                    "state": str(item.get("state") or ""),
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_order(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            item = await self.database.set_world_sort_order(
                str(payload.get("id") or ""),
                int(payload.get("sort_order") or 1),
                self._actor(),
            )
            await self.broker.publish({"type": "world", "action": "reorder"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_archive(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            world_id = str(payload.get("id", ""))
            world = await self.database.get_world(world_id)
            config = TavernConfig.from_mapping(self.plugin_config)
            if world["slug"] == config.default_world_slug:
                raise ValueError(
                    "该世界是当前默认世界，请先在设置中更换默认世界"
                )
            item = await self.database.archive_world(
                world_id,
                self._actor(),
            )
            self._purge_rule_runtime(world["slug"])
            await self.broker.publish({"type": "world", "action": "archive"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_restore(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            world_id = str(payload.get("id", ""))
            current = await self.database.get_world(world_id)
            item = await self.database.restore_world(
                world_id,
                self._actor(),
            )
            self._purge_rule_runtime(current["slug"])
            await self.broker.publish({"type": "world", "action": "restore"})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def characters(self):
        try:
            self._username()
            world_id = str(request.query.get("world_id", "") or "")
            return json_response(
                {"items": await self.database.list_characters(world_id)}
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def character_save(self):
        try:
            self._require_admin()
            item = await self.database.save_character(
                await self._payload(),
                self._actor(),
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def character_delete(self):
        try:
            self._require_admin()
            payload = await self._payload()
            await self.database.delete_character(
                str(payload.get("id", "")),
                self._actor(),
            )
            return json_response({"deleted": True})
        except Exception as exc:
            return self._handle_error(exc)
