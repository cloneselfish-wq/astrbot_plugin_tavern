from __future__ import annotations

from .fate_support import *


class RescueWindowsRepositoryMixin:
    def _expire_rescue_windows_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        world: Mapping[str, Any],
        current_turn: int,
        event_ref: str,
        now: str,
    ) -> list[dict[str, Any]]:
        contract = parse_actor_fate(world)
        if not bool(contract.get("declared")):
            return []
        rows = connection.execute(
            """
            SELECT * FROM rescue_windows
            WHERE session_id = ? AND status = 'open'
            ORDER BY opened_at, id
            """,
            (session_id,),
        ).fetchall()
        expired: list[dict[str, Any]] = []
        for raw in rows:
            # Re-read under the current transaction: an earlier settlement
            # in this same pass (e.g. a terminal transition for the same
            # character) may already have closed this window.
            live = connection.execute(
                """
                SELECT * FROM rescue_windows
                WHERE id = ? AND status = 'open'
                """,
                (str(raw["id"]),),
            ).fetchone()
            if live is None:
                continue
            window = dict(live)
            expires_on = str(window.get("expires_on") or "")
            turn_match = _TURN_EXPIRY_RE.match(expires_on)
            due = bool(
                turn_match
                and int(turn_match.group(1)) <= int(current_turn or 0)
            )
            if not due and expires_on and not turn_match:
                due = expires_on <= now
            if not due:
                continue
            current = connection.execute(
                """
                SELECT * FROM actor_fate_states
                WHERE session_id = ? AND character_id = ?
                """,
                (session_id, str(window["character_id"])),
            ).fetchone()
            if current is None:
                connection.execute(
                    """
                    UPDATE rescue_windows
                    SET status = 'cancelled', outcome = 'missing_fate',
                        completed_at = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ? AND status = 'open'
                    """,
                    (now, now, str(window["id"])),
                )
                continue
            current_state = str(current["state"] or "")
            state_def = state_definition(contract, current_state)
            if state_def is not None and bool(state_def.get("terminal")):
                # The character already reached a terminal fate (settled by
                # an earlier window or consequence in this transaction);
                # this window can no longer be resolved.
                connection.execute(
                    """
                    UPDATE rescue_windows
                    SET status = 'cancelled', outcome = 'already_terminal',
                        completed_at = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ? AND status = 'open'
                    """,
                    (now, now, str(window["id"])),
                )
                continue
            from_state, to_state = _transition_pair(
                json_load(window.get("failure_transition_json"), {})
            )
            if not from_state or not to_state:
                definition = _window_definition(
                    contract,
                    str(window.get("kind") or ""),
                )
                from_state, to_state = _transition_pair(
                    definition.get("failure_transition")
                )
            record = apply_transition(
                contract=contract,
                actor_ref=str(window["character_id"]),
                from_state=current_state,
                to_state=to_state,
                reason="救援窗口结束，角色未获救",
                source="rescue_window.expired",
                sequence=int(current["revision"] or 0) + 1,
                created_at=now,
                event_ref=event_ref,
            ).to_dict()
            state = self._apply_fate_transition_locked(
                connection,
                session_id=session_id,
                character_id=str(window["character_id"]),
                contract=contract,
                record=record,
                now=now,
                turn_no=current_turn,
                active_window_id=str(window["id"]),
            )
            connection.execute(
                """
                UPDATE rescue_windows
                SET status = 'failed', outcome = 'expired',
                    completed_at = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ? AND status = 'open'
                """,
                (now, now, str(window["id"])),
            )
            expired.append(state)
        return expired

    async def resolve_actor_rescue(
        self,
        *,
        session_id: str,
        actor_ref: str,
        command: str,
        actor_id: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._resolve_actor_rescue,
            session_id,
            actor_ref,
            command,
            actor_id,
            expected_revision,
            idempotency_key,
        )

    def _resolve_actor_rescue(
        self,
        session_id: str,
        actor_ref: str,
        command: str,
        actor_id: str,
        expected_revision: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        command = clean_text(command or "rescue", max_chars=80)
        request_key = clean_text(idempotency_key, max_chars=200)
        fingerprint = request_fingerprint(
            {
                "session_id": str(session_id),
                "actor_ref": str(actor_ref),
                "command": command,
                "actor_id": str(actor_id),
                "expected_revision": expected_revision,
            }
        )
        operation_id = (
            "actor-fate-rescue:"
            + hashlib.sha256(
                f"{session_id}\0{request_key}".encode("utf-8")
            ).hexdigest()[:24]
            if request_key
            else ""
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if operation_id:
                    prior = connection.execute(
                        "SELECT * FROM operation_commits WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if prior is not None:
                        if str(prior["input_hash"] or "") != fingerprint:
                            raise DatabaseConflictError(
                                "相同防重复凭证已用于另一项救援操作；"
                                "系统没有覆盖原结果。"
                            )
                        replay = json_load(prior["result_json"], {})
                        connection.execute("COMMIT")
                        return {**dict(replay), "replayed": True}
                self._assert_session_writable(connection, session_id)
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if str(session["state"]) != SESSION_RUNNING:
                    raise InvalidTransitionError(
                        "救援失败：副本当前不在进行中，无法执行救援。"
                        "\n系统没有修改角色状态。"
                        "\n下一步：副本进入进行中状态后再发送 /团 救援。"
                    )
                rescuer = connection.execute(
                    """
                    SELECT id FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                      AND card_status = 'approved'
                      AND participation_status
                          IN ('active', 'standby', 'away')
                    """,
                    (session_id, actor_id),
                ).fetchone()
                if rescuer is None:
                    raise PermissionError(
                        "救援失败：只有当前副本中的出场角色可以执行救援。"
                        "\n系统没有修改角色状态。"
                        "\n下一步：先发送 /团 加入 并完成建卡。"
                    )
                participant = self._resolve_fate_participant_locked(
                    connection,
                    session_id=session_id,
                    actor_ref=actor_ref,
                    require_open_window=True,
                )
                world, _frozen_world_revision = (
                    self._frozen_fate_world_locked(connection, session_id)
                )
                contract = parse_actor_fate(world)
                window = connection.execute(
                    """
                    SELECT * FROM rescue_windows
                    WHERE session_id = ? AND character_id = ?
                      AND status = 'open'
                    ORDER BY opened_at, id LIMIT 1
                    """,
                    (session_id, str(participant["id"])),
                ).fetchone()
                fate = connection.execute(
                    """
                    SELECT * FROM actor_fate_states
                    WHERE session_id = ? AND character_id = ?
                    """,
                    (session_id, str(participant["id"])),
                ).fetchone()
                if window is None or fate is None:
                    raise DatabaseNotFoundError("救援窗口已经结束")
                if (
                    expected_revision is not None
                    and int(window["revision"] or 0)
                    != int(expected_revision)
                ):
                    raise DatabaseConflictError(
                        "救援窗口已更新，本次操作没有覆盖新状态。"
                        "请重新查看角色状态后再试。"
                    )
                definition = _window_definition(
                    contract,
                    str(window["kind"] or ""),
                )
                failure_commands = {
                    str(item)
                    for item in _sequence(
                        definition.get("failure_commands")
                    )
                }
                success_commands = {
                    str(item)
                    for item in _sequence(definition.get("success_commands"))
                }
                if command in failure_commands:
                    outcome = "failed"
                elif command in success_commands:
                    outcome = "succeeded"
                else:
                    raise ValueError(
                        "救援失败：当前窗口不接受该操作。"
                        "\n系统没有修改角色状态。"
                        "\n下一步：发送 /团 状态 查看可用救援操作。"
                    )
                transition_key = (
                    "success_transition"
                    if outcome == "succeeded"
                    else "failure_transition"
                )
                from_state, to_state = _transition_pair(
                    definition.get(transition_key)
                )
                record = apply_transition(
                    contract=contract,
                    actor_ref=str(participant["id"]),
                    from_state=str(fate["state"]),
                    to_state=to_state,
                    reason=(
                        "队友完成了救援"
                        if outcome == "succeeded"
                        else "救援失败"
                    ),
                    source=f"rescue_window.{command}",
                    sequence=int(fate["revision"] or 0) + 1,
                    created_at=utc_now(),
                    event_ref=str(window["id"]),
                ).to_dict()
                now = utc_now()
                state = self._apply_fate_transition_locked(
                    connection,
                    session_id=session_id,
                    character_id=str(participant["id"]),
                    contract=contract,
                    record=record,
                    now=now,
                    turn_no=int(session["turn_no"] or 0),
                    active_window_id=str(window["id"]),
                )
                closed = connection.execute(
                    """
                    UPDATE rescue_windows
                    SET status = ?, outcome = ?, command = ?,
                        completed_at = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ? AND status = 'open' AND revision = ?
                    """,
                    (
                        "succeeded" if outcome == "succeeded" else "failed",
                        outcome,
                        command,
                        now,
                        now,
                        str(window["id"]),
                        int(window["revision"] or 0),
                    ),
                )
                if closed.rowcount != 1:
                    raise DatabaseConflictError(
                        "救援窗口状态已变化，请重新查询后再试"
                    )
                terminal = self._evaluate_terminal_locked(
                    connection,
                    session_id=session_id,
                    actor_id=actor_id,
                    trigger_revision=int(session["revision"] or 0) + 1,
                    world=world,
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "actor_fate.rescue",
                    str(window["id"]),
                    {
                        "character_id": str(participant["id"]),
                        "command": command,
                        "outcome": outcome,
                        "target_state": state["state"],
                    },
                )
                result = {
                    "session_id": session_id,
                    "character_name": str(
                        participant.get("character_name") or ""
                    ),
                    "outcome": outcome,
                    "state": state,
                    "terminal": terminal,
                    "window_revision": int(window["revision"] or 0) + 1,
                    "replayed": False,
                }
                if operation_id:
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
                            fingerprint,
                            json_dump(result),
                            now,
                            now,
                        ),
                    )
                connection.execute("COMMIT")
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise
