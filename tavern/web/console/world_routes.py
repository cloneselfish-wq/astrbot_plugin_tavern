from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


class ConsoleWorldRouteMethods:
    async def session_opening(self):
        try:
            self._username()
            query = self._route_query("session_id")
            session_id = str(query.get("session_id") or "")
            if not session_id:
                raise ValueError("请先选择副本")
            result = await self.database.opening_decision(session_id)
            if result is None:
                result = await self.database.prepare_opening_decision(
                    session_id
                )
            principal = self._web_principal()
            return json_response(
                {
                    **result,
                    "permissions": {
                        "can_override": bool(principal.get("is_admin"))
                        and not bool(result.get("frozen")),
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_opening_override(self):
        try:
            principal = self._require_console_admin()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            option_ref = str(payload.get("option_ref") or "")
            expected_revision = int(payload.get("expected_revision"))
            current = await self.database.opening_decision(session_id)
            if current is None:
                current = await self.database.prepare_opening_decision(
                    session_id
                )
            selected = next(
                (
                    item
                    for item in current.get("candidates") or []
                    if str(item.get("option_ref") or "") == option_ref
                ),
                None,
            )
            if selected is None:
                raise ValueError("所选开局已经失效，请刷新后重新选择")
            result = await self.database.override_opening_decision(
                session_id,
                str(selected.get("scene_ref") or ""),
                str(principal.get("username") or "console"),
                expected_revision,
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "opening",
                    "session_id": session_id,
                }
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def session_ai_companions(self):
        try:
            principal = self._console_principal()
            query = self._route_query("session_id")
            session_id = str(query.get("session_id") or "")
            if not session_id:
                raise ValueError("请先选择副本")
            result = await self.database.list_ai_companions(session_id)
            return json_response(
                {
                    **result,
                    "permissions": {
                        "can_manage": bool(principal.get("is_admin")),
                        "maximum_active": 8,
                        "default_visible_limit": 3,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_ai_companions_configure(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            result = await self.database.configure_ai_companions(
                session_id=str(payload.get("session_id") or ""),
                count=int(payload.get("count")),
                mode=str(payload.get("mode") or "confirm"),
                expected_session_revision=int(
                    payload.get("expected_revision")
                ),
                idempotency_key=str(
                    payload.get("idempotency_key") or ""
                ),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "ai_companions",
                    "session_id": str(payload.get("session_id") or ""),
                }
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def session_ai_companion_decision(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            operation_ref = str(payload.get("operation_ref") or "")
            action = str(payload.get("action") or "")
            if action == "confirm":
                result = await self.ai_turn_runner.confirm_pending(
                    session_id=session_id,
                    operation_ref=operation_ref,
                    expected_session_revision=int(
                        payload.get("expected_session_revision")
                    ),
                )
            elif action == "reselect":
                result = await self.ai_turn_runner.reselect_pending(
                    session_id=session_id,
                    operation_ref=operation_ref,
                )
            elif action == "pause":
                result = await self.ai_turn_runner.pause_pending(
                    session_id=session_id,
                    operation_ref=operation_ref,
                )
            else:
                raise ValueError("请选择确认、重选或暂停")
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "ai_companion_decision",
                    "session_id": session_id,
                }
            )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def github_world_import(self):
        """从白名单主机下载世界包 ZIP 并走 TWP 导入链路。"""
        temp_path: Path | None = None
        try:
            self._require_admin()
            payload = await self._payload()
            url = str(payload.get("url") or "")
            if not url:
                raise ValueError("缺少下载地址")
            raw = await fetch_zip(
                self.context.http_client,
                url,
                max_bytes=64 * 1024 * 1024,
            )
            temp_dir = self.data_dir / "imports" / "world-github"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"{uuid.uuid4().hex}.zip"
            temp_path.write_bytes(raw)
            return json_response(await self._install_twp_zip(temp_path))
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path is not None:
                unlink_with_retry(temp_path)

    async def world_twp_module(self):
        try:
            self._require_console_admin()
            payload = await self._payload()
            package_id = str(
                payload.get("package_ref")
                or payload.get("package_id")
                or ""
            )
            result = await self.world_twp.set_module(
                package_id,
                str(payload.get("module_id") or ""),
                bool(payload.get("enabled")),
                self._actor(),
            )
            compiled = self.world_twp.compiled_world(package_id)
            current = await self.database.get_world(str(compiled["slug"]))
            compiled["id"] = current["id"]
            compiled["revision"] = current["revision"]
            item = await self.database.save_world(compiled, self._actor())
            self._purge_rule_runtime(str(item.get("slug") or ""))
            await self.broker.publish(
                {
                    "type": "world_twp",
                    "action": "module",
                    "package_ref": self.world_twp.package_reference(
                        self.world_twp.resolve_reference(package_id)
                    ),
                }
            )
            return json_response({"item": item, **result})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_export(self, package_id: str):
        try:
            self._username()
            resolved_id = self.world_twp.resolve_reference(package_id)
            item = self.world_twp.get(resolved_id)
            return file_response(
                self.world_twp.archive_path(package_id),
                filename=f"tavern-world-{item['version']}.zip",
                content_type="application/zip",
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_preset_libraries(self, package_id: str):
        try:
            principal = self._web_principal()
            world = self.world_twp.compiled_world(package_id)
            rules = world.get("rules")
            rules = rules if isinstance(rules, dict) else {}
            actor = rules.get("actor")
            actor = actor if isinstance(actor, dict) else {}
            normalized = normalize_preset_libraries(actor)
            issues = [
                {
                    "code": str(
                        problem.get("code")
                        or "actor.preset_library.problem"
                    ),
                    "set_id": _preset_issue_set_id(problem),
                    "path": str(problem.get("path") or ""),
                    "message": str(
                        problem.get("message") or "预设库信息不完整"
                    ),
                    "severity": str(
                        problem.get("severity") or "error"
                    ),
                }
                for problem in normalized.get("problems") or []
            ]
            return json_response(
                {
                    "package_ref": self.world_twp.package_reference(
                        self.world_twp.resolve_reference(package_id)
                    ),
                    "candidate_contract": str(
                        actor.get("candidate_contract")
                        or "twp-actor-candidate/1.0.0-rc10"
                    ),
                    "items": normalized.get("items") or [],
                    "count": int(normalized.get("count") or 0),
                    "referenced_library_ids": list(
                        normalized.get("referenced_library_ids") or []
                    ),
                    "metadata_complete": bool(
                        normalized.get("metadata_complete")
                    ),
                    "issues": issues,
                    "permissions": {
                        "can_view": True,
                        "can_edit": bool(principal["is_admin"]),
                        "role_source": principal["role_source"],
                    },
                }
            )
        except PresetLibraryContractError as exc:
            return error_response(
                f"预设库契约错误（{exc.code}）：{exc}",
                status_code=400,
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def _designer_world(self, payload: dict[str, Any]) -> dict[str, Any]:
        world_ref = str(payload.get("world_ref") or "").strip()
        world = payload.get("world", payload.get("world_snapshot", {}))
        if world_ref:
            world = await self.database.get_world(world_ref)
        if not isinstance(world, dict):
            raise ValueError("world 必须是 JSON 对象")
        return world

    @staticmethod
    def _designer_participant_token(
        session_id: str,
        participant_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"designer-actor\0{session_id}\0{participant_id}".encode("utf-8")
        ).hexdigest()
        return "actor_" + digest[:24]

    async def designer_session_actors(self):
        """Return only the actor fields required by the authoring selector."""

        try:
            self._require_console_admin()
            session_id = str(request.query.get("session_id", "") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            if session is None:
                raise ValueError("副本不存在")
            roster = await self.database.list_roster(session_id)
            items = []
            for item in roster:
                if not isinstance(item, Mapping):
                    continue
                has_card = bool(item.get("card_profile")) or bool(
                    item.get("draft_profile")
                )
                if not has_card:
                    continue
                participant_id = str(item.get("id") or "")
                if not participant_id:
                    continue
                items.append(
                    {
                        "participant_token": self._designer_participant_token(
                            session_id,
                            participant_id,
                        ),
                        "name": str(
                            item.get("character_name")
                            or item.get("display_name")
                            or "角色资料缺少名称"
                        ),
                        "card_status": str(item.get("card_status") or ""),
                        "draft_status": str(item.get("draft_status") or ""),
                        "card_version": int(item.get("card_version_no") or 0),
                    }
                )
            items.sort(key=lambda item: (item["name"], item["participant_token"]))
            return json_response({"items": items, "count": len(items)})
        except Exception as exc:
            return self._handle_error(exc)

    async def _designer_character_fields(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load a trusted persisted card/draft for simulation.

        Browser-supplied free-form fields are accepted only when no participant
        is requested, which keeps template authoring previews possible while
        making live-character simulation authoritative.
        """
        session_id = str(payload.get("session_id") or "").strip()
        participant_token = str(
            payload.get("participant_token") or ""
        ).strip()
        if not session_id and not participant_token:
            fields = payload.get("fields", {})
            return (
                fields if isinstance(fields, dict) else {},
                {"source": "authoring_preview", "trusted": False},
            )
        if not session_id or not participant_token:
            raise ValueError(
                "真实角色模拟必须同时提供副本与角色选择；请刷新角色列表后重试"
            )
        roster = await self.database.list_roster(session_id)
        participant = next(
            (
                item
                for item in roster
                if self._designer_participant_token(
                    session_id,
                    str(item.get("id") or ""),
                )
                == participant_token
            ),
            None,
        )
        if not isinstance(participant, dict):
            raise ValueError("所选角色不属于当前副本")
        expected = payload.get("expected_card_version")
        actual_version = int(participant.get("card_version_no") or 0)
        if expected not in {None, ""} and int(expected) != actual_version:
            raise ValueError(
                f"角色卡版本已变化：期望 v{int(expected)}，当前 v{actual_version}"
            )
        card_profile = participant.get("card_profile")
        draft_profile = participant.get("draft_profile")
        if isinstance(card_profile, dict) and card_profile:
            fields = card_profile
            source = "approved_card"
        elif isinstance(draft_profile, dict) and draft_profile:
            fields = draft_profile
            source = "active_draft"
        else:
            raise ValueError("所选角色没有可模拟的已保存角色卡或建卡草稿")
        character_name = str(
            participant.get("character_name")
            or participant.get("display_name")
            or ""
        ).strip()
        if not character_name:
            raise ValueError(
                "所选角色缺少可公开显示的名称；请先修复角色名称并刷新后重试"
            )
        return (
            dict(fields),
            {
                "source": source,
                "trusted": True,
                "session_id": session_id,
                "participant_token": participant_token,
                "character_name": character_name,
                "card_version": actual_version,
                "draft_status": str(participant.get("draft_status") or ""),
                "card_status": str(participant.get("card_status") or ""),
            },
        )

    async def designer_health(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...lifecycle import card_template
            from ...twp.validation.privacy import check_template

            report = check_template(world)
            report["actor_template"] = card_template(world)
            return json_response(report)
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_coverage(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.validation.privacy import coverage_matrix

            return json_response(coverage_matrix(world))
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_candidates(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...lifecycle import card_template
            from ...twp.designer import candidate_resolution

            fields = payload.get("fields", {})
            fields = fields if isinstance(fields, dict) else {}
            return json_response(
                {
                    "items": candidate_resolution(card_template(world), fields),
                    "count": len(card_template(world).get("fields", [])),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_simulate(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...lifecycle import card_template
            from ...twp.designer import build_simulation

            fields, input_meta = await self._designer_character_fields(payload)
            result = build_simulation(card_template(world), fields, world)
            result["input"] = input_meta
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_effects(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import effect_reducer

            fields, input_meta = await self._designer_character_fields(payload)
            result = effect_reducer(
                world,
                fields,
                dry_run=bool(payload.get("dry_run", True)),
            )
            result["input"] = input_meta
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_template_diff(self):
        try:
            self._username()
            payload = await self._payload()
            from ...twp.designer import template_diff

            return json_response(
                template_diff(
                    payload.get("base", {}),
                    payload.get("candidate", {}),
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_card_groups(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...lifecycle import card_template
            from ...twp.designer import card_groups

            fields = payload.get("fields", {})
            fields = fields if isinstance(fields, dict) else {}
            return json_response({"groups": card_groups(card_template(world), fields)})
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_card_diff(self):
        try:
            self._username()
            payload = await self._payload()
            from ...twp.designer import card_diff

            return json_response(
                card_diff(
                    payload.get("current", {}),
                    payload.get("candidate", {}),
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_preset_references(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import preset_references

            refs = preset_references(
                world,
                str(payload.get("set_key") or ""),
                str(payload.get("preset_id") or ""),
            )
            return json_response({"references": refs, "count": len(refs)})
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_preset_delete(self):
        try:
            self._require_author()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import delete_preset
            from ...twp.validation.privacy import check_template

            candidate = delete_preset(
                world,
                str(payload.get("set_key") or ""),
                str(payload.get("preset_id") or ""),
            )
            report = check_template(candidate)
            if not report["compatible"]:
                messages = [
                    item["message"]
                    for item in report["errors"][:5]
                ]
                raise ValueError("删除后模板体检未通过：" + "；".join(messages))
            item = await self.database.save_world(candidate, self._actor())
            self._purge_rule_runtime(str(item.get("slug") or ""))
            await self.broker.publish(
                {"type": "world", "action": "preset_delete", "world_id": item.get("id")}
            )
            return json_response({"item": item, "report": report})
        except Exception as exc:
            return self._handle_error(exc)

    async def _persist_designer_edit(self, candidate: dict[str, Any]) -> dict[str, Any]:
        from ...twp.validation.privacy import check_template

        report = check_template(candidate)
        if not report["compatible"]:
            messages = [item["message"] for item in report["errors"][:5]]
            raise ValueError("编辑后模板体检未通过：" + "；".join(messages))
        item = await self.database.save_world_edit(candidate, self._actor())
        self._purge_rule_runtime(str(item.get("slug") or ""))
        await self.broker.publish({"type": "world", "action": "designer_edit", "world_id": item.get("id")})
        return {"item": item, "report": report}

    async def designer_preset_save(self):
        try:
            self._require_author()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import upsert_preset

            candidate = upsert_preset(
                world,
                str(payload.get("set_key") or ""),
                payload.get("preset", {}),
            )
            return json_response(await self._persist_designer_edit(candidate))
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_field_save(self):
        try:
            self._require_author()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import upsert_field

            candidate = upsert_field(world, payload.get("field", {}))
            return json_response(await self._persist_designer_edit(candidate))
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_reorder(self):
        try:
            self._require_author()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import reorder_presets

            candidate = reorder_presets(
                world,
                str(payload.get("set_key") or ""),
                payload.get("order", []),
            )
            return json_response(await self._persist_designer_edit(candidate))
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_revert(self):
        try:
            self._require_author()
            payload = await self._payload()
            world_id = str(payload.get("world_ref") or payload.get("id") or "").strip()
            if not world_id:
                raise ValueError("缺少 world_ref")
            item = await self.database.revert_world_edit(world_id, self._actor())
            self._purge_rule_runtime(str(item.get("slug") or ""))
            await self.broker.publish({"type": "world", "action": "designer_revert", "world_id": item.get("id")})
            return json_response({"item": item})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_l10n_report(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.localization import localization_report

            return json_response(
                localization_report(
                    world,
                    requested_locale=str(payload.get("requested_locale") or "")
                    or None,
                )
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def designer_distribution(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.designer import distribution_summary

            return json_response(distribution_summary(world))
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_simulate(self):
        try:
            self._username()
            payload = await self._payload()
            world = await self._designer_world(payload)
            from ...twp.simulation import run_smoke_simulation

            report = run_smoke_simulation(
                world,
                turns=int(payload.get("turns", 30) or 30),
                party_sizes=payload.get("party_sizes") or [1, 4, 8],
            )
            return json_response(report)
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_commands(self):
        try:
            self._username()
            catalog = await self.database.world_command_catalog()
            return json_response({"items": catalog, "count": len(catalog)})
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_runtime(self):
        try:
            self._username()
            session_id = str(getattr(request, "query", {}) or {}).strip()
            import urllib.parse

            raw = urllib.parse.urlparse(str(getattr(request, "url", "") or ""))
            query = urllib.parse.parse_qs(raw.query)
            session_id = (query.get("session_id") or [""])[0]
            if not session_id:
                raise ValueError("缺少 session_id")
            return json_response(await self.database.world_runtime_state(session_id))
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_endings(self):
        try:
            self._username()
            session_id = str(getattr(request, "query", {}) or {}).strip()
            import urllib.parse

            raw = urllib.parse.urlparse(str(getattr(request, "url", "") or ""))
            query = urllib.parse.parse_qs(raw.query)
            session_id = (query.get("session_id") or [""])[0]
            if not session_id:
                raise ValueError("缺少 session_id")
            return json_response(
                await self.database.world_ending_readiness(session_id)
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_command_preview(self):
        try:
            self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = payload.get("command", {})
            if not session_id or not isinstance(command, dict):
                raise ValueError("需要 session_id 与 command")
            return json_response(
                await self.database.world_command_preview(session_id, command)
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def world_twp_command(self):
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            command = payload.get("command", {})
            if not session_id or not isinstance(command, dict):
                raise ValueError("需要 session_id 与 command")
            await self._require_dm_capability(session_id, user)
            command.setdefault("operator", self._actor())
            result = await self.database.execute_world_command(session_id, command)
            if result.get("events"):
                await self.broker.publish(
                    {
                        "type": "world_command",
                        "action": "executed",
                        "session_id": session_id,
                        "domain": command.get("domain"),
                        "operation_id": result.get("operation_id"),
                        "summary": result.get("summary"),
                    }
                )
            return json_response(result)
        except Exception as exc:
            return self._handle_error(exc)
