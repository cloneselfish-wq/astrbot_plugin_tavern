from __future__ import annotations

from .story_support import *
from ..recovery_ranges import RecoveryState, parse_recovery_json


def _validated_restore_workflow(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    session_id: str,
) -> tuple[dict[str, Any], RecoveryState]:
    row = connection.execute(
        """
        SELECT workflow_json FROM snapshot_workflows
        WHERE snapshot_id = ?
        """,
        (snapshot_id,),
    ).fetchone()
    raw_workflow = row["workflow_json"] if row else None
    if not isinstance(raw_workflow, str):
        raise ValueError("存档缺少可验证的流程状态，无法安全恢复")
    try:
        workflow = json.loads(raw_workflow)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("存档流程状态损坏，无法安全恢复") from exc
    if (
        not isinstance(workflow, Mapping)
        or workflow.get("format") != "astrbot-tavern-workflow"
        or workflow.get("version") != 4
    ):
        raise ValueError("存档流程状态格式无效，无法安全恢复")
    rule_rows = workflow.get("session_rule_states")
    if not isinstance(rule_rows, (list, tuple)) or len(rule_rows) != 1:
        raise ValueError("存档副本规则状态不完整，无法安全恢复")
    rule_row = rule_rows[0]
    if (
        not isinstance(rule_row, Mapping)
        or str(rule_row.get("session_id") or "") != session_id
    ):
        raise ValueError("存档副本规则状态归属无效，无法安全恢复")
    recovery_state = parse_recovery_json(rule_row.get("recovery_json"))
    return dict(workflow), recovery_state


