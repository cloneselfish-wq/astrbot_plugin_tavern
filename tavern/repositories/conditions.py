from __future__ import annotations

from .rules_support import *


class ConditionsRepositoryMixin:
    @staticmethod
    def _ai_check_actor_locked(
        connection: sqlite3.Connection,
        session_id: str,
        actor_ref: str,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT a.*, i.frozen_profile_json,
                   i.status AS action_status
            FROM actors a
            JOIN ai_companion_instances i ON i.actor_id=a.id
            WHERE a.session_id=? AND a.actor_kind='ai_companion'
            ORDER BY a.created_at, a.id
            """,
            (session_id,),
        ).fetchall()
        matches = [
            item
            for item in rows
            if (
                "public:actor:"
                + hashlib.sha256(
                    str(item["id"]).encode("utf-8")
                ).hexdigest()[:12].upper()
            )
            == str(actor_ref)
        ]
        if len(matches) > 1:
            raise DatabaseConflictError(
                "AI 队友公开引用发生冲突，已停止规则结算"
            )
        if not matches:
            return None
        actor = matches[0]
        if (
            str(actor["status"]) != "active"
            or str(actor["action_status"]) == "retired"
        ):
            return None
        return actor

    async def resolve_action_intent(
        self,
        session_id: str,
        intent: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = True,
        operation_id: str = "",
        actor_id: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._resolve_action_intent,
            session_id,
            dict(intent),
            dict(context or {}),
            bool(dry_run),
            operation_id,
            actor_id,
        )

    def _resolve_action_intent(
        self,
        session_id: str,
        intent: dict[str, Any],
        context: dict[str, Any],
        dry_run: bool,
        operation_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        operation_id = clean_text(operation_id, max_chars=240) or new_id("op")
        request_hash = content_hash(
            {"session_id": session_id, "intent": intent, "context": context}
        )
        with self._connect() as connection:
            if not dry_run:
                connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM operation_commits WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if existing:
                    if existing["input_hash"] != request_hash:
                        raise DatabaseConflictError("操作 ID 已被不同请求使用")
                    result = json_load(existing["result_json"], {})
                    if not dry_run:
                        connection.execute("COMMIT")
                    return result

                row = connection.execute(
                    """
                    SELECT s.*, ic.world_snapshot_json, ic.world_revision
                    FROM sessions s
                    JOIN instance_configs ic ON ic.session_id=s.id
                    WHERE s.id=?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在或没有冻结世界配置")
                if not dry_run:
                    self._assert_session_writable(connection, session_id)
                world = json_load(row["world_snapshot_json"], {})
                actor_ref = str(intent.get("actor_ref") or "")
                capability_rows = connection.execute(
                    """
                    SELECT * FROM actor_capability_instances
                    WHERE session_id=? AND actor_ref=? AND available=1
                    ORDER BY created_at ASC
                    """,
                    (session_id, actor_ref),
                ).fetchall()
                participant_id = (
                    actor_ref.split(":", 1)[1]
                    if actor_ref.startswith("character:") else ""
                )
                actor_state_row = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id=? AND participant_id=?
                    """,
                    (session_id, participant_id),
                ).fetchone() if participant_id else None
                ai_actor_state_row = None
                if not participant_id and actor_ref.startswith("public:actor:"):
                    ai_actor_state_row = self._ai_check_actor_locked(
                        connection,
                        session_id,
                        actor_ref,
                    )
                    if ai_actor_state_row is None:
                        raise DatabaseNotFoundError(
                            "AI 队友不存在、已退役或不属于当前副本"
                        )
                actor_runtime_state = json_load(
                    (
                        actor_state_row["state_json"]
                        if actor_state_row is not None
                        else ai_actor_state_row["state_json"]
                        if ai_actor_state_row is not None
                        else "{}"
                    ),
                    {},
                )
                actor_runtime_state = (
                    dict(actor_runtime_state)
                    if isinstance(actor_runtime_state, Mapping) else {}
                )
                actor_context = context.get("actor")
                actor_context = dict(actor_context) if isinstance(actor_context, Mapping) else {}
                runtime_refs = actor_runtime_state.get("refs", {})
                runtime_refs = runtime_refs if isinstance(runtime_refs, Mapping) else {}
                provided_refs = actor_context.get("refs", {})
                provided_refs = provided_refs if isinstance(provided_refs, Mapping) else {}
                actor_context["refs"] = {**runtime_refs, **provided_refs}
                if "capabilities" not in actor_context:
                    actor_context["capabilities"] = [
                        {
                            "instance_id": cap["id"],
                            "capability_ref": cap["capability_ref"],
                            "source_ref": cap["source_ref"],
                            "available": bool(cap["available"]),
                            **json_load(cap["state_json"], {}),
                        }
                        for cap in capability_rows
                    ]
                current_state = json_load(row["world_state_json"], {})
                run_context = {
                    **context,
                    "actor": actor_context,
                    "session": {
                        "refs": {
                            "custom:session_state": row["state"],
                            "counter:turn": int(row["turn_no"]),
                        }
                    },
                    "state": {
                        "world": current_state,
                        "actor": actor_runtime_state,
                    },
                }
                snapshot = connection.execute(
                    """
                    SELECT id FROM world_snapshots
                    WHERE world_id=? AND world_revision=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (row["world_id"], int(row["world_revision"])),
                ).fetchone()
                snapshot_id = str(snapshot["id"] if snapshot else "")
                runtime = RuleRuntime(world)
                result = runtime.resolve_action_intent(
                    intent, run_context, operation_id=operation_id,
                    world_snapshot_id=snapshot_id, dry_run=dry_run,
                )
                if dry_run:
                    return result

                now = utc_now()
                resolved_state = result["state"]
                resolved_state = resolved_state if isinstance(resolved_state, Mapping) else {}
                resolved_world_state = resolved_state.get("world", current_state)
                resolved_actor_state = resolved_state.get("actor", actor_runtime_state)
                if not isinstance(resolved_actor_state, Mapping):
                    raise TypeError("角色运行态结算结果不是对象")
                actor_state_changed = (
                    json_dump(resolved_actor_state)
                    != json_dump(actor_runtime_state)
                )
                connection.execute(
                    """
                    UPDATE sessions SET world_state_json=?, revision=revision+1,
                        updated_at=? WHERE id=?
                    """,
                    (json_dump(resolved_world_state), now, session_id),
                )
                if actor_state_row:
                    connection.execute(
                        """
                        UPDATE character_runtime_states
                        SET state_json=?, revision=revision+1, updated_at=?
                        WHERE id=?
                        """,
                        (json_dump(resolved_actor_state), now, actor_state_row["id"]),
                    )
                elif ai_actor_state_row is not None and actor_state_changed:
                    cursor = connection.execute(
                        """
                        UPDATE actors SET state_json=?, revision=revision+1,
                            updated_at=?
                        WHERE id=? AND session_id=? AND revision=?
                          AND actor_kind='ai_companion'
                          AND status NOT IN ('retired','archived')
                        """,
                        (
                            json_dump(resolved_actor_state),
                            now,
                            ai_actor_state_row["id"],
                            session_id,
                            int(ai_actor_state_row["revision"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseConflictError("AI 队友状态已更新")
                    event_digest = hashlib.sha256(
                        f"{operation_id}:ai-actor-state".encode("utf-8")
                    ).hexdigest()[:32]
                    event_revision = next_session_event_seq(
                        connection, session_id
                    )
                    insert_session_event(
                        connection,
                        session_id=session_id,
                        event_id=f"event:actor.state:{event_digest}",
                        type_="event:actor.state_changed",
                        payload={
                            "title": "队友状态更新",
                            "summary": "一名队友的可见状态与资源已经更新。",
                            "affected_modules": ["party"],
                            "kind": "party",
                            "revision": event_revision,
                        },
                        visibility="public",
                        created_at=now,
                    )
                for operation in result.get("planned_operations", []):
                    if not isinstance(operation, Mapping):
                        continue
                    op = str(operation.get("op") or "")
                    target_ref = str(operation.get("target_ref") or "")
                    if target_ref.startswith("capability:") and op == "grant_reference":
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO actor_capability_instances(
                                id, session_id, actor_ref, capability_ref,
                                definition_version, source_ref, state_json,
                                persistence_scope, available, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 1, ?, '{}', ?, 1, ?, ?)
                            """,
                            (
                                new_id("capability_instance"), session_id, actor_ref,
                                target_ref, str(operation.get("source_ref") or "rule"),
                                str(operation.get("persistence_scope") or "campaign"), now, now,
                            ),
                        )
                    elif target_ref.startswith("capability:") and op == "revoke_reference":
                        connection.execute(
                            """
                            UPDATE actor_capability_instances SET available=0, updated_at=?
                            WHERE session_id=? AND actor_ref=? AND capability_ref=?
                            """,
                            (now, session_id, actor_ref, target_ref),
                        )
                receipt = result["receipt"]
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?)
                    """,
                    (
                        operation_id, session_id, request_hash, json_dump(result),
                        json_dump({
                            "world_state": current_state,
                            "actor_state": actor_runtime_state,
                            "session_revision": int(row["revision"]),
                        }),
                        now, now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO resolution_receipts(
                        receipt_id, operation_id, session_id, world_snapshot_id,
                        content_hash, receipt_json, public_projection_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt["receipt_id"], operation_id, session_id, snapshot_id,
                        receipt["content_hash"], json_dump(receipt),
                        json_dump({"outcome_id": receipt["outcome_id"],
                                   "narrative_projection": receipt["narrative_projection"]}), now,
                    ),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "rules.action_commit",
                    operation_id, {"receipt_id": receipt["receipt_id"]},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                if not dry_run:
                    connection.execute("ROLLBACK")
                raise

    async def list_actor_capabilities(
        self, session_id: str, actor_ref: str, *, available_only: bool = True
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_actor_capabilities,
            session_id,
            actor_ref,
            bool(available_only),
        )

    def _list_actor_capabilities(
        self, session_id: str, actor_ref: str, available_only: bool
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actor_capability_instances
                WHERE session_id=? AND actor_ref=?
                  AND (?=0 OR available=1)
                ORDER BY created_at ASC, capability_ref ASC
                """,
                (session_id, actor_ref, int(available_only)),
            ).fetchall()
            return [
                {
                    "instance_id": row["id"],
                    "capability_ref": row["capability_ref"],
                    "definition_version": row["definition_version"],
                    "source_ref": row["source_ref"],
                    "state": json_load(row["state_json"], {}),
                    "persistence_scope": row["persistence_scope"],
                    "available": bool(row["available"]),
                }
                for row in rows
            ]

    async def check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        *,
        proposed_advantages: Sequence[str] = (),
        proposed_disadvantages: Sequence[str] = (),
        locked_advantages: Sequence[str] = (),
        locked_disadvantages: Sequence[str] = (),
    ) -> dict[str, Any]:
        return await self._run(
            self._check_context,
            session_id,
            user_id,
            stat,
            list(proposed_advantages),
            list(proposed_disadvantages),
            list(locked_advantages),
            list(locked_disadvantages),
        )

    def _check_context(
        self,
        session_id: str,
        user_id: str,
        stat: str,
        proposed_advantages: list[str],
        proposed_disadvantages: list[str],
        locked_advantages: list[str],
        locked_disadvantages: list[str],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT pt.id AS participant_id, pt.character_name,
                       pt.display_name, ccv.profile_json,
                       crs.state_json, s.world_state_json
                FROM participants pt
                JOIN sessions s ON s.id = pt.session_id
                LEFT JOIN character_card_versions ccv
                  ON ccv.id = pt.character_version_id
                LEFT JOIN character_runtime_states crs
                  ON crs.participant_id = pt.id
                WHERE pt.session_id = ? AND pt.group_user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
            if row:
                participant_id = str(row["participant_id"])
                profile = json_load(row["profile_json"], {})
                runtime = json_load(row["state_json"], {})
                world_state = json_load(row["world_state_json"], {})
            else:
                actor = self._ai_check_actor_locked(
                    connection,
                    session_id,
                    user_id,
                )
                session = connection.execute(
                    "SELECT world_state_json FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if actor is None or session is None:
                    raise DatabaseNotFoundError(
                        "当前行动角色没有权威检定资料"
                    )
                participant_id = ""
                profile = json_load(actor["frozen_profile_json"], {})
                runtime = json_load(actor["state_json"], {})
                world_state = json_load(session["world_state_json"], {})
            profile = profile if isinstance(profile, Mapping) else {}
            runtime = runtime if isinstance(runtime, Mapping) else {}
            world_state = (
                world_state if isinstance(world_state, Mapping) else {}
            )
            allowed_advantages: set[str] = set()
            allowed_disadvantages: set[str] = set()
            raw_specialties = profile.get("specialties")
            specialties = (
                raw_specialties
                if isinstance(raw_specialties, list)
                else str(raw_specialties or "").replace("，", ",").split(",")
            )
            for specialty in specialties:
                text = clean_text(specialty, max_chars=80)
                if text:
                    allowed_advantages.add(f"专长：{text}")
            for source in runtime.get("advantage_sources", []):
                text = clean_text(source, max_chars=120)
                if text:
                    allowed_advantages.add(text)
            for status in runtime.get("statuses", []):
                if not isinstance(status, Mapping):
                    continue
                affects = status.get("affects")
                affects = affects if isinstance(affects, list) else []
                reference = str(stat or "").casefold()
                if affects and not any(
                    reference in str(item).casefold()
                    or str(item).casefold() in reference
                    for item in affects
                ):
                    continue
                name = clean_text(status.get("name"), max_chars=100)
                if name:
                    allowed_disadvantages.add(f"状态：{name}")
            modifiers = world_state.get("check_modifiers")
            if isinstance(modifiers, Mapping):
                for source in modifiers.get("advantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_advantages.add(text)
                for source in modifiers.get("disadvantages", []):
                    text = clean_text(source, max_chars=120)
                    if text:
                        allowed_disadvantages.add(text)

            # Option generation may disclose an environmental or prepared
            # source before the check. It is accepted only when the source can
            # be matched to an already persisted character/scene fact; the
            # model cannot manufacture a bonus merely by writing it twice.
            trusted_texts: list[str] = []

            def collect_trusted(value: Any) -> None:
                if isinstance(value, Mapping):
                    for nested in value.values():
                        collect_trusted(nested)
                elif isinstance(value, Sequence) and not isinstance(
                    value,
                    (str, bytes),
                ):
                    for nested in value:
                        collect_trusted(nested)
                else:
                    text = truncate_text(value, max_chars=500)
                    if len(text) >= 2:
                        trusted_texts.append(text.casefold())

            collect_trusted(profile)
            collect_trusted(runtime)
            collect_trusted(
                {
                    "location": world_state.get("location"),
                    "time": world_state.get("time"),
                    "scene_summary": world_state.get("scene_summary"),
                    "facts": world_state.get("facts"),
                    "inventory": world_state.get("inventory"),
                    "check_modifiers": world_state.get("check_modifiers"),
                }
            )

            def source_is_proven(source: str) -> bool:
                probe = source
                for prefix in (
                    "专长：",
                    "装备：",
                    "情报：",
                    "环境：",
                    "准备：",
                    "状态：",
                ):
                    if probe.startswith(prefix):
                        probe = probe[len(prefix) :]
                        break
                normalized = " ".join(probe.casefold().split())
                if len(normalized) < 2:
                    return False
                return any(
                    normalized in fact
                    or (
                        len(fact) >= 4
                        and len(normalized) >= 4
                        and fact in normalized
                    )
                    for fact in trusted_texts
                )

            for raw in locked_advantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_advantages.add(source)
            for raw in locked_disadvantages:
                source = clean_text(raw, max_chars=120)
                if source and source_is_proven(source):
                    allowed_disadvantages.add(source)
            assist = (
                connection.execute(
                    """
                    SELECT at.*, source.character_name AS source_name,
                           source.display_name AS source_display
                    FROM assist_tokens at
                    JOIN participants source
                      ON source.id = at.source_participant_id
                    WHERE at.session_id = ?
                      AND at.target_participant_id = ?
                      AND at.status = 'active'
                      AND (at.stat = '' OR lower(at.stat) = lower(?))
                    ORDER BY at.created_at
                    LIMIT 1
                    """,
                    (session_id, participant_id, stat),
                ).fetchone()
                if participant_id
                else None
            )
            assist_token_id = ""
            if assist:
                assist_token_id = str(assist["id"])
                allowed_advantages.add(
                    "协助："
                    + str(
                        assist["source_name"]
                        or assist["source_display"]
                        or "队友"
                    )
                )
            proposed_adv = {
                clean_text(item, max_chars=120)
                for item in [*proposed_advantages, *locked_advantages]
                if clean_text(item, max_chars=120)
            }
            proposed_dis = {
                clean_text(item, max_chars=120)
                for item in [*proposed_disadvantages, *locked_disadvantages]
                if clean_text(item, max_chars=120)
            }
            advantages = sorted(
                allowed_advantages & proposed_adv
                | {
                    item
                    for item in allowed_advantages
                    if item.startswith("协助：")
                }
            )
            disadvantages = sorted(
                allowed_disadvantages & proposed_dis
                | {
                    item
                    for item in allowed_disadvantages
                    if item.startswith("状态：")
                }
            )
            return {
                "participant_id": participant_id,
                "advantages": advantages[:8],
                "disadvantages": disadvantages[:8],
                "assist_token_id": assist_token_id,
                "rejected_advantages": sorted(
                    proposed_adv - allowed_advantages
                ),
                "rejected_disadvantages": sorted(
                    proposed_dis - allowed_disadvantages
                ),
            }
