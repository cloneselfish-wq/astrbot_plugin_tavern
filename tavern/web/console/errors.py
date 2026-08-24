from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)


class ConsoleErrorMethods:
    async def session_context_compile(self):
        """上下文编译调试快照（只读，不调用模型）。"""
        try:
            self._username()
            session_id = str(request.query.get("session_id", "") or "")
            if not session_id:
                raise ValueError("缺少 session_id")
            session = await self.database.get_session(session_id)
            turn = await self.database.get_turn_status(session_id)
            events = await self.database.recent_events(session_id, 20)
            roster = await self.database.list_roster(session_id)
            memories = await self.database.list_memories(
                session_id, "", 50, include_invalidated=False
            )
            instance = await self.database.get_instance_config(session_id)
            snapshot = instance.get("world_snapshot") or {}
            config = TavernConfig.from_mapping(self.plugin_config)
            world_state = session.get("world_state") or {}
            return json_response(
                {
                    "session": {
                        "id": session.get("id"),
                        "state": session.get("state"),
                        "turn_no": session.get("turn_no"),
                        "revision": session.get("revision"),
                    },
                    "turn": turn,
                    "location": (world_state or {}).get("location", ""),
                    "recent_events": [
                        {
                            "role": e.get("role"),
                            "content": str(e.get("content") or "")[:200],
                            "created_at": e.get("created_at"),
                        }
                        for e in events
                        if isinstance(e, dict)
                    ],
                    "roster_summary": [
                        {
                            "character_name": r.get("character_name") or r.get("display_name"),
                            "card_status": r.get("card_status"),
                            "ready": bool(r.get("ready")),
                        }
                        for r in roster
                        if isinstance(r, dict)
                    ],
                    "memory_count": len(memories),
                    "world_snapshot": {
                        "name": snapshot.get("name"),
                        "slug": snapshot.get("slug"),
                        "revision": snapshot.get("revision"),
                    },
                    "prompt_budget": {
                        "recent_turns": config.recent_turns,
                        "memory_limit": config.memory_limit,
                        "max_input_chars": config.max_input_chars,
                        "max_output_chars": config.max_output_chars,
                        "temperature": config.temperature,
                        "max_tokens": config.max_tokens,
                        "two_phase_checks": config.two_phase_checks,
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_inject_fact(self):
        """向受控世界状态注入一条事实（安全快照 + 审计 + 修订号）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            fact = str(payload.get("fact") or "").strip()
            if not session_id:
                raise ValueError("缺少 session_id")
            await self._require_dm_capability(session_id, user)
            if not fact:
                raise ValueError("fact 不能为空")
            if len(fact) > 2000:
                raise ValueError("fact 过长（上限 2000 字）")
            session = await self.database.get_session(session_id)
            world_state = dict(session.get("world_state") or {})
            facts = list(world_state.get("facts") or [])
            facts = [item for item in facts if isinstance(item, (str, dict))]
            existing = {
                (
                    str(item.get("text") or item.get("content") or item.get("fact") or "")
                    if isinstance(item, dict)
                    else str(item)
                )
                for item in facts
            }
            added = fact not in existing
            if added:
                facts.append(
                    {
                        "text": fact,
                        "round_no": int(session.get("turn_no") or 0),
                        "time": str(world_state.get("time") or ""),
                    }
                )
                world_state["facts"] = facts[-500:]
                await self.database.save_manual_state(
                    session_id,
                    world_state,
                    int(session["revision"]),
                    self._actor(),
                )
            await self.database.write_audit(
                session_id,
                self._actor(),
                "world.fact.inject",
                "",
                {"fact": fact[:200], "added": added},
            )
            await self.broker.publish(
                {"type": "session", "action": "inject_fact", "session_id": session_id}
            )
            return json_response(
                {
                    "session_id": session_id,
                    "fact": fact,
                    "added": added,
                    "fact_count": len(world_state.get("facts") or []),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_apply_effect(self):
        """校验并干跑一批声明式操作（默认不落库；commit=true 仅写审计）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            operations = payload.get("operations")
            if not session_id:
                raise ValueError("缺少 session_id")
            if not isinstance(operations, list) or not operations:
                raise ValueError("operations 必须是操作数组")
            instance = await self.database.get_instance_config(session_id)
            world = instance.get("world_snapshot") or {}
            runtime = self._cached_rule_runtime(world)
            validated = runtime.operations.validate(operations)
            session = await self.database.get_session(session_id)
            state = {"world": dict(session.get("world_state") or {})}
            _, changes, narrative = runtime.operations.apply(
                validated, state, dry_run=True
            )
            commit = bool(payload.get("commit"))
            if commit:
                await self._require_dm_capability(session_id, user)
                summary = "；".join(
                    str(change.get("path") or change.get("op") or "")
                    for change in changes[:10]
                )
                await self.database.write_audit(
                    session_id,
                    self._actor(),
                    "world.effect.dry_run_commit",
                    "",
                    {
                        "operation_count": len(validated),
                        "changes": summary[:500],
                    },
                )
            return json_response(
                {
                    "validated": validated,
                    "changes": changes,
                    "narrative": narrative,
                    "committed": commit,
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def session_advance_clock(self):
        """推进场景时钟（校验 + 审计 + 修订）。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "")
            clock_id = str(payload.get("clock_id") or "")
            segments = payload.get("segments")
            if not session_id or not clock_id:
                raise ValueError("缺少 session_id 或 clock_id")
            await self._require_dm_capability(session_id, user)
            try:
                delta = int(segments)
            except (TypeError, ValueError):
                raise ValueError("segments 必须是整数")
            result = await self.database.advance_scene_clock(
                session_id,
                clock_id,
                delta,
                self._actor(),
                str(payload.get("note") or ""),
            )
            await self.broker.publish(
                {"type": "session", "action": "advance_clock", "session_id": session_id}
            )
            return json_response({"clock": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_pacing_preview(self):
        """生成安全剧情节奏计划，不修改副本状态。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            action = str(payload.get("action") or "").strip()
            if not session_id or not action:
                raise ValueError("缺少 session_id 或 action")
            await self._require_dm_capability(session_id, user)
            expected = payload.get("expected_session_revision")
            expected_revision = None if expected in (None, "") else int(expected)
            plan = await self.database.preview_story_pacing(
                session_id=session_id,
                action=action,
                target_ref=str(payload.get("target_ref") or ""),
                expected_session_revision=expected_revision,
                actor_id=self._actor(),
                source="web_console",
                reason=str(payload.get("reason") or "WebUI 剧情节奏预览"),
            )
            return json_response({"plan": plan})
        except Exception as exc:
            return self._handle_error(exc)

    async def session_pacing_commit(self):
        """提交已确认的剧情节奏计划并发布副本刷新事件。"""
        try:
            user = self._username()
            payload = await self._payload()
            session_id = str(payload.get("session_id") or "").strip()
            plan_id = str(payload.get("plan_id") or "").strip()
            preview_hash = str(payload.get("preview_hash") or "").strip()
            idempotency_key = str(
                payload.get("idempotency_key") or f"web:pacing:{uuid.uuid4().hex}"
            ).strip()
            if not session_id or not plan_id or not preview_hash:
                raise ValueError("缺少 session_id、plan_id 或 preview_hash")
            await self._require_dm_capability(session_id, user)
            result = await self.database.commit_story_pacing(
                session_id=session_id,
                plan_id=plan_id,
                preview_hash=preview_hash,
                expected_session_revision=int(
                    payload.get("expected_session_revision")
                ),
                idempotency_key=idempotency_key,
                actor_id=self._actor(),
                source="web_console",
                reason=str(payload.get("reason") or "WebUI 确认剧情节奏操作"),
            )
            await self.broker.publish(
                {
                    "type": "session",
                    "action": "story_pacing",
                    "session_id": session_id,
                }
            )
            return json_response({"result": result})
        except Exception as exc:
            return self._handle_error(exc)

    async def extensions(self):
        """已注册扩展点清单（逐项隔离并保证响应可 JSON 序列化）。"""
        try:
            self._username()
            items, errors = _extension_catalog(self._extension_registry)
            return json_response(
                {
                    "kinds": items,
                    "total": sum(len(names) for names in items.values()),
                    "errors": errors,
                    "partial": bool(errors),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def hook_events(self):
        """可订阅事件目录与当前订阅数（逐字段容错）。"""
        try:
            self._username()
            subscriptions, errors = _hook_catalog(self.hooks)
            return json_response(
                {
                    "supported": _safe(lambda: sorted(HOOK_EVENTS), []),
                    "subscriptions": subscriptions,
                    "subscribed_count": sum(subscriptions.values()),
                    "errors": errors,
                    "partial": bool(errors),
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def meta_capabilities(self):
        """运行时自描述能力清单（逐字段容错）。"""
        try:
            principal = self._web_principal()
            return json_response(
                {
                    "internal_world_model_revision": _safe(
                        lambda: WORLD_SCHEMA_VERSION, 0
                    ),
                    "elemental_contract_version": _safe(
                        lambda: parse_elemental({})["version"],
                        "1.0.0-rc10",
                    ),
                    "resolution_modes": _safe(
                        lambda: sorted(RESOLUTION_MODES), []
                    ),
                    "operation_types": _safe(
                        lambda: sorted(OPERATION_TYPES), []
                    ),
                    "persistence_scopes": _safe(
                        lambda: sorted(PERSISTENCE_SCOPES), []
                    ),
                    "extension_kinds": _safe(
                        lambda: sorted(ExtensionRegistry._KINDS), []
                    ),
                    "hook_events": _safe(lambda: sorted(HOOK_EVENTS), []),
                    "platforms": _safe(capability_matrix, []),
                    "delivery_mode": "plain_text_with_persistent_fallback",
                    "web_permissions": {
                        "is_admin": principal["is_admin"],
                        "can_author": principal["capabilities"]["author"],
                        "can_install_worlds": principal["capabilities"][
                            "world_install"
                        ],
                        "role_source": principal["role_source"],
                    },
                }
            )
        except Exception as exc:
            return self._handle_error(exc)

    async def events(self):
        try:
            self._username()

            async def stream():
                async for item in self.broker.subscribe():
                    yield "data: " + json.dumps(
                        item,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ) + "\n\n"

            return stream_response(stream())
        except Exception as exc:
            return self._handle_error(exc)
