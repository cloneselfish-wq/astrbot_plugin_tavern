from __future__ import annotations

from .workflow_support import *


class AiTurnsRepositoryMixin:
    @staticmethod
    def _ai_actor_ref(actor_id: object) -> str:
        digest = hashlib.sha256(
            str(actor_id or "").encode("utf-8")
        ).hexdigest()[:12].upper()
        return f"public:actor:{digest}"

    @classmethod
    def _ai_turn_actor(cls, row: Mapping[str, Any]) -> dict[str, Any]:
        profile = json_load(row.get("frozen_profile_json", "{}"), {})
        return {
            "id": str(row.get("id") or ""),
            "actor_id": str(row.get("id") or ""),
            "actor_ref": cls._ai_actor_ref(row.get("id")),
            "actor_kind": "ai_companion",
            "participant_id": None,
            "group_user_id": "",
            "display_name": str(row.get("display_name") or ""),
            "character_name": str(row.get("display_name") or ""),
            "card_profile": profile,
            "card_stats": dict(profile.get("stats") or {}),
            "runtime_state": json_load(row.get("state_json", "{}"), {}),
            "mode": str(row.get("mode") or "confirm"),
            "status": str(row.get("instance_status") or row.get("status") or ""),
        }

    async def list_ai_turn_actors(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_ai_turn_actors, session_id)

    def _list_ai_turn_actors(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, i.mode, i.status AS instance_status,
                       i.frozen_profile_json
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status='active' AND i.status<>'retired'
                ORDER BY a.created_at, a.id
                """,
                (session_id,),
            ).fetchall()
        return [self._ai_turn_actor(dict(row)) for row in rows]

    @classmethod
    def _ai_vote_projection_locked(
        cls,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        world: Mapping[str, Any],
    ) -> dict[str, Any]:
        module = world.get("ai_companions")
        if not isinstance(module, Mapping):
            rules = world.get("rules")
            rules = rules if isinstance(rules, Mapping) else {}
            module = rules.get("ai_companions")
        module = module if isinstance(module, Mapping) else {}
        policy = str(module.get("vote_policy") or "normal")
        if policy not in {"normal", "advisory", "disabled"}:
            policy = "normal"
        refs = [
            cls._ai_actor_ref(row["id"])
            for row in connection.execute(
                """
                SELECT a.id FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status='active' AND i.status<>'retired'
                ORDER BY a.created_at, a.id
                """,
                (session_id,),
            ).fetchall()
        ]
        return {
            "policy": policy,
            "eligible_refs": refs if policy == "normal" else [],
            "automatic_ballots": (
                [{"user_id": ref, "option_key": "A"} for ref in refs]
                if policy == "normal"
                else []
            ),
            "advisory": (
                {
                    "count": len(refs),
                    "option_key": "A",
                    "message": "AI 队友倾向同意推进，但不计入法定人数。",
                }
                if policy == "advisory" and refs
                else None
            ),
        }

    @staticmethod
    def _apply_return_vote_result(
        connection: sqlite3.Connection,
        *,
        vote_id: str,
        passed: bool,
        now: str,
    ) -> None:
        request_row = connection.execute(
            """
            SELECT * FROM return_requests WHERE vote_id = ?
            """,
            (vote_id,),
        ).fetchone()
        if not request_row:
            return
        if passed:
            config = connection.execute(
                """
                SELECT world_snapshot_json FROM instance_configs
                WHERE session_id = ?
                """,
                (request_row["session_id"],),
            ).fetchone()
            world = json_load(
                config["world_snapshot_json"] if config else "",
                {},
            )
            limits = player_limits(world)
            placeholders = ",".join(
                "?" for _ in SEAT_HOLDING_STATUSES
            )
            occupied = connection.execute(
                f"""
                SELECT COUNT(*) FROM participants
                WHERE session_id = ?
                  AND participation_status IN ({placeholders})
                """,
                (
                    request_row["session_id"],
                    *sorted(SEAT_HOLDING_STATUSES),
                ),
            ).fetchone()[0]
            if occupied >= limits["maximum"]:
                connection.execute(
                    """
                    UPDATE return_requests
                    SET status = 'cancelled',
                        progress_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump({"reason": "seat_unavailable"}),
                        now,
                        request_row["id"],
                    ),
                )
                return
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'quest_active', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )
            connection.execute(
                """
                UPDATE participants
                SET participation_status = 'standby', ready = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["participant_id"]),
            )
        else:
            connection.execute(
                """
                UPDATE return_requests
                SET status = 'rejected', updated_at = ?
                WHERE id = ?
                """,
                (now, request_row["id"]),
            )

    @staticmethod
    def _record_return_progress(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        request_id: str,
        evidence: str,
        completed: bool,
        round_no: int,
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT rr.*, pt.character_name, pt.display_name
            FROM return_requests rr
            JOIN participants pt ON pt.id = rr.participant_id
            WHERE rr.id = ? AND rr.session_id = ?
              AND rr.status = 'quest_active'
            """,
            (request_id, session_id),
        ).fetchone()
        if not row:
            return None
        progress = json_load(row["progress_json"], {})
        entries = progress.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(
            {
                "turn_no": turn_no,
                "evidence": evidence,
                "created_at": now,
            }
        )
        progress["entries"] = entries[-20:]
        if completed:
            progress["completed_at"] = now
            progress["completion_evidence"] = evidence
            connection.execute(
                """
                UPDATE return_requests SET
                    status = 'completed', progress_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json_dump(progress), now, request_id),
            )
            connection.execute(
                """
                UPDATE participants SET
                    participation_status = 'active', ready = 1,
                    joined_round = ?, updated_at = ?
                WHERE id = ?
                """,
                (round_no + 1, now, row["participant_id"]),
            )
            name = row["character_name"] or row["display_name"]
            narrative = (
                f"众人完成了约定的寻找条件，并在合理的时机重新找到了{name}。"
                f"{name}将在下一轮队尾重新加入行动。"
            )
            append_event(
                connection,
                session_id=session_id,
                turn_no=turn_no,
                role="system",
                actor_id="system",
                actor_name="返场幕间",
                content=narrative,
                meta={
                    "kind": "return_complete",
                    "return_request_id": request_id,
                    "participant_id": row["participant_id"],
                },
                created_at=now,
            )
            return {
                "request_id": request_id,
                "completed": True,
                "narrative": narrative,
            }
        connection.execute(
            """
            UPDATE return_requests
            SET progress_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dump(progress), now, request_id),
        )
        return {
            "request_id": request_id,
            "completed": False,
            "evidence": evidence,
        }

    async def set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._set_participant_away,
            session_id,
            user_id,
        )

    def _set_participant_away(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] not in {
                    PARTICIPANT_ACTIVE,
                    PARTICIPANT_STANDBY,
                }:
                    raise ValueError("当前角色状态不能暂离")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'away', ready = 0,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, participant["id"]),
                )
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                next_turn, removed = leave_turn(turn_state, user_id)
                if removed:
                    connection.execute(
                        """
                        UPDATE sessions SET
                            world_state_json = ?, revision = revision + 1,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            json_dump(
                                embed_turn_state(
                                    public_world_state(stored_state),
                                    next_turn,
                                )
                            ),
                            now,
                            session_id,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE choice_sets
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND participant_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.away",
                    participant["id"],
                    {"seat_released": False},
                )
                character_name = (
                    participant["character_name"] or participant["display_name"]
                )
                away_narrative = (
                    f"🎬 暂离幕间：{character_name} 退到门边阴影中休整，"
                    "席位保留；返回时发送 /团 返回队列。"
                )
                append_event(
                    connection,
                    session_id=session_id,
                    turn_no=session["turn_no"],
                    role="system",
                    actor_id="system",
                    actor_name="暂离幕间",
                    content=away_narrative,
                    meta={
                        "kind": "away_interlude",
                        "participant_id": participant["id"],
                    },
                    created_at=now,
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["away_narrative"] = away_narrative
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._return_to_queue,
            session_id,
            user_id,
        )

    def _return_to_queue(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                self._assert_session_writable(connection, session_id)
                if participant["participation_status"] != PARTICIPANT_AWAY:
                    raise ValueError("当前角色并非暂离状态")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                stored_state = json_load(session["world_state_json"], {})
                turn_state = turn_state_from_world(stored_state)
                joined_round = int(turn_state["round_no"]) + 1
                now = utc_now()
                connection.execute(
                    """
                    UPDATE participants SET
                        participation_status = 'active',
                        joined_round = ?, ready = 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (joined_round, now, participant["id"]),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "participant.return_queue",
                    participant["id"],
                    {"effective_round": joined_round},
                )
                character_name = (
                    participant["character_name"] or participant["display_name"]
                )
                return_narrative = (
                    f"🎬 返场幕间：{character_name} 重新汇入队伍，"
                    f"将从第 {joined_round} 轮队尾恢复行动。"
                )
                append_event(
                    connection,
                    session_id=session_id,
                    turn_no=session["turn_no"],
                    role="system",
                    actor_id="system",
                    actor_name="返场幕间",
                    content=return_narrative,
                    meta={
                        "kind": "return_interlude",
                        "participant_id": participant["id"],
                        "effective_round": joined_round,
                    },
                    created_at=now,
                )
                updated = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                result = self._participant(updated)
                result["effective_round"] = joined_round
                result["return_narrative"] = return_narrative
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def retire_participant(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        forced: bool = False,
        reason: str = "",
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            actor_id,
            forced,
            reason,
            expected_revision,
            idempotency_key,
        )

    async def retire_self(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            user_id=user_id,
        )
        return await self._run(
            self._retire_participant,
            session_id,
            participant["id"],
            user_id,
            False,
            "player_exit",
        )

    def _retire_participant(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
        expected_revision: int | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        request_key = clean_text(idempotency_key, max_chars=240)
        request_payload = {
            "participant_id": clean_text(participant_id, max_chars=240),
            "forced": bool(forced),
            "reason": clean_text(reason, max_chars=500),
            "expected_revision": expected_revision,
        }
        input_hash = content_hash(request_payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if request_key:
                    receipt = connection.execute(
                        "SELECT * FROM operation_commits WHERE operation_id=?",
                        (request_key,),
                    ).fetchone()
                    if receipt is not None:
                        if str(receipt["input_hash"] or "") != input_hash:
                            raise DatabaseConflictError(
                                "相同幂等键已用于另一份角色退场请求"
                            )
                        if str(receipt["status"] or "") == "completed":
                            replay = json_load(receipt["result_json"], {})
                            replay["replayed"] = True
                            connection.execute("COMMIT")
                            return replay
                        raise DatabaseConflictError(
                            "角色退场仍在处理中，请稍后重试"
                        )
                self._assert_session_writable(connection, session_id)
                participant = connection.execute(
                    "SELECT * FROM participants WHERE id=? AND session_id=?",
                    (participant_id, session_id),
                ).fetchone()
                session = connection.execute(
                    "SELECT revision FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if participant is None or session is None:
                    raise DatabaseNotFoundError("角色或副本不存在")
                if (
                    expected_revision is not None
                    and participant_revision(
                        dict(participant), int(session["revision"] or 0)
                    )
                    != int(expected_revision)
                ):
                    raise DatabaseConflictError("角色或副本状态已经变化")
                result = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=forced,
                    reason=reason,
                )
                if request_key:
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO operation_commits(
                            operation_id, session_id, input_hash, status,
                            result_json, rollback_json, created_at, updated_at
                        ) VALUES (?, ?, ?, 'completed', ?, '{}', ?, ?)
                        """,
                        (
                            request_key,
                            session_id,
                            input_hash,
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

    def _retire_participant_in_tx(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        participant_id: str,
        actor_id: str,
        forced: bool,
        reason: str,
    ) -> dict[str, Any]:
        participant = connection.execute(
            "SELECT * FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        if not participant or participant["session_id"] != session_id:
            raise DatabaseNotFoundError("角色不存在")
        if participant["participation_status"] in {
            PARTICIPANT_RETIRED,
            PARTICIPANT_ARCHIVED,
        }:
            raise ValueError("该角色已经退场")
        session = connection.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        config = connection.execute(
            """
            SELECT world_snapshot_json FROM instance_configs
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        character_name = (
            participant["character_name"]
            or participant["display_name"]
        )
        narrative = safe_exit_narrative(
            world,
            character_name,
            forced=forced,
        )
        now = utc_now()
        connection.execute(
            """
            UPDATE participants SET
                participation_status = 'retired', ready = 0,
                exit_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                clean_text(reason or "departure", max_chars=500),
                now,
                participant_id,
            ),
        )
        connection.execute(
            """
            UPDATE players SET enabled = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, participant["player_id"]),
        )
        stored_state = json_load(session["world_state_json"], {})
        turn_state = turn_state_from_world(stored_state)
        next_turn, removed = leave_turn(
            turn_state,
            participant["group_user_id"],
        )
        if removed:
            connection.execute(
                """
                UPDATE sessions SET
                    world_state_json = ?, revision = revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json_dump(
                        embed_turn_state(
                            public_world_state(stored_state),
                            next_turn,
                        )
                    ),
                    now,
                    session_id,
                ),
            )
        connection.execute(
            """
            UPDATE choice_sets
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ?
              AND status IN ('active', 'paused')
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE delegation_grants
            SET status = 'revoked', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE character_card_drafts
            SET status = 'cancelled', updated_at = ?
            WHERE participant_id = ? AND status = 'active'
            """,
            (now, participant_id),
        )
        connection.execute(
            """
            UPDATE card_binding_codes
            SET status = 'expired'
            WHERE participant_id = ? AND status = 'active'
            """,
            (participant_id,),
        )
        append_event(
            connection,
            session_id=session_id,
            turn_no=session["turn_no"],
            role="system",
            actor_id="system",
            actor_name="退场幕间",
            content=narrative,
            meta={
                "kind": "safe_exit",
                "participant_id": participant_id,
                "forced": forced,
            },
            created_at=now,
        )
        self._insert_audit(
            connection,
            session_id,
            actor_id,
            "participant.retire",
            participant_id,
            {
                "forced": forced,
                "reason": reason,
                "seat_released": True,
            },
        )
        return {
            "participant": self._participant(participant),
            "narrative": narrative,
            "turn_changed": removed,
        }

    async def create_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
        *,
        scope: str = "instance",
        duration_seconds: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        retirement = await self._run(
            self._retire_and_ban,
            session_id,
            participant["id"],
            actor_id,
            scope,
            duration_seconds,
            reason,
        )
        return retirement

    def _retire_and_ban(
        self,
        session_id: str,
        participant_id: str,
        actor_id: str,
        scope: str,
        duration_seconds: int | None,
        reason: str,
    ) -> dict[str, Any]:
        if scope not in {"instance", "group", "global"}:
            raise ValueError("封禁范围必须是 instance、group 或 global")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                retirement = self._retire_participant_in_tx(
                    connection,
                    session_id=session_id,
                    participant_id=participant_id,
                    actor_id=actor_id,
                    forced=True,
                    reason=reason or "banned",
                )
                participant = connection.execute(
                    "SELECT * FROM participants WHERE id = ?",
                    (participant_id,),
                ).fetchone()
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                now = utc_now()
                expires_at = deadline_after(duration_seconds)
                ban_id = new_id("ban")
                connection.execute(
                    """
                    INSERT INTO ban_records(
                        id, session_id, platform_id, group_id, user_id,
                        participant_id, scope, reason, actor_id, status,
                        expires_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        ban_id,
                        session_id if scope == "instance" else "",
                        (
                            session["platform_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        (
                            session["group_id"]
                            if scope in {"instance", "group"}
                            else ""
                        ),
                        participant["group_user_id"],
                        participant_id,
                        scope,
                        clean_text(reason, max_chars=500),
                        actor_id,
                        expires_at,
                        now,
                        now,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.create",
                    ban_id,
                    {
                        "scope": scope,
                        "duration_seconds": duration_seconds,
                        "participant_id": participant_id,
                    },
                )
                connection.execute("COMMIT")
                return {
                    **retirement,
                    "ban": {
                        "id": ban_id,
                        "scope": scope,
                        "expires_at": expires_at,
                        "reason": reason,
                    },
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_ban(
        self,
        session_id: str,
        participant_ref: str,
        actor_id: str,
    ) -> int:
        participant = await self.get_participant(
            session_id,
            participant_ref=participant_ref,
        )
        return await self._run(
            self._revoke_ban,
            session_id,
            participant["group_user_id"],
            actor_id,
        )
