from __future__ import annotations

from .fate_support import *


_FATE_PREVIEW_PREFIX = "actor-fate-preview:"


def _preview_expiry(
    window: Mapping[str, Any],
    *,
    turn_no: int,
) -> str:
    declared = str(window.get("expires_on") or "").strip()
    if "next_scene" in declared or "before_next_scene" in declared:
        return f"turn:{max(0, int(turn_no or 0)) + 1}"
    return declared


class FateStateRepositoryMixin:
    def _frozen_fate_world_locked(
        self,
        connection: sqlite3.Connection,
        session_id: str,
    ) -> tuple[dict[str, Any], str]:
        """Return the session-frozen world and a stable consent revision.

        Fate previews may live longer than the installed world row.  Reading
        ``worlds`` here would let an installation or upgrade silently change
        the rules
        between preview and actor consent, so the instance snapshot is the
        only acceptable authority.
        """

        row = connection.execute(
            """
            SELECT world_revision, world_snapshot_json
            FROM instance_configs WHERE session_id = ?
            """,
            (str(session_id),),
        ).fetchone()
        if row is None:
            raise DatabaseConflictError(
                "副本缺少冻结世界快照，无法安全处理角色命运。"
                "系统没有修改角色状态；请先修复副本世界配置。"
            )
        world = json_load(row["world_snapshot_json"], {})
        if not isinstance(world, Mapping) or not world:
            raise DatabaseConflictError(
                "副本冻结世界快照无效，无法安全处理角色命运。"
                "系统没有修改角色状态；请先修复副本世界配置。"
            )
        frozen_world = dict(world)
        content_hash = request_fingerprint(frozen_world)
        revision = f"instance:{int(row['world_revision'] or 0)}:{content_hash}"
        return frozen_world, revision

    async def list_actor_fate_states(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """Return the authoritative fate rows for approved active roster actors."""

        return await self._run(self._list_actor_fate_states, str(session_id))

    def _list_actor_fate_states(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    afs.*,
                    pt.character_name,
                    pt.display_name,
                    pt.card_status,
                    pt.participation_status,
                    EXISTS(
                        SELECT 1 FROM rescue_windows rw
                        WHERE rw.session_id=afs.session_id
                          AND rw.character_id=afs.character_id
                          AND rw.status='open'
                    ) AS rescue_open
                FROM actor_fate_states afs
                JOIN participants pt
                  ON pt.id=afs.character_id
                 AND pt.session_id=afs.session_id
                WHERE afs.session_id=?
                  AND pt.card_status='approved'
                  AND pt.participation_status IN ('active','standby','away')
                ORDER BY pt.created_at, pt.id
                """,
                (str(session_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_actor_fate_previews(
        self,
        session_id: str,
        participant_id: str,
        *,
        status: str = "pending_consent",
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_actor_fate_previews,
            str(session_id),
            str(participant_id),
            str(status),
        )

    def _list_actor_fate_previews(
        self,
        session_id: str,
        participant_id: str,
        status: str,
    ) -> list[dict[str, Any]]:
        clauses = [
            "session_id = ?",
            "operation_id LIKE ?",
        ]
        values: list[Any] = [session_id, _FATE_PREVIEW_PREFIX + "%"]
        if status:
            clauses.append("status = ?")
            values.append(status)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM operation_commits
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at, operation_id
                """,
                tuple(values),
            ).fetchall()
        previews: list[dict[str, Any]] = []
        for row in rows:
            result = json_load(row["result_json"], {})
            preview = _mapping(result.get("preview"))
            if str(preview.get("participant_id") or "") != participant_id:
                continue
            previews.append(
                {
                    **preview,
                    "operation_id": str(row["operation_id"]),
                    "status": str(row["status"]),
                }
            )
        return previews

    async def resolve_actor_fate_preview(
        self,
        *,
        session_id: str,
        preview_operation_id: str,
        participant_id: str,
        decision: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._resolve_actor_fate_preview,
            str(session_id),
            str(preview_operation_id),
            str(participant_id),
            str(decision),
            int(expected_revision),
            str(actor_id),
            str(idempotency_key),
        )

    def _resolve_actor_fate_preview(
        self,
        session_id: str,
        preview_operation_id: str,
        participant_id: str,
        decision: str,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        decision = clean_text(decision, max_chars=20)
        if decision not in {"accept", "refuse"}:
            raise ValueError(
                "命运预览操作无效。系统没有修改角色状态；"
                "请选择确认或拒绝。"
            )
        request_key = clean_text(idempotency_key, max_chars=200)
        if not request_key:
            raise ValueError(
                "命运预览操作缺少防重复凭证。系统没有修改角色状态；"
                "请保留当前页面并重新提交。"
            )
        fingerprint = request_fingerprint(
            {
                "session_id": session_id,
                "preview_operation_id": preview_operation_id,
                "participant_id": participant_id,
                "decision": decision,
                "expected_revision": expected_revision,
                "actor_id": actor_id,
            }
        )
        receipt_id = "actor-fate-consent:" + hashlib.sha256(
            f"{session_id}\0{request_key}".encode("utf-8")
        ).hexdigest()[:24]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT * FROM operation_commits WHERE operation_id = ?",
                    (receipt_id,),
                ).fetchone()
                if prior is not None:
                    if str(prior["input_hash"] or "") != fingerprint:
                        raise DatabaseConflictError(
                            "相同防重复凭证已用于另一项命运预览操作。"
                            "系统没有覆盖原结果；请查询原回执或使用新凭证。"
                        )
                    replay = json_load(prior["result_json"], {})
                    connection.execute("COMMIT")
                    return {**dict(replay), "replayed": True}
                self._assert_session_writable(connection, session_id)
                preview_row = connection.execute(
                    """
                    SELECT * FROM operation_commits
                    WHERE operation_id = ? AND session_id = ?
                      AND operation_id LIKE ?
                    """,
                    (
                        preview_operation_id,
                        session_id,
                        _FATE_PREVIEW_PREFIX + "%",
                    ),
                ).fetchone()
                if preview_row is None:
                    raise DatabaseNotFoundError(
                        "命运预览不存在或已清理。系统没有修改角色状态；"
                        "请重新查看自己的待确认命运预览。"
                    )
                if str(preview_row["status"] or "") != "pending_consent":
                    raise DatabaseConflictError(
                        "命运预览已经处理。系统没有重复修改角色状态；"
                        "请刷新后查看原回执。"
                    )
                stored = json_load(preview_row["result_json"], {})
                preview = _mapping(stored.get("preview"))
                if str(preview.get("participant_id") or "") != participant_id:
                    raise PermissionError(
                        "你只能处理自己的命运预览。系统没有修改任何角色；"
                        "请返回自己的角色状态重新选择。"
                    )
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                fate = connection.execute(
                    """
                    SELECT * FROM actor_fate_states
                    WHERE session_id = ? AND character_id = ?
                    """,
                    (session_id, participant_id),
                ).fetchone()
                if session is None or fate is None:
                    raise DatabaseNotFoundError(
                        "角色命运状态不存在。系统没有应用预览；"
                        "请联系管理员修复角色状态。"
                    )
                preview_revision = int(
                    preview.get("expected_fate_revision") or 0
                )
                current_revision = int(fate["revision"] or 0)
                if (
                    expected_revision != preview_revision
                    or current_revision != preview_revision
                ):
                    raise DatabaseConflictError(
                        "角色命运已更新，本次确认没有覆盖新状态。"
                        "请刷新并重新查看预览。"
                    )
                expires_on = str(preview.get("expires_on") or "")
                turn_match = _TURN_EXPIRY_RE.match(expires_on)
                expired = bool(
                    turn_match
                    and int(turn_match.group(1))
                    <= int(session["turn_no"] or 0)
                )
                if not expired and expires_on and not turn_match:
                    expired = expires_on <= utc_now()
                if expired:
                    expired_preview = {**preview, "status": "expired"}
                    connection.execute(
                        """
                        UPDATE operation_commits
                        SET status = 'expired', result_json = ?, updated_at = ?
                        WHERE operation_id = ? AND status = 'pending_consent'
                        """,
                        (
                            json_dump({"preview": expired_preview}),
                            utc_now(),
                            preview_operation_id,
                        ),
                    )
                    result = {
                        "status": "expired",
                        "decision": decision,
                        "state_changed": False,
                        "message": (
                            "命运预览已经过期。系统没有修改角色状态；"
                            "请等待规则重新生成可确认的预览。"
                        ),
                        "replayed": False,
                    }
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO operation_commits(
                            operation_id, session_id, input_hash, status,
                            result_json, rollback_json, created_at, updated_at
                        ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                        """,
                        (
                            receipt_id,
                            session_id,
                            fingerprint,
                            json_dump(result),
                            now,
                            now,
                        ),
                    )
                    connection.execute("COMMIT")
                    return result
                world, frozen_world_revision = (
                    self._frozen_fate_world_locked(connection, session_id)
                )
                if str(preview.get("world_revision") or "") != (
                    frozen_world_revision
                ):
                    raise DatabaseConflictError(
                        "冻结世界版本已变化，本次确认没有拼接新旧命运规则。"
                        "系统没有修改角色状态；请刷新后重新处理。"
                    )
                now = utc_now()
                if decision == "refuse":
                    state = {
                        "character_id": participant_id,
                        "state": str(fate["state"]),
                        "state_label": str(fate["state_label"] or ""),
                        "can_act": bool(fate["can_act"]),
                        "terminal": bool(fate["terminal"]),
                    }
                    state_changed = False
                    preview_status = "refused"
                    message = (
                        "你已拒绝本次致命命运预览。系统保留了原命运状态，"
                        "没有开启救援窗口。"
                    )
                else:
                    contract = parse_actor_fate(world)
                    transition_data = _mapping(preview.get("transition"))
                    transition = find_transition(
                        contract,
                        str(fate["state"]),
                        str(transition_data.get("to") or ""),
                    )
                    target = state_definition(
                        contract,
                        str(transition_data.get("to") or ""),
                    )
                    if (
                        transition is None
                        or target is None
                        or bool(target.get("terminal"))
                        or not bool(transition.get("opens_rescue_window"))
                    ):
                        raise InvalidTransitionError(
                            "冻结命运预览不再对应合法的非终态救援转换。"
                            "系统没有修改角色状态；请刷新后重新处理。"
                        )
                    protection_ids = {
                        str(item.get("id") or "")
                        for item in _sequence(
                            contract.get("protection_resources")
                        )
                        if isinstance(item, Mapping)
                    }
                    resource_rows = connection.execute(
                        """
                        SELECT * FROM character_resources
                        WHERE session_id = ? AND character_id = ?
                        """,
                        (session_id, participant_id),
                    ).fetchall()
                    protection = {
                        str(row["resource_ref"]): int(row["current"] or 0)
                        for row in resource_rows
                        if str(row["resource_ref"]) in protection_ids
                    }
                    record = apply_transition(
                        contract=contract,
                        actor_ref=participant_id,
                        from_state=str(fate["state"]),
                        to_state=str(transition_data.get("to") or ""),
                        transition=transition,
                        reason=str(preview.get("reason") or ""),
                        source="actor_fate.preview.accepted",
                        sequence=current_revision + 1,
                        created_at=now,
                        event_ref=preview_operation_id,
                        protection=protection,
                    ).to_dict()
                    consumed = str(
                        record.get("consumed_protection_resource") or ""
                    )
                    if consumed:
                        changed = connection.execute(
                            """
                            UPDATE character_resources
                            SET current = current - 1, updated_at = ?
                            WHERE session_id = ? AND character_id = ?
                              AND resource_ref = ? AND current > 0
                            """,
                            (now, session_id, participant_id, consumed),
                        )
                        if changed.rowcount != 1:
                            raise DatabaseConflictError(
                                "保护资源已变化，命运确认没有覆盖新状态。"
                            )
                    window_kind = str(
                        transition.get("rescue_window_kind") or "default"
                    )
                    state = self._apply_fate_transition_locked(
                        connection,
                        session_id=session_id,
                        character_id=participant_id,
                        contract=contract,
                        record=record,
                        now=now,
                        turn_no=int(session["turn_no"] or 0),
                        protection_consumed=bool(consumed),
                        window_definition=_window_definition(
                            contract,
                            window_kind,
                        ),
                    )
                    state_changed = True
                    preview_status = "accepted"
                    message = (
                        "你已确认本次致命命运预览。角色进入世界声明的"
                        "非终态救援窗口，尚未被直接判定死亡。"
                    )
                resolved_preview = {
                    **preview,
                    "status": preview_status,
                    "resolved_at": now,
                    "decision": decision,
                }
                updated = connection.execute(
                    """
                    UPDATE operation_commits
                    SET status = ?, result_json = ?, updated_at = ?
                    WHERE operation_id = ? AND status = 'pending_consent'
                    """,
                    (
                        preview_status,
                        json_dump({"preview": resolved_preview}),
                        now,
                        preview_operation_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise DatabaseConflictError(
                        "命运预览已由另一项操作处理，请查询原回执。"
                    )
                result = {
                    "status": preview_status,
                    "decision": decision,
                    "state_changed": state_changed,
                    "state": state,
                    "message": message,
                    "replayed": False,
                }
                connection.execute(
                    """
                    INSERT INTO operation_commits(
                        operation_id, session_id, input_hash, status,
                        result_json, rollback_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                    """,
                    (
                        receipt_id,
                        session_id,
                        fingerprint,
                        json_dump(result),
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    f"actor_fate.preview.{decision}",
                    preview_operation_id,
                    {
                        "participant_id": participant_id,
                        "state_changed": state_changed,
                        "target_state": str(state.get("state") or ""),
                    },
                )
                insert_session_event(
                    connection,
                    session_id=session_id,
                    event_id=f"{receipt_id}:event",
                    type_=f"event:actor_fate.preview_{decision}",
                    actor_ref=actor_id,
                    command_id=receipt_id,
                    payload={
                        "title": "角色命运预览已处理",
                        "summary": message[:240],
                        "affected_modules": ["actor_fate"],
                    },
                    visibility="private",
                    created_at=now,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    """Transaction-local D1 fate orchestration shared by story and commands."""

    def _initialize_player_fate_locked(
        self,
        connection: sqlite3.Connection,
        *,
        participant: Mapping[str, Any],
        world: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any] | None:
        """Create the player actor identity and initial fate in the card transaction."""

        contract = parse_actor_fate(world)
        if not bool(contract.get("declared")):
            return None
        participant_id = str(participant.get("id") or "").strip()
        session_id = str(participant.get("session_id") or "").strip()
        if (
            not participant_id
            or not session_id
            or str(participant.get("card_status") or "") != CARD_APPROVED
        ):
            return None
        character_name = clean_text(
            participant.get("character_name")
            or participant.get("display_name")
            or "玩家角色",
            max_chars=80,
        )
        character_code = clean_text(
            participant.get("character_code"),
            max_chars=80,
        )
        aliases = [character_code] if character_code else []
        connection.execute(
            """
            INSERT INTO session_characters(
                id, session_id, stable_key, name, aliases_json,
                role_type, public_profile_json, known_facts_json,
                misconceptions_json, source, review_status,
                lifecycle_status, persistent, first_event_id,
                last_event_id, first_turn, last_turn, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'player', ?, '[]', '[]', 'admin',
                      'approved', 'active', 1, '', '', 0, 0, 1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                aliases_json = excluded.aliases_json,
                public_profile_json = excluded.public_profile_json,
                review_status = 'approved',
                lifecycle_status = 'active',
                revision = session_characters.revision + 1,
                updated_at = excluded.updated_at
            """,
            (
                participant_id,
                session_id,
                f"participant:{participant_id}",
                character_name,
                json_dump(aliases),
                json_dump(
                    {
                        "identity": "玩家角色",
                        "display_name": character_name,
                        "character_code": character_code,
                    }
                ),
                now,
                now,
            ),
        )
        initial_state = str(contract.get("initial_state") or "").strip()
        definition = state_definition(contract, initial_state)
        if not initial_state or definition is None:
            raise ValueError("当前世界的角色命运初始状态无效")
        connection.execute(
            """
            INSERT INTO actor_fate_states(
                character_id, session_id, state, state_label,
                can_act, terminal, transitioned_at,
                rescue_window_until, rescue_window_kind,
                reason, source, revision, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '',
                      '角色卡确认后初始化', 'card.confirm', 1, ?)
            ON CONFLICT(character_id) DO NOTHING
            """,
            (
                participant_id,
                session_id,
                initial_state,
                str(definition.get("label") or initial_state)[:120],
                1 if bool(definition.get("can_act", True)) else 0,
                1 if bool(definition.get("terminal")) else 0,
                now,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM actor_fate_states
            WHERE session_id = ? AND character_id = ?
            """,
            (session_id, participant_id),
        ).fetchone()
        return dict(row) if row is not None else None

    def _resolve_fate_participant_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        actor_ref: str,
        require_open_window: bool = False,
    ) -> dict[str, Any]:
        actor_ref = clean_text(actor_ref, max_chars=128)
        params: list[Any] = [session_id]
        clauses = [
            "pt.session_id = ?",
            "pt.card_status = 'approved'",
            "pt.participation_status IN ('active', 'standby', 'away')",
        ]
        if actor_ref:
            clauses.append(
                """(
                    pt.id = ? OR pt.group_user_id = ?
                    OR lower(pt.character_code) = lower(?)
                    OR lower(pt.character_name) = lower(?)
                )"""
            )
            params.extend([actor_ref, actor_ref, actor_ref, actor_ref])
        if require_open_window:
            clauses.append(
                """EXISTS(
                    SELECT 1 FROM rescue_windows rw
                    WHERE rw.session_id = pt.session_id
                      AND rw.character_id = pt.id
                      AND rw.status = 'open'
                )"""
            )
        rows = connection.execute(
            f"""
            SELECT pt.*
            FROM participants pt
            WHERE {' AND '.join(clauses)}
            ORDER BY pt.created_at, pt.id
            LIMIT 3
            """,
            tuple(params),
        ).fetchall()
        if not rows:
            if require_open_window:
                raise DatabaseNotFoundError(
                    "救援失败：没有找到可救援的濒危角色。"
                    "\n系统没有修改角色状态。"
                    "\n下一步：发送 /团 状态 查看当前救援窗口。"
                )
            raise DatabaseNotFoundError("结构化后果指定的玩家角色不存在")
        if len(rows) > 1:
            raise ValueError(
                "救援失败：当前有多个同名或多个待救援角色。"
                "\n系统没有修改角色状态。"
                "\n下一步：请使用角色完整名称或副本昵称重试。"
            )
        return dict(rows[0])

    def _party_fate_projection_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        contract: Mapping[str, Any],
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            SELECT pt.id AS actor_ref, pt.participation_status,
                   pt.card_status, pt.card_stage,
                   COALESCE(sc.role_type, 'player') AS role_type,
                   COALESCE(afs.state, '') AS state
            FROM participants pt
            LEFT JOIN session_characters sc
              ON sc.id = pt.id AND sc.session_id = pt.session_id
            LEFT JOIN actor_fate_states afs
              ON afs.character_id = pt.id AND afs.session_id = pt.session_id
            WHERE pt.session_id = ?
            ORDER BY pt.created_at, pt.id
            """,
            (session_id,),
        ).fetchall()
        return project_party_summary(
            contract=contract,
            members=[dict(row) for row in rows],
        ).to_dict()

    def _apply_fate_transition_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        character_id: str,
        contract: Mapping[str, Any],
        record: Mapping[str, Any],
        now: str,
        turn_no: int,
        protection_consumed: bool = False,
        window_definition: Mapping[str, Any] | None = None,
        active_window_id: str = "",
    ) -> dict[str, Any]:
        target_state = str(record.get("to_state") or "").strip()
        definition = state_definition(contract, target_state)
        if definition is None:
            raise ValueError(f"世界未注册角色命运状态：{target_state}")
        terminal = bool(definition.get("terminal"))
        opens_window = bool(record.get("opens_rescue_window")) and not terminal
        window_kind = (
            str(record.get("rescue_window_kind") or "default")
            if opens_window
            else ""
        )
        window = dict(window_definition or {})
        declared_expiry = str(window.get("expires_on") or "").strip()
        if opens_window and (
            "next_scene" in declared_expiry
            or "next_scene" in window_kind
            or "before_next_scene" in window_kind
        ):
            expires_on = f"turn:{max(0, int(turn_no or 0)) + 1}"
        else:
            expires_on = declared_expiry if opens_window else ""
        connection.execute(
            """
            INSERT INTO actor_fate_states(
                character_id, session_id, state, state_label,
                can_act, terminal, transitioned_at,
                rescue_window_until, rescue_window_kind,
                reason, source, revision, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                state = excluded.state,
                state_label = excluded.state_label,
                can_act = excluded.can_act,
                terminal = excluded.terminal,
                transitioned_at = excluded.transitioned_at,
                rescue_window_until = excluded.rescue_window_until,
                rescue_window_kind = excluded.rescue_window_kind,
                reason = excluded.reason,
                source = excluded.source,
                revision = actor_fate_states.revision + 1,
                updated_at = excluded.updated_at
            """,
            (
                character_id,
                session_id,
                target_state,
                str(definition.get("label") or target_state)[:120],
                1 if bool(definition.get("can_act", not terminal)) else 0,
                1 if terminal else 0,
                now,
                expires_on[:64],
                window_kind[:64],
                str(record.get("reason") or "")[:500],
                str(record.get("source") or "")[:160],
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO actor_fate_transitions(
                id, session_id, character_id, from_state, to_state,
                reason, source, reversible, rescue_window,
                protection_consumed, event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("fate"),
                session_id,
                character_id,
                str(record.get("from_state") or "")[:80],
                target_state[:80],
                str(record.get("reason") or "")[:500],
                str(record.get("source") or "")[:160],
                1 if bool(record.get("reversible")) else 0,
                1 if opens_window else 0,
                1 if protection_consumed else 0,
                str(record.get("event_ref") or "")[:160],
                now,
            ),
        )
        if terminal:
            connection.execute(
                """
                UPDATE rescue_windows
                SET status = 'cancelled', outcome = 'terminal',
                    completed_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE session_id = ? AND character_id = ?
                  AND status = 'open'
                  AND (? = '' OR id <> ?)
                """,
                (
                    now,
                    now,
                    session_id,
                    character_id,
                    str(active_window_id or ""),
                    str(active_window_id or ""),
                ),
            )
        elif opens_window:
            existing = connection.execute(
                """
                SELECT * FROM rescue_windows
                WHERE session_id = ? AND character_id = ?
                  AND kind = ? AND status = 'open'
                """,
                (session_id, character_id, window_kind),
            ).fetchone()
            if existing is None:
                success_transition = _transition_pair(
                    window.get("success_transition")
                )
                failure_transition = _transition_pair(
                    window.get("failure_transition")
                )
                connection.execute(
                    """
                    INSERT INTO rescue_windows(
                        id, session_id, character_id, kind, status,
                        opened_at, expires_on, allowed_rescue_commands_json,
                        success_transition_json, failure_transition_json,
                        command_labels_json, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        new_id("rescue_window"),
                        session_id,
                        character_id,
                        window_kind,
                        now,
                        expires_on,
                        json_dump(
                            [
                                str(item)
                                for item in _sequence(
                                    window.get("allowed_rescue_commands")
                                )
                            ]
                        ),
                        json_dump(
                            {
                                "from": success_transition[0],
                                "to": success_transition[1],
                            }
                        ),
                        json_dump(
                            {
                                "from": failure_transition[0],
                                "to": failure_transition[1],
                            }
                        ),
                        json_dump(_mapping(window.get("command_labels"))),
                        now,
                        now,
                    ),
                )
        return {
            "character_id": character_id,
            "state": target_state,
            "state_label": str(definition.get("label") or target_state),
            "can_act": bool(definition.get("can_act", not terminal)),
            "terminal": terminal,
            "rescue_window": opens_window,
            "rescue_window_kind": window_kind,
            "rescue_window_until": expires_on,
        }

    def _expire_fate_previews_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        current_turn: int,
        now: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT * FROM operation_commits
            WHERE session_id = ? AND operation_id LIKE ?
              AND status = 'pending_consent'
            """,
            (session_id, _FATE_PREVIEW_PREFIX + "%"),
        ).fetchall()
        expired: list[str] = []
        for row in rows:
            stored = json_load(row["result_json"], {})
            preview = _mapping(stored.get("preview"))
            participant_id = str(preview.get("participant_id") or "")
            fate = connection.execute(
                """
                SELECT revision FROM actor_fate_states
                WHERE session_id = ? AND character_id = ?
                """,
                (session_id, participant_id),
            ).fetchone()
            stale = fate is None or int(fate["revision"] or 0) != int(
                preview.get("expected_fate_revision") or 0
            )
            expires_on = str(preview.get("expires_on") or "")
            turn_match = _TURN_EXPIRY_RE.match(expires_on)
            due = bool(
                turn_match
                and int(turn_match.group(1)) <= int(current_turn or 0)
            )
            if not due and expires_on and not turn_match:
                due = expires_on <= now
            if not stale and not due:
                continue
            updated_preview = {**preview, "status": "expired"}
            connection.execute(
                """
                UPDATE operation_commits
                SET status = 'expired', result_json = ?, updated_at = ?
                WHERE operation_id = ? AND status = 'pending_consent'
                """,
                (
                    json_dump({"preview": updated_preview}),
                    now,
                    str(row["operation_id"]),
                ),
            )
            expired.append(str(row["operation_id"]))
        return expired
