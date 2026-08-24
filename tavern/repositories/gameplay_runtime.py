"""Transactional RC10 narrative-style and gameplay-module persistence."""

from __future__ import annotations

from ..database_support import *
from ..narrative_styles import (
    DEFAULT_NARRATIVE_STYLE,
    narrative_style_view,
    normalize_style_id,
    normalize_world_narrative_style,
    validate_custom_expectation,
)
from ..gameplay_runtime import (
    can_view_visibility, input_sha256, validate_character_resource_updates,
    validate_effect_updates, validate_item_instance_updates,
    validate_runtime_effect_instance_updates, validate_semantic_events,
    validate_state_payload,
)


class GameplayRuntimeRepositoryMixin:
    async def get_narrative_style(
        self,
        session_id: str,
        *,
        can_manage: bool = False,
        include_private: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._get_narrative_style,
            session_id,
            can_manage,
            include_private,
        )

    def _get_narrative_style(
        self,
        session_id: str,
        can_manage: bool,
        include_private: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                """
                SELECT s.id, ic.world_snapshot_json
                FROM sessions s
                LEFT JOIN instance_configs ic ON ic.session_id=s.id
                WHERE s.id=?
                """,
                (session_id,),
            ).fetchone()
            if session is None:
                raise DatabaseNotFoundError("副本不存在")
            world = json_load(session["world_snapshot_json"], {})
            world_style = normalize_world_narrative_style(
                world.get("narrative_style")
                if isinstance(world, Mapping)
                else {}
            )
            row = connection.execute(
                "SELECT * FROM session_narrative_styles WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return narrative_style_view(
                world_style.get("default_preset_id")
                or DEFAULT_NARRATIVE_STYLE,
                can_manage=can_manage,
                include_private=include_private,
                world_style=world_style,
            )
        return narrative_style_view(
            row["preset_id"],
            custom_expectation=str(row["custom_expectation"] or ""),
            revision=int(row["revision"] or 0),
            updated_at=str(row["updated_at"] or ""),
            source_world_style_sha=str(row["source_world_style_sha"] or ""),
            can_manage=can_manage,
            include_private=include_private,
            world_style=world_style,
        )

    async def set_narrative_style(
        self,
        session_id: str,
        preset_id: str,
        custom_expectation: str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        source_world_style_sha: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._set_narrative_style,
            session_id,
            preset_id,
            custom_expectation,
            int(expected_revision),
            actor_id,
            idempotency_key,
            source_world_style_sha,
        )

    def _set_narrative_style(
        self,
        session_id: str,
        preset_id: str,
        custom_expectation: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        source_world_style_sha: str,
    ) -> dict[str, Any]:
        requested = str(preset_id or "").strip().lower()
        normalized = normalize_style_id(requested)
        if requested != normalized:
            raise ValueError("叙事文风必须选择五个已声明档位之一")
        custom = validate_custom_expectation(custom_expectation)
        request_key = clean_text(idempotency_key, max_chars=160)
        if not request_key:
            raise ValueError("修改叙事文风需要防重复凭证")
        request_payload = {
            "preset_id": normalized,
            "custom_expectation": custom,
            "expected_revision": expected_revision,
        }
        digest = input_sha256(request_payload)
        operation_id = "narrative-style:" + hashlib.sha256(
            f"{session_id}\0{request_key}".encode("utf-8")
        ).hexdigest()[:24]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM gameplay_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_sha256"] or "") != digest:
                        raise DatabaseConflictError("相同防重复凭证已用于另一份文风修改")
                    result = json_load(receipt["result_json"], {})
                    result["replayed"] = True
                    connection.execute("COMMIT")
                    return result
                session = connection.execute(
                    """
                    SELECT s.state, ic.world_snapshot_json
                    FROM sessions s
                    LEFT JOIN instance_configs ic ON ic.session_id=s.id
                    WHERE s.id=?
                    """,
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                world = json_load(session["world_snapshot_json"], {})
                world_style = normalize_world_narrative_style(
                    world.get("narrative_style")
                    if isinstance(world, Mapping)
                    else {}
                )
                self._assert_session_writable(connection, session_id)
                current = connection.execute(
                    "SELECT * FROM session_narrative_styles WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                current_revision = int(current["revision"] or 0) if current else 0
                if current_revision != expected_revision:
                    raise DatabaseConflictError(
                        "叙事文风已有较新修改；草稿未丢失，请刷新比较后重试"
                    )
                now = utc_now()
                next_revision = current_revision + 1
                style_sha = clean_text(source_world_style_sha, max_chars=64)
                connection.execute(
                    """
                    INSERT INTO session_narrative_styles(
                        session_id, preset_id, custom_expectation, revision,
                        source_world_style_sha, updated_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        preset_id=excluded.preset_id,
                        custom_expectation=excluded.custom_expectation,
                        revision=excluded.revision,
                        source_world_style_sha=excluded.source_world_style_sha,
                        updated_by=excluded.updated_by,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        normalized,
                        custom,
                        next_revision,
                        style_sha,
                        clean_text(actor_id, max_chars=160),
                        now,
                    ),
                )
                result = narrative_style_view(
                    normalized,
                    custom_expectation=custom,
                    revision=next_revision,
                    updated_at=now,
                    source_world_style_sha=style_sha,
                    can_manage=True,
                    include_private=True,
                    world_style=world_style,
                )
                connection.execute(
                    """
                    INSERT INTO gameplay_receipts(
                        operation_id, session_id, module_id, intent,
                        idempotency_key, input_sha256, revision_before,
                        revision_after, result_json, created_at
                    ) VALUES (?, ?, 'narrative_style',
                              'session.narrative_style.save', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        request_key,
                        digest,
                        current_revision,
                        next_revision,
                        json_dump(result),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.narrative_style.save",
                    session_id,
                    {
                        "preset_before": str(current["preset_id"] or "") if current else DEFAULT_NARRATIVE_STYLE,
                        "preset_after": normalized,
                        "has_custom_expectation": bool(custom),
                        "revision_before": current_revision,
                        "revision_after": next_revision,
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{operation_id}:event",
                    type_="event:narrative-style.changed",
                    actor_ref=actor_id,
                    command_id=operation_id,
                    payload={
                        "title": "叙事文风已更新",
                        "summary": "新的对白与描写侧重会从下一次故事生成开始生效。",
                        "preset_id": normalized,
                        "has_custom_expectation": bool(custom),
                        "revision": next_revision,
                    },
                    visibility="public",
                    created_at=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def get_gameplay_states(
        self,
        session_id: str,
        module_id: str,
        *,
        viewer_role: str = "player",
    ) -> dict[str, Any]:
        return await self._run(
            self._get_gameplay_states,
            session_id,
            module_id,
            viewer_role,
        )

    def _get_gameplay_states(
        self,
        session_id: str,
        module_id: str,
        viewer_role: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM gameplay_states
                WHERE session_id=? AND module_id=?
                ORDER BY state_key
                """,
                (session_id, module_id),
            ).fetchall()
        items = []
        for row in rows:
            visibility = str(row["visibility"] or "public")
            if not can_view_visibility(viewer_role, visibility):
                continue
            item = json_load(row["payload_json"], {})
            items.append(
                {
                    "state_key": str(row["state_key"]),
                    "state": item,
                    "revision": int(row["revision"] or 0),
                    "updated_at": str(row["updated_at"] or ""),
                }
            )
        return {"module_id": module_id, "items": items, "count": len(items)}

    async def put_gameplay_state(
        self,
        session_id: str,
        module_id: str,
        state_key: str,
        payload: Mapping[str, Any],
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        intent: str,
        archive_current: bool = False,
    ) -> dict[str, Any]:
        return await self._run(
            self._put_gameplay_state,
            session_id,
            module_id,
            state_key,
            dict(payload),
            int(expected_revision),
            actor_id,
            idempotency_key,
            intent,
            bool(archive_current),
        )

    async def get_gameplay_state_revisions(
        self,
        session_id: str,
        targets: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], int]:
        return await self._run(
            self._get_gameplay_state_revisions,
            session_id,
            list(targets),
        )

    def _get_gameplay_state_revisions(
        self,
        session_id: str,
        targets: list[tuple[str, str]],
    ) -> dict[tuple[str, str], int]:
        session_key = clean_text(session_id, max_chars=160)
        if not session_key:
            raise ValueError("批量读取玩法 revision 需要副本")
        if len(targets) > 128:
            raise ValueError("单次最多读取 128 个玩法状态 revision")
        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for raw in targets:
            if (
                not isinstance(raw, (tuple, list))
                or len(raw) != 2
            ):
                raise ValueError("玩法 revision 目标必须是 module/state 二元组")
            module_id = clean_text(raw[0], max_chars=160)
            state_key = clean_text(raw[1], max_chars=160)
            if not module_id or not state_key:
                raise ValueError("玩法 revision 目标缺少 module 或 state")
            target = (module_id, state_key)
            if target not in seen:
                seen.add(target)
                unique.append(target)
        unique = sorted(unique)
        result = {target: 0 for target in unique}
        if not unique:
            return result
        predicates = " OR ".join(
            "(module_id=? AND state_key=?)" for _target in unique
        )
        values: list[Any] = [session_key]
        for module_id, state_key in unique:
            values.extend((module_id, state_key))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT module_id, state_key, revision
                FROM gameplay_states
                WHERE session_id=? AND ({predicates})
                """,
                tuple(values),
            ).fetchall()
        for row in rows:
            result[(str(row["module_id"]), str(row["state_key"]))] = int(
                row["revision"] or 0
            )
        return result

    async def get_gameplay_receipt(
        self,
        session_id: str,
        module_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._get_gameplay_receipt,
            session_id,
            module_id,
            idempotency_key,
        )

    def _get_gameplay_receipt(
        self,
        session_id: str,
        module_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        key = clean_text(idempotency_key, max_chars=160)
        if not key:
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT intent, revision_before, revision_after, result_json, created_at
                FROM gameplay_receipts
                WHERE session_id=? AND module_id=? AND idempotency_key=?
                """,
                (session_id, module_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "intent": str(row["intent"] or ""),
            "revision_before": int(row["revision_before"] or 0),
            "revision_after": int(row["revision_after"] or 0),
            "result": json_load(row["result_json"], {}),
            "created_at": str(row["created_at"] or ""),
        }

    def _put_gameplay_state(
        self,
        session_id: str,
        module_id: str,
        state_key: str,
        payload: dict[str, Any],
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
        intent: str,
        archive_current: bool = False,
    ) -> dict[str, Any]:
        key = clean_text(state_key, max_chars=160)
        request_key = clean_text(idempotency_key, max_chars=160)
        semantic_intent = clean_text(intent, max_chars=160)
        if not key or not request_key or not semantic_intent:
            raise ValueError("玩法写入需要状态键、语义动作和防重复凭证")
        normalized = validate_state_payload(module_id, payload)
        effect_updates = validate_effect_updates(
            module_id,
            normalized.pop("_effect_updates", []),
        )
        semantic_events = validate_semantic_events(
            module_id,
            normalized.pop("_semantic_events", []),
        )
        item_instance_updates = validate_item_instance_updates(
            module_id,
            normalized.pop("_item_instance_updates", []),
        )
        character_resource_updates = validate_character_resource_updates(
            module_id,
            normalized.pop("_character_resource_updates", []),
        )
        runtime_effect_instance_updates = validate_runtime_effect_instance_updates(
            module_id,
            normalized.pop("_runtime_effect_instance_updates", []),
        )
        private_runtime_updates = bool(
            item_instance_updates
            or character_resource_updates
            or runtime_effect_instance_updates
        )
        if (
            private_runtime_updates
            and semantic_intent != "tactical.phase.advance"
        ):
            raise ValueError("私有实例或资源更新只能由战术行动结算生成")
        request = {
            "module_id": module_id,
            "state_key": key,
            "payload": normalized,
            "expected_revision": expected_revision,
            "intent": semantic_intent,
            "effect_updates": effect_updates,
            "semantic_events": semantic_events,
            "item_instance_updates": item_instance_updates,
            "character_resource_updates": character_resource_updates,
            "runtime_effect_instance_updates": runtime_effect_instance_updates,
            "archive_current": bool(archive_current),
        }
        digest = input_sha256(request)
        operation_id = "gameplay:" + hashlib.sha256(
            f"{session_id}\0{module_id}\0{request_key}".encode("utf-8")
        ).hexdigest()[:24]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM gameplay_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_sha256"] or "") != digest:
                        raise DatabaseConflictError("相同防重复凭证已用于另一项玩法修改")
                    result = json_load(receipt["result_json"], {})
                    result["replayed"] = True
                    connection.execute("COMMIT")
                    return result
                self._assert_session_writable(connection, session_id)
                row = connection.execute(
                    """
                    SELECT revision, payload_json, visibility FROM gameplay_states
                    WHERE session_id=? AND module_id=? AND state_key=?
                    """,
                    (session_id, module_id, key),
                ).fetchone()
                current_revision = int(row["revision"] or 0) if row else 0
                if current_revision != expected_revision:
                    raise DatabaseConflictError(
                        "该玩法状态已有较新内容；当前修改未覆盖新版本，请刷新后重试"
                    )
                if private_runtime_updates:
                    current_state = (
                        json_load(row["payload_json"], {})
                        if row is not None
                        else {}
                    )
                    allowed_after = {
                        "resolve_players",
                        "victory",
                        "partial_success",
                        "retreat",
                        "negotiated",
                        "defeat_forward",
                        "aborted_by_host",
                    }
                    if (
                        str(current_state.get("phase") or "") != "locked"
                        or str(normalized.get("phase") or "") not in allowed_after
                    ):
                        raise DatabaseConflictError(
                            "装备消耗只能随已锁定的战术行动一起结算"
                        )
                next_revision = current_revision + 1
                now = utc_now()
                archive_state_key = ""
                if archive_current:
                    if row is None:
                        raise DatabaseConflictError("没有可归档的当前玩法状态")
                    archive_state_key = (
                        f"archive:{current_revision}:"
                        + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:12]
                    )
                    connection.execute(
                        """
                        INSERT INTO gameplay_states(
                            session_id, module_id, state_key, payload_json,
                            visibility, revision, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            session_id,
                            module_id,
                            archive_state_key,
                            str(row["payload_json"] or "{}"),
                            str(row["visibility"] or "party"),
                            now,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO gameplay_states(
                        session_id, module_id, state_key, payload_json,
                        visibility, revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, module_id, state_key) DO UPDATE SET
                        payload_json=excluded.payload_json,
                        visibility=excluded.visibility,
                        revision=excluded.revision,
                        updated_at=excluded.updated_at
                    """,
                    (
                        session_id,
                        module_id,
                        key,
                        json_dump(normalized),
                        normalized["visibility"],
                        next_revision,
                        now,
                    ),
                )
                affected_effects: list[dict[str, Any]] = []
                for index, effect in enumerate(effect_updates):
                    target_module = str(effect["module_id"])
                    target_key = str(effect["state_key"])
                    target_row = connection.execute(
                        """
                        SELECT revision, payload_json FROM gameplay_states
                        WHERE session_id=? AND module_id=? AND state_key=?
                        """,
                        (session_id, target_module, target_key),
                    ).fetchone()
                    target_before = int(target_row["revision"] or 0) if target_row else 0
                    expected_effect_revision = effect.get("expected_revision")
                    if (
                        expected_effect_revision is not None
                        and target_before != int(expected_effect_revision)
                    ):
                        raise DatabaseConflictError(
                            "关联玩法状态已有较新内容；本次结果已全部回滚"
                        )
                    target_after = target_before + 1
                    existing_state = (
                        json_load(target_row["payload_json"], {})
                        if target_row is not None
                        else {}
                    )
                    target_state = dict(effect["state"])
                    if effect.get("operation") == "merge":
                        target_state = {**existing_state, **target_state}
                    target_state = validate_state_payload(target_module, target_state)
                    connection.execute(
                        """
                        INSERT INTO gameplay_states(
                            session_id, module_id, state_key, payload_json,
                            visibility, revision, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, module_id, state_key) DO UPDATE SET
                            payload_json=excluded.payload_json,
                            visibility=excluded.visibility,
                            revision=excluded.revision,
                            updated_at=excluded.updated_at
                        """,
                        (
                            session_id,
                            target_module,
                            target_key,
                            json_dump(target_state),
                            target_state["visibility"],
                            target_after,
                            now,
                        ),
                    )
                    affected_effects.append(
                        {
                            "label": str(effect["label"]),
                            "module_id": target_module,
                            "revision_before": target_before,
                            "revision_after": target_after,
                        }
                    )
                    insert_session_event(
                        connection,
                        session_id=session_id,
                        event_id=f"{operation_id}:effect:{index}",
                        type_=f"event:{target_module}.changed",
                        actor_ref=actor_id,
                        command_id=operation_id,
                        payload={
                            "title": "关联玩法状态已更新",
                            "summary": str(effect["label"]),
                            "revision": target_after,
                        },
                        visibility=target_state["visibility"],
                        created_at=now,
                    )
                for item_update in item_instance_updates:
                    cursor = connection.execute(
                        """
                        UPDATE item_instances
                        SET quantity=?, durability=?, charges=?, updated_at=?
                        WHERE id=? AND session_id=?
                          AND owner_type=? AND owner_ref=? AND item_id=?
                          AND quantity=? AND durability=? AND charges=?
                        """,
                        (
                            int(item_update["quantity_after"]),
                            int(item_update["durability_after"]),
                            int(item_update["charges_after"]),
                            now,
                            str(item_update["instance_id"]),
                            session_id,
                            str(item_update["owner_type"]),
                            str(item_update["owner_ref"]),
                            str(item_update["item_id"]),
                            int(item_update["quantity_before"]),
                            int(item_update["durability_before"]),
                            int(item_update["charges_before"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseConflictError(
                            "装备状态已在锁定后改变；战术结算、关联效果与事件均已回滚，请刷新战况后重新锁定"
                        )
                for resource_update in character_resource_updates:
                    cursor = connection.execute(
                        """
                        UPDATE character_resources
                        SET current=?, updated_at=?
                        WHERE session_id=? AND character_id=?
                          AND resource_ref=? AND current=? AND maximum=?
                        """,
                        (
                            int(resource_update["current_after"]),
                            now,
                            session_id,
                            str(resource_update["participant_ref"]),
                            str(resource_update["resource_ref"]),
                            int(resource_update["current_before"]),
                            int(resource_update["maximum_before"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise DatabaseConflictError(
                            "能力资源已在锁定后改变；战术结算、关联效果与事件均已回滚，请刷新战况后重新锁定"
                        )
                for effect_update in runtime_effect_instance_updates:
                    if effect_update["operation"] == "create":
                        cursor = connection.execute(
                            """
                            INSERT INTO runtime_effect_instances(
                                id, session_id, target_ref, effect_ref,
                                source_ref, state_json, duration_json,
                                persistence_scope, status, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                            ON CONFLICT(id) DO NOTHING
                            """,
                            (
                                str(effect_update["instance_id"]),
                                session_id,
                                str(effect_update["target_ref"]),
                                str(effect_update["effect_ref"]),
                                str(effect_update["source_ref"]),
                                json_dump(effect_update["state"]),
                                json_dump(effect_update["duration"]),
                                str(effect_update["persistence_scope"]),
                                now,
                                now,
                            ),
                        )
                    else:
                        cursor = connection.execute(
                            """
                            UPDATE runtime_effect_instances
                            SET status=?, updated_at=?
                            WHERE id=? AND session_id=? AND target_ref=?
                              AND effect_ref=? AND source_ref=?
                              AND persistence_scope=? AND status=?
                            """,
                            (
                                str(effect_update["status_after"]),
                                now,
                                str(effect_update["instance_id"]),
                                session_id,
                                str(effect_update["target_ref"]),
                                str(effect_update["effect_ref"]),
                                str(effect_update["source_ref"]),
                                str(effect_update["persistence_scope"]),
                                str(effect_update["status_before"]),
                            ),
                        )
                    if cursor.rowcount != 1:
                        raise DatabaseConflictError(
                            "能力状态实例已在锁定后改变；战术结算、关联效果与事件均已回滚，请刷新战况后重新锁定"
                        )
                result = {
                    "module_id": module_id,
                    "state_key": key,
                    "state": normalized,
                    "revision": next_revision,
                    "updated_at": now,
                    "replayed": False,
                    "affected_effects": affected_effects,
                    "archive_state_key": archive_state_key,
                }
                if item_instance_updates:
                    result["affected_item_count"] = len(item_instance_updates)
                if character_resource_updates:
                    result["affected_resource_count"] = len(
                        character_resource_updates
                    )
                if runtime_effect_instance_updates:
                    result["affected_runtime_effect_count"] = len(
                        runtime_effect_instance_updates
                    )
                connection.execute(
                    """
                    INSERT INTO gameplay_receipts(
                        operation_id, session_id, module_id, intent,
                        idempotency_key, input_sha256, revision_before,
                        revision_after, result_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        module_id,
                        semantic_intent,
                        request_key,
                        digest,
                        current_revision,
                        next_revision,
                        json_dump(result),
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    semantic_intent,
                    key,
                    {
                        "module": module_id,
                        "revision_before": current_revision,
                        "revision_after": next_revision,
                        "visibility": normalized["visibility"],
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{operation_id}:event",
                    type_=f"event:{module_id}.changed",
                    actor_ref=actor_id,
                    command_id=operation_id,
                    payload={
                        "title": "跑团状态已更新",
                        "summary": "新的状态与回执已经写入，可在跑团现场查看。",
                        "module_id": module_id,
                        "state_key": key,
                        "revision": next_revision,
                    },
                    visibility=normalized["visibility"],
                    created_at=now,
                )
                if archive_state_key:
                    insert_session_event(
                        connection,
                        session_id=session_id,
                        event_id=f"{operation_id}:archive",
                        type_=f"event:{module_id}.runtime_archived",
                        actor_ref=actor_id,
                        command_id=operation_id,
                        payload={
                            "title": "上一场玩法已归档",
                            "summary": "终态和历史回执已保留，新的活动状态已开始。",
                            "revision": current_revision,
                        },
                        visibility=str(row["visibility"] or "party") if row else "party",
                        created_at=now,
                    )
                for index, semantic in enumerate(semantic_events):
                    insert_session_event(
                        connection,
                        session_id=session_id,
                        event_id=f"{operation_id}:semantic:{index}",
                        type_=f"event:{module_id}.{semantic['kind']}",
                        actor_ref=actor_id,
                        command_id=operation_id,
                        payload={
                            "title": str(semantic["label"]),
                            "summary": str(semantic["summary"]),
                            "revision": next_revision,
                            **dict(semantic.get("details") or {}),
                        },
                        visibility=str(semantic["visibility"]),
                        created_at=now,
                    )
                if item_instance_updates:
                    self._emit_item_visual_event_locked(
                        connection,
                        session_id=session_id,
                        operation_id=operation_id,
                        action="tactical-use",
                        created_at=now,
                    )
                if character_resource_updates or runtime_effect_instance_updates:
                    insert_session_event(
                        connection,
                        session_id=session_id,
                        event_id=f"{operation_id}:capability-state",
                        type_="event:actor.state_changed",
                        actor_ref=actor_id,
                        command_id=operation_id,
                        payload={
                            "title": "角色能力状态已更新",
                            "summary": "作者明示的角色资源或状态效果已经结算。",
                            "revision": next_revision,
                        },
                        visibility="party",
                        created_at=now,
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise


__all__ = ["GameplayRuntimeRepositoryMixin"]
