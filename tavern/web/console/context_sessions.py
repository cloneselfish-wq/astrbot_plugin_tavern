from __future__ import annotations

import json
import asyncio
import hashlib
import zipfile

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    is_standalone_upload,
    json_response,
    request,
    stream_response,
)
from ..query import QueryAdapter
from ..routes.narrative_mode import (
    narrative_mode_view as route_narrative_mode_view,
)
from ..intents.dispatcher import execute_intent
from ..routes.event_stream import open_event_stream
from ..surfaces.registry import resolve_surface_key
from ...protocol.common import MAX_ARCHIVE_BYTES




class ContextSessionsMixin:
    async def session_character_detail_view(self):
        query = self._route_query()
        envelope = await route_character_detail_view(
            self._web_principal(),
            self.database,
            str(query.get("session_id") or ""),
            participant_ref=str(
                query.get("participant_ref")
                or query.get("character")
                or ""
            ),
            query=query,
        )
        return self._route_json_response(envelope)
    async def session_character_supplements_view(self):
        query = self._route_query()
        envelope = await route_supplement_offers_view(
            self._web_principal(),
            self.database,
            str(query.get("session_id") or ""),
            query=query,
        )
        return self._route_json_response(envelope)
    async def session_world_state_view(self):
        query = self._route_query()
        envelope = await route_world_state_view(
            self._web_principal(),
            self.database,
            str(query.get("session_id") or ""),
            query=query,
        )
        return self._route_json_response(envelope)
    async def session_actor_fate_consent(self):
        method = str(getattr(request, "method", "GET") or "GET").upper()
        payload = await self._payload() if method == "POST" else {}
        return self._route_json_response(
            await route_actor_fate_consent_view(
                self._surface_principal(),
                self.database,
                query=self._route_query("session_id"),
                payload=payload,
                method=method,
                idempotency_key=self._request_idempotency_key(payload),
            )
        )
    async def session_assets_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_assets_view(
                self._web_principal(),
                self.database,
                query=query,
            )
        )
    async def session_economy_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_economy_summary(
                self._web_principal(),
                self.database,
                query=query,
            )
        )
    async def session_economy_transactions(self):
        query = self._route_query()
        return self._route_json_response(
            await route_economy_transactions(
                self._web_principal(),
                self.database,
                query=query,
            )
        )
    async def session_economy_set_enabled(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_economy_set_enabled(
                self._web_principal(),
                self.database,
                payload=payload,
                actor=self._actor(),
                audit=self._route_audit,
                publish=self._route_publish,
            )
        )
    async def session_economy_migrate_world(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_economy_migrate_world(
                self._web_principal(),
                self.database,
                payload=payload,
                actor=self._actor(),
                publish=self._route_publish,
            )
        )
    async def session_economy_adjust(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_economy_adjust(
                self._web_principal(),
                self.database,
                payload=payload,
                actor=self._actor(),
                audit=self._route_audit,
                publish=self._route_publish,
            )
        )
    async def session_operations_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_operations_view(
                self._web_principal(),
                self.database,
                query=query,
            )
        )
    async def session_operation_cancel(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_operation_cancel(
                self._web_principal(),
                self.database,
                payload=payload,
                actor=self._actor(),
                audit=self._route_audit,
                publish=self._route_publish,
            )
        )
    async def session_deliveries_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_deliveries_view(
                self._web_principal(),
                self.database,
                query=query,
                delivery_service=self.delivery_service,
            )
        )
    async def session_deliveries_action(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_deliveries_act(
                self._web_principal(),
                self.database,
                payload=payload,
                actor=self._actor(),
                delivery_service=self.delivery_service,
                audit=self._route_audit,
                publish=self._route_publish,
            )
        )
    async def session_diagnostics_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_diagnostics_view(
                self._web_principal(),
                self.database,
                query=query,
            )
        )
    async def _growth_viewer(
        self,
        principal: Mapping[str, Any],
        session_id: str,
        participant_id: str = "",
    ) -> tuple[str, str]:
        user = str(principal.get("username") or "")
        viewer_role = await self._supplement_viewer_role(
            session_id,
            user,
            principal,
        )
        if viewer_role == "player":
            participant = await self._viewer_participant_or_none(
                session_id,
                user,
            )
            if participant is None:
                raise PolicyRejection(
                    "你不是该副本的成员，无法查看技能成长"
                )
            participant_id = str(participant.get("id") or "")
        return viewer_role, str(participant_id or "").strip()
    async def session_growth_view(self):
        try:
            principal = self._web_principal()
            query = self._route_query()
            session_id = str(query.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id：请先选择副本")
            viewer_role, participant_id = await self._growth_viewer(
                principal,
                session_id,
                str(query.get("participant_id") or ""),
            )
            profiles = await self.database.list_growth_profiles(
                session_id,
                participant_id=participant_id,
                viewer_role=viewer_role,
                include_technical_refs=viewer_role in {"dm", "admin"},
            )
            return json_response(
                {
                    "session_id": session_id,
                    "viewer_role": viewer_role,
                    "participant_id": (
                        participant_id
                        if viewer_role in {"dm", "admin"}
                        else ""
                    ),
                    "readonly": await self._session_readonly(session_id),
                    "tracks": list(profiles.get("tracks") or []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def _growth_track_ref(
        self,
        session_id: str,
        participant_id: str,
        ordinal: int,
        *,
        viewer_role: str,
    ) -> str:
        profiles = await self.database.list_growth_profiles(
            session_id,
            participant_id=participant_id,
            viewer_role=viewer_role,
            include_technical_refs=True,
        )
        tracks = [
            dict(item)
            for item in (profiles.get("tracks") or [])
            if isinstance(item, Mapping)
        ]
        if ordinal < 1 or ordinal > len(tracks):
            raise ValueError("技能序号无效，请刷新成长面板后重试")
        technical = tracks[ordinal - 1].get("technical")
        technical = dict(technical) if isinstance(technical, Mapping) else {}
        track_ref = str(technical.get("track_ref") or "").strip()
        if not track_ref:
            raise ValueError("无法解析该技能的成长轨迹")
        return track_ref
    async def session_growth_confirm(self):
        try:
            principal = self._web_principal()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id：请先选择副本")
            viewer_role, participant_id = await self._growth_viewer(
                principal,
                session_id,
                str(payload.get("participant_id") or ""),
            )
            if not participant_id:
                raise ValueError("请先选择要处理的角色")
            ordinal = int(payload.get("ordinal") or 0)
            track_ref = await self._growth_track_ref(
                session_id,
                participant_id,
                ordinal,
                viewer_role=viewer_role,
            )
            result = await self.database.confirm_growth(
                session_id,
                participant_id,
                track_ref,
                actor=self._actor(),
                private_origin=(
                    str(
                        (
                            await self._viewer_participant_or_none(
                                session_id,
                                str(principal.get("username") or ""),
                            )
                            or {}
                        ).get("private_origin")
                        or ""
                    )
                    if viewer_role == "player"
                    else ""
                ),
                authority_confirm=viewer_role in {"dm", "admin"},
            )
            self.broker.schedule(
                {
                    "type": "growth",
                    "action": "confirmed",
                    "session_id": session_id,
                }
            )
            return json_response(
                {
                    "ok": True,
                    "message": str(result.get("message") or "技能成长已确认。"),
                    "view": result.get("view") or {},
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def session_growth_evidence(self):
        try:
            principal = self._web_principal()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id：请先选择副本")
            viewer_role, participant_id = await self._growth_viewer(
                principal,
                session_id,
                str(payload.get("participant_id") or ""),
            )
            if viewer_role not in {"dm", "admin"}:
                raise PolicyRejection("只有主持人或管理员可以登记成长证据")
            if not participant_id:
                raise ValueError("请先选择要登记成长证据的角色")
            ordinal = int(payload.get("ordinal") or 0)
            track_ref = await self._growth_track_ref(
                session_id,
                participant_id,
                ordinal,
                viewer_role=viewer_role,
            )
            note = str(payload.get("note") or "").strip()
            if not note:
                raise ValueError("请填写真实的成长证据说明")
            result = await self.database.record_growth_evidence(
                session_id,
                participant_id,
                track_ref,
                evidence_id=str(payload.get("evidence_key") or ""),
                kind=str(payload.get("kind") or "host_confirmed"),
                note=note,
                actor=self._actor(),
                milestone=bool(payload.get("milestone")),
            )
            self.broker.schedule(
                {
                    "type": "growth",
                    "action": "evidence_recorded",
                    "session_id": session_id,
                }
            )
            return json_response(
                {
                    "ok": True,
                    "message": str(result.get("message") or "成长证据已记录。"),
                    "pending_created": bool(result.get("pending_created")),
                    "view": result.get("view") or {},
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    @staticmethod
    def _request_idempotency_key(
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        explicit = str(
            data.get("idempotency_key")
            or data.get("request_id")
            or ""
        ).strip()
        if explicit:
            return explicit
        headers = getattr(request, "headers", {})
        getter = getattr(headers, "get", None)
        if callable(getter):
            header_value = str(
                getter("X-Idempotency-Key")
                or getter("x-idempotency-key")
                or getter("Idempotency-Key")
                or getter("idempotency-key")
                or ""
            ).strip()
            if header_value:
                return header_value
        query = getattr(request, "query", {})
        query_get = getattr(query, "get", None)
        if callable(query_get):
            return str(query_get("idempotency_key") or "").strip()
        return ""
    async def session_tendency_view(self):
        query = self._route_query()
        return self._route_json_response(
            await route_tendency_view(
                self._web_principal(),
                self.database,
                session_id=str(query.get("session_id") or ""),
            )
        )
    async def session_tendency_action(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_tendency_action(
                self._web_principal(),
                self.database,
                self.application_router,
                payload=payload,
                idempotency_key=self._request_idempotency_key(payload),
            )
        )
    async def author_jobs_view(self):
        query = self._route_query()
        try:
            limit = int(query.get("limit") or 100)
        except (TypeError, ValueError, OverflowError):
            limit = 100
        return self._route_json_response(
            await route_author_jobs_view(
                self._console_principal(),
                self.database,
                world_ref=str(query.get("world_ref") or ""),
                limit=limit,
            )
        )
    async def author_job_create(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_author_job_create(
                self._console_principal(),
                self.application_router,
                payload=payload,
                idempotency_key=self._request_idempotency_key(payload),
            )
        )
    async def author_job_action(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_author_job_action(
                self._console_principal(),
                self.application_router,
                payload=payload,
                idempotency_key=self._request_idempotency_key(payload),
            )
        )
    async def author_job_artifact(self):
        query = self._route_query()
        return self._route_json_response(
            await route_author_job_artifact(
                self._console_principal(),
                self.database,
                job_ref=str(query.get("job_ref") or ""),
                artifact_type=str(query.get("artifact_type") or ""),
            )
        )
    async def health_view(self):
        return self._route_json_response(
            await route_health_view(
                self._console_principal(),
                self.database,
            )
        )
    async def health_action(self):
        payload = await self._payload()
        return self._route_json_response(
            await route_health_action(
                self._console_principal(),
                self.application_router,
                payload=payload,
                idempotency_key=self._request_idempotency_key(payload),
            )
        )
    async def health_diagnostic(self, token: str):
        try:
            self._require_admin()
            path = self.health_recovery_service.diagnostic_path(token)
            if not path.is_file():
                raise ValueError("诊断下载凭据已失效，请重新导出")
            return file_response(
                path,
                filename="tavern-health-diagnostic.zip",
                content_type="application/zip",
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def plugin_modules(self):
        """Return the 27 runtime domains and their live dependency state."""
        try:
            self._username()
            items = self.modules.catalog()
            return json_response(
                {
                    "items": items,
                    "count": len(items),
                    "capabilities": self.modules.capabilities(),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
    async def plugin_module_toggle(self):
        try:
            self._require_admin()
            payload = await self._payload()
            item = await self.modules.set_enabled(
                str(payload.get("module_id") or ""),
                bool(payload.get("enabled")),
            )
            await self.database.write_audit(
                "",
                self._actor(),
                "plugin.module.toggle",
                str(item["id"]),
                {"enabled": bool(item["enabled"])},
            )
            await self.broker.publish(
                {"type": "plugin_module", "action": "toggle", "module_id": item["id"]}
            )
            return json_response({"item": item, "items": self.modules.catalog()})
        except Exception as exc:
            return self._handle_error(exc)
    async def world_twp_protocol(self):
        try:
            self._username()
            return json_response(self.world_twp.protocol_info())
        except Exception as exc:
            return self._handle_error(exc)
    async def world_twp_packages(self):
        try:
            self._username()
            return json_response({"items": self.world_twp.public_packages()})
        except Exception as exc:
            return self._handle_error(exc)
    async def _save_twp_upload(self) -> tuple[Path, str]:
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile) and not is_standalone_upload(upload):
            raise ValueError("请选择一个 TWP ZIP 世界包")
        filename = str(upload.filename or "").strip()
        if not filename.lower().endswith(".zip"):
            raise ValueError("世界包只接受 .zip 整包导入，不再接受旧版 JSON")
        temp_dir = self.data_dir / "imports" / "world-twp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        path = temp_dir / f"{uuid.uuid4().hex}.zip"
        content_length = getattr(upload, "content_length", None)
        request_length = getattr(request, "content_length", None)
        if request_length is not None and int(request_length or 0) > MAX_ARCHIVE_BYTES:
            raise ValueError("世界包超过 64 MiB 上传上限")
        if content_length is not None and int(content_length or 0) > MAX_ARCHIVE_BYTES:
            raise ValueError("世界包超过 64 MiB 上传上限")
        stream = getattr(upload, "stream", None) or getattr(upload, "file", None)
        if stream is None:
            raise ValueError("世界包上传流不可用")

        def _bounded_copy() -> str:
            digest = hashlib.sha256()
            total = 0
            seek = getattr(stream, "seek", None)
            if callable(seek):
                seek(0)
            with path.open("wb") as output:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("世界包超过 64 MiB 上传上限")
                    output.write(chunk)
                    digest.update(chunk)
            return digest.hexdigest()

        try:
            archive_hash = await asyncio.to_thread(_bounded_copy)
            return path, archive_hash
        except Exception:
            unlink_with_retry(path)
            raise
    async def world_twp_preflight(self):
        temp_path: Path | None = None
        try:
            self._require_admin()
            temp_path, _archive_hash = await self._save_twp_upload()
            from ...protocol.references import inspect_twp_archive
            report = inspect_twp_archive(temp_path)
            return json_response({
                "compatible": bool(report.get("compatible", True)),
                "issues": list(report.get("issues") or ()),
                "summary": dict(report.get("summary") or {}),
                "artifact_hash": str(report.get("artifact_hash") or ""),
                "source_hash": str(report.get("source_hash") or ""),
            })
        except TwpPackageError as exc:
            return json_response(
                {
                    "compatible": False,
                    "issues": [item.export() for item in exc.issues],
                    "summary": {},
                }
            )
        except Exception as exc:
            return self._handle_error(exc)
        finally:
            if temp_path is not None:
                unlink_with_retry(temp_path)
    async def _install_twp_zip(self, temp_path: Path) -> dict[str, Any]:
        """幂等安装一个已就绪的 TWP ZIP；事件由回执提交者发布。"""
        result = await self.world_twp.ensure_installed(temp_path, self._actor())
        compiled = dict(result["report"]["compiled_world"])
        installed_world = await self.database.install_package_world(
            compiled,
            package=result["package"],
            actor_id=f"package:{self._actor()}",
        )
        item = installed_world["item"]
        self._purge_rule_runtime(str(item.get("slug") or ""))
        return {
            "item": item,
            "package": result["package"],
            "preflight": result["report"],
            "mode": installed_world["mode"],
        }
    async def world_twp_import(self):
        temp_path: Path | None = None
        actor = ""
        idempotency_key = ""
        receipt_reserved = False
        try:
            self._require_admin()
            actor = self._actor()
            idempotency_key = self._request_idempotency_key()
            if not idempotency_key:
                raise ValueError("导入本地世界包缺少 idempotency_key")
            temp_path, archive_hash = await self._save_twp_upload()
            prepared = await self.database.prepare_local_twp_import(
                actor, idempotency_key=idempotency_key, archive_sha256=archive_hash
            )
            if bool(prepared.get("replayed")):
                return json_response(prepared)
            receipt_reserved = True
            installed = await self._install_twp_zip(temp_path)
            completed = await self.database.complete_local_twp_import(
                actor,
                idempotency_key=idempotency_key,
                archive_sha256=archive_hash,
                result=installed,
            )
            if not bool(completed.get("replayed")):
                await self.broker.publish({"type": "world_twp", "action": "import"})
            return json_response(completed)
        except Exception as exc:
            if receipt_reserved:
                await self.database.fail_local_twp_import(
                    actor,
                    idempotency_key=idempotency_key,
                    error_code=str(getattr(exc, "code", type(exc).__name__)),
                )
            return self._handle_error(exc)
        finally:
            if temp_path is not None:
                unlink_with_retry(temp_path)
    async def world_twp_readme(self):
        """Return an audience-safe README index or one lazily-read section."""
        try:
            principal = self._surface_principal()
            roles = {
                str(item).strip().lower()
                for item in principal.get("roles") or ()
                if str(item).strip()
            }
            capabilities = dict(principal.get("capabilities") or {})
            if bool(principal.get("is_admin")) or bool(capabilities.get("admin")):
                roles.add("admin")
            if bool(principal.get("is_author")) or bool(capabilities.get("author")):
                roles.add("author")
            if (
                bool(principal.get("is_dm"))
                or bool(capabilities.get("dm"))
                or bool(capabilities.get("host"))
            ):
                roles.add("host")
            query = self._route_query("world_key", "section_key", "expected_revision", "expected_readme_revision")
            world_key = str(query.get("world_key") or "")
            world_ref = resolve_surface_key(principal, "worlds", world_key, kind="world")
            if not world_ref:
                raise ValueError("所选世界已经失效；系统未读取任何世界设定。请返回世界库重新选择。")
            # The opaque surface key resolves to the database world's internal
            # identity.  TWP package records are indexed by world slug, so the
            # route must cross that authoritative database boundary before
            # selecting the installed archive.  Comparing the internal id
            # directly with package.slug made every valid README lookup miss.
            world = await self.database.get_world(world_ref)
            world_slug = str(world.get("slug") or "").strip()
            if not world_slug:
                raise ValueError("读取世界设定失败：当前世界缺少可用名称；系统未读取其他世界。请刷新世界库后重试。")
            package = next(
                (
                    item for item in self.world_twp.public_packages()
                    if str(item.get("slug") or "") == world_slug
                ),
                None,
            )
            if not isinstance(package, dict):
                raise ValueError("读取世界设定失败：当前世界没有可用的 RC10 世界包；系统未回退到旧包。请联系管理员重新安装。")
            package_ref = str(package.get("package_ref") or "")
            compiled = self.world_twp.compiled_world(package_ref)
            compiled_revision = str(compiled.get("artifact_hash") or compiled.get("artifact_id") or "")
            expected_revision = str(query.get("expected_revision") or "")
            if expected_revision and expected_revision != compiled_revision:
                raise ValueError("读取世界设定失败：世界已经更新；系统已丢弃旧读取动作。请刷新世界库后重试。")
            archive = self.world_twp.archive_path(package_ref)
            with zipfile.ZipFile(archive, "r") as bundle:
                index_raw = bundle.read("compiled/readme_index.json")
                index = json.loads(index_raw.decode("utf-8-sig"))
                audience = "admin" if "admin" in roles else "author" if "author" in roles else "host" if "host" in roles else "player"
                allowed = {
                    "player": {"player"},
                    "host": {"player", "host"},
                    "author": {"player", "host", "author"},
                    "admin": {"player", "host", "author", "admin"},
                }[audience]
                readme_revision = str(index.get("readme_revision") or "")
                expected_readme = str(query.get("expected_readme_revision") or "")
                if expected_readme and expected_readme != readme_revision:
                    raise ValueError("读取世界设定失败：设定目录已经更新；系统已清除旧章节请求。请重新加载目录。")
                visible = [item for item in index.get("sections") or [] if isinstance(item, dict) and item.get("audience") in allowed]
                section_key = str(query.get("section_key") or "")
                contract = {
                    "world_revision": compiled_revision,
                    "readme_revision": readme_revision,
                    "audience_scope": audience,
                }
                if not section_key:
                    sections = []
                    for item in visible:
                        sections.append({
                            "section_key": str(item.get("key") or ""),
                            "title": str(item.get("title") or "世界设定"),
                            "level": int(item.get("level") or 2),
                            "summary": str(item.get("summary") or ""),
                            "read_action": {
                                "intent": "world.readme.section.read",
                                "target": "world-twp/readme",
                                "world_key": world_key,
                                "section_key": str(item.get("key") or ""),
                                "expected_revision": compiled_revision,
                                "expected_readme_revision": readme_revision,
                            },
                        })
                    return json_response({
                        "state": "ready",
                        "title": "世界设定",
                        "contract": contract,
                        "sections": sections,
                        "projection": {"audience_scope": audience, "section_count": len(sections)},
                        "summary": "目录已按当前身份在服务端裁剪；正文仅在打开章节时读取。",
                    }, headers={"Cache-Control": "private, no-store"})
                selected = next((item for item in visible if str(item.get("key") or "") == section_key), None)
                if not isinstance(selected, dict):
                    raise ValueError("读取世界设定章节失败：章节不存在或当前身份不可见；系统未改读其他章节。请返回目录选择。")
                body_ref = str(selected.get("body_ref") or "")
                if not body_ref.startswith("content/readme/") or ".." in body_ref.split("/"):
                    raise ValueError("读取世界设定章节失败：章节索引损坏；系统已隔离该章节。请管理员重新安装世界包。")
                raw = bundle.read(body_ref)
                if len(raw) > 128 * 1024:
                    raise ValueError("读取世界设定章节失败：章节超过安全上限；系统未返回正文。请联系世界作者拆分章节。")
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                if digest != str(selected.get("body_digest") or ""):
                    raise ValueError("读取世界设定章节失败：正文完整性校验失败；系统已隔离该章节。请管理员恢复世界包。")
                body = raw.decode("utf-8-sig")
                return json_response({
                    "state": "ready",
                    "contract": contract,
                    "section_key": section_key,
                    "title": str(selected.get("title") or "世界设定"),
                    "body_digest": digest,
                    "blocks": [{"kind": "text", "text": body}],
                }, headers={"Cache-Control": "private, no-store"})
        except Exception as exc:
            return self._handle_error(exc)
    async def github_world_scan(self):
        """扫描公开 GitHub 仓库中的世界包 ZIP（仓库文件树 + Release 附件）。"""
        try:
            self._username()
            payload = await self._payload()
            parsed = parse_repo_url(str(payload.get("url") or ""))
            owner = parsed["owner"]
            repo = parsed["repo"]
            branch = str(payload.get("branch") or "").strip() or parsed["branch"]
            client = self.context.http_client
            if not branch:
                info = await fetch_json(
                    client, f"{GITHUB_API}/repos/{owner}/{repo}"
                )
                if not isinstance(info, dict):
                    raise GithubWorldError("仓库信息无效")
                branch = default_branch(info)
            tree = await fetch_json(
                client,
                f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1",
            )
            paths: list[str] = []
            if isinstance(tree, dict):
                tree_items = tree.get("tree")
                if isinstance(tree_items, list):
                    paths = [
                        str(item.get("path") or "")
                        for item in tree_items
                        if isinstance(item, dict)
                    ]
                if tree.get("truncated"):
                    raise GithubWorldError(
                        "仓库文件较多，GitHub 只返回了部分内容；请在仓库 Release 发布 ZIP 后重试"
                    )
            items: list[dict[str, Any]] = []
            for candidate in zip_candidates(paths):
                items.append(
                    {
                        "name": candidate["name"],
                        "path": candidate["path"],
                        "folder": candidate["folder"] or "/",
                        "source": "repo",
                        "url": raw_zip_url(owner, repo, branch, candidate["path"]),
                    }
                )
            releases = await fetch_json(
                client, f"{GITHUB_API}/repos/{owner}/{repo}/releases"
            )
            if isinstance(releases, list):
                for release in releases:
                    if not isinstance(release, dict):
                        continue
                    for asset in release_assets(release.get("assets")):
                        items.append(
                            {
                                "name": asset["name"],
                                "path": asset["name"],
                                "folder": "Release 附件",
                                "source": "release",
                                "url": asset["url"],
                            }
                        )
            return json_response(
                {"owner": owner, "repo": repo, "branch": branch, "items": items}
            )
        except Exception as exc:
            return self._handle_error(exc)
