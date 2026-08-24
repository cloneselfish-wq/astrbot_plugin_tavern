from __future__ import annotations

from .story_support import *


class SnapshotsQueriesRepositoryMixin:
    async def create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        *,
        replace: bool,
        expected_revision: int,
        idempotency_key: str,
        expected_snapshot_revision: int | None = None,
    ) -> dict[str, Any]:
        """Create or replace a snapshot with CAS and durable replay."""

        return await self._run(
            self._create_snapshot,
            str(session_id or ""),
            str(name or ""),
            str(actor_id or ""),
            bool(replace),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
            (
                int(expected_snapshot_revision)
                if expected_snapshot_revision is not None
                else None
            ),
        )

    def _create_snapshot(
        self,
        session_id: str,
        name: str,
        actor_id: str,
        replace: bool,
        expected_revision: int,
        operation_id: str,
        expected_snapshot_revision: int | None,
    ) -> dict[str, Any]:
        name = clean_text(name, max_chars=100)
        actor_id = clean_text(actor_id, max_chars=200)
        if not name:
            raise ValueError("存档名不能为空")
        if not actor_id:
            raise ValueError("存档操作缺少执行者")
        request = {
            "operation": "replace" if replace else "create",
            "session_id": session_id,
            "name": name,
            "actor_id": actor_id,
            "expected_revision": int(expected_revision),
            "expected_snapshot_revision": expected_snapshot_revision,
        }
        input_hash = self._snapshot_request_hash(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = self._snapshot_completed_receipt(
                    connection,
                    operation_id,
                    input_hash,
                )
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                current_session_revision = int(session["revision"] or 0)
                if current_session_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "副本状态已经变化，请刷新后重新创建存档"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE session_id = ? AND name = ?
                    """,
                    (session_id, name),
                ).fetchone()
                if replace:
                    if existing is None:
                        raise DatabaseNotFoundError("要覆盖的同名存档不存在")
                    if str(existing["kind"] or "") in {
                        "safety",
                        "undo",
                        "final",
                    }:
                        raise InvalidTransitionError(
                            "安全快照、回滚点与最终保护存档不能手动覆盖"
                        )
                    if expected_snapshot_revision is None:
                        raise DatabaseConflictError(
                            "覆盖存档前必须确认当前存档版本"
                        )
                    current_snapshot_revision = self._snapshot_revision(
                        existing,
                        current_session_revision,
                    )
                    if current_snapshot_revision != int(
                        expected_snapshot_revision
                    ):
                        raise DatabaseConflictError(
                            "同名存档已经变化，请刷新后重新确认覆盖"
                        )
                elif existing is not None:
                    raise DatabaseConflictError(
                        "已存在同名存档，请确认当前版本后使用覆盖操作"
                    )
                snapshot_id = self._insert_snapshot(
                    connection,
                    session,
                    name,
                    "manual",
                    actor_id,
                    replace=replace,
                )
                row = connection.execute(
                    "SELECT * FROM snapshots WHERE id = ?",
                    (snapshot_id,),
                ).fetchone()
                result = {
                    "operation": "replace" if replace else "create",
                    "state": "ready",
                    "revision": current_session_revision,
                    "snapshot": self._snapshot_public(
                        row,
                        current_session_revision,
                    ),
                    "replayed": False,
                }
                now = utc_now()
                self._store_snapshot_receipt(
                    connection,
                    operation_id=operation_id,
                    session_id=session_id,
                    input_hash=input_hash,
                    result=result,
                    now=now,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.replace" if replace else "snapshot.create",
                    snapshot_id,
                    {"name": name, "turn_no": int(session["turn_no"] or 0)},
                )
                self._enqueue_storage_sync(
                    connection,
                    [session_id],
                    "archive_save",
                    payload={"reason": "手动命名存档"},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Delete one manual snapshot with target CAS and durable replay."""

        return await self._run(
            self._delete_snapshot,
            str(session_id or ""),
            str(snapshot_ref or ""),
            str(actor_id or ""),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
        )

    def _delete_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        snapshot_ref = clean_text(snapshot_ref, max_chars=200)
        actor_id = clean_text(actor_id, max_chars=200)
        if not snapshot_ref:
            raise ValueError("缺少要删除的存档")
        if not actor_id:
            raise ValueError("存档操作缺少执行者")
        request = {
            "operation": "delete",
            "session_id": session_id,
            "snapshot_ref": snapshot_ref,
            "actor_id": actor_id,
            "expected_revision": int(expected_revision),
        }
        input_hash = self._snapshot_request_hash(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = self._snapshot_completed_receipt(
                    connection,
                    operation_id,
                    input_hash,
                )
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot = self._snapshot_row(
                    connection,
                    session_id,
                    snapshot_ref,
                )
                current_revision = self._snapshot_revision(
                    snapshot,
                    int(session["revision"] or 0),
                )
                if current_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "存档或副本状态已经变化，请刷新后重新确认删除"
                    )
                if str(snapshot["kind"] or "") in {
                    "safety",
                    "undo",
                    "final",
                }:
                    raise InvalidTransitionError(
                        "安全快照、回滚点与最终保护存档不能手动删除"
                    )
                public_snapshot = self._snapshot_public(
                    snapshot,
                    int(session["revision"] or 0),
                )
                connection.execute(
                    "DELETE FROM snapshots WHERE id = ?",
                    (snapshot["id"],),
                )
                result = {
                    "operation": "delete",
                    "state": "deleted",
                    "revision": int(session["revision"] or 0),
                    "snapshot": public_snapshot,
                    "replayed": False,
                }
                now = utc_now()
                self._store_snapshot_receipt(
                    connection,
                    operation_id=operation_id,
                    session_id=session_id,
                    input_hash=input_hash,
                    result=result,
                    now=now,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "snapshot.delete",
                    snapshot["id"],
                    {"name": snapshot["name"]},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Restore one snapshot atomically with target CAS and response replay."""

        return await self._run(
            self._restore_snapshot,
            str(session_id or ""),
            str(snapshot_ref or ""),
            str(actor_id or ""),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
        )

    def _restore_snapshot(
        self,
        session_id: str,
        snapshot_ref: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        snapshot_ref = clean_text(snapshot_ref, max_chars=200)
        actor_id = clean_text(actor_id, max_chars=200)
        if not snapshot_ref:
            raise ValueError("缺少要恢复的存档")
        if not actor_id:
            raise ValueError("存档操作缺少执行者")
        request = {
            "operation": "restore",
            "session_id": session_id,
            "snapshot_ref": snapshot_ref,
            "actor_id": actor_id,
            "expected_revision": int(expected_revision),
        }
        input_hash = self._snapshot_request_hash(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay = self._snapshot_completed_receipt(
                    connection,
                    operation_id,
                    input_hash,
                )
                if replay is not None:
                    connection.execute("COMMIT")
                    return replay
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("会话不存在")
                self._assert_session_writable(connection, session_id)
                snapshot = self._snapshot_row(
                    connection,
                    session_id,
                    snapshot_ref,
                )
                current_revision = self._snapshot_revision(
                    snapshot,
                    int(session["revision"] or 0),
                )
                if current_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "存档或副本状态已经变化，请刷新后重新确认恢复"
                    )
                restored = self._apply_snapshot_restore(
                    connection,
                    session=session,
                    snapshot=snapshot,
                    actor_id=actor_id,
                )
                next_session_revision = int(restored["revision"] or 0)
                result = {
                    "operation": "restore",
                    "state": "paused",
                    "revision": next_session_revision,
                    "snapshot": self._snapshot_public(
                        snapshot,
                        next_session_revision,
                    ),
                    "replayed": False,
                }
                now = utc_now()
                self._store_snapshot_receipt(
                    connection,
                    operation_id=operation_id,
                    session_id=session_id,
                    input_hash=input_hash,
                    result=result,
                    now=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _restore_workflow_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        session_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT workflow_json FROM snapshot_workflows
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if not row:
            # Old snapshots had no workflow section. Cancel incompatible
            # in-flight work rather than mixing it with the restored branch.
            now = utc_now()
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
                UPDATE timer_instances SET status = 'cancelled', updated_at = ?
                WHERE session_id = ? AND status IN ('active', 'paused')
                """,
                (now, session_id),
            )
            return
        data = json_load(row["workflow_json"], {})
        if data.get("format") != "astrbot-tavern-workflow":
            raise ValueError("存档中的流程快照格式无效")
        for table in (
            "session_character_states",
            "assist_tokens",
            "vote_ballots",
            "group_votes",
            "choice_sets",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "participants",
            "actor_fate_states",
            "actor_fate_transitions",
            "rescue_windows",
            "character_capabilities",
            "character_resources",
            "terminal_receipts",
            "session_finalizations",
            "scene_clocks",
            "story_ledger",
            "session_characters",
            "session_rule_states",
            "dm_control_states",
        ):
            if table == "vote_ballots":
                connection.execute(
                    """
                    DELETE FROM vote_ballots
                    WHERE vote_id IN (
                        SELECT id FROM group_votes WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table in {
                "character_card_drafts",
                "card_binding_codes",
            }:
                connection.execute(
                    f"""
                    DELETE FROM {table}
                    WHERE participant_id IN (
                        SELECT id FROM participants WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "session_character_states":
                connection.execute(
                    """
                    DELETE FROM session_character_states
                    WHERE character_id IN (
                        SELECT id FROM session_characters
                        WHERE session_id = ?
                    )
                    """,
                    (session_id,),
                )
            elif table == "ban_records":
                connection.execute(
                    """
                    DELETE FROM ban_records
                    WHERE session_id = ? AND scope = 'instance'
                    """,
                    (session_id,),
                )
            else:
                connection.execute(
                    f"DELETE FROM {table} WHERE session_id = ?",
                    (session_id,),
                )
        columns: dict[str, tuple[str, ...]] = {
            "participants": (
                "id", "session_id", "player_id", "group_user_id",
                "private_user_id", "private_origin", "display_name",
                "character_card_id", "character_version_id",
                "character_name", "character_code", "aliases_json",
                "card_status", "card_stage", "ready",
                "participation_status", "seat_reserved_at",
                "joined_round", "consecutive_timeouts", "action_locked",
                "exit_reason", "created_at", "updated_at",
            ),
            "character_card_drafts": (
                "id", "participant_id", "template_version", "fields_json",
                "current_step", "status", "expires_at",
                "created_at", "updated_at",
            ),
            "card_binding_codes": (
                "id", "participant_id", "code", "status", "expires_at",
                "private_user_id", "private_origin", "created_at", "used_at",
            ),
            "character_runtime_states": (
                "id", "session_id", "participant_id", "character_card_id",
                "state_json", "revision", "created_at", "updated_at",
            ),
            "choice_sets": (
                "id", "session_id", "participant_id", "round_no",
                "session_revision", "choices_json", "status",
                "reroll_count", "selected_key", "flavor_text",
                "idempotency_key", "created_at", "updated_at",
            ),
            "group_votes": (
                "id", "session_id", "source_event_id", "question",
                "options_json", "eligible_user_ids_json", "stage", "status",
                "winner_key", "suspended_user_id", "deadline_at",
                "result_json", "created_at", "updated_at",
            ),
            "vote_ballots": (
                "id", "vote_id", "user_id", "option_key",
                "created_at", "updated_at",
            ),
            "selected_world_events": (
                "id", "session_id", "round_no", "pool_item_id",
                "payload_json", "status", "narrative",
                "created_at", "resolved_at",
            ),
            "timer_instances": (
                "id", "session_id", "participant_id", "timer_type",
                "status", "deadline_at", "remaining_seconds",
                "reminder_at", "reminder_sent", "action_json",
                "created_at", "updated_at",
            ),
            "delegation_grants": (
                "id", "session_id", "participant_id", "owner_user_id",
                "delegate_user_id", "permissions_json", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "permission_grants": (
                "id", "session_id", "user_id", "role",
                "granted_by", "created_at",
            ),
            "return_requests": (
                "id", "session_id", "participant_id", "requested_by",
                "status", "exit_type", "objective", "progress_json",
                "vote_id", "created_at", "updated_at",
            ),
            "ban_records": (
                "id", "session_id", "platform_id", "group_id", "user_id",
                "participant_id", "scope", "reason", "actor_id", "status",
                "expires_at", "created_at", "updated_at",
            ),
            "session_rule_states": (
                "session_id", "progress_json",
                "content_boundaries_json", "npc_policy_json",
                "context_budget_json", "dice_rules_json", "recovery_json",
                "revision", "created_at", "updated_at",
            ),
            "dm_control_states": (
                "session_id", "mode", "active_dm_user_id", "phase",
                "directive", "beat_no", "current_actor_type",
                "current_actor_ref", "preserved_turn_json", "revision",
                "created_at", "updated_at",
            ),
            "session_characters": (
                "id", "session_id", "stable_key", "name", "aliases_json",
                "role_type", "public_profile_json", "known_facts_json",
                "misconceptions_json", "source", "review_status",
                "lifecycle_status", "persistent", "first_event_id",
                "last_event_id", "first_turn", "last_turn", "revision",
                "created_at", "updated_at",
            ),
            "session_character_states": (
                "character_id", "state_json", "revision", "updated_at",
            ),
            "actor_fate_states": (
                "character_id", "session_id", "state", "state_label",
                "can_act", "terminal", "transitioned_at",
                "rescue_window_until", "rescue_window_kind", "reason",
                "source", "revision", "updated_at",
            ),
            "actor_fate_transitions": (
                "id", "session_id", "character_id", "from_state",
                "to_state", "reason", "source", "reversible",
                "rescue_window", "protection_consumed", "event_id",
                "created_at",
            ),
            "rescue_windows": (
                "id", "session_id", "character_id", "kind", "status",
                "opened_at", "expires_on",
                "allowed_rescue_commands_json",
                "success_transition_json", "failure_transition_json",
                "command_labels_json", "command", "outcome",
                "completed_at", "revision", "created_at", "updated_at",
            ),
            "character_capabilities": (
                "id", "session_id", "character_id", "capability_ref",
                "source_ref", "state_json", "available", "created_at",
                "updated_at",
            ),
            "character_resources": (
                "id", "session_id", "character_id", "resource_ref",
                "label", "current", "maximum", "state_json",
                "created_at", "updated_at",
            ),
            "terminal_receipts": (
                "id", "session_id", "condition_id", "condition_label",
                "priority", "ending_ref", "termination_type",
                "archive_policy", "trigger_revision", "payload_json",
                "status", "idempotency_key", "created_at", "updated_at",
            ),
            "session_finalizations": (
                "session_id", "status", "termination_type", "ending_ref",
                "ending_label", "archive_policy", "idempotency_key",
                "input_locked", "snapshot_status", "final_snapshot_id",
                "payload_json", "attempts", "last_error", "created_at",
                "updated_at",
            ),
            "story_ledger": (
                "id", "session_id", "stable_key", "kind", "title",
                "description", "status", "visibility", "source_event_id",
                "completed_event_id", "revision", "created_at", "updated_at",
            ),
            "scene_clocks": (
                "id", "session_id", "stable_key", "title", "segments",
                "current_value", "visibility", "trigger_text", "status",
                "triggered_event_id", "revision", "created_at", "updated_at",
            ),
            "assist_tokens": (
                "id", "session_id", "source_participant_id",
                "target_participant_id", "stat", "method", "status",
                "expires_round", "source_event_id", "created_at",
                "consumed_at",
            ),
        }
        insert_order = (
            "session_rule_states",
            "dm_control_states",
            "participants",
            "character_card_drafts",
            "card_binding_codes",
            "character_runtime_states",
            "choice_sets",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "return_requests",
            "ban_records",
            "session_characters",
            "session_character_states",
            "actor_fate_states",
            "actor_fate_transitions",
            "rescue_windows",
            "character_capabilities",
            "character_resources",
            "terminal_receipts",
            "session_finalizations",
            "story_ledger",
            "scene_clocks",
            "assist_tokens",
        )
        for table in insert_order:
            rows = data.get(table, [])
            snapshot_version = int(data.get("version", 1) or 1)
            if table in {
                "session_rule_states",
                "dm_control_states",
                "session_characters",
                "session_character_states",
                "story_ledger",
                "scene_clocks",
                "assist_tokens",
            } and snapshot_version < 2:
                rows = []
            if table in {
                "actor_fate_states",
                "actor_fate_transitions",
                "rescue_windows",
                "character_capabilities",
                "character_resources",
                "terminal_receipts",
                "session_finalizations",
            } and snapshot_version < 4:
                # 旧格式快照没有这些领域表：不尝试兼容补造，按旧行为
                # 在删除阶段清空后不再重建。
                rows = []
            if not isinstance(rows, list):
                raise ValueError(f"流程快照表 {table} 格式错误")
            if table == "participants" and snapshot_version < 4:
                # D1 Schema 20 新增列：旧快照行缺列时补默认值，避免
                # 恢复失败；v4 快照行本身已含真实值。
                rows = [
                    {
                        **dict(row),
                        "card_stage": row.get("card_stage", ""),
                        "action_locked": row.get("action_locked", 0),
                    }
                    for row in rows
                ]
            self._import_rows(
                connection,
                table,
                rows,
                columns[table],
            )
        if not data.get("session_rule_states"):
            self._initialize_current_rows(connection)
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'paused', reminder_at = '', reminder_sent = 0
            WHERE session_id = ? AND status = 'active'
            """,
            (session_id,),
        )

    @classmethod
    def _archive_trash_request(
        cls,
        *,
        session_id: str,
        filename: str,
        actor_id: str,
        expected_revision: int,
    ) -> tuple[dict[str, Any], str]:
        request = {
            "operation": "archive.trash",
            "session_id": str(session_id or ""),
            "filename": clean_text(filename, max_chars=240),
            "actor_id": clean_text(actor_id, max_chars=200),
            "expected_revision": int(expected_revision),
        }
        if not request["filename"]:
            raise ValueError("缺少要移入回收目录的独立存档")
        if not request["actor_id"]:
            raise ValueError("存档操作缺少执行者")
        return request, cls._snapshot_request_hash(request)

    async def prepare_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        plan: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read or reserve the durable half of the filesystem trash workflow."""

        return await self._run(
            self._prepare_archive_trash,
            str(session_id or ""),
            str(filename or ""),
            str(actor_id or ""),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
            dict(plan) if isinstance(plan, Mapping) else None,
        )

    def _prepare_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
        plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        request, input_hash = self._archive_trash_request(
            session_id=session_id,
            filename=filename,
            actor_id=actor_id,
            expected_revision=expected_revision,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if (
                        str(receipt["operation_type"] or "")
                        != "archive.trash"
                        or str(receipt["input_hash"] or "") != input_hash
                    ):
                        raise DatabaseConflictError(
                            "该防重复凭据已用于另一项独立存档操作"
                        )
                    status = str(receipt["status"] or "")
                    result = json_load(receipt["result_json"], {})
                    stored_plan = json_load(receipt["plan_json"], {})
                    if status == "completed":
                        if not isinstance(result, Mapping):
                            raise DatabaseConflictError(
                                "独立存档操作回执无法安全重放"
                            )
                        replay_result = {
                            "status": "completed",
                            "result": {**dict(result), "replayed": True},
                            "plan": {},
                        }
                        connection.execute("COMMIT")
                        return replay_result
                    self._assert_archive_trash_plan(stored_plan)
                    prepared = {
                        "status": status,
                        "result": {},
                        "plan": dict(stored_plan),
                    }
                    connection.execute("COMMIT")
                    return prepared
                self._assert_session_writable(connection, session_id)
                if plan is None:
                    connection.execute("COMMIT")
                    return {"status": "unreserved", "result": {}, "plan": {}}
                self._assert_archive_trash_plan(plan)
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        retry_count, plan_json, input_hash,
                        created_at, updated_at
                    ) VALUES (?, ?, 'archive.trash', ?, '{}',
                              'reserved', 'filesystem_planned', 0, ?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request),
                        json_dump(plan),
                        input_hash,
                        now,
                        now,
                    ),
                )
                connection.execute("COMMIT")
                return {"status": "reserved", "result": {}, "plan": plan}
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _assert_archive_trash_plan(plan: Any) -> None:
        if not isinstance(plan, Mapping):
            raise DatabaseConflictError("独立存档回收计划缺失")
        source_path = str(plan.get("source_path") or "")
        destination_path = str(plan.get("destination_path") or "")
        fingerprint = str(plan.get("fingerprint") or "")
        identity = plan.get("archive_identity")
        if (
            not source_path
            or not destination_path
            or source_path == destination_path
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or not isinstance(identity, Mapping)
        ):
            raise DatabaseConflictError("独立存档回收计划无法安全恢复")

    async def complete_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._complete_archive_trash,
            str(session_id or ""),
            str(filename or ""),
            str(actor_id or ""),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
        )
