from __future__ import annotations

from .sessions_support import *


class SessionLifecycleQueriesRepositoryMixin:
    def _finalize_session(
        self,
        session_id: str,
        actor_id: str,
        termination_type: str,
        reason: str,
        terminal_match: Mapping[str, Any] | None,
        trigger_revision: int,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        termination_type = str(termination_type or "").strip().lower()
        if termination_type not in {"completed", "failed", "aborted"}:
            raise ValueError("结束类型必须为 completed、failed 或 aborted")
        reason = clean_text(reason, max_chars=1000)
        if termination_type == "aborted" and not reason:
            raise ValueError("放弃本轮必须填写原因")
        owns_connection = connection is None
        connection_scope = (
            self._connect()
            if owns_connection
            else nullcontext(connection)
        )
        with connection_scope as connection:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                # D1：终局计划与并发判定（幂等键 + 唯一回执 + 确定性哈希）。
                from ..runtime.finalization_service import (
                    build_finalization_plan,
                    classify_plan,
                )
                from ..twp.endings import ending_definitions

                world = self._world_snapshot_for(
                    connection, str(session["world_id"] or "")
                )
                if terminal_match:
                    plan = build_finalization_plan(
                        session_id=session_id,
                        match=dict(terminal_match),
                        trigger_revision=int(trigger_revision or 0),
                        created_at=utc_now(),
                        endings=ending_definitions(world),
                    )
                else:
                    plan = build_finalization_plan(
                        session_id=session_id,
                        match={
                            "condition_id": "manual",
                            "label": "管理员完结",
                            "matched": True,
                            "priority": 0,
                            "termination_type": termination_type,
                            "ending_ref": "",
                            "archive_policy": "automatic_readonly",
                            "reason": (
                                reason
                                or {
                                    "completed": "正常完结",
                                    "failed": "副本以失败告终",
                                    "aborted": "管理员放弃本轮",
                                }[termination_type]
                            ),
                        },
                        trigger_revision=int(session["revision"] or 1),
                        created_at=utc_now(),
                        endings=ending_definitions(world),
                    )
                receipts = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM terminal_receipts WHERE session_id = ?",
                        (session_id,),
                    ).fetchall()
                ]
                decision = classify_plan(plan, receipts)
                finalization_row = connection.execute(
                    "SELECT * FROM session_finalizations WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                idempotency_key = str(plan.get("idempotency_key") or "")
                existing = connection.execute(
                    "SELECT * FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if existing or session["state"] == SESSION_FINISHED:
                    if decision in {"replayed", "superseded"}:
                        if owns_connection:
                            connection.execute("COMMIT")
                        return self._finalize_existing_result(
                            connection,
                            session_id,
                            plan,
                            decision=decision,
                        )
                    raise InvalidTransitionError("该副本已经永久归档")
                if decision == "superseded":
                    if owns_connection:
                        connection.execute("COMMIT")
                    return self._finalize_existing_result(
                        connection,
                        session_id,
                        plan,
                        decision=decision,
                    )
                if decision == "replayed" and (
                    finalization_row is None
                    or str(finalization_row["status"] or "") == "finalized"
                ):
                    if owns_connection:
                        connection.execute("COMMIT")
                    return self._finalize_existing_result(
                        connection,
                        session_id,
                        plan,
                        decision=decision,
                    )

                now = utc_now()
                connection.execute(
                    """
                    UPDATE character_card_drafts
                    SET status = 'cancelled', updated_at = ?
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    ) AND status = 'active'
                    """,
                    (now, session_id),
                )
                # ── D1：唯一终局回执（同幂等键只允许一行）──────────────
                condition_id = str(plan.get("condition_id") or "")
                projection = dict(plan.get("projection") or {})
                ending_ref = str(plan.get("ending_ref") or "")
                ending_label = str(projection.get("ending") or "")
                archive_policy = str(
                    plan.get("archive_policy") or "automatic_readonly"
                )
                plan_reason = str(plan.get("reason") or reason or "")
                if idempotency_key:
                    receipt_row = connection.execute(
                        """
                        SELECT * FROM terminal_receipts
                        WHERE idempotency_key = ?
                        """,
                        (idempotency_key,),
                    ).fetchone()
                    if receipt_row is None:
                        connection.execute(
                            """
                            INSERT INTO terminal_receipts(
                                id, session_id, condition_id, condition_label,
                                priority, ending_ref, termination_type,
                                archive_policy, trigger_revision, payload_json,
                                status, idempotency_key, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'finalizing',
                                      ?, ?, ?)
                            """,
                            (
                                new_id("terminal"),
                                session_id,
                                str(condition_id)[:160],
                                str(ending_label or condition_id)[:160],
                                max(0, int(plan.get("priority", 0) or 0)),
                                str(ending_ref)[:160],
                                termination_type,
                                str(archive_policy)[:80],
                                max(0, int(plan.get("trigger_revision", 0) or 0)),
                                json_dump(
                                    {
                                        "plan_hash": str(plan.get("plan_hash") or ""),
                                        "reason": plan_reason,
                                    }
                                ),
                                idempotency_key[:200],
                                now,
                                now,
                            ),
                        )
                # ── D1：终局最终化预留（输入锁 + finalization_pending）──
                if finalization_row is None:
                    connection.execute(
                        """
                        INSERT INTO session_finalizations(
                            session_id, status, termination_type, ending_ref,
                            ending_label, archive_policy, idempotency_key,
                            input_locked, snapshot_status, final_snapshot_id,
                            payload_json, attempts, last_error,
                            created_at, updated_at
                        ) VALUES (?, 'pending', ?, ?, ?, ?, ?, 1, 'pending',
                                  '', ?, 0, '', ?, ?)
                        """,
                        (
                            session_id,
                            termination_type,
                            str(ending_ref)[:160],
                            str(ending_label)[:160],
                            str(archive_policy)[:80],
                            idempotency_key[:200],
                            json_dump(plan),
                            now,
                            now,
                        ),
                    )
                    finalization_row = connection.execute(
                        "SELECT * FROM session_finalizations WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()
                # ── 最终快照：复用已生成快照，重试不产生第二份 ──────────
                final_snapshot_id = str(
                    finalization_row["final_snapshot_id"] or ""
                ) if finalization_row is not None else ""
                if not final_snapshot_id:
                    stamp = str(session_id)[-12:] or "final"
                    final_snapshot_id = self._insert_snapshot(
                        connection,
                        session,
                        f"final-{termination_type}-{stamp}",
                        "final",
                        actor_id,
                        replace=True,
                    )
                # ── 终局事件（唯一权威事件写入器）────────────────────
                if terminal_match:
                    ending_text = plan_reason
                    if not ending_text:
                        ending_text = "副本触发了终局条件并永久归档。"
                elif termination_type == "completed":
                    ending_text = "故事抵达了已经确认的结局，副本进入永久归档。"
                elif termination_type == "failed":
                    ending_text = "故事未能抵达结局，副本以失败告终并永久归档。"
                else:
                    ending_text = (
                        f"副本由管理员放弃本轮并永久归档。原因：{reason}"
                    )
                self._write_session_event(
                    connection,
                    session_id=session_id,
                    turn_no=int(session["turn_no"] or 0),
                    actor_id=actor_id,
                    content=ending_text,
                    meta={
                        "kind": "session_finalized",
                        "termination_type": termination_type,
                        "reason": plan_reason,
                        "ending_ref": ending_ref,
                        "ending_label": ending_label,
                        "final_snapshot_id": final_snapshot_id,
                    },
                )
                connection.execute(
                    """
                    UPDATE choice_sets SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE group_votes SET status = 'cancelled',
                        decision_status='cancelled', resolution_status='cancelled',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'open'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ? AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE delegation_grants SET status = 'revoked',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    "DELETE FROM permission_grants WHERE session_id = ?",
                    (session_id,),
                )
                connection.execute(
                    """
                    UPDATE return_requests SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ?
                      AND status NOT IN ('completed', 'rejected', 'cancelled')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (session_id,),
                )
                # D1-RUN-013：未决操作全部取消（终局后不允许残留待执行动作）。
                connection.execute(
                    """
                    UPDATE action_operations SET status = 'cancelled',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'pending'
                    """,
                    (now, session_id),
                )
                recovery_row = connection.execute(
                    """
                    SELECT recovery_json FROM session_rule_states
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                recovery = json_load(
                    recovery_row["recovery_json"] if recovery_row else "",
                    {},
                )
                recovery = (
                    dict(recovery) if isinstance(recovery, Mapping) else {}
                )
                recovery.update(
                    {
                        "state": "archived",
                        "message": ending_text,
                        "operation_id": final_snapshot_id,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """
                    UPDATE session_rule_states
                    SET recovery_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(recovery), now, session_id),
                )
                connection.execute(
                    """
                    UPDATE sessions SET state = 'finished', selected = 0,
                        input_locked = 1, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, session_id),
                )
                insert_session_archive(
                    connection,
                    session_id=session_id,
                    termination_type=termination_type,
                    reason=plan_reason,
                    final_snapshot_id=final_snapshot_id,
                    ended_by=actor_id,
                    ended_at=now,
                    readonly=1,
                    ending_ref=ending_ref,
                    ending_label=ending_label,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.finish"
                    if termination_type == "completed"
                    else (
                        "session.fail"
                        if termination_type == "failed"
                        else "session.abort"
                    ),
                    session_id,
                    {
                        "termination_type": termination_type,
                        "reason": plan_reason,
                        "ending_ref": ending_ref,
                        "ending_label": ending_label,
                        "idempotency_key": idempotency_key,
                        "final_snapshot_id": final_snapshot_id,
                        "readonly": True,
                    },
                )
                # D1：终局最终化提交（快照完成 → 永久只读）。
                connection.execute(
                    """
                    UPDATE session_finalizations SET
                        status = 'finalized',
                        snapshot_status = 'completed',
                        final_snapshot_id = ?,
                        ending_label = ?,
                        last_error = '',
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        final_snapshot_id,
                        ending_label,
                        now,
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE terminal_receipts SET status = 'finalized',
                        updated_at = ?
                    WHERE session_id = ? AND status = 'finalizing'
                    """,
                    (now, session_id),
                )
                finalization_row = connection.execute(
                    "SELECT * FROM session_finalizations WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                           srs.progress_json, srs.recovery_json,
                           sa.termination_type,
                           sa.reason AS archive_reason,
                           sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                           sa.readonly
                    FROM sessions s
                    JOIN worlds w ON w.id = s.world_id
                    LEFT JOIN session_rule_states srs
                      ON srs.session_id = s.id
                    JOIN session_archives sa ON sa.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if owns_connection:
                    connection.execute("COMMIT")
                return {
                    **self._session(row),
                    "idempotency_key": idempotency_key,
                    "decision": "applied",
                    "projection": projection,
                    "finalization": dict(finalization_row)
                    if finalization_row is not None
                    else {},
                }
            except Exception:
                if owns_connection:
                    connection.execute("ROLLBACK")
                raise

    async def save_manual_state(
        self,
        session_id: str,
        state: Mapping[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._save_manual_state,
            session_id,
            dict(state),
            expected_revision,
            actor_id,
        )

    def _save_manual_state(
        self,
        session_id: str,
        state: dict[str, Any],
        expected_revision: int,
        actor_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                if current["revision"] != expected_revision:
                    raise DatabaseConflictError("会话状态已改变，请刷新后重试")
                self._insert_snapshot(
                    connection,
                    current,
                    f"manual-before-edit-{current['revision']}",
                    "safety",
                    actor_id,
                    replace=True,
                )
                stored_state = json_load(current["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(
                    public_world_state(state),
                    turn_state,
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), utc_now(), session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.state_edit",
                    session_id,
                    {"previous_revision": current["revision"]},
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def apply_session_lifecycle(
        self,
        session_id: str,
        action: str,
        actor_id: str,
        *,
        reason: str,
        expected_revision: int,
        idempotency_key: str,
        confirmation_name: str,
        acknowledge_archive: bool,
    ) -> dict[str, Any]:
        return await self._run(
            self._apply_session_lifecycle,
            session_id,
            action,
            actor_id,
            reason,
            int(expected_revision),
            idempotency_key,
            confirmation_name,
            bool(acknowledge_archive),
        )

    def _apply_session_lifecycle(
        self,
        session_id: str,
        action: str,
        actor_id: str,
        reason: str,
        expected_revision: int,
        idempotency_key: str,
        confirmation_name: str,
        acknowledge_archive: bool,
    ) -> dict[str, Any]:
        from ..session_lifecycle import (
            LIFECYCLE_ACTIONS,
            lifecycle_capabilities,
        )

        action = str(action or "").strip().lower()
        if action not in LIFECYCLE_ACTIONS:
            raise ValueError("不支持的副本生命周期操作")
        request_key = clean_text(idempotency_key, max_chars=160)
        if not request_key:
            raise ValueError("缺少幂等请求键，系统没有执行生命周期操作")
        if int(expected_revision or 0) < 1:
            raise ValueError("副本修订号必须是正整数")
        reason = clean_text(reason, max_chars=1000)
        if action == "abort" and not reason:
            raise ValueError("放弃本轮失败：必须填写原因")

        operation_id = f"session.lifecycle:{session_id}:{request_key}"
        request_payload = {
            "session_id": session_id,
            "action": action,
            "reason": reason,
            "expected_revision": int(expected_revision),
            "confirmation_name": str(confirmation_name or ""),
            "acknowledge_archive": bool(acknowledge_archive),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    """
                    SELECT request_json, result_json
                    FROM operation_receipts
                    WHERE operation_id = ?
                      AND operation_type = 'session.lifecycle'
                      AND status = 'completed'
                    """,
                    (operation_id,),
                ).fetchone()
                if receipt:
                    recorded = json_load(receipt["request_json"], {})
                    if recorded != request_payload:
                        raise DatabaseConflictError(
                            "同一幂等请求键对应的生命周期操作内容不同，"
                            "系统没有覆盖原结果"
                        )
                    result = json_load(receipt["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(result), "idempotent_replay": True}

                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("副本不存在")
                if int(current["revision"] or 0) != int(expected_revision):
                    raise DatabaseConflictError(
                        "副本状态已变化；系统未执行操作，请刷新后重新确认"
                    )
                if action in {"finish", "abort"} and str(
                    confirmation_name or ""
                ).strip() != str(current["instance_name"] or ""):
                    raise ValueError("二次确认的副本名称与当前副本不一致")
                if action in {"finish", "abort"} and not acknowledge_archive:
                    raise ValueError("必须确认该操作会永久归档当前副本")
                if action == "finish" and int(current["turn_no"] or 0) <= 0:
                    raise DatabaseConflictError(
                        "完结故事失败：故事尚未开演。"
                        "系统未改变副本；如需结束本轮，请使用“放弃本轮”"
                    )

                before = self._session_lifecycle_context(
                    connection,
                    session_id,
                )
                if action == "close":
                    session = self._transition_session(
                        session_id,
                        SESSION_CLOSED,
                        actor_id,
                        "",
                        expected_revision,
                        connection,
                    )
                elif action == "reopen":
                    session = self._transition_session(
                        session_id,
                        SESSION_PREPARING,
                        actor_id,
                        "",
                        expected_revision,
                        connection,
                    )
                else:
                    session = self._finalize_session(
                        session_id,
                        actor_id,
                        "completed" if action == "finish" else "aborted",
                        reason or "正常完结",
                        None,
                        expected_revision,
                        connection,
                    )

                after = self._session_lifecycle_context(
                    connection,
                    session_id,
                )
                lifecycle = lifecycle_capabilities(
                    session,
                    after,
                    authorized=True,
                )
                result = {
                    "session": session,
                    "result": {
                        "action": action,
                        "idempotent_replay": False,
                        "termination_type": (
                            "completed"
                            if action == "finish"
                            else ("aborted" if action == "abort" else "")
                        ),
                        "reason": reason,
                        "cancelled_card_drafts": (
                            before["active_card_drafts"]
                            + before["suspended_card_drafts"]
                            if action in {"finish", "abort"}
                            else 0
                        ),
                        "suspended_card_drafts": (
                            before["active_card_drafts"]
                            if action == "close"
                            else 0
                        ),
                        "resumed_card_drafts": (
                            before["suspended_card_drafts"]
                            if action == "reopen"
                            else 0
                        ),
                        "cancelled_choices": (
                            before["pending_choices"]
                            if action in {"close", "finish", "abort"}
                            else 0
                        ),
                        "cancelled_votes": (
                            before["pending_votes"]
                            if action in {"close", "finish", "abort"}
                            else 0
                        ),
                        "cancelled_timers": (
                            before["pending_timers"]
                            if action in {"close", "finish", "abort"}
                            else 0
                        ),
                        "cancelled_operations": (
                            before["pending_operations"]
                            if action in {"finish", "abort"}
                            else 0
                        ),
                        "revoked_temporary_grants": (
                            before["temporary_grants"]
                            if action in {"finish", "abort"}
                            else 0
                        ),
                        "next_actions": (
                            ["create_session", "select_world"]
                            if action == "abort"
                            else []
                        ),
                    },
                    **lifecycle,
                }
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        committed_revision, created_at, updated_at
                    ) VALUES (?, ?, 'session.lifecycle', ?, ?,
                              'completed', 'committed', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        json_dump(result),
                        int(session.get("revision", 0) or 0),
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str = "",
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        return await self._run(
            self._transition_session,
            session_id,
            target_state,
            actor_id,
            world_ref,
            int(expected_revision or 0),
        )
