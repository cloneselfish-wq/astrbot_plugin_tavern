from __future__ import annotations

from .fate_support import *


class TerminalStateRepositoryMixin:
    def _session_terminal_projection_locked(
        self,
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
    ) -> dict[str, Any]:
        state = json_load(session.get("world_state_json"), {})
        state = dict(state) if isinstance(state, Mapping) else {}
        runtime = flatten_runtime(runtime_from_state(state))
        projection = {
            key: value
            for key, value in runtime.items()
            if key not in {"events", "enabled_modules"}
        }
        projection.update(
            {
                "id": str(session.get("id") or ""),
                "state": str(session.get("state") or ""),
                "turn_no": int(session.get("turn_no") or 0),
                "revision": int(session.get("revision") or 0),
                "opening_committed": (
                    str(session.get("state") or "") in {
                        SESSION_RUNNING,
                        SESSION_PAUSED,
                        SESSION_MAINTENANCE,
                    }
                    or int(session.get("turn_no") or 0) > 0
                ),
                "scene_ref": str(
                    runtime.get("scene_ref")
                    or runtime.get("current_scene")
                    or state.get("current_scene")
                    or state.get("scene_ref")
                    or ""
                ),
            }
        )
        completed_clock = connection.execute(
            """
            SELECT 1 FROM scene_clocks
            WHERE session_id = ? AND status = 'completed'
            LIMIT 1
            """,
            (str(session.get("id") or ""),),
        ).fetchone()
        projection["clock_expired"] = bool(
            runtime.get("clock_expired") or completed_clock
        )
        clocks: dict[str, int | float] = {}
        raw_clocks = runtime.get("clocks")
        if isinstance(raw_clocks, Mapping):
            for raw_ref, raw_clock in raw_clocks.items():
                if not isinstance(raw_clock, Mapping):
                    continue
                value = raw_clock.get("value")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                ref = str(raw_ref or "")
                key = ref.removeprefix("clock:").strip()
                if key:
                    clocks[key] = value
        projection["clocks"] = clocks
        return projection

    def _evaluate_terminal_locked(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        actor_id: str,
        trigger_revision: int,
        world: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        session_row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise DatabaseNotFoundError("会话不存在")
        if str(session_row["state"] or "") == SESSION_FINISHED:
            return None
        world_snapshot = dict(world or {})
        if not world_snapshot:
            world_snapshot = self._world_snapshot_for(
                connection,
                str(session_row["world_id"] or ""),
            )
        conditions = parse_terminal_conditions(world_snapshot)
        if not conditions:
            return None
        contract = parse_actor_fate(world_snapshot)
        party = self._party_fate_projection_locked(
            connection,
            session_id=session_id,
            contract=contract,
        )
        context = build_terminal_context(
            world=world_snapshot,
            session=self._session_terminal_projection_locked(
                connection,
                dict(session_row),
            ),
            party=party,
        )
        winner = arbitrate_terminal_conditions(
            evaluate_terminal_conditions(conditions, context)
        )
        if winner is None:
            return {
                "matched": False,
                "party": party,
            }
        if str(winner.get("archive_policy") or "") == "manual":
            condition_id = str(winner.get("condition_id") or winner.get("id") or "")
            idempotency_key = f"terminal-manual:{session_id}:{condition_id}"
            existing = connection.execute(
                """
                SELECT * FROM terminal_receipts
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is None:
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO terminal_receipts(
                        id, session_id, condition_id, condition_label,
                        priority, ending_ref, termination_type,
                        archive_policy, trigger_revision, payload_json,
                        status, idempotency_key, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?,
                              'pending', ?, ?, ?)
                    """,
                    (
                        new_id("terminal"),
                        session_id,
                        condition_id[:160],
                        str(winner.get("label") or condition_id)[:160],
                        max(0, int(winner.get("priority") or 0)),
                        str(winner.get("ending_ref") or "")[:160],
                        str(winner.get("termination_type") or "completed"),
                        max(0, int(trigger_revision or 0)),
                        json_dump(
                            {
                                "reason": str(winner.get("reason") or ""),
                                "match": dict(winner),
                            }
                        ),
                        idempotency_key[:200],
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id or "system",
                    "terminal.pending",
                    condition_id,
                    {
                        "condition_id": condition_id,
                        "ending_ref": str(winner.get("ending_ref") or ""),
                        "trigger_revision": max(0, int(trigger_revision or 0)),
                    },
                )
                append_event(
                    connection,
                    session_id=session_id,
                    turn_no=int(session_row["turn_no"] or 0),
                    role="system",
                    actor_id=actor_id or "system",
                    actor_name="开团系统",
                    content="世界终局条件已满足，等待主持确认。",
                    meta={
                        "kind": "terminal.pending",
                        "condition_id": condition_id,
                    },
                    created_at=now,
                )
            return {
                "matched": True,
                "decision": "manual",
                "match": dict(winner),
                "party": party,
                "receipt": (
                    dict(existing)
                    if existing is not None
                    else {
                        "status": "pending",
                        "idempotency_key": idempotency_key,
                    }
                ),
            }
        finalized = self._finalize_session(
            session_id,
            actor_id or "system",
            str(winner.get("termination_type") or "completed"),
            str(winner.get("reason") or winner.get("label") or ""),
            dict(winner),
            int(trigger_revision or session_row["revision"] or 0),
            connection=connection,
        )
        return {
            "matched": True,
            "decision": str(finalized.get("decision") or "applied"),
            "match": dict(winner),
            "party": party,
            "finalization": finalized,
        }
