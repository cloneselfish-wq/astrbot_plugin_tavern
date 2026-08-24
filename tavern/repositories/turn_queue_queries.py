from __future__ import annotations

from .workflow_support import *
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    canonical_narrative_json,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
)


class TurnQueueQueriesRepositoryMixin:
    def _commit_vote_resolution(
        self,
        session_id: str,
        expected_revision: int,
        narrative: str,
        narrative_document: dict[str, Any],
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        model_payload: dict[str, Any],
        workflow: dict[str, Any],
        vote_id: str,
        item_ops: list[dict[str, Any]],
        economy_ops: list[dict[str, Any]],
    ) -> dict[str, Any]:
        document = parse_narrative_document(
            narrative_document,
            dialogue_expected=False,
        )
        document_text = narrative_document_to_plain_text(document)
        if document_text != str(narrative or ""):
            raise ValueError("NarrativeDocument 与表决故事正文不一致")
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
                if int(session["revision"]) != int(expected_revision):
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("酒馆当前不在运行状态")
                if vote_id:
                    vote_row = connection.execute(
                        "SELECT * FROM group_votes WHERE id = ?",
                        (vote_id,),
                    ).fetchone()
                    if vote_row is None:
                        raise DatabaseNotFoundError("集体投票不存在")
                    if vote_row["decision_status"] == "collecting":
                        raise InvalidTransitionError("集体投票尚未结束")
                    if vote_row["decision_status"] != "decided":
                        raise InvalidTransitionError("集体投票没有可落实的多数决定")
                    if vote_row["resolution_status"] == "committed":
                        raise DatabaseConflictError("该表决结果已经落实")
                    operation_id = str(
                        vote_row["resolution_operation_id"] or ""
                    )
                    operation_row = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if operation_row is None:
                        raise DatabaseConflictError("表决落实回执不存在")
                    if str(operation_row["status"] or "") != "ready_to_commit":
                        raise DatabaseConflictError(
                            "表决落实已取消、完成或进入恢复状态，禁止提交迟到结果"
                        )
                asset_effects = self._apply_item_ops_locked(
                    connection,
                    session_id=session_id,
                    item_ops=item_ops or (),
                )
                # C6：模型提议的经济操作与表决叙事同一事务提交。
                # 余额不足/货币缺失等任何失败都会整体回滚，不留半提交。
                for op in economy_ops or ():
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
                        source=str(op.get("source") or "vote"),
                        actor_id=str(op.get("actor_id") or ""),
                        target_ref=str(op.get("target_ref") or ""),
                    )
                now = utc_now()
                new_turn = int(session["turn_no"]) + 1
                stored_state = json_load(session["world_state_json"], {})
                turn = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(
                    public_world_state(world_state),
                    turn,
                )
                event_id = new_id("event")
                meta = story_progress_meta(
                    connection,
                    session_id,
                    source="ai",
                    session_revision=expected_revision + 1,
                    extra={
                        "vote_resolution": True,
                        "vote_id": vote_id,
                    },
                )
                meta["progress"] = compute_turn_progress_indicators(
                    stored_state,
                    persisted_state,
                    workflow,
                )
                meta["scene_ref"] = str(
                    persisted_state.get("current_scene")
                    or persisted_state.get("scene_ref")
                    or ""
                )
                meta["roleplay_active"] = True
                if model_payload:
                    meta["model_payload"] = model_payload
                event_id = append_event(
                    connection,
                    event_id=event_id,
                    session_id=session_id,
                    turn_no=new_turn,
                    role="narrator",
                    actor_id="system",
                    actor_name="集体表决",
                    content=narrative,
                    meta=meta,
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
                        event_id,
                        session_id,
                        new_turn,
                        NARRATIVE_DOCUMENT_SCHEMA_ID,
                        document_json,
                        document_text,
                        document_text_hash,
                        now,
                    ),
                )
                for memory in memories[:12]:
                    content = clean_text(
                        memory.get("content"),
                        max_chars=1200,
                    )
                    if not content:
                        continue
                    scope = str(memory.get("scope") or "world")
                    scope_id = str(memory.get("scope_id") or "")
                    kind = str(memory.get("kind") or "fact")
                    fingerprint = memory_fingerprint(
                        session_id, scope, scope_id, kind, content
                    )
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
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            new_id("memory"),
                            session_id,
                            scope,
                            scope_id,
                            kind,
                            content,
                            max(1, min(5, int(memory.get("importance", 3)))),
                            json_dump(list(memory.get("tags") or [])),
                            fingerprint,
                            event_id,
                            now,
                            now,
                            now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_turn,
                        json_dump(persisted_state),
                        now,
                        session_id,
                    ),
                )
                result: dict[str, Any] = {
                    "session_id": session_id,
                    "event_id": event_id,
                    "turn_no": new_turn,
                    "narrative": narrative,
                    "asset_effects": asset_effects,
                }
                next_choices = workflow.get("next_choices")
                next_choice_set_id = ""
                if next_choices:
                    recovery = (
                        dict(workflow.get("choice_recovery_receipt") or {})
                        if isinstance(
                            workflow.get("choice_recovery_receipt"),
                            Mapping,
                        )
                        else {}
                    )
                    # choice_sets 为每会话单集（UNIQUE(session_id)），
                    # 插入前必须先作废现有 active 集。
                    connection.execute(
                        """
                        UPDATE choice_sets SET status = 'superseded'
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (session_id,),
                    )
                    world_row = connection.execute(
                        "SELECT * FROM worlds WHERE id=?",
                        (session["world_id"],),
                    ).fetchone()
                    if world_row is None:
                        raise DatabaseNotFoundError("会话世界不存在")
                    world_payload = self._world(world_row)
                    try:
                        normalized = normalize_choices(
                            next_choices,
                            world_payload,
                        )
                    except ValueError:
                        normalized = fallback_choices(
                            world_state,
                            world_payload,
                        )
                        if not recovery:
                            operation_ref = str(
                                workflow.get("operation_id")
                                or operation_id
                                or ""
                            )
                            recovery = {
                                "status": "fallback",
                                "failure_kind": "commit_validation_failed",
                                "repair_count": 0,
                                "fallback_version": (
                                    "choices-fallback/1.0.0-rc10"
                                ),
                                "provider_class": "none",
                                "message": (
                                    "选项在提交前未通过安全校验，系统已改用"
                                    "包含安全行动和合法尝试的本地兜底。"
                                ),
                                "trace_id": hashlib.sha256(
                                    operation_ref.encode("utf-8")
                                ).hexdigest()[:8].upper(),
                                "idempotency_key": (
                                    f"{operation_ref}:choice-recovery"
                                ),
                                "resolution_summary": {
                                    "choice_count": len(normalized),
                                    "has_check": any(
                                        str(
                                            item.get("resolution_kind") or ""
                                        )
                                        == "check"
                                        for item in normalized
                                    ),
                                    "has_safe": any(
                                        str(item.get("risk") or "") == "safe"
                                        for item in normalized
                                    ),
                                },
                            }
                    participant = connection.execute(
                        """
                        SELECT * FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        ORDER BY CASE WHEN group_user_id = ? THEN 0 ELSE 1 END,
                                 created_at LIMIT 1
                        """,
                        (session_id, turn.get("current_user_id") or ""),
                    ).fetchone()
                    if participant:
                        choice_id = new_id("choices")
                        connection.execute(
                            """
                            INSERT INTO choice_sets(
                                id, session_id, participant_id, round_no,
                                session_revision, choices_json, status,
                                reroll_count, idempotency_key, created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                            """,
                            (
                                choice_id,
                                session_id,
                                participant["id"],
                                turn["round_no"],
                                int(session["revision"]) + 1,
                                json_dump(normalized),
                                f"vote-resolution:{vote_id or '?'}",
                                now,
                                now,
                            ),
                        )
                        config = connection.execute(
                            """
                            SELECT time_rules_json FROM instance_configs
                            WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        rules = normalize_time_rules(
                            json_load(
                                config["time_rules_json"] if config else "",
                                {},
                            )
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id=participant["id"],
                            timer_type="turn",
                            timeout_seconds=rules["turn_timeout_seconds"],
                            reminder_seconds=rules[
                                "turn_reminder_seconds"
                            ],
                            action={
                                "choice_set_id": choice_id,
                                "user_id": participant[
                                    "group_user_id"
                                ],
                            },
                        )
                        next_choice_set_id = choice_id
                        if recovery:
                            operation_ref = str(
                                recovery.get("operation_id")
                                or workflow.get("operation_id")
                                or operation_id
                                or ""
                            )
                            result["choice_recovery_receipt"] = (
                                self._choice_recovery_view(
                                    self._insert_choice_recovery_locked(
                                        connection,
                                        session_id=session_id,
                                        choice_set_id=choice_id,
                                        operation_id=operation_ref,
                                        recovery=recovery,
                                        now=now,
                                    )
                                )
                            )
                result["next_choice_set_id"] = next_choice_set_id
                if vote_id:
                    connection.execute(
                        """
                        UPDATE group_votes SET
                            status='resolved', resolution_status='committed',
                            resolved_at=?, committed_event_id=?, updated_at=?
                        WHERE id=? AND decision_status='decided'
                          AND resolution_status<>'committed'
                        """,
                        (now, event_id, now, vote_id),
                    )
                    op_result = json_load(operation_row["result_json"], {})
                    op_result = op_result if isinstance(op_result, dict) else {}
                    op_result.update(
                        {
                            "phase": "committed",
                            "vote_id": vote_id,
                            "event_id": event_id,
                            "turn_no": new_turn,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            status='completed', phase='committed',
                            result_json=?, committed_revision=?,
                            lease_expires_at='', reminder_next_at='',
                            updated_at=?
                        WHERE operation_id=? AND status='ready_to_commit'
                        """,
                        (
                            json_dump(op_result),
                            expected_revision + 1,
                            now,
                            operation_id,
                        ),
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    "system",
                    "vote.resolution_committed",
                    event_id,
                    {"vote_id": vote_id, "turn_no": new_turn},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def pending_vote_resolution(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        """0.11.3：查询等待自动推进的表决。

        定时器结束且已通过但尚未落实叙事的投票会被标记
        result_json.pending_resolution=true，由下次输入自动推进。
        """
        return await self._run(
            self._pending_vote_resolution,
            session_id,
        )

    def _pending_vote_resolution(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_votes
                WHERE session_id = ? AND decision_status = 'decided'
                  AND resolution_status IN ('pending', 'failed_retryable')
                ORDER BY updated_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return self._vote(row) if row else None

    async def update_vote_resolution_status(
        self,
        vote_id: str,
        status: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._update_vote_resolution_status,
            vote_id,
            status,
        )

    def _update_vote_resolution_status(
        self,
        vote_id: str,
        status: str,
    ) -> dict[str, Any]:
        allowed = {
            "pending",
            "generating",
            "failed_retryable",
            "cancelled",
            "needs_recovery",
        }
        status = str(status or "")
        if status not in allowed:
            raise ValueError("表决落实状态无效")
        now = utc_now()
        group_status = (
            "needs_recovery" if status == "needs_recovery" else "decided"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM group_votes WHERE id=?",
                    (vote_id,),
                ).fetchone()
                if row is None:
                    raise DatabaseNotFoundError("集体投票不存在")
                if str(row["resolution_status"] or "") == "committed":
                    connection.execute("COMMIT")
                    return self._vote(row)
                connection.execute(
                    """
                    UPDATE group_votes SET status=?, resolution_status=?,
                        updated_at=? WHERE id=? AND decision_status='decided'
                    """,
                    (group_status, status, now, vote_id),
                )
                refreshed = connection.execute(
                    "SELECT * FROM group_votes WHERE id=?",
                    (vote_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._vote(refreshed)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def clear_vote_resolution_pending(
        self,
        vote_id: str,
    ) -> None:
        """清除「待推进」标记（表决已落实叙事）。"""
        await self._run(
            self._clear_vote_resolution_pending,
            vote_id,
        )

    def _clear_vote_resolution_pending(
        self,
        vote_id: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT result_json FROM group_votes WHERE id = ?",
                    (vote_id,),
                ).fetchone()
                if row and row["result_json"]:
                    data = json_load(row["result_json"], {})
                    data.pop("pending_resolution", None)
                    connection.execute(
                        """
                        UPDATE group_votes
                        SET result_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (json_dump(data), utc_now(), vote_id),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
