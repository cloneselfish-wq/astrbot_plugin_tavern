from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    is_standalone_upload,
    json_response,
    request,
    stream_response,
)
from ...presentation.turns import turn_command_group_notice
from ...config import safe_config_projection
from ..intents.dispatcher import project_recovery_preview


class ConsoleSystemRouteMethods:
    async def snapshot_delete(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            expected_revision = payload.get("expected_revision")
            idempotency_key = self._request_idempotency_key(payload)
            if expected_revision in {None, ""}:
                raise ValueError("缺少 expected_revision")
            if not idempotency_key:
                raise ValueError("缺少 idempotency_key")
            result = await self.database.delete_snapshot(
                session_id,
                str(payload.get("id", "")),
                self._actor(),
                expected_revision=int(expected_revision),
                idempotency_key=idempotency_key,
            )
            return json_response({"deleted": True, "operation": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def archive_delete(self):
        """删除独立存档归档（危险写操作，必须登录并写审计）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            item = await asyncio.to_thread(
                self.database.storage.trash_archive,
                session_id,
                kind=str(payload.get("kind") or "save"),
                filename=str(payload.get("filename") or ""),
            )
            try:
                await self.database.write_audit(
                    session_id,
                    self._actor(),
                    "storage.archive_delete",
                    "",
                    {
                        "kind": str(payload.get("kind") or "save"),
                        "filename": str(payload.get("filename") or ""),
                    },
                )
            except Exception:
                self.logger.exception("321开团归档删除审计写入失败")
            await self.broker.publish(
                {
                    "type": "storage",
                    "action": "archive_delete",
                    "session_id": item["session_id"],
                }
            )
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def audit(self):
        try:
            self._username()
            return json_response(
                {
                    "items": await self.database.list_audit(
                        str(request.query.get("session_id", "") or ""),
                        request.query.get("limit", 100, type=int),
                        request.query.get("offset", 0, type=int),
                    )
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def providers(self):
        try:
            self._username()
            getter = getattr(self.context, "get_all_providers", None)
            providers = getter() if callable(getter) else []
            items: list[dict[str, str]] = []
            seen: set[str] = set()
            for provider in providers or []:
                meta_getter = getattr(provider, "meta", None)
                meta = meta_getter() if callable(meta_getter) else None
                meta_id = (
                    meta.get("id", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "id", "")
                )
                provider_id = str(
                    meta_id
                    or getattr(provider, "id", "")
                    or ""
                ).strip()
                if not provider_id or provider_id in seen:
                    continue
                seen.add(provider_id)
                meta_name = (
                    meta.get("name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "name", "")
                )
                provider_name = (
                    meta.get("provider_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "provider_name", "")
                )
                name = str(
                    meta_name
                    or provider_name
                    or provider_id
                ).strip()
                meta_model = (
                    meta.get("model", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model", "")
                )
                meta_model_name = (
                    meta.get("model_name", "")
                    if isinstance(meta, dict)
                    else getattr(meta, "model_name", "")
                )
                model = str(
                    meta_model
                    or meta_model_name
                    or getattr(provider, "model_name", "")
                    or ""
                ).strip()
                items.append(
                    {
                        "id": provider_id,
                        "name": name,
                        "model": model,
                    }
                )
            return json_response({"items": items})
        except Exception as exc:
            return self._handle_error(exc)

    async def provider_health_check(self):
        try:
            principal = self._require_console_admin()
            payload = await self._payload()
            requested = payload.get("provider_ids", [])
            if not isinstance(requested, list):
                raise ValueError("provider_ids 必须是数组")
            report = await self.provider_health_service.probe(
                requested,
                idempotency_key=str(payload.get("idempotency_key") or ""),
                actor=str(principal.get("username") or "console"),
            )
            return json_response({"probe": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def settings(self):
        try:
            self._require_console_admin()
            config = TavernConfig.from_mapping(self.plugin_config)
            settings = safe_config_projection(config.to_mapping())
            revision = await self.database.record_configuration_revision(
                settings,
                self._actor(),
            )
            return json_response(
                {
                    "settings": settings,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                    "provider_probe": self.provider_health_service.summarize(
                        self.provider_health_service.configured_chain(),
                        await self.database.list_provider_health(),
                    ),
                    "readiness": {
                        "has_admin": bool(config.admin_ids),
                        "has_allowed_group": bool(config.allowed_group_ids)
                        or not config.require_group_whitelist,
                    },
                    "permissions": {
                        "can_save": True,
                        "requires_plugin_login": False,
                        "bootstrap_required": not bool(config.admin_ids),
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def settings_save(self):
        try:
            self._require_console_admin()
            was_bootstrap = not bool(self._config().admin_ids)
            payload = await self._payload()
            # 1.0.0-A3：WebUI 设置表单只提交它渲染的 4 个分区；
            # 未渲染的 token_quota / auto_backup / webhook
            # 由宿主 schema 管理，必须保留现值而不是被重置为默认值。
            merged = merge_config_payload(self.plugin_config, payload)
            normalized = TavernConfig.from_mapping(merged).to_mapping()
            if not normalized["security"]["admin_ids"]:
                raise ValueError(
                    "管理员 ID 至少填写一个消息平台真实用户 ID；设置未保存，请填写后重试"
                )
            default_world = await self.database.get_world(
                normalized["runtime"]["default_world_slug"]
            )
            if default_world["archived"]:
                raise ValueError("默认世界不能使用已归档世界")
            async with self.config_lock:
                missing = object()
                previous = {
                    section: self.plugin_config.get(section, missing)
                    for section in normalized
                }
                try:
                    for section, value in normalized.items():
                        self.plugin_config[section] = value
                    save_async = getattr(
                        self.plugin_config,
                        "save_config_async",
                        None,
                    )
                    if callable(save_async):
                        await save_async()
                    else:
                        save = getattr(
                            self.plugin_config,
                            "save_config",
                            None,
                        )
                        if callable(save):
                            save()
                except Exception:
                    for section, value in previous.items():
                        if value is missing:
                            self.plugin_config.pop(section, None)
                        else:
                            self.plugin_config[section] = value
                    # A host config object may include its serialized payload in
                    # an exception.  Never forward that exception to the shared
                    # failure logger because it could contain webhook.secret.
                    raise RuntimeError(
                        "配置保存失败，内存中的原配置已恢复"
                    ) from None
            persisted = TavernConfig.from_mapping(
                self.plugin_config
            ).to_mapping()
            if persisted != normalized:
                raise RuntimeError("配置保存后回读校验不一致")
            safe_persisted = safe_config_projection(persisted)
            revision = await self.database.record_configuration_revision(
                safe_persisted,
                self._actor(),
            )
            await self.database.write_audit(
                "",
                self._actor(),
                "settings.bootstrap" if was_bootstrap else "settings.update",
                "plugin",
                {
                    "admin_count": len(
                        normalized["security"]["admin_ids"]
                    ),
                    "group_count": len(
                        normalized["security"]["allowed_group_ids"]
                    ),
                },
            )
            await self.broker.publish(
                {"type": "settings", "action": "update"}
            )
            return json_response(
                {
                    "settings": safe_persisted,
                    "config_state": revision,
                    "provider_health": (
                        await self.database.list_provider_health()
                    ),
                    "bootstrap_completed": was_bootstrap,
                    "permissions": {
                        "can_save": True,
                        "requires_plugin_login": False,
                        "bootstrap_required": False,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_export(self):
        try:
            self._username()
            path = await build_backup_archive(
                data_dir=self.data_dir,
                database=self.database,
                export_dir=self.data_dir / "exports",
            )
            return file_response(
                path,
                filename=path.name,
                content_type="application/zip",
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def backup_import(self, mode: str):
        temp_path: Path | None = None
        stage_dir: Path | None = None
        staged_group_files: list[tuple[Path, PurePosixPath]] = []
        try:
            self._require_admin()
            if mode not in {"merge", "replace"}:
                raise ValueError("导入模式必须为 merge 或 replace")
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile) and not is_standalone_upload(upload):
                raise ValueError("缺少备份文件")
            filename = str(upload.filename or "").lower()
            if not filename.endswith((".json", ".zip")):
                raise ValueError(
                    f"只接受完整的 Schema {DATABASE_SCHEMA_VERSION} JSON/ZIP 备份"
                )
            temp_dir = self.data_dir / "imports"
            temp_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".zip" if filename.endswith(".zip") else ".json"
            temp_path = temp_dir / f"{uuid.uuid4().hex}{suffix}"
            await upload.save(temp_path)
            limit = 512 * 1024 * 1024 if suffix == ".zip" else 25 * 1024 * 1024
            if temp_path.stat().st_size > limit:
                raise ValueError(
                    "ZIP 备份不能超过 512 MiB"
                    if suffix == ".zip"
                    else "JSON 备份不能超过 25 MiB"
                )
            if suffix == ".zip":
                with zipfile.ZipFile(temp_path) as archive:
                    _verify_backup_archive(archive)
                    try:
                        info = archive.getinfo("bundle.json")
                    except KeyError as exc:
                        raise ValueError(
                            "ZIP 备份缺少 bundle.json"
                        ) from exc
                    if info.file_size > 25 * 1024 * 1024:
                        raise ValueError("ZIP 内的 bundle.json 过大")
                    bundle = json.loads(
                        archive.read(info).decode("utf-8")
                    )
                    if int(bundle.get("format_version") or 0) == 2:
                        raise ValueError(
                            "完整备份请先使用恢复预览；"
                            "格式 2 不允许直接导入或合并"
                        )
                    stage_dir = temp_dir / f".stage-{uuid.uuid4().hex}"
                    stage_dir.mkdir(parents=True)
                    staged_group_files = _stage_managed_files(
                        archive,
                        stage_dir,
                    )
            else:
                bundle = json.loads(temp_path.read_text(encoding="utf-8"))
            if not isinstance(bundle, dict):
                raise ValueError("备份根节点必须是 JSON 对象")
            counts = await self.database.import_bundle(
                bundle,
                mode,
                self._actor(),
            )
            data_root = self.data_dir.resolve()
            for staged, relative in staged_group_files:
                destination = self.data_dir.joinpath(
                    *relative.parts
                ).resolve()
                if destination == data_root or data_root not in destination.parents:
                    raise ValueError("ZIP 备份含非法数据目录路径")
                is_group_archive = (
                    relative.parts[0] == "groups"
                    and any(part in {"saves", "backups"} for part in relative.parts)
                )
                is_managed_asset = relative.parts[0] in {
                    "world_packages_twp", "plugin_modules.json"
                }
                # Active group databases/manifests were rebuilt from bundle;
                # only immutable group archives plus separately managed TWP
                # package/module assets are restored here.
                if not (is_group_archive or is_managed_asset):
                    continue
                if mode == "merge" and destination.exists():
                    if file_sha256(destination) == file_sha256(staged):
                        continue
                    if relative.as_posix() in {
                        "world_packages_twp/index.json",
                    }:
                        _merge_package_index(destination, staged)
                        continue
                    if is_managed_asset:
                        # Insert-only merge keeps active compiled snapshots and
                        # module state. Content-addressed archives normally do
                        # not collide.
                        continue
                    destination = _collision_path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, destination)
            await self.broker.publish(
                {"type": "backup", "action": "import", "mode": mode}
            )
            return json_response({"imported": counts})
        except json.JSONDecodeError:
            return error_response("备份 JSON 无法解析", status_code=400)
        except UnicodeDecodeError:
            return error_response("备份文本编码必须为 UTF-8", status_code=400)
        except zipfile.BadZipFile:
            return error_response("ZIP 备份已损坏或格式无效", status_code=400)
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            if stage_dir and stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)

    async def backup_restore_preview(self, mode: str):
        temp_path: Path | None = None
        try:
            self._require_admin()
            if mode != "replace":
                raise ValueError(
                    "完整恢复只接受“全部覆盖”；旧格式合并请使用原导入入口"
                )
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile) and not is_standalone_upload(upload):
                raise ValueError("缺少备份文件")
            filename = str(upload.filename or "").lower()
            if not filename.endswith(".zip"):
                raise ValueError("完整恢复只接受 ZIP 备份")
            self.data_dir.joinpath("imports").mkdir(parents=True, exist_ok=True)
            temp_path = (
                self.data_dir
                / "imports"
                / f".restore-upload-{uuid.uuid4().hex}.zip"
            )
            await upload.save(temp_path)
            if temp_path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("ZIP 备份不能超过 512 MiB")
            preview = await asyncio.to_thread(
                self.backup_recovery_service.preview,
                temp_path,
                mode="replace",
            )
            return json_response({"preview": preview})
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path is not None:
                unlink_with_retry(temp_path, suppress_errors=True)

    async def dashboard_recovery_preview(self):
        """Verify one uploaded backup and return only an opaque console continuation."""

        temp_path: Path | None = None
        try:
            self._require_admin()
            files = await request.files()
            upload = files.get("file")
            if not isinstance(upload, PluginUploadFile) and not is_standalone_upload(upload):
                raise ValueError("缺少备份文件")
            filename = str(upload.filename or "").lower()
            if not filename.endswith(".zip"):
                raise ValueError("完整恢复只接受 ZIP 备份")
            self.data_dir.joinpath("imports").mkdir(parents=True, exist_ok=True)
            temp_path = (
                self.data_dir
                / "imports"
                / f".tavern-restore-upload-{uuid.uuid4().hex}.zip"
            )
            await upload.save(temp_path)
            if temp_path.stat().st_size > 512 * 1024 * 1024:
                raise ValueError("ZIP 备份不能超过 512 MiB")
            preview = await asyncio.to_thread(
                self.backup_recovery_service.preview,
                temp_path,
                mode="replace",
            )
            projected = project_recovery_preview(
                self._surface_principal(),
                preview,
            )
            return json_response(
                projected.get("body", {}),
                status_code=int(projected.get("status", 200)),
            )
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path is not None:
                unlink_with_retry(temp_path, suppress_errors=True)

    async def backup_restore_execute(self):
        try:
            self._require_admin()
            payload = await self._payload()
            result = await asyncio.to_thread(
                self.backup_recovery_service.execute,
                str(payload.get("preview_token") or ""),
                confirm_text=str(payload.get("confirm_text") or ""),
                operation_id=self._request_idempotency_key(payload),
                actor_id=self._actor(),
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_turn_command(self):
        # A17：后台调整回合顺序（reorder/designate/skip/supersede_choices）。
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = str(payload.get("command") or "").strip().lower()
            if not session_id or not command:
                raise ValueError("缺少 session_id/command")
            await self._require_dm_capability(session_id, user)
            db = self.database
            result: dict[str, Any] = {}
            if command == "reorder":
                order = payload.get("order") or []
                order = [str(x) for x in order if str(x or "").strip()]
                if len(order) < 2 or len(set(order)) != len(order):
                    raise ValueError("行动顺序必须是不重复的玩家 ID 列表")
                await db.supersede_active_choices(session_id, user)
                result = await db.set_turn_order(session_id, order, user)
            elif command == "designate":
                result = await db.designate_turn(
                    session_id, str(payload.get("user_id") or ""), user
                )
            elif command == "skip":
                result = await db.skip_turn(
                    session_id,
                    str(payload.get("user_id") or ""),
                    user,
                    force=True,
                )
            elif command == "supersede_choices":
                result = {
                    "count": await db.supersede_active_choices(session_id, user)
                }
            else:
                raise ValueError("不支持的回合指令")
            await self.broker.publish(
                {"type": "turn", "action": command, "session_id": session_id}
            )
            try:
                session = await db.get_session(session_id)
                turn = await db.get_turn_status(session_id)
                note = turn_command_group_notice(command, turn)
                await self._send_group_text(
                    session_id,
                    str(session.get("unified_origin") or ""),
                    note,
                    kind="turn.command",
                )
            except Exception:
                pass
            return json_response({"ok": True, "result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def panel_status(self):
        """独立 Web 面板状态（只读）。"""
        try:
            self._username()
            config = self._config()
            creds = load_credentials(self.data_dir)
            status_provider = getattr(
                self, "remote_panel_runtime_status_provider", None
            )
            runtime_status = (
                status_provider() if callable(status_provider) else {}
            )
            if not isinstance(runtime_status, Mapping):
                runtime_status = {}
            return json_response(
                {
                    "enabled": bool(config.remote_panel_enabled),
                    "host": config.remote_panel_host,
                    "port": int(config.remote_panel_port),
                    "allow_insecure_http": bool(
                        config.remote_panel_allow_insecure_http
                    ),
                    "username": str(creds.get("username") or ""),
                    "password_set": bool(creds.get("password_hash")),
                    "ip_allowlist": creds.get("ip_allowlist") or [],
                    "session_ttl_seconds": int(
                        creds.get("session_ttl_seconds") or 0
                    ),
                    "allow_write_actions": bool(
                        creds.get("allow_write_actions")
                    ),
                    "runtime_state": str(
                        runtime_status.get("state") or "unknown"
                    ),
                    "runtime_message": str(
                        runtime_status.get("message")
                        or "当前宿主没有提供独立面板运行状态。"
                    ),
                    "runtime_recovery": str(
                        runtime_status.get("recovery")
                        or "请检查插件日志和独立面板配置。"
                    ),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def panel_reset_password(self):
        """重置独立 Web 面板密码（AstrBot 登录后操作；写审计并吊销旧会话）。"""
        try:
            self._require_admin()
            payload = await self._payload()
            new_password = str(payload.get("new_password") or "")
            if len(new_password) < 8:
                raise ValueError("新密码至少 8 位")
            creds = load_credentials(self.data_dir)
            creds["password_hash"] = hash_password(new_password)
            creds["sessions_revoked_at"] = _utc_now()
            save_credentials(self.data_dir, creds)
            await self.database.write_audit(
                "",
                self._actor(),
                "panel.reset_password",
                "",
                {"username": str(creds.get("username") or "")},
            )
            return json_response(
                {
                    "ok": True,
                    "message": "面板密码已重置，旧会话已全部失效",
                    "username": str(creds.get("username") or ""),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    def _config(self) -> TavernConfig:
        try:
            return TavernConfig.from_mapping(self.plugin_config)
        except Exception:
            return TavernConfig()

    async def _session_control(self, session_id: str) -> dict[str, Any]:
        try:
            return await self.database.get_control_state(session_id)
        except Exception:
            return {"mode": "auto", "active_dm_user_id": ""}

    async def _require_dm_capability(self, session_id: str, user: str) -> None:
        from ...permissions import can_manage_dm

        control = await self._session_control(session_id)
        if await can_manage_dm(
            self.database, self._config(), session_id, control, user
        ):
            return
        # C6：登录不再自动授予 DM/管理员能力。只有插件 admin_ids、宿主
        # 明确管理员声明、活动 DM 或副本 host 记录才能放行；否则 403。
        if self._web_principal()["is_admin"]:
            return
        raise PolicyRejection("需要副本 DM 或管理员权限")

    async def _session_capabilities(
        self,
        session_id: str,
        user: str,
        principal: Mapping[str, Any],
        *,
        can_view_private: bool,
    ) -> dict[str, bool]:
        """返回跑团现场服务端权威能力。

        前端只能据此展示/禁用入口，后端仍逐请求鉴权。``review_character_cards``
        与 ``resolve_character_supplements`` 必须是真实 DM/管理员能力，
        不得用“只读状态”近似替代。
        """
        is_admin = bool(principal.get("is_admin"))
        can_dm = is_admin or bool(can_view_private)
        is_member = is_admin
        if not is_member:
            participant = await self._viewer_participant_or_none(
                session_id,
                user,
            )
            is_member = participant is not None
        return {
            "review_character_cards": can_dm,
            "read_character_supplements": can_dm or is_member,
            "resolve_character_supplements": can_dm,
        }

    async def _require_economy_capability(self, session_id: str, user: str) -> None:
        from ...permissions import can_adjust_economy

        control = await self._session_control(session_id)
        if await can_adjust_economy(
            self.database, self._config(), session_id, control, user
        ):
            return
        if self._web_principal()["is_admin"]:
            return
        raise PolicyRejection("需要 DM/管理员或 host/mod 权限")

    async def economy_summary(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            summary = await self.database.economy_summary(session_id)
            return json_response(summary)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_set_enabled(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            enabled = bool(payload.get("enabled", False))
            result = await self.database.set_economy_enabled(
                session_id, enabled, self._actor()
            )
            await self.broker.publish(
                {"type": "economy", "action": "enabled", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_adjust(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_economy_capability(session_id, user)
            result = await self.database.economy_apply(
                session_id=session_id,
                operation_id=str(
                    payload.get("operation_id") or f"web:{uuid.uuid4().hex}"
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
                source="web",
                actor_id=user,
            )
            await self.broker.publish(
                {"type": "economy", "action": "apply", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def economy_transactions(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            limit = int(request.query.get("limit", "100") or "100")
            rows = await self.database.economy_list_transactions(session_id, limit)
            return json_response({"items": rows})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_list(self):
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            items = await self.database.list_delegations(session_id)
            return json_response({"items": items})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_grant(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            owner = str(payload.get("owner_user_id") or "")
            delegate = str(payload.get("delegate_user_id") or "")
            if not session_id or not owner or not delegate:
                raise ValueError("缺少 session_id/owner_user_id/delegate_user_id")
            source = str(payload.get("source") or "player")
            if source in {"admin", "dm"}:
                await self._require_dm_capability(session_id, user)
            result = await self.database.grant_delegation(
                session_id,
                owner,
                delegate,
                self._actor(),
                permissions=payload.get("permissions"),
                expiry_kind=str(payload.get("expiry_kind") or "none"),
                expires_round=int(payload.get("expires_round") or 0),
                auto_restore=bool(payload.get("auto_restore", False)),
                source=source,
            )
            await self.broker.publish(
                {"type": "delegation", "action": "grant", "session_id": session_id}
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_revoke(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            owner = str(payload.get("owner_user_id") or "")
            if not session_id or not owner:
                raise ValueError("缺少 session_id/owner_user_id")
            force = user not in {owner, ""}
            if force:
                await self._require_dm_capability(session_id, user)
            count = await self.database.revoke_delegation(
                session_id, owner, self._actor(), force=force
            )
            await self.broker.publish(
                {"type": "delegation", "action": "revoke", "session_id": session_id}
            )
            return json_response({"count": count})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_restore(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            participant_id = str(payload.get("participant_id") or "")
            if not session_id or not participant_id:
                raise ValueError("缺少 session_id/participant_id")
            await self._require_dm_capability(session_id, user)
            count = await self.database.restore_owner_control(
                session_id, participant_id, self._actor()
            )
            await self.broker.publish(
                {"type": "delegation", "action": "restore", "session_id": session_id}
            )
            return json_response({"count": count})
        except Exception as exc:
            return self._handle_error(exc)

    async def delegations_forced_choose(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            choice_key = str(payload.get("choice_key") or "")
            if not session_id or not choice_key:
                raise ValueError("缺少 session_id/choice_key")
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
                raise ValueError("当前没有可强制选择的行动角色")
            # A17：幂等——同一 operation_id 只执行一次，防止重复点击重复消费。
            operation_id = str(payload.get("operation_id") or "").strip() or (
                f"forced:{session_id}:{uuid.uuid4().hex}"
            )
            claim = await self.database.claim_action_operation(
                session_id,
                operation_id,
                "forced_choose",
                acting_user_id,
                user,
                {"choice_key": choice_key},
            )
            if not claim["claimed"]:
                return json_response(
                    {
                        "ok": False,
                        "idempotent_replay": True,
                        "message": "该操作已执行过，未重复提交",
                    }
                )
            session = await self.database.get_session(session_id)
            from types import SimpleNamespace

            event = SimpleNamespace(
                unified_msg_origin=str(session.get("unified_origin", "")),
                message_obj=None,
            )
            reply = await self._engine().process_choice(
                event=event,
                session_id=session_id,
                sender_id=user,
                sender_name="管理员",
                choice_key=choice_key,
                operator_id=user,
                force=True,
            )
            await self.broker.publish(
                {
                    "type": "delegation",
                    "action": "forced_choose",
                    "session_id": session_id,
                    "hook": "forced_choose",
                }
            )
            # A17/A21：后台代选成功后向群聊发送通知（失败不影响已提交操作，
            # 但把发送结果如实返回给前端，避免误报“已通知群聊”）。
            selected_text = ""
            for choice_item in (choice_set.get("choices") or []):
                if isinstance(choice_item, Mapping) and str(
                    choice_item.get("key") or ""
                ).upper() == str(choice_key).upper():
                    selected_text = str(choice_item.get("text") or "")
                    break
            notice = (
                f"🎭 后台代操作\n角色：{acting_name}\n"
                f"操作者：{user}\n选择：{choice_key}. {selected_text}".rstrip()
            )
            parts = [part for part in (reply.story_text, reply.turn_text) if part]
            group_text = notice + (("\n\n" + "\n\n".join(parts)) if parts else "")
            send_result = await self._send_group_text(
                session_id,
                str(session.get("unified_origin") or ""),
                group_text,
                kind="delegation.forced_choose",
            )
            return json_response(
                {
                    "ok": True,
                    "operation_id": operation_id,
                    "story": reply.story_text,
                    "turn": reply.turn_text,
                    "actor_user_id": acting_user_id,
                    "operator_id": user,
                    "notice_sent": bool(send_result.get("ok")),
                    "notice_reason": str(send_result.get("reason") or ""),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
