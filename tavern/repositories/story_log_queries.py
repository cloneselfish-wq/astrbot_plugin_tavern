from __future__ import annotations

from .story_support import *
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    canonical_narrative_json,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
)


class StoryLogQueriesRepositoryMixin:
    def _commit_turn_sync(
        self,
        session_id: str,
        expected_revision: int,
        player_id: str,
        player_user_id: str,
        player_name: str,
        player_input: str,
        narrative: str,
        narrative_document: dict[str, Any],
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        check_payload: dict[str, Any],
        model_payload: dict[str, Any],
        director_note: str,
        auto_snapshot_interval: int,
        store_model_payload: bool,
        workflow: dict[str, Any],
        actor_kind: str = "human",
        actor_id: str = "",
        operation_id: str = "",
        operation_result: dict[str, Any] | None = None,
        item_ops: list[dict[str, Any]] | None = None,
        economy_ops: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        document = parse_narrative_document(
            narrative_document,
            dialogue_expected=False,
        )
        document_text = narrative_document_to_plain_text(document)
        if document_text != str(narrative or ""):
            raise ValueError(
                "NarrativeDocument 与提交故事正文不一致"
            )
        document_json = canonical_narrative_json(document)
        document_text_hash = narrative_text_sha256(document)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["revision"] != expected_revision:
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("会话不在运行状态")
                if operation_id:
                    operation_row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if operation_row is None:
                        raise DatabaseConflictError("本轮操作回执不存在，禁止提交")
                    if str(operation_row["status"] or "") != "ready_to_commit":
                        raise DatabaseConflictError(
                            "本轮操作已取消、完成或进入恢复状态，禁止提交迟到结果"
                        )
                world_row = connection.execute(
                    "SELECT * FROM worlds WHERE id = ?",
                    (session["world_id"],),
                ).fetchone()
                if world_row is None:
                    raise DatabaseNotFoundError("会话世界不存在")
                world_data = {
                    "rules": json_load(world_row["rules_json"], {}),
                    "system_prompt": str(world_row["system_prompt"] or ""),
                    "opening_scene": str(world_row["opening_scene"] or ""),
                    "name": str(world_row["name"] or ""),
                    "slug": str(world_row["slug"] or ""),
                }
                squad = [
                    self._participant(item)
                    for item in connection.execute(
                        """
                        SELECT * FROM participants
                        WHERE session_id = ?
                          AND participation_status IN
                              ('active', 'standby', 'away')
                        """,
                        (session_id,),
                    ).fetchall()
                ]

                enabled_ids = {
                    str(row["user_id"])
                    for row in connection.execute(
                        """
                        SELECT user_id FROM players
                        WHERE session_id = ? AND enabled = 1
                        """,
                        (session_id,),
                    ).fetchall()
                }
                ai_actor_rows = connection.execute(
                    """
                    SELECT a.id FROM actors a
                    JOIN ai_companion_instances i ON i.actor_id=a.id
                    WHERE a.session_id=? AND a.actor_kind='ai_companion'
                      AND a.status='active' AND i.status<>'retired'
                    """,
                    (session_id,),
                ).fetchall()
                enabled_ids.update(
                    "public:actor:"
                    + hashlib.sha256(
                        str(row["id"]).encode("utf-8")
                    ).hexdigest()[:12].upper()
                    for row in ai_actor_rows
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=enabled_ids,
                )
                if player_user_id not in turn_state["order"]:
                    if turn_state["order"]:
                        raise InvalidTransitionError(
                            "该玩家尚未加入当前回合队列"
                        )
                    turn_state, _ = join_turn(
                        turn_state,
                        player_user_id,
                    )
                if turn_state["current_user_id"] != player_user_id:
                    status = self._turn_status_for(
                        connection,
                        session_id,
                        stored_state,
                    )
                    current_name = str(status.get("current_name") or "").strip()
                    if not current_name:
                        raise InvalidTransitionError(
                            "提交行动失败：当前行动者缺少可公开显示的名称；"
                            "系统没有写入故事或推进回合，请主持人先修复阵容名称"
                        )
                    raise InvalidTransitionError(f"当前轮到「{current_name}」")
                acting_round = turn_state["round_no"]
                group_decision = workflow.get("group_decision")
                preserves_action_right = isinstance(
                    group_decision,
                    Mapping,
                )
                next_turn_state = (
                    dict(turn_state)
                    if preserves_action_right
                    else advance_turn(
                        turn_state,
                        player_user_id,
                    )
                )
                if (
                    not preserves_action_right
                    and next_turn_state["round_no"] > acting_round
                ):
                    pending_rows = connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ? AND card_status = 'approved'
                          AND participation_status = 'active'
                          AND joined_round <= ?
                        ORDER BY created_at
                        """,
                        (session_id, next_turn_state["round_no"]),
                    ).fetchall()
                    for pending in pending_rows:
                        pending_user_id = str(pending["group_user_id"])
                        if pending_user_id not in next_turn_state["order"]:
                            next_turn_state, _ = join_turn(
                                next_turn_state,
                                pending_user_id,
                            )
                persisted_world_state = embed_turn_state(
                    public_world_state(world_state),
                    next_turn_state,
                )
                turn_progress = compute_turn_progress_indicators(
                    stored_state,
                    persisted_world_state,
                    workflow,
                )
                new_turn = session["turn_no"] + 1
                self._insert_snapshot(
                    connection,
                    session,
                    (
                        f"undo-before-turn-{new_turn}"
                        f"-revision-{session['revision']}"
                    ),
                    "undo",
                    "system",
                    replace=False,
                )
                if (
                    auto_snapshot_interval > 0
                    and session["turn_no"] > 0
                    and session["turn_no"] % auto_snapshot_interval == 0
                ):
                    self._insert_snapshot(
                        connection,
                        session,
                        f"auto-turn-{session['turn_no']}",
                        "auto",
                        "system",
                        replace=True,
                    )

                now = utc_now()
                player_event_id = append_event(
                    connection,
                    session_id=session_id,
                    turn_no=new_turn,
                    role="player",
                    actor_id=player_user_id,
                    actor_name=player_name,
                    content=player_input,
                    meta={
                        "player_id": player_id if actor_kind == "human" else "",
                        "actor_kind": actor_kind,
                        "actor_ref": (
                            player_user_id if actor_kind == "ai_companion" else ""
                        ),
                    },
                    created_at=now,
                )
                narrator_event_id = new_id("event")

                workflow_result: dict[str, Any] = {}
                if (
                    str(workflow.get("choice_set_id") or "")
                    and str(workflow.get("selected_key") or "").upper()
                    in CHOICE_KEYS
                ):
                    workflow_result = self._commit_vnext_workflow(
                        connection,
                        session=session,
                        new_turn=new_turn,
                        acting_round=acting_round,
                        next_turn_state=next_turn_state,
                        player_user_id=player_user_id,
                        player_event_id=player_event_id,
                        narrator_event_id=narrator_event_id,
                        world_state=persisted_world_state,
                        check_payload=check_payload,
                        workflow={
                            **workflow,
                            "actor_id": actor_id,
                            "actor_kind": actor_kind,
                        },
                        world=world_data,
                        now=now,
                    )
                narrator_meta = story_progress_meta(
                    connection,
                    session_id,
                    source="ai",
                    session_revision=expected_revision + 1,
                )
                narrator_meta["progress"] = turn_progress
                narrator_meta["scene_ref"] = str(
                    persisted_world_state.get("current_scene")
                    or persisted_world_state.get("scene_ref")
                    or ""
                )
                if check_payload:
                    narrator_meta["check"] = check_payload
                if store_model_payload and model_payload:
                    narrator_meta["model_payload"] = model_payload
                narrator_event_id = append_event(
                    connection,
                    event_id=narrator_event_id,
                    session_id=session_id,
                    turn_no=new_turn,
                    role="narrator",
                    actor_id="narrator",
                    actor_name="开团叙事者",
                    content=narrative,
                    meta=narrator_meta,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO story_documents(
                        event_id, session_id, turn_no, schema,
                        document_json, plain_text, text_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        narrator_event_id,
                        session_id,
                        new_turn,
                        NARRATIVE_DOCUMENT_SCHEMA_ID,
                        document_json,
                        document_text,
                        document_text_hash,
                        now,
                    ),
                )
                stall_check = story_stall_after_write(
                    connection,
                    session_id,
                    world=world_data,
                    runtime=persisted_world_state,
                    session={
                        "id": session_id,
                        "state": str(session["state"] or ""),
                        "turn_no": int(session["turn_no"] or 0),
                        "revision": int(session["revision"] or 0),
                    },
                    squad=squad,
                )
                participant_row = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, player_user_id),
                ).fetchone()
                if workflow and participant_row and actor_kind == "human":
                    v05_result = self._apply_v05_turn_ops(
                        connection,
                        session=session,
                        participant=participant_row,
                        new_turn=new_turn,
                        acting_round=acting_round,
                        source_event_id=narrator_event_id,
                        workflow=workflow,
                        check_payload=check_payload,
                        now=now,
                    )
                    workflow_result["v05"] = v05_result
                connection.execute(
                    """
                    UPDATE sessions SET
                        turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_turn,
                        json_dump(persisted_world_state),
                        now,
                        session_id,
                    ),
                )

                for memory in memories[:12]:
                    scope = str(memory.get("scope", "world"))
                    scope_id = str(memory.get("scope_id", ""))
                    kind = str(memory.get("kind", "fact"))
                    content = str(memory.get("content", "")).strip()
                    if not content:
                        continue
                    importance = max(
                        1,
                        min(5, int(memory.get("importance", 3))),
                    )
                    tags = (
                        memory.get("tags")
                        if isinstance(memory.get("tags"), list)
                        else []
                    )
                    fingerprint = memory_fingerprint(
                        session_id,
                        scope,
                        scope_id,
                        kind,
                        content,
                    )
                    memory_id = new_id("memory")
                    connection.execute(
                        """
                        INSERT INTO memories(
                            id, session_id, scope, scope_id, kind, content,
                            importance, salience, tags_json, fingerprint,
                            source_event_id, created_at, updated_at,
                            last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            salience = MIN(10, salience + 0.5),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            memory_id,
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            importance,
                            json_dump(tags),
                            fingerprint,
                            narrator_event_id,
                            now,
                            now,
                            now,
                        ),
                    )
                    stored_memory = connection.execute(
                        """
                        SELECT id FROM memories
                        WHERE session_id = ? AND fingerprint = ?
                        """,
                        (session_id, fingerprint),
                    ).fetchone()
                    if stored_memory:
                        memory_id = str(stored_memory["id"])
                        visibility = str(
                            memory.get("visibility") or "public"
                        ).lower()
                        if visibility not in {"public", "host", "private"}:
                            visibility = "public"
                        supersedes_id = clean_text(
                            memory.get("supersedes_id"),
                            max_chars=128,
                        )
                        connection.execute(
                            """
                            INSERT INTO memory_governance(
                                memory_id, visibility, locked, pinned,
                                invalidated, supersedes_id, conflict_status,
                                note, updated_by, updated_at
                            ) VALUES (?, ?, ?, ?, 0, ?, 'clear', '',
                                      'narrator', ?)
                            ON CONFLICT(memory_id) DO UPDATE SET
                                visibility = excluded.visibility,
                                locked = MAX(locked, excluded.locked),
                                pinned = MAX(pinned, excluded.pinned),
                                supersedes_id = CASE
                                    WHEN excluded.supersedes_id <> ''
                                    THEN excluded.supersedes_id
                                    ELSE supersedes_id
                                END,
                                updated_at = excluded.updated_at
                            """,
                            (
                                memory_id,
                                visibility,
                                int(bool(memory.get("locked", False))),
                                int(bool(memory.get("pinned", False))),
                                supersedes_id,
                                now,
                            ),
                        )
                        if supersedes_id:
                            connection.execute(
                                """
                                UPDATE memory_governance
                                SET invalidated = 1, updated_by = 'narrator',
                                    updated_at = ?
                                WHERE memory_id = ?
                                """,
                                (now, supersedes_id),
                            )

                self._insert_audit(
                    connection,
                    session_id,
                    player_user_id,
                    "turn.commit",
                    narrator_event_id,
                    {
                        "turn_no": new_turn,
                        "round_no": acting_round,
                        "next_player_user_id": (
                            next_turn_state["current_user_id"]
                        ),
                        "check": check_payload or None,
                        "memory_count": len(memories[:12]),
                        "director_note": director_note,
                        "workflow": workflow_result,
                    },
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'auto'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                connection.execute(
                    """
                    DELETE FROM snapshots
                    WHERE id IN (
                        SELECT id FROM snapshots
                        WHERE session_id = ? AND kind = 'undo'
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT -1 OFFSET 20
                    )
                    """,
                    (session_id,),
                )
                # C6：道具消耗与经济操作与回合同事务提交。
                # 任一校验/应用失败 → 整个事务回滚，不留半提交资产。
                asset_effects = self._apply_item_ops_locked(
                    connection,
                    session_id=session_id,
                    item_ops=item_ops or (),
                )
                for op in economy_ops or []:
                    if not isinstance(op, Mapping):
                        raise ValueError("经济操作格式无效")
                    self._economy_apply_locked(
                        connection,
                        session_id=session_id,
                        operation_id=str(op.get("operation_id") or ""),
                        kind=str(op.get("kind") or "adjust"),
                        currency_id=str(op.get("currency_id") or ""),
                        amount=op.get("amount"),
                        from_owner=_owner_tuple_locked(
                            op.get("from_owner_type"),
                            op.get("from_owner_ref"),
                        ),
                        to_owner=_owner_tuple_locked(
                            op.get("to_owner_type"),
                            op.get("to_owner_ref"),
                        ),
                        reason=str(op.get("reason") or ""),
                        source=str(op.get("source") or "story"),
                        actor_id=str(op.get("actor_id") or ""),
                        target_ref=str(op.get("target_ref") or ""),
                    )
                fate_result = self._commit_actor_fate_locked(
                    connection,
                    session_id=session_id,
                    world=world_data,
                    consequences=[
                        dict(item)
                        for item in (
                            workflow.get("fate_consequences")
                            if isinstance(
                                workflow.get("fate_consequences"),
                                Sequence,
                            )
                            and not isinstance(
                                workflow.get("fate_consequences"),
                                (str, bytes),
                            )
                            else []
                        )
                        if isinstance(item, Mapping)
                    ],
                    event_ref=narrator_event_id,
                    actor_id=player_user_id,
                    turn_no=new_turn,
                    trigger_revision=expected_revision + 1,
                    now=now,
                )
                workflow_result["fate"] = fate_result
                self._enqueue_storage_sync(connection, [session_id], "sync")
                if (
                    auto_snapshot_interval > 0
                    and new_turn > 0
                    and new_turn % auto_snapshot_interval == 0
                ):
                    self._enqueue_storage_sync(
                        connection,
                        [session_id],
                        "archive_backup",
                        payload={
                            "reason": f"第 {new_turn} 回合自动安全备份",
                        },
                    )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                # 0.11.1：回合提交与事务回执完成合并进同一事务。
                # 此前 commit_turn 成功后再单独 update_operation(completed)，
                # 两者跨事务，崩溃/异常会把已提交回合永久留在 pending，
                # 导致同一行动的后续重试被“该行动正在处理中”拦截。
                if operation_id:
                    op_row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if op_row:
                        merged = json_load(op_row["result_json"], {})
                        merged = (
                            merged if isinstance(merged, dict) else {}
                        )
                        merged.update(dict(operation_result or {}))
                        merged["phase"] = "committed"
                        connection.execute(
                            """
                            UPDATE operation_receipts
                            SET result_json = ?, status = 'completed',
                                phase = 'committed', committed_revision = ?,
                                lease_expires_at = '', reminder_next_at = '',
                                updated_at = ?
                            WHERE operation_id = ? AND status = 'ready_to_commit'
                            """,
                            (
                                json_dump(merged),
                                expected_revision + 1,
                                utc_now(),
                                op_row["operation_id"],
                            ),
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            "system",
                            "operation.update",
                            op_row["operation_id"],
                            {"status": "completed", "phase": "committed"},
                        )
                connection.execute("COMMIT")
                result = self._session(row)
                result["player_event_id"] = player_event_id
                result["narrator_event_id"] = narrator_event_id
                result["workflow"] = workflow_result
                result["asset_effects"] = asset_effects
                result["turn_progress"] = turn_progress
                result["stall_intervention"] = stall_check.get(
                    "intervention"
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _participant(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        result = {
            "id": row["id"],
            "session_id": row["session_id"],
            "player_id": row["player_id"],
            "group_user_id": row["group_user_id"],
            "private_user_id": row["private_user_id"],
            "private_origin": row["private_origin"],
            "display_name": row["display_name"],
            "character_card_id": row["character_card_id"],
            "character_version_id": row["character_version_id"],
            "character_name": row["character_name"],
            "character_code": row["character_code"],
            "aliases": json_load(row["aliases_json"], []),
            "card_status": row["card_status"],
            "ready": bool(row["ready"]),
            "participation_status": row["participation_status"],
            "seat_reserved_at": row["seat_reserved_at"],
            "joined_round": row["joined_round"],
            "consecutive_timeouts": row["consecutive_timeouts"],
            "exit_reason": row["exit_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for key in (
            "draft_status",
            "draft_step",
            "draft_expires_at",
            "binding_code",
            "binding_expires_at",
            "draft_profile_json",
            "draft_template_version",
            "runtime_state_json",
            "runtime_revision",
            "card_profile_json",
            "card_stats_json",
            "card_version_no",
            "card_template_version",
            "card_version_status",
            "card_review_note",
            "card_reviewed_by",
            "card_version_created_at",
        ):
            if key in keys:
                value = row[key]
                if key == "draft_profile_json":
                    result["draft_profile"] = json_load(value, {})
                elif key == "runtime_state_json":
                    result["runtime_state"] = json_load(value, {})
                elif key == "card_profile_json":
                    result["card_profile"] = json_load(value, {})
                elif key == "card_stats_json":
                    result["card_stats"] = json_load(value, {})
                elif key == "runtime_revision":
                    result["runtime_revision"] = value
                else:
                    result[key] = value
        return result

    async def restore_latest_auto(
        self,
        session_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        target = await self._run(
            self._latest_auto_restore_target,
            session_id,
        )
        return await self.restore_snapshot(
            session_id,
            target["snapshot_ref"],
            actor_id,
            expected_revision=int(target["expected_revision"]),
            idempotency_key=(
                f"automatic-restore:{session_id}:{target['snapshot_ref']}:"
                f"{target['expected_revision']}"
            ),
        )

    def _latest_auto_restore_target(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshots.*, sessions.revision AS session_revision
                FROM snapshots
                JOIN sessions ON sessions.id = snapshots.session_id
                WHERE snapshots.session_id = ?
                  AND kind IN ('auto', 'safety', 'undo')
                ORDER BY snapshots.created_at DESC, snapshots.rowid DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if not row:
            raise DatabaseNotFoundError("没有可回滚的保护点")
        return {
            "snapshot_ref": str(row["id"]),
            "expected_revision": self._snapshot_revision(
                row,
                int(row["session_revision"] or 0),
            ),
        }
