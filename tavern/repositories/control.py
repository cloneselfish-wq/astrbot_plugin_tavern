"""Session-level AI/DM control mode and non-player narrative commits."""

from ..database_support import *
from ..narrative_modes import narrative_mode_view, normalize_narrative_mode
from ..story_pacing import compute_turn_progress_indicators
from .events import append_event
from .story_support import _owner_tuple_locked
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    NarrativeDocument,
    canonical_narrative_json,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
)


class ControlRepositoryMixin:
    @staticmethod
    def _control_state(row: sqlite3.Row | None, session_id: str) -> dict[str, Any]:
        if not row:
            return {
                "session_id": session_id,
                "mode": "auto",
                "active_dm_user_id": "",
                "phase": "auto",
                "directive": "",
                "beat_no": 0,
                "current_actor_type": "",
                "current_actor_ref": "",
                "preserved_turn": {},
                "revision": 0,
                "updated_at": "",
            }
        return {
            "session_id": row["session_id"],
            "mode": row["mode"],
            "active_dm_user_id": row["active_dm_user_id"],
            "phase": row["phase"],
            "directive": row["directive"],
            "beat_no": int(row["beat_no"]),
            "current_actor_type": row["current_actor_type"],
            "current_actor_ref": row["current_actor_ref"],
            "preserved_turn": json_load(row["preserved_turn_json"], {}),
            "revision": int(row["revision"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def get_control_state(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_control_state, session_id)

    async def get_narrative_mode(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._get_narrative_mode, session_id)

    def _get_narrative_mode(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT ic.phase_meta_json, ic.updated_at
                FROM instance_configs ic
                JOIN sessions s ON s.id=ic.session_id
                WHERE ic.session_id=?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                raise DatabaseNotFoundError("副本叙事设置不存在")
            phase = json_load(row["phase_meta_json"], {})
            phase = dict(phase) if isinstance(phase, Mapping) else {}
            return narrative_mode_view(
                phase.get("narrative_mode"),
                revision=int(phase.get("narrative_mode_revision") or 0),
                updated_at=str(
                    phase.get("narrative_mode_updated_at")
                    or row["updated_at"]
                    or ""
                ),
            )

    async def set_narrative_mode(
        self,
        session_id: str,
        mode: str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_narrative_mode,
            session_id,
            mode,
            int(expected_revision),
            actor_id,
            idempotency_key,
        )

    def _set_narrative_mode(
        self,
        session_id: str,
        mode: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        requested = str(mode or "").strip().lower()
        normalized = normalize_narrative_mode(requested)
        if requested != normalized:
            raise ValueError("正文模式必须为 minimal、balanced 或 epic")
        request_key = clean_text(idempotency_key, max_chars=160)
        if not request_key:
            raise ValueError("切换正文模式需要幂等键")
        request_payload = {
            "mode": normalized,
            "expected_revision": int(expected_revision),
        }
        input_hash = hashlib.sha256(
            json_dump(request_payload).encode("utf-8")
        ).hexdigest()
        operation_id = (
            "narrative-mode:"
            + hashlib.sha256(
                f"{session_id}\0{request_key}".encode("utf-8")
            ).hexdigest()[:24]
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                receipt = connection.execute(
                    "SELECT * FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if receipt is not None:
                    if str(receipt["input_hash"] or "") != input_hash:
                        raise DatabaseConflictError(
                            "相同幂等键已用于另一份正文模式修改"
                        )
                    if str(receipt["status"] or "") == "completed":
                        result = json_load(receipt["result_json"], {})
                        result["replayed"] = True
                        connection.execute("COMMIT")
                        return result
                    raise DatabaseConflictError(
                        "正文模式修改仍在处理中，请稍后重试"
                    )
                session = connection.execute(
                    "SELECT state FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                self._assert_session_writable(connection, session_id)
                config = connection.execute(
                    "SELECT * FROM instance_configs WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if config is None:
                    raise DatabaseNotFoundError("副本叙事设置不存在")
                phase = json_load(config["phase_meta_json"], {})
                phase = dict(phase) if isinstance(phase, Mapping) else {}
                current_revision = int(
                    phase.get("narrative_mode_revision") or 0
                )
                if current_revision != int(expected_revision):
                    raise DatabaseConflictError(
                        "正文模式已经变化；已保留你的选择，请刷新比较后重试"
                    )
                now = utc_now()
                next_revision = current_revision + 1
                phase.update(
                    {
                        "narrative_mode": normalized,
                        "narrative_mode_revision": next_revision,
                        "narrative_mode_updated_at": now,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO operation_receipts(
                        operation_id, session_id, operation_type,
                        request_json, result_json, status, phase,
                        input_hash, created_at, updated_at
                    ) VALUES (?, ?, 'narrative.mode_update', ?, '{}',
                              'pending', 'configure', ?, ?, ?)
                    """,
                    (
                        operation_id,
                        session_id,
                        json_dump(request_payload),
                        input_hash,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET phase_meta_json=?, updated_at=?
                    WHERE session_id=?
                    """,
                    (json_dump(phase), now, session_id),
                )
                result = narrative_mode_view(
                    normalized,
                    revision=next_revision,
                    updated_at=now,
                )
                connection.execute(
                    """
                    UPDATE operation_receipts
                    SET result_json=?, status='completed', phase='committed',
                        updated_at=?
                    WHERE operation_id=?
                    """,
                    (json_dump(result), now, operation_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "narrative.mode_update",
                    session_id,
                    {
                        "mode": normalized,
                        "revision_before": current_revision,
                        "revision_after": next_revision,
                        "applies_to": "next_generation",
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{operation_id}:event",
                    type_="event:narrative_mode.changed",
                    actor_ref=actor_id,
                    command_id=operation_id,
                    payload={
                        "title": "正文模式已更新",
                        "summary": "新的正文篇幅会从下一次故事生成开始生效。",
                        "mode": normalized,
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

    def _get_control_state(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dm_control_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return self._control_state(row, session_id)

    async def get_narrative_control_view(
        self,
        session_id: str,
        *,
        viewer_role: str = "player",
        include_technical_refs: bool = False,
        host_labels: Mapping[str, str] | None = None,
        input_locked: bool = False,
    ) -> dict[str, Any]:
        """D1-UX-007：唯一叙事控制视图（普通视图不含 DM 用户 ID / revision）。"""
        return await self._run(
            self._get_narrative_control_view,
            session_id,
            viewer_role,
            include_technical_refs,
            dict(host_labels or {}),
            bool(input_locked),
        )

    def _get_narrative_control_view(
        self,
        session_id: str,
        viewer_role: str,
        include_technical_refs: bool,
        host_labels: dict[str, Any],
        input_locked: bool,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dm_control_states WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            control = self._control_state(row, session_id)
        from ..projections.session import project_narrative_control_view

        return project_narrative_control_view(
            control,
            host_labels=host_labels,
            input_locked=input_locked,
            viewer_role=viewer_role,
            include_technical_refs=include_technical_refs,
        )

    async def enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(
            self._enable_dm_mode, session_id, dm_user_id, actor_id
        )

    def _enable_dm_mode(
        self, session_id: str, dm_user_id: str, actor_id: str
    ) -> dict[str, Any]:
        dm_user_id = validate_platform_id(dm_user_id, label="主持人用户 ID")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("主持模式只能在运行中的副本开启")
                if connection.execute(
                    "SELECT 1 FROM group_votes WHERE session_id = ? AND status = 'open'",
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError(
                        "当前存在未结束的集体投票，请先结束或明确取消投票"
                    )
                now = utc_now()
                turn = turn_state_from_world(json_load(session["world_state_json"], {}))
                connection.execute(
                    """
                    INSERT INTO dm_control_states(
                        session_id, mode, active_dm_user_id, phase, directive,
                        beat_no, current_actor_type, current_actor_ref,
                        preserved_turn_json, revision, created_at, updated_at
                    ) VALUES (?, 'dm', ?, 'awaiting_dm', '', 0, '', '', ?, 1, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        mode = 'dm', active_dm_user_id = excluded.active_dm_user_id,
                        phase = 'awaiting_dm', directive = '',
                        current_actor_type = '', current_actor_ref = '',
                        preserved_turn_json = excluded.preserved_turn_json,
                        revision = dm_control_states.revision + 1,
                        updated_at = excluded.updated_at
                    """,
                    (session_id, dm_user_id, json_dump(turn), now, now),
                )
                connection.execute(
                    """
                    UPDATE choice_sets SET status = 'superseded', updated_at = ?
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE timer_instances SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'turn'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "dm_mode_enabled",
                    dm_user_id,
                    {"choice_status": "superseded_by_dm", "preserved_turn": turn},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(
            self._set_dm_directive, session_id, directive, actor_id
        )

    def _set_dm_directive(
        self, session_id: str, directive: str, actor_id: str
    ) -> dict[str, Any]:
        directive = clean_text(directive, max_chars=4000)
        if not directive:
            raise ValueError("主持指引不能为空")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    raise InvalidTransitionError("当前未开启主持模式")
                if actor_id != row["active_dm_user_id"]:
                    raise PermissionError("只有当前活动 DM 可以设置指引")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET directive = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (directive, now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_directive_saved",
                    session_id, {"length": len(directive)},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def set_dm_handoff(
        self,
        session_id: str,
        actor_type: str,
        actor_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_dm_handoff,
            session_id,
            actor_type,
            actor_ref,
            actor_id,
        )

    def _set_dm_handoff(
        self,
        session_id: str,
        actor_type: str,
        actor_ref: str,
        actor_id: str,
    ) -> dict[str, Any]:
        actor_type = str(actor_type or "").lower()
        if actor_type not in {"player", "npc"}:
            raise ValueError("交棒目标必须是 player 或 npc")
        actor_ref = clean_text(actor_ref, max_chars=160)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    raise InvalidTransitionError("当前未开启主持模式")
                if actor_id != row["active_dm_user_id"]:
                    raise PermissionError("只有当前活动 DM 可以交棒")
                phase = "player_handoff" if actor_type == "player" else "npc_handoff"
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET phase = ?,
                        current_actor_type = ?, current_actor_ref = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (phase, actor_type, actor_ref, now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_handoff_started",
                    actor_ref, {"actor_type": actor_type},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def finish_dm_handoff(self, session_id: str) -> dict[str, Any]:
        return await self._run(self._finish_dm_handoff, session_id)

    def _finish_dm_handoff(self, session_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row or row["mode"] != "dm":
                    connection.execute("COMMIT")
                    return self._control_state(row, session_id)
                now = utc_now()
                connection.execute(
                    "UPDATE choice_sets SET status = 'superseded', updated_at = ? WHERE session_id = ? AND status = 'active'",
                    (now, session_id),
                )
                connection.execute(
                    "UPDATE timer_instances SET status = 'cancelled', updated_at = ? WHERE session_id = ? AND timer_type = 'turn' AND status IN ('active','paused')",
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE dm_control_states SET phase = 'awaiting_dm',
                        current_actor_type = '', current_actor_ref = '',
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def disable_dm_mode(
        self, session_id: str, actor_id: str
    ) -> dict[str, Any]:
        return await self._run(self._disable_dm_mode, session_id, actor_id)

    def _disable_dm_mode(self, session_id: str, actor_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not row:
                    connection.execute("COMMIT")
                    return self._control_state(None, session_id)
                now = utc_now()
                connection.execute(
                    """
                    UPDATE dm_control_states SET mode = 'auto', phase = 'auto',
                        directive = '', current_actor_type = '',
                        current_actor_ref = '', revision = revision + 1,
                        updated_at = ? WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                self._insert_audit(
                    connection, session_id, actor_id, "dm_mode_disabled",
                    session_id, {},
                )
                row = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._control_state(row, session_id)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def commit_dm_beat(
        self,
        *,
        session_id: str,
        expected_revision: int,
        dm_user_id: str,
        instruction: str,
        narrative: str,
        narrative_document: NarrativeDocument | Mapping[str, Any],
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]] = (),
        model_payload: Mapping[str, Any] | None = None,
        workflow: Mapping[str, Any] | None = None,
        direct: bool = False,
        item_ops: Sequence[Mapping[str, Any]] | None = None,
        economy_ops: Sequence[Mapping[str, Any]] | None = None,
        operation_id: str = "",
    ) -> dict[str, Any]:
        """C6：模型提议的经济操作与主持剧情同一事务提交。"""
        return await self._run(
            self._commit_dm_beat,
            session_id,
            expected_revision,
            dm_user_id,
            instruction,
            narrative,
            (
                narrative_document.to_dict()
                if isinstance(narrative_document, NarrativeDocument)
                else dict(narrative_document)
            ),
            dict(world_state),
            [dict(item) for item in memories],
            dict(model_payload or {}),
            dict(workflow or {}),
            direct,
            [dict(op) for op in (item_ops or ())],
            [dict(op) for op in (economy_ops or ())],
            str(operation_id or ""),
        )

    def _commit_dm_beat(
        self,
        session_id: str,
        expected_revision: int,
        dm_user_id: str,
        instruction: str,
        narrative: str,
        narrative_document: dict[str, Any],
        world_state: dict[str, Any],
        memories: list[dict[str, Any]],
        model_payload: dict[str, Any],
        workflow: dict[str, Any],
        direct: bool,
        item_ops: list[dict[str, Any]],
        economy_ops: list[dict[str, Any]],
        operation_id: str,
    ) -> dict[str, Any]:
        instruction = clean_text(instruction, max_chars=4000)
        document = parse_narrative_document(
            narrative_document,
            dialogue_expected=False,
        )
        document_text = narrative_document_to_plain_text(document)
        if document_text != str(narrative or ""):
            raise ValueError("NarrativeDocument 与主持故事正文不一致")
        document_json = canonical_narrative_json(document)
        document_text_hash = narrative_text_sha256(document)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                control = connection.execute(
                    "SELECT * FROM dm_control_states WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if not session or not control:
                    raise DatabaseNotFoundError("副本或主持状态不存在")
                if int(session["revision"]) != int(expected_revision):
                    raise DatabaseConflictError("会话已被其他请求更新")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("暂停或非运行状态不能主持推进")
                if control["mode"] != "dm" or control["active_dm_user_id"] != dm_user_id:
                    raise PermissionError("只有当前活动 DM 可以提交主持剧情")
                if connection.execute(
                    "SELECT 1 FROM group_votes WHERE session_id = ? AND status = 'open'",
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError("集体投票进行中，不能主持推进")
                if operation_id:
                    operation = connection.execute(
                        "SELECT * FROM operation_receipts WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone()
                    if operation is None or str(operation["status"] or "") != "ready_to_commit":
                        raise DatabaseConflictError(
                            "主持故事操作已取消、完成或进入恢复状态"
                        )
                asset_effects = self._apply_item_ops_locked(
                    connection,
                    session_id=session_id,
                    item_ops=item_ops or (),
                )
                # C6：模型提议的经济操作与主持剧情同一事务提交；
                # 任一失败整体回滚，主持推进不会留下半提交资产。
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
                        source=str(op.get("source") or "dm"),
                        actor_id=str(op.get("actor_id") or ""),
                        target_ref=str(op.get("target_ref") or ""),
                    )

                self._insert_snapshot(
                    connection,
                    session,
                    f"undo-before-dm-{int(control['beat_no']) + 1}-revision-{session['revision']}",
                    "undo",
                    dm_user_id,
                    replace=False,
                )
                now = utc_now()
                new_turn = int(session["turn_no"]) + 1
                beat_no = int(control["beat_no"]) + 1
                stored_state = json_load(session["world_state_json"], {})
                turn = turn_state_from_world(stored_state)
                persisted_state = embed_turn_state(public_world_state(world_state), turn)
                event_id = new_id("event")
                meta = story_progress_meta(
                    connection,
                    session_id,
                    source="dm",
                    session_revision=expected_revision + 1,
                    extra={
                        "edited_by_dm": True,
                        "mode": "append",
                        "dm_beat": True,
                        "dm_beat_no": beat_no,
                        "direct": bool(direct),
                        "instruction": instruction,
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
                    actor_id=dm_user_id,
                    actor_name=(
                        "主持人直述" if direct else "主持推进"
                    ),
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
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND participation_status = 'active'
                      AND card_status = 'approved'
                    ORDER BY CASE WHEN group_user_id = ? THEN 0 ELSE 1 END,
                             created_at LIMIT 1
                    """,
                    (session_id, turn.get("current_user_id") or ""),
                ).fetchone()
                workflow_result: dict[str, Any] = {}
                if participant and workflow:
                    workflow_result = self._apply_v05_turn_ops(
                        connection,
                        session=session,
                        participant=participant,
                        new_turn=new_turn,
                        acting_round=int(turn.get("round_no") or 1),
                        source_event_id=event_id,
                        workflow=workflow,
                        check_payload={},
                        now=now,
                    )
                for memory in memories[:12]:
                    content = clean_text(memory.get("content"), max_chars=1200)
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
                            source_event_id, created_at, updated_at, last_accessed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, fingerprint) DO UPDATE SET
                            importance = MAX(importance, excluded.importance),
                            updated_at = excluded.updated_at,
                            source_event_id = excluded.source_event_id
                        """,
                        (
                            new_id("memory"), session_id, scope, scope_id, kind,
                            content, max(1, min(5, int(memory.get("importance", 3)))),
                            json_dump(list(memory.get("tags") or [])), fingerprint,
                            event_id, now, now, now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET turn_no = ?, revision = revision + 1,
                        world_state_json = ?, updated_at = ? WHERE id = ?
                    """,
                    (new_turn, json_dump(persisted_state), now, session_id),
                )
                connection.execute(
                    """
                    UPDATE dm_control_states SET beat_no = ?, directive = '',
                        phase = 'awaiting_dm', current_actor_type = '',
                        current_actor_ref = '', revision = revision + 1,
                        updated_at = ? WHERE session_id = ?
                    """,
                    (beat_no, now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    dm_user_id,
                    "dm_narrative_appended" if direct else "dm_beat_committed",
                    event_id,
                    {"beat_no": beat_no, "turn_no": new_turn, "workflow": workflow_result},
                )
                world = self._world_snapshot_for(
                    connection,
                    str(session["world_id"] or ""),
                )
                fate_result = self._commit_actor_fate_locked(
                    connection,
                    session_id=session_id,
                    world=world,
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
                    event_ref=event_id,
                    actor_id=dm_user_id,
                    turn_no=new_turn,
                    trigger_revision=expected_revision + 1,
                    now=now,
                )
                workflow_result["fate"] = fate_result
                if operation_id:
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
                            json_dump(
                                {
                                    "beat_no": beat_no,
                                    "turn_no": new_turn,
                                    "phase": "committed",
                                }
                            ),
                            expected_revision + 1,
                            now,
                            operation_id,
                        ),
                    )
                connection.execute("COMMIT")
                return {
                    "session_id": session_id,
                    "event_id": event_id,
                    "operation_id": operation_id or event_id,
                    "beat_no": beat_no,
                    "turn_no": new_turn,
                    "revision": expected_revision + 1,
                    "narrative": narrative,
                    "narrative_document": document.to_dict(),
                    "workflow": workflow_result,
                    "fate": fate_result,
                    "asset_effects": asset_effects,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