class SnapshotsRepositoryMixin:
    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        name: str,
        kind: str,
        created_by: str,
        *,
        replace: bool,
    ) -> str:
        name = clean_text(name, max_chars=100)
        if not name:
            raise ValueError("存档名不能为空")
        # Fresh Schema 29 snapshots always carry the one authoritative rule
        # row needed by strict restore validation.
        self._initialize_current_rows(connection)
        snapshot_id = new_id("save")
        if replace:
            connection.execute(
                """
                DELETE FROM snapshots
                WHERE session_id = ? AND name = ?
                """,
                (session["id"], name),
            )
        elif connection.execute(
            """
            SELECT 1 FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone():
            raise ValueError(
                "已存在同名存档；请确认后使用覆盖模式"
            )
        sql = """
            INSERT INTO snapshots(
                id, session_id, name, kind, turn_no, session_revision,
                world_id, world_state_json, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
        connection.execute(
            sql,
            (
                snapshot_id,
                session["id"],
                name,
                kind,
                session["turn_no"],
                session["revision"],
                session["world_id"],
                session["world_state_json"],
                created_by,
                utc_now(),
            ),
        )
        row = connection.execute(
            """
            SELECT id FROM snapshots
            WHERE session_id = ? AND name = ?
            """,
            (session["id"], name),
        ).fetchone()
        snapshot_id = str(row["id"])
        workflow = self._collect_workflow_snapshot(
            connection,
            session["id"],
        )
        connection.execute(
            """
            INSERT INTO snapshot_workflows(snapshot_id, workflow_json)
            VALUES (?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                workflow_json = excluded.workflow_json
            """,
            (snapshot_id, json_dump(workflow)),
        )
        self._upsert_snapshot_checkpoint(
            connection,
            session=session,
            snapshot_id=snapshot_id,
            kind=kind,
            name=name,
            workflow=workflow,
        )
        return snapshot_id

    def _upsert_snapshot_checkpoint(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        snapshot_id: str,
        kind: str,
        name: str,
        workflow: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """在同一事务内写入快照投影检查点（WP-11 完整重建锚点）。

        - ``last_seq`` 取该副本当前 session_events 最新序号，检查点之后
          的事件即为增量，可用于「检查点 + 增量 = 完整重建」；
        - payload 只存放能定位/验证重建锚点的内部字段：snapshot_id、
          名称、kind、turn、revision、event_anchor_seq 与
          world_state 哈希；
        - 终局/迁移的 final 快照在归档事务提交前写入，允许；已永久
          归档（readonly）的副本禁止再写检查点。
        """
        session_id = str(session["id"])
        archived = connection.execute(
            """
            SELECT 1 FROM session_archives
            WHERE session_id = ? AND readonly = 1
            """,
            (session_id,),
        ).fetchone()
        if archived is not None:
            raise InvalidTransitionError("该副本已永久归档并处于只读状态")
        last_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) FROM session_events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        world_state = str(session["world_state_json"] or "")
        payload = {
            "snapshot_id": str(snapshot_id),
            "name": str(name or "")[:100],
            "kind": str(kind or "")[:40],
            "turn_no": int(session["turn_no"] or 0),
            "revision": int(session["revision"] or 0),
            "event_anchor_seq": int(
                workflow.get("event_anchor_seq", 0)
                if isinstance(workflow, Mapping)
                else 0
            ),
            "world_state_hash": hashlib.sha256(
                world_state.encode("utf-8")
            ).hexdigest(),
        }
        now = utc_now()
        connection.execute(
            """
            INSERT INTO projection_checkpoints(
                session_id, projection_name, last_seq,
                payload_json, revision, updated_at
            ) VALUES (?, 'snapshot', ?, ?, 1, ?)
            ON CONFLICT(session_id, projection_name) DO UPDATE SET
                last_seq = excluded.last_seq,
                payload_json = excluded.payload_json,
                revision = projection_checkpoints.revision + 1,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                last_seq,
                json_dump(payload),
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM projection_checkpoints
            WHERE session_id = ? AND projection_name = 'snapshot'
            """,
            (session_id,),
        ).fetchone()
        return dict(row)

    @staticmethod
    def _collect_workflow_snapshot(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> dict[str, Any]:
        session = connection.execute(
            """
            SELECT history_floor_seq FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        event_anchor_seq = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(seq), 0) FROM events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()[0]
        )
        tables = {
            "participants": (
                "SELECT * FROM participants WHERE session_id = ?"
            ),
            "character_runtime_states": (
                "SELECT * FROM character_runtime_states WHERE session_id = ?"
            ),
            "choice_sets": (
                "SELECT * FROM choice_sets WHERE session_id = ?"
            ),
            "group_votes": (
                "SELECT * FROM group_votes WHERE session_id = ?"
            ),
            "selected_world_events": (
                "SELECT * FROM selected_world_events WHERE session_id = ?"
            ),
            "timer_instances": (
                "SELECT * FROM timer_instances WHERE session_id = ?"
            ),
            "delegation_grants": (
                "SELECT * FROM delegation_grants WHERE session_id = ?"
            ),
            "permission_grants": (
                "SELECT * FROM permission_grants WHERE session_id = ?"
            ),
            "return_requests": (
                "SELECT * FROM return_requests WHERE session_id = ?"
            ),
            "ban_records": (
                """
                SELECT * FROM ban_records
                WHERE session_id = ? AND scope = 'instance'
                """
            ),
            "session_rule_states": (
                "SELECT * FROM session_rule_states WHERE session_id = ?"
            ),
            "dm_control_states": (
                "SELECT * FROM dm_control_states WHERE session_id = ?"
            ),
            "session_characters": (
                "SELECT * FROM session_characters WHERE session_id = ?"
            ),
            "story_ledger": (
                "SELECT * FROM story_ledger WHERE session_id = ?"
            ),
            "scene_clocks": (
                "SELECT * FROM scene_clocks WHERE session_id = ?"
            ),
            "assist_tokens": (
                "SELECT * FROM assist_tokens WHERE session_id = ?"
            ),
            # D1 Schema 20：命运/救援/资源/能力/终局领域表必须随快照
            # 一起保存，否则回档后命运状态、救援窗口、职业资源与能力、
            # 终局回执和最终化状态全部丢失（D1-RUN-018 备份恢复）。
            "actor_fate_states": (
                "SELECT * FROM actor_fate_states WHERE session_id = ?"
            ),
            "actor_fate_transitions": (
                "SELECT * FROM actor_fate_transitions WHERE session_id = ?"
            ),
            "rescue_windows": (
                "SELECT * FROM rescue_windows WHERE session_id = ?"
            ),
            "character_capabilities": (
                "SELECT * FROM character_capabilities WHERE session_id = ?"
            ),
            "character_resources": (
                "SELECT * FROM character_resources WHERE session_id = ?"
            ),
            "terminal_receipts": (
                "SELECT * FROM terminal_receipts WHERE session_id = ?"
            ),
            "session_finalizations": (
                "SELECT * FROM session_finalizations WHERE session_id = ?"
            ),
        }
        result: dict[str, Any] = {
            "format": "astrbot-tavern-workflow",
            # 4：加入 D1 Schema 20 命运/救援/资源/能力/终局领域表，
            # 以及 participants 的 card_stage/action_locked 持久化列。
            "version": 4,
            "history_floor_seq": int(
                session["history_floor_seq"] if session else 0
            ),
            "event_anchor_seq": event_anchor_seq,
        }
        vote_ids: list[str] = []
        participant_ids: list[str] = []
        session_character_ids: list[str] = []
        for table, query in tables.items():
            rows = connection.execute(query, (session_id,)).fetchall()
            result[table] = [dict(row) for row in rows]
            if table == "group_votes":
                vote_ids = [str(row["id"]) for row in rows]
            if table == "participants":
                participant_ids = [str(row["id"]) for row in rows]
            if table == "session_characters":
                session_character_ids = [str(row["id"]) for row in rows]
        if vote_ids:
            placeholders = ",".join("?" for _ in vote_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM vote_ballots
                WHERE vote_id IN ({placeholders})
                """,
                tuple(vote_ids),
            ).fetchall()
            result["vote_ballots"] = [dict(row) for row in rows]
        else:
            result["vote_ballots"] = []
        if participant_ids:
            placeholders = ",".join("?" for _ in participant_ids)
            for table in (
                "character_card_drafts",
                "card_binding_codes",
            ):
                rows = connection.execute(
                    f"""
                    SELECT * FROM {table}
                    WHERE participant_id IN ({placeholders})
                    """,
                    tuple(participant_ids),
                ).fetchall()
                result[table] = [dict(row) for row in rows]
        else:
            result["character_card_drafts"] = []
            result["card_binding_codes"] = []
        if session_character_ids:
            placeholders = ",".join("?" for _ in session_character_ids)
            rows = connection.execute(
                f"""
                SELECT * FROM session_character_states
                WHERE character_id IN ({placeholders})
                """,
                tuple(session_character_ids),
            ).fetchall()
            result["session_character_states"] = [
                dict(row) for row in rows
            ]
        else:
            result["session_character_states"] = []
        return result

    @staticmethod
    def _snapshot_revision(
        snapshot: Mapping[str, Any] | sqlite3.Row,
        session_revision: int,
    ) -> int:
        """Return a JS-safe CAS value without exposing the stored snapshot id."""

        value = dict(snapshot)
        canonical = json_dump(
            {
                "snapshot_id": str(value.get("id") or ""),
                "session_id": str(value.get("session_id") or ""),
                "name": str(value.get("name") or ""),
                "kind": str(value.get("kind") or ""),
                "turn_no": int(value.get("turn_no") or 0),
                "snapshot_session_revision": int(
                    value.get("session_revision") or 0
                ),
                "current_session_revision": int(session_revision),
                "created_at": str(value.get("created_at") or ""),
            }
        )
        return int(hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:13], 16)

    @classmethod
    def _snapshot_public(
        cls,
        snapshot: Mapping[str, Any] | sqlite3.Row,
        session_revision: int,
    ) -> dict[str, Any]:
        value = dict(snapshot)
        return {
            "name": str(value.get("name") or ""),
            "kind": str(value.get("kind") or "manual"),
            "turn_no": int(value.get("turn_no") or 0),
            "revision": cls._snapshot_revision(value, session_revision),
            "created_at": str(value.get("created_at") or ""),
        }

    @staticmethod
    def _snapshot_request_hash(request: Mapping[str, Any]) -> str:
        return hashlib.sha256(json_dump(dict(request)).encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot_operation_key(value: str) -> str:
        operation_id = clean_text(value, max_chars=200)
        if not operation_id:
            raise ValueError("存档操作缺少防重复凭据")
        return operation_id

    @staticmethod
    def _snapshot_row(
        connection: sqlite3.Connection,
        session_id: str,
        snapshot_ref: str,
    ) -> sqlite3.Row:
        snapshot_ref = clean_text(snapshot_ref, max_chars=200)
        if not snapshot_ref:
            raise ValueError("缺少存档目标")
        row = connection.execute(
            """
            SELECT * FROM snapshots
            WHERE session_id = ? AND (id = ? OR name = ?)
            ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END,
                     created_at DESC, rowid DESC
            LIMIT 1
            """,
            (session_id, snapshot_ref, snapshot_ref, snapshot_ref),
        ).fetchone()
        if row is None:
            raise DatabaseNotFoundError("存档不存在")
        return row

    @staticmethod
    def _snapshot_completed_receipt(
        connection: sqlite3.Connection,
        operation_id: str,
        input_hash: str,
    ) -> dict[str, Any] | None:
        receipt = connection.execute(
            """
            SELECT input_hash, status, result_json
            FROM operation_commits WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if receipt is None:
            return None
        if str(receipt["input_hash"] or "") != input_hash:
            raise DatabaseConflictError(
                "该防重复凭据已用于另一项存档操作"
            )
        if str(receipt["status"] or "") != "completed":
            raise DatabaseConflictError(
                "该存档操作仍在处理中，请稍后重试"
            )
        result = json_load(receipt["result_json"], {})
        if not isinstance(result, Mapping):
            raise DatabaseConflictError("存档操作回执无法安全重放")
        return {**dict(result), "replayed": True}

    @staticmethod
    def _store_snapshot_receipt(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        session_id: str,
        input_hash: str,
        result: Mapping[str, Any],
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO operation_commits(
                operation_id, session_id, input_hash, status,
                result_json, rollback_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
            """,
            (
                operation_id,
                session_id,
                input_hash,
                json_dump(dict(result)),
                now,
                now,
            ),
        )

    async def snapshot_action_context(
        self,
        session_id: str,
        snapshot_ref: str = "",
    ) -> dict[str, Any]:
        """Load the current CAS value used by a server-side action descriptor."""

        return await self._run(
            self._snapshot_action_context,
            str(session_id or ""),
            str(snapshot_ref or ""),
        )

    def _snapshot_action_context(
        self,
        session_id: str,
        snapshot_ref: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise DatabaseNotFoundError("会话不存在")
            self._assert_session_writable(connection, session_id)
            result = {
                "revision": int(session["revision"] or 0),
                "state": str(session["state"] or ""),
            }
            if snapshot_ref:
                snapshot = self._snapshot_row(
                    connection,
                    session_id,
                    snapshot_ref,
                )
                result.update(
                    {
                        "revision": self._snapshot_revision(
                            snapshot,
                            int(session["revision"] or 0),
                        ),
                        "snapshot": self._snapshot_public(
                            snapshot,
                            int(session["revision"] or 0),
                        ),
                    }
                )
            return result

    def _complete_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
    ) -> dict[str, Any]:
        _request, input_hash = self._archive_trash_request(
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
                if receipt is None:
                    raise DatabaseConflictError("独立存档回收计划尚未建立")
                if (
                    str(receipt["operation_type"] or "")
                    != "archive.trash"
                    or str(receipt["input_hash"] or "") != input_hash
                ):
                    raise DatabaseConflictError(
                        "该防重复凭据已用于另一项独立存档操作"
                    )
                if str(receipt["status"] or "") == "completed":
                    result = json_load(receipt["result_json"], {})
                    if not isinstance(result, Mapping):
                        raise DatabaseConflictError(
                            "独立存档操作回执无法安全重放"
                        )
                    connection.execute("COMMIT")
                    return {**dict(result), "replayed": True}
                if str(receipt["status"] or "") not in {
                    "reserved",
                    "failed_retryable",
                }:
                    raise DatabaseConflictError("独立存档回收状态无法提交")
                result = {
                    "operation": "trash",
                    "state": "trashed",
                    "revision": int(expected_revision),
                    "archive": {"kind": "save", "state": "trashed"},
                    "replayed": False,
                }
                now = utc_now()
                connection.execute(
                    """
                    UPDATE operation_receipts SET
                        result_json = ?, status = 'completed',
                        phase = 'filesystem_reconciled',
                        last_error_code = '', updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (json_dump(result), now, operation_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "storage.archive_trash",
                    "",
                    {"kind": "save", "filename": filename},
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def fail_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        error_code: str,
    ) -> None:
        await self._run(
            self._fail_archive_trash,
            str(session_id or ""),
            str(filename or ""),
            str(actor_id or ""),
            int(expected_revision),
            self._snapshot_operation_key(idempotency_key),
            str(error_code or "archive.trash_failed"),
        )

    def _fail_archive_trash(
        self,
        session_id: str,
        filename: str,
        actor_id: str,
        expected_revision: int,
        operation_id: str,
        error_code: str,
    ) -> None:
        _request, input_hash = self._archive_trash_request(
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
                if receipt is None:
                    raise DatabaseConflictError("独立存档回收计划尚未建立")
                if (
                    str(receipt["operation_type"] or "")
                    != "archive.trash"
                    or str(receipt["input_hash"] or "") != input_hash
                ):
                    raise DatabaseConflictError(
                        "该防重复凭据已用于另一项独立存档操作"
                    )
                if str(receipt["status"] or "") != "completed":
                    connection.execute(
                        """
                        UPDATE operation_receipts SET
                            status = 'failed_retryable',
                            phase = 'reconcile_required',
                            retry_count = retry_count + 1,
                            last_error_code = ?, updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (
                            clean_text(error_code, max_chars=120),
                            utc_now(),
                            operation_id,
                        ),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_snapshots(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_snapshots, session_id)

    def _list_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            session = connection.execute(
                "SELECT revision FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise DatabaseNotFoundError("会话不存在")
            session_revision = int(session["revision"] or 0)
            rows = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE session_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = self._snapshot(row)
                item["revision"] = self._snapshot_revision(
                    row,
                    session_revision,
                )
                result.append(item)
            return result

    def _apply_snapshot_restore(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        snapshot: sqlite3.Row,
        actor_id: str,
    ) -> sqlite3.Row:
        """Apply an already-authorized restore inside the caller transaction."""

        session_id = str(session["id"])
        rule_row = connection.execute(
            """
            SELECT recovery_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        parse_recovery_json(
            rule_row["recovery_json"] if rule_row else "{}"
        )
        workflow, restored_recovery_state = _validated_restore_workflow(
            connection,
            snapshot_id=str(snapshot["id"]),
            session_id=session_id,
        )
        self._insert_snapshot(
            connection,
            session,
            f"safety-before-restore-{session['revision']}",
            "safety",
            actor_id,
            replace=True,
        )
        max_seq = connection.execute(
            """
            SELECT COALESCE(MAX(seq), 0)
            FROM events WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()[0]
        floor = bounded_int(
            workflow.get("history_floor_seq"),
            int(session["history_floor_seq"] or 0),
            0,
            int(max_seq) + 1,
        )
        anchor = bounded_int(
            workflow.get("event_anchor_seq"),
            int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0) FROM events
                    WHERE session_id = ? AND created_at <= ?
                    """,
                    (session_id, snapshot["created_at"]),
                ).fetchone()[0]
            ),
            0,
            int(max_seq),
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE sessions SET
                world_id = ?, turn_no = ?, world_state_json = ?,
                history_floor_seq = ?, revision = revision + 1,
                state = 'paused', updated_at = ?
            WHERE id = ?
            """,
            (
                snapshot["world_id"],
                snapshot["turn_no"],
                snapshot["world_state_json"],
                floor,
                now,
                session_id,
            ),
        )
        self._restore_workflow_snapshot(
            connection,
            snapshot["id"],
            session_id,
        )
        recovery = dict(restored_recovery_state.payload)
        excluded = [
            [start, end]
            for start, end in restored_recovery_state.excluded_event_ranges
        ]
        if anchor + 1 <= int(max_seq):
            excluded.append([anchor + 1, int(max_seq)])
        recovery.update(
            {
                "state": "restored",
                "snapshot_id": str(snapshot["id"]),
                "event_anchor_seq": anchor,
                "excluded_event_ranges": excluded[-64:],
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
        append_event(
            connection,
            session_id=session_id,
            turn_no=snapshot["turn_no"],
            role="system",
            actor_id=actor_id,
            actor_name="开团系统",
            content=f"已恢复存档「{snapshot['name']}」，会话已暂停。",
            meta={
                "kind": "snapshot.restored",
                "snapshot_id": snapshot["id"],
                "restored": True,
            },
            created_at=now,
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id,
            "snapshot.restore",
            snapshot["id"],
            {
                "name": snapshot["name"],
                "turn_no": snapshot["turn_no"],
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
        if row is None:
            raise DatabaseNotFoundError("会话不存在")
        return row
