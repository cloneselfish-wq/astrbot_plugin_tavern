from __future__ import annotations

from .plugin_shared import *


class StartupMethods:
    def _build_application_router(self) -> ApplicationRouter:
        """Register every BOT action once; one action has one production handler."""

        router = ApplicationRouter()
        actions = set(MANAGEMENT_ACTIONS.values()) | {
            "unknown",
            "tendency_action",
        }
        for action in sorted(actions):
            if action == "tendency":
                spec = CommandSpec(
                    action=action,
                    mode="query",
                    service="TendencyApplicationService.execute",
                    session_required=True,
                )
                handler = self._execute_tendency_application
            elif action == "tendency_action":
                spec = CommandSpec(
                    action=action,
                    mode="deterministic_write",
                    service="TendencyApplicationService.execute",
                    session_required=True,
                    idempotency_required=True,
                    expected_revision=True,
                )
                handler = self._execute_tendency_application
            else:
                mode = (
                    "deterministic_write"
                    if action in MUTATING_ACTIONS
                    else "query"
                )
                spec = CommandSpec(
                    action=action,
                    mode=mode,
                    service="LegacyDomainApplicationAdapter.execute",
                    idempotency_required=mode != "query",
                )
                handler = self._execute_legacy_application
            router.register(spec, handler)
        return router

    @staticmethod
    def _normalize_application_command(
        command: ParsedCommand,
    ) -> ParsedCommand:
        """Split one public command into query/write authority actions."""

        argument = str(command.argument or "").strip()
        first = argument.split(maxsplit=1)[0] if argument else ""
        if command.action == "tendency" and first in {"忽略", "恢复"}:
            return ParsedCommand(
                matched=command.matched,
                action="tendency_action",
                argument=argument,
                raw_action=command.raw_action,
                source=command.source,
                public_target=command.public_target,
            )
        return command

    def _application_authority(
        self,
    ) -> tuple[ApplicationRouter, ApplicationCommandOrchestrator]:
        """Lazy fallback for unit fixtures constructed through ``__new__``."""

        router = getattr(self, "application_router", None)
        orchestrator = getattr(self, "application_orchestrator", None)
        if router is None:
            router = self._build_application_router()
            self.application_router = router
        if orchestrator is None:
            orchestrator = ApplicationCommandOrchestrator(router)
            self.application_orchestrator = orchestrator
        return router, orchestrator

    async def _execute_legacy_application(
        self,
        _: RequestContext,
        invocation: _BotApplicationInvocation,
    ) -> ApplicationCommandResult:
        text = await self._handle_command_impl(
            event=invocation.event,
            command=invocation.parsed,
            config=invocation.config,
            group_id=invocation.group_id,
            platform_id=invocation.platform_id,
            sender_id=invocation.sender_id,
        )
        if text is None:
            return ApplicationCommandResult.ignored()
        return ApplicationCommandResult.reply(
            text,
            code=f"command.{invocation.action}.completed",
        )

    async def _execute_tendency_application(
        self,
        ctx: RequestContext,
        invocation: _BotApplicationInvocation,
    ) -> ApplicationCommandResult:
        service = getattr(self, "tendency_commands", None)
        if service is None:
            service = TendencyApplicationService(self.database)
            self.tendency_commands = service
        return await service.execute(ctx, invocation.parsed)

    def runtime_config(self) -> TavernConfig:
        return TavernConfig.from_mapping(self.plugin_config)

    def builtin_world_status(self) -> list[dict[str, Any]]:
        return [
            dict(self._builtin_world_status[spec.key])
            for spec in builtin_world_specs()
        ]

    def _schedule_builtin_world_bootstrap(
        self,
    ) -> asyncio.Task[list[dict[str, Any]]] | None:
        """Start recovery on plugin construction, including hot reload installs."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = loop.create_task(
            self.ensure_builtin_worlds(),
            name="321kaituan-builtins-bootstrap",
        )
        task.add_done_callback(self._builtin_world_bootstrap_finished)
        return task

    def _builtin_world_bootstrap_finished(
        self,
        task: asyncio.Task[list[dict[str, Any]]],
    ) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error("内置世界后台初始化失败：%s", error)

    async def ensure_builtin_worlds(self) -> list[dict[str, Any]]:
        """Idempotently restore missing or outdated built-in world rows."""

        async with self._builtin_world_install_lock:
            worlds = await self.database.list_worlds(include_archived=True)
            by_identity = {
                (
                    str(
                        world.get("source_package_id")
                        or world.get("package_id")
                        or ""
                    ),
                    str(world.get("slug") or ""),
                ): dict(world)
                for world in worlds
            }
            results: list[dict[str, Any]] = []
            for spec in builtin_world_specs():
                current = by_identity.get((spec.package_id, spec.slug))
                current_version_matches = (
                    current is not None
                    and str(current.get("content_version") or "")
                    == spec.content_version
                )
                artifact_matches = False
                if current_version_matches:
                    try:
                        from ..protocol.references import inspect_twp_archive

                        builtin_archive = resolve_builtin_archive(
                            Path(__file__).resolve().parents[2],
                            spec,
                        )
                        report = await asyncio.to_thread(
                            inspect_twp_archive,
                            builtin_archive,
                        )
                        artifact_matches = (
                            str(current.get("source_artifact_hash") or "")
                            == str(report.get("artifact_hash") or "")
                        )
                    except Exception:
                        # Let the normal per-world installer produce the
                        # durable degraded/blocked status and recovery detail.
                        artifact_matches = False
                if current_version_matches and artifact_matches:
                    status = self._builtin_world_status[spec.key]
                    if str(status.get("state") or "") == "pending":
                        status.update(
                            {
                                "state": "ready",
                                "using_previous_version": False,
                                "message": "内置世界已就绪",
                                "installed_content_version": spec.content_version,
                                "last_error": "",
                                "mode": "unchanged",
                                "archived": bool(current.get("archived")),
                            }
                        )
                    results.append(dict(status))
                    continue
                try:
                    result = await self._install_builtin_world(spec.key)
                except Exception as exc:
                    status = self._builtin_world_status[spec.key]
                    status.update(
                        {
                            "state": "degraded" if current else "blocked",
                            "using_previous_version": bool(current),
                            "message": (
                                "内置世界自动恢复失败，继续使用上次成功版本"
                                if current
                                else "内置世界自动恢复失败，当前不可用"
                            ),
                            "installed_content_version": (
                                str(current.get("content_version") or "")
                                if current
                                else ""
                            ),
                            "last_error": str(exc)[:500],
                        }
                    )
                    logger.exception("内置世界自动恢复失败：key=%s", spec.key)
                    result = dict(status)
                results.append(result)
            return results

    async def _previous_builtin_world(
        self,
        package_id: str,
    ) -> dict[str, Any] | None:
        worlds = await self.database.list_worlds()
        return next(
            (
                dict(world)
                for world in worlds
                if str(
                    world.get("source_package_id")
                    or world.get("package_id")
                    or ""
                )
                == package_id
                and not bool(world.get("archived"))
            ),
            None,
        )

    async def _install_builtin_world(self, key: str) -> dict[str, Any]:
        spec = builtin_world_spec_by_key(key)
        status = self._builtin_world_status[spec.key]
        status.update(
            {
                "state": "installing",
                "using_previous_version": False,
                "message": "正在验证并安装",
                "last_error": "",
            }
        )
        builtin_archive = resolve_builtin_archive(
            Path(__file__).resolve().parents[2],
            spec,
        )
        previous = await self._previous_builtin_world(spec.package_id)
        try:
            previous_package = dict(self.world_twp.get(spec.package_id))
        except Exception:
            previous_package = None
        if not builtin_archive.is_file():
            status.update(
                {
                    "state": "degraded" if previous else "blocked",
                    "using_previous_version": bool(previous),
                    "message": (
                        "新版本文件缺失，继续使用上次成功版本"
                        if previous
                        else "内置世界文件缺失，当前不可用"
                    ),
                    "installed_content_version": (
                        str(previous.get("content_version") or "")
                        if previous
                        else ""
                    ),
                    "last_error": "builtin_archive_missing",
                }
            )
            logger.error(
                "内置世界归档缺失：key=%s archive=%s",
                spec.key,
                builtin_archive,
            )
            return dict(status)
        try:
            installed = await self.world_twp.ensure_installed(
                builtin_archive,
                "system:builtin",
            )
            validate_installed_builtin(spec, installed)
            migration = await self.database.install_builtin_world(
                installed["report"]["compiled_world"],
                package=installed["package"],
                actor_id="system:builtin",
                characters=spec.seed_characters,
            )
            status.update(
                {
                    "state": "ready",
                    "using_previous_version": False,
                    "message": "内置世界已就绪",
                    "installed_content_version": str(
                        installed["package"]["version"]
                    ),
                    "last_error": "",
                    "mode": str(migration.get("mode") or ""),
                }
            )
            logger.info(
                "内置 TWP 世界已就绪：key=%s package=%s version=%s mode=%s",
                spec.key,
                installed["package"]["id"],
                installed["package"]["version"],
                migration["mode"],
            )
        except Exception as exc:
            try:
                await self.world_twp.restore_package_record(
                    spec.package_id,
                    previous_package,
                )
            except Exception:
                logger.exception(
                    "内置世界安装失败后恢复上次包索引失败：key=%s",
                    spec.key,
                )
            previous = await self._previous_builtin_world(spec.package_id)
            status.update(
                {
                    "state": "degraded" if previous else "blocked",
                    "using_previous_version": bool(previous),
                    "message": (
                        "新版本安装失败，继续使用上次成功版本"
                        if previous
                        else "内置世界安装失败，当前不可用"
                    ),
                    "installed_content_version": (
                        str(previous.get("content_version") or "")
                        if previous
                        else ""
                    ),
                    "last_error": str(exc)[:500],
                }
            )
            logger.exception(
                "内置世界包安装失败：key=%s archive=%s：%s",
                spec.key,
                builtin_archive,
                exc,
            )
        return dict(status)

    async def retry_builtin_world(self, key: str) -> dict[str, Any]:
        return await self._install_builtin_world(key)

    async def _allow_group(
        self,
        *,
        group_id: str,
        platform_id: str,
        actor_id: str,
        source: str,
    ) -> bool:
        """Persist an explicitly authorized group binding.

        The caller has already authenticated either a configured group
        administrator or an AstrBot Dashboard user.
        """

        normalized_group = validate_platform_id(group_id, label="群 ID")
        normalized_platform = validate_platform_id(
            platform_id,
            label="平台实例 ID",
        )
        async with self._config_lock:
            current = self.runtime_config()
            if normalized_group in current.allowed_group_ids:
                return False

            previous_security = self.plugin_config.get("security")
            security: dict[str, Any]
            if isinstance(previous_security, Mapping):
                security = dict(previous_security)
            else:
                security = {}
            security["allowed_group_ids"] = sorted(
                {*current.allowed_group_ids, normalized_group}
            )
            self.plugin_config["security"] = security

            try:
                save_async = getattr(
                    self.plugin_config,
                    "save_config_async",
                    None,
                )
                if callable(save_async):
                    await save_async()
                else:
                    save = getattr(self.plugin_config, "save_config", None)
                    if callable(save):
                        save()
            except Exception:
                if previous_security is None:
                    self.plugin_config.pop("security", None)
                else:
                    self.plugin_config["security"] = previous_security
                raise

        try:
            await self.database.write_audit(
                "",
                actor_id,
                "security.group_auto_allowed",
                normalized_group,
                {
                    "platform_id": normalized_platform,
                    "source": source,
                },
            )
        except Exception:
            logger.exception("321开团写入自动绑定审计失败")
        await self.broker.publish(
            {
                "type": "settings",
                "action": "group_auto_allowed",
                "group_id": normalized_group,
            }
        )
        return True

    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "group_id", "") or "")

    @staticmethod
    def _platform_id(event: AstrMessageEvent) -> str:
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if ":" in origin:
            platform_instance_id = origin.split(":", 1)[0].strip()
            if platform_instance_id:
                return platform_instance_id
        getter = getattr(event, "get_platform_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)
        return "qq"

    def _qqbot_markdown_for_platform(self, platform_id: Any) -> bool:
        # Known native Markdown adapters are enabled automatically. The
        # explicit switch remains an opt-in for custom instance IDs that do
        # not expose their adapter type in the persisted target snapshot.
        return markdown_supported(platform_id) or bool(
            self.runtime_config().qqbot_markdown_enabled
        )

    @staticmethod
    def _event_origin(event: Any) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    async def _send_or_queue(
        self,
        *,
        session_id: str,
        origin: str,
        text: str,
        kind: str,
        dedupe_key: str = "",
    ) -> bool:
        target = DeliveryTarget.from_origin(
            origin,
            verified_binding=True,
            source="persisted_origin",
        )
        if target is None:
            try:
                session = await self.database.get_session(session_id)
                platform_id = str(session.get("platform_id") or "").strip()
                group_id = str(session.get("group_id") or "").strip()
                if platform_id and group_id:
                    target = DeliveryTarget(
                        platform_instance_id=platform_id,
                        message_type="group",
                        target_id=group_id,
                        unified_origin=str(origin or ""),
                        target_kind="group",
                        verified_binding=True,
                        source="session_group",
                    )
            except Exception:
                target = None
        if target is None:
            logger.warning(
                "321开团投递目标不可解析：session=%s kind=%s",
                session_id,
                kind,
            )
            return False
        if "whisper" in kind:
            message_type = "dm_whisper"
        elif "card_code" in kind:
            message_type = "card_code"
        elif "card_completion" in kind:
            message_type = "card_reminder"
        elif target.message_type == TARGET_KIND_PRIVATE:
            message_type = "card_reminder"
        else:
            message_type = "group_notice"
        outcome = await self.delivery_service.send(
            session_id=session_id,
            target=target,
            kind=message_type,
            text=text,
            audience=(
                AUDIENCE_PRIVATE_OWNER
                if target.message_type == TARGET_KIND_PRIVATE
                else AUDIENCE_GROUP
            ),
            dedupe_key=dedupe_key,
            meta={"source_kind": kind},
        )
        if not outcome.ok:
            await self.broker.publish(
                {
                    "type": "delivery",
                    "action": outcome.status,
                    "session_id": session_id,
                }
            )
        return outcome.ok

    async def _deliver_pending(self, origin: str) -> int:
        """Prompt one leased outbox scan without depending on the inbound origin."""

        summary = await self.delivery_worker.run_once()
        return int(summary.delivered)

    async def _open_supplements_after_progress(
        self,
        session_id: str,
    ) -> None:
        """成功推进后幂等扫描 B/C 补充窗口。

        扫描失败不会回滚已经提交的剧情；offer 与 outbox 仍由自己的
        SQLite 事务原子写入，后续推进会再次安全扫描。
        """

        try:
            current = await self.database.get_session(session_id)
            world_state = current.get("world_state")
            world_state = (
                dict(world_state)
                if isinstance(world_state, Mapping)
                else {}
            )
            runtime = world_state.get("runtime")
            runtime = (
                dict(runtime)
                if isinstance(runtime, Mapping)
                else {}
            )
            await self.database.maybe_open_supplement_offers(
                session_id,
                turn_no=int(current.get("turn_no") or 0),
                current_scene=str(
                    runtime.get("current_scene")
                    or world_state.get("current_scene")
                    or ""
                ),
                chapter=str(
                    runtime.get("current_chapter")
                    or runtime.get("chapter")
                    or ""
                ),
                actor="system",
            )
        except Exception:
            logger.exception(
                "321开团角色补充窗口扫描失败：session=%s",
                session_id,
            )

    async def _persist_delivery_target(
        self,
        target: DeliveryTarget,
        *,
        session_id: str,
        verified: bool,
        source: str,
        target_kind: str = "player",
        allow_downgrade: bool = False,
    ) -> None:
        """把目标写入 delivery_targets 权威表（(平台实例,消息类型,目标ID) 唯一键）。

        临时目标（verified=False）绝不覆盖已存在且已验证的绑定行，
        避免建卡码提醒等临时投递把真实私聊绑定降级。
        """
        try:
            if not verified and not allow_downgrade:
                existing = await self.database.get_delivery_target(
                    platform_instance_id=target.platform_instance_id,
                    message_type=target.message_type,
                    target_id=target.target_id,
                )
                if existing and existing.get("verified_binding"):
                    return
            await self.database.upsert_delivery_target(
                platform_instance_id=target.platform_instance_id,
                message_type=target.message_type,
                target_id=target.target_id,
                session_id=session_id,
                unified_origin=target.unified_origin,
                target_kind=target_kind,
                verified_binding=verified,
                source=source,
            )
        except Exception:
            logger.warning(
                "321开团投递目标持久化失败：platform=%s type=%s target=%s",
                target.platform_instance_id,
                target.message_type,
                target.target_id,
                exc_info=True,
            )

    async def _sync_group_delivery_target(
        self,
        *,
        event: AstrMessageEvent,
        session_id: str,
    ) -> None:
        """玩家加入时保存当前群真实目标（D1-DEL-002 §3.1）。"""

        platform_id = self._platform_id(event)
        group_id = self._group_id(event)
        if not platform_id or not group_id:
            return
        try:
            target = DeliveryTarget(
                platform_instance_id=platform_id,
                message_type=TARGET_KIND_GROUP,
                target_id=group_id,
                unified_origin=self._event_origin(event),
                target_kind=TARGET_KIND_GROUP,
                verified_binding=False,
                source="join_group",
            )
        except ValueError:
            return
        await self._persist_delivery_target(
            target,
            session_id=session_id,
            verified=False,
            source="join_group",
        )

    async def _revoke_private_delivery_target(
        self,
        *,
        platform_id: str,
        user_id: str,
        reason: str,
    ) -> None:
        """席位放弃/退场时降级已验证私聊目标，避免后续误投（D1-DEL-003）。"""

        if not platform_id or not user_id:
            return
        existing = await self.database.get_delivery_target(
            platform_instance_id=platform_id,
            message_type=TARGET_KIND_PRIVATE,
            target_id=user_id,
        )
        if not existing or not existing.get("verified_binding"):
            return
        await self._persist_delivery_target(
            DeliveryTarget.from_authoritative(existing)
            or DeliveryTarget.temporary_private(platform_id, user_id),
            session_id=str(existing.get("session_id") or ""),
            verified=False,
            source=f"binding_revoked:{reason}",
            allow_downgrade=True,
        )

    async def _resolve_private_delivery_target(
        self,
        *,
        session_id: str,
        platform_id: str,
        user_id: str,
        participant: Mapping[str, Any] | None = None,
    ) -> DeliveryTarget | None:
        """解析私聊投递目标：权威表优先，参与者快照回退，最后临时目标。

        - 参与者已退场/归档或无席位时返回 None（撤销不误投）；
        - 权威行已验证且与参与者当前私聊来源一致时直接复用；
        - 参与者仍持有私聊来源时刷新权威行；
        - 来源被清除（重加入/放弃席位）时同步降级旧行并改用临时目标。
        """
        if participant is None:
            try:
                participant = await self.database.get_participant(
                    session_id,
                    user_id=user_id,
                )
            except Exception:
                participant = None
        if not participant:
            return None
        if str(participant.get("participation_status") or "") in {
            "retired",
            "archived",
        }:
            return None
        private_origin = str(participant.get("private_origin") or "").strip()
        row = await self.database.get_delivery_target(
            platform_instance_id=platform_id,
            message_type=TARGET_KIND_PRIVATE,
            target_id=user_id,
        )
        if (
            row
            and row.get("verified_binding")
            and private_origin
            and str(row.get("unified_origin") or "") == private_origin
        ):
            authoritative = DeliveryTarget.from_authoritative(row)
            if authoritative is not None:
                return authoritative
        if private_origin:
            target = DeliveryTarget.from_origin(
                private_origin,
                verified_binding=True,
                source="verified_private",
            )
            if target is not None:
                await self._persist_delivery_target(
                    target,
                    session_id=session_id,
                    verified=True,
                    source="verified_private",
                )
                return target
        if row:
            # 参与者已无私聊来源：旧权威行已失效，降级为临时目标。
            await self._persist_delivery_target(
                DeliveryTarget.from_authoritative(row)
                or DeliveryTarget.temporary_private(platform_id, user_id),
                session_id=session_id,
                verified=False,
                source="temporary_friend",
                allow_downgrade=True,
            )
        try:
            return DeliveryTarget.temporary_private(platform_id, user_id)
        except ValueError:
            return None

    async def _send_card_code_private(
        self,
        *,
        event: AstrMessageEvent,
        session_id: str,
        participant: Mapping[str, Any],
        resend: bool = False,
    ):
        code = str(participant.get("binding_code") or "").strip()
        if not code:
            raise ValueError("当前没有可发送的有效建卡码")
        platform_id = self._platform_id(event)
        user_id = str(
            event.get_sender_id() or participant.get("group_user_id") or ""
        )
        target = await self._resolve_private_delivery_target(
            session_id=session_id,
            platform_id=platform_id,
            user_id=user_id,
            participant=participant,
        )
        if target is None:
            raise ValueError(
                "当前席位已退场，无法投递建卡入口；请先发送 /团 加入 重新占位。"
            )
        text = (
            "【建卡入口】\n"
            f"建卡码：{code}\n"
            f"有效期至：{participant.get('binding_expires_at') or '以系统记录为准'}\n\n"
            "下一步：\n"
            f"/团 建卡 {code}\n\n"
            "请勿把建卡码转发到群聊。"
        )
        return await self.delivery_service.send(
            session_id=session_id,
            target=target,
            kind="card_code",
            text=text,
            audience=AUDIENCE_PRIVATE_OWNER,
            dedupe_key=(
                f"card-code:{session_id}:"
                f"{participant.get('id') or participant.get('participant_id') or ''}:{code}"
                + (f":resend:{transport_event_id(event)}" if resend else "")
            ),
            projection={
                "title": "建卡入口",
                "status": "等待私聊送达",
            },
            meta={
                "expires_at": str(participant.get("binding_expires_at") or ""),
                "recipient_name": str(
                    participant.get("display_name")
                    or event.get_sender_name()
                    or ""
                ),
            },
        )

    async def _activate_pending_private_card(
        self,
        event: AstrMessageEvent,
    ) -> str:
        """收到玩家私聊时，用真实会话来源自动绑定唯一待建卡席位。"""

        origin = self._event_origin(event)
        target = DeliveryTarget.from_origin(
            origin,
            verified_binding=True,
            source="observed_private",
        )
        if target is None or target.message_type != TARGET_KIND_PRIVATE:
            return ""
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id or target.target_id != sender_id:
            return ""
        platform_candidates = []
        for platform_id in (
            self._platform_id(event),
            target.platform_instance_id,
        ):
            normalized = str(platform_id or "").strip()
            if normalized and normalized not in platform_candidates:
                platform_candidates.append(normalized)
        candidates = []
        matched_platform = ""
        for platform_id in platform_candidates:
            candidates = await self.database.pending_card_bindings_for_user(
                platform_id,
                sender_id,
            )
            if candidates:
                matched_platform = platform_id
                break
        if not candidates:
            return ""
        target = DeliveryTarget(
            platform_instance_id=matched_platform,
            message_type=target.message_type,
            target_id=target.target_id,
            unified_origin=target.unified_origin,
            target_kind=target.target_kind,
            verified_binding=True,
            source="observed_private",
        )
        if any(
            not str(item.get("instance_name") or "").strip()
            for item in candidates
        ):
            return (
                "【建卡入口确认失败】\n"
                "操作：确认本次私聊对应的待建卡席位。\n"
                "原因：至少一个待建卡副本缺少可公开显示的名称。\n"
                "自动处理：系统没有绑定任何席位，也没有消耗建卡码，"
                "并已隐藏不完整的副本资料。\n"
                "下一步：请联系主持人修复副本资料后，在目标群重新发送：\n\n"
                "/团 建卡"
            )
        if len(candidates) > 1:
            names = [
                f"《{str(item.get('instance_name')).strip()}》"
                for item in candidates[:5]
            ]
            return (
                "【建卡入口需要确认】\n"
                "操作：确认本次私聊对应的待建卡席位。\n"
                "原因：你在多个副本中都有尚未绑定的角色卡：\n"
                + "\n".join(f"- {name}" for name in names)
                + "\n自动处理：系统没有绑定任何席位，也没有消耗建卡码。\n"
                "下一步：请在要建卡的群里发送 /团 建卡，"
                "再私聊发送该次收到的建卡码。"
            )
        pending = candidates[0]
        bound = await self.database.bind_card_code(
            str(pending.get("binding_code") or ""),
            sender_id,
            origin,
        )
        await self._persist_delivery_target(
            target,
            session_id=str(bound.get("session_id") or ""),
            verified=True,
            source="observed_private",
        )
        return (
            "【私聊通道已确认】\n"
            "系统已自动匹配你在群内预留的席位；"
            "无需等待主动消息，也不需要手工输入建卡码。\n\n"
            + format_card_prompt(bound)
        )
