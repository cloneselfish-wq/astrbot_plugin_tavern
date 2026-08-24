from __future__ import annotations

from .sessions_support import *


class SessionLifecycleMutationsRepositoryMixin:
    def _transition_session(
        self,
        session_id: str,
        target_state: str,
        actor_id: str,
        world_ref: str,
        expected_revision: int = 0,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if target_state not in SESSION_STATES:
            raise ValueError("非法会话状态")
        if target_state == SESSION_FINISHED:
            raise InvalidTransitionError(
                "完结必须使用原子归档流程，不能直接切换状态"
            )
        allowed = {
            SESSION_CLOSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_MAINTENANCE,
            },
            SESSION_PREPARING: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
            SESSION_RUNNING: {
                SESSION_RUNNING,
                SESSION_PAUSED,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_PAUSED: {
                SESSION_PREPARING,
                SESSION_RUNNING,
                SESSION_CLOSED,
                SESSION_MAINTENANCE,
            },
            SESSION_FINISHED: set(),
            SESSION_MAINTENANCE: {
                SESSION_PREPARING,
                SESSION_PAUSED,
                SESSION_CLOSED,
            },
        }
        owns_connection = connection is None
        connection_scope = (
            self._connect() if owns_connection else nullcontext(connection)
        )
        with connection_scope as connection:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not current:
                    raise DatabaseNotFoundError("会话不存在")
                if expected_revision and int(current["revision"] or 0) != int(
                    expected_revision
                ):
                    raise DatabaseConflictError(
                        "副本状态已变化；系统未执行生命周期操作，请刷新后重新确认"
                    )
                archived = connection.execute(
                    "SELECT 1 FROM session_archives WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if archived or current["state"] == SESSION_FINISHED:
                    raise InvalidTransitionError(
                        "该副本已经永久归档；如需继续，请从最终存档克隆新副本"
                    )
                if target_state not in allowed[current["state"]]:
                    raise InvalidTransitionError(
                        f"不能从 {current['state']} 切换为 {target_state}"
                    )

                world_id = current["world_id"]
                world_state_json = current["world_state_json"]
                turn_no = current["turn_no"]
                history_floor_seq = current["history_floor_seq"]
                switched_world = False
                if world_ref:
                    world = connection.execute(
                        """
                        SELECT * FROM worlds
                        WHERE (id = ? OR slug = ?) AND archived = 0
                        """,
                        (world_ref, world_ref),
                    ).fetchone()
                    if not world:
                        raise DatabaseNotFoundError("世界包不存在或已归档")
                    if world["id"] != world_id:
                        if current["state"] != SESSION_CLOSED:
                            raise InvalidTransitionError(
                                "只有关闭状态才能更换世界包"
                            )
                        world_id = world["id"]
                        world_state_json = json_dump(
                            public_world_state(
                                json_load(world["initial_state_json"], {})
                            )
                        )
                        turn_no = 0
                        max_seq = connection.execute(
                            """
                            SELECT COALESCE(MAX(seq), 0)
                            FROM events WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()[0]
                        history_floor_seq = max_seq + 1
                        switched_world = True
                        world_payload = {
                            "id": world["id"],
                            "slug": world["slug"],
                            "name": world["name"],
                            "description": world["description"],
                            "system_prompt": world["system_prompt"],
                            "rules": json_load(world["rules_json"], {}),
                            "opening_scene": world["opening_scene"],
                            "initial_state": json_load(
                                world["initial_state_json"],
                                {},
                            ),
                            "revision": world["revision"],
                        }
                        connection.execute(
                            """
                            UPDATE instance_configs SET
                                world_revision = ?,
                                world_snapshot_json = ?,
                                ui_profile_json = ?,
                                time_rules_json = ?,
                                phase_meta_json = ?,
                                updated_at = ?
                            WHERE session_id = ?
                            """,
                            (
                                world["revision"],
                                json_dump(world_payload),
                                world["ui_profile_json"],
                                json_dump(world_time_rules(world_payload)),
                                json_dump({"resume_mode": False}),
                                utc_now(),
                                session_id,
                            ),
                        )

                now = utc_now()
                selected = int(current["selected"])
                auto_paused: list[str] = []
                if target_state in {
                    SESSION_PREPARING,
                    SESSION_RUNNING,
                }:
                    running_rows = connection.execute(
                        """
                        SELECT id FROM sessions
                        WHERE platform_id = ? AND group_id = ?
                          AND state = 'running' AND id <> ?
                        """,
                        (
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    ).fetchall()
                    auto_paused = [str(row["id"]) for row in running_rows]
                    connection.execute(
                        """
                        UPDATE sessions SET
                            state = CASE
                                WHEN state = 'running' THEN 'paused'
                                ELSE state
                            END,
                            selected = 0,
                            revision = CASE
                                WHEN state = 'running' THEN revision + 1
                                ELSE revision
                            END,
                            updated_at = CASE
                                WHEN state = 'running' THEN ?
                                ELSE updated_at
                            END
                        WHERE platform_id = ? AND group_id = ? AND id <> ?
                        """,
                        (
                            now,
                            current["platform_id"],
                            current["group_id"],
                            session_id,
                        ),
                    )
                    selected = 1
                if target_state == SESSION_PREPARING:
                    connection.execute(
                        """
                        UPDATE participants
                        SET ready = 0, updated_at = ?
                        WHERE session_id = ?
                          AND participation_status IN (
                              'reserved', 'active', 'standby', 'away'
                          )
                        """,
                        (now, session_id),
                    )
                    config_row = connection.execute(
                        """
                        SELECT phase_meta_json, time_rules_json
                        FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    phase_meta = json_load(
                        config_row["phase_meta_json"] if config_row else "",
                        {},
                    )
                    phase_meta["resume_mode"] = bool(turn_no)
                    phase_meta["entered_preparing_at"] = now
                    connection.execute(
                        """
                        UPDATE instance_configs
                        SET phase_meta_json = ?, updated_at = ?
                        WHERE session_id = ?
                        """,
                        (json_dump(phase_meta), now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'preparation'
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    time_rules = normalize_time_rules(
                        json_load(
                            config_row["time_rules_json"]
                            if config_row
                            else "",
                            {},
                        )
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id="",
                        timer_type="preparation",
                        timeout_seconds=time_rules[
                            "preparation_timeout_seconds"
                        ],
                        reminder_seconds=None,
                        action={"resume_mode": bool(turn_no)},
                    )
                if target_state in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE group_votes
                        SET status = 'cancelled', decision_status='cancelled',
                            resolution_status='cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'open'
                        """,
                        (now, session_id),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                suspended_drafts = 0
                resumed_drafts = 0
                if target_state == SESSION_CLOSED:
                    suspended_drafts = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM character_card_drafts
                            WHERE participant_id IN (
                                SELECT id FROM participants
                                WHERE session_id = ?
                            ) AND status = 'active'
                            """,
                            (session_id,),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'suspended', updated_at = ?
                        WHERE participant_id IN (
                            SELECT id FROM participants WHERE session_id = ?
                        ) AND status = 'active'
                        """,
                        (now, session_id),
                    )
                elif (
                    target_state == SESSION_PREPARING
                    and str(current["state"] or "") == SESSION_CLOSED
                ):
                    resumed_drafts = int(
                        connection.execute(
                            """
                            SELECT COUNT(*)
                            FROM character_card_drafts
                            WHERE participant_id IN (
                                SELECT id FROM participants
                                WHERE session_id = ?
                            ) AND status = 'suspended'
                            """,
                            (session_id,),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        UPDATE character_card_drafts
                        SET status = 'active', updated_at = ?
                        WHERE participant_id IN (
                            SELECT id FROM participants WHERE session_id = ?
                        ) AND status = 'suspended'
                        """,
                        (now, session_id),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET
                        world_id = ?, state = ?, selected = ?, turn_no = ?,
                        world_state_json = ?, history_floor_seq = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        world_id,
                        target_state,
                        selected,
                        turn_no,
                        world_state_json,
                        history_floor_seq,
                        now,
                        session_id,
                    ),
                )
                detail = {
                    "from": current["state"],
                    "to": target_state,
                    "world_changed": switched_world,
                    "auto_paused_instances": auto_paused,
                    "suspended_card_drafts": suspended_drafts,
                    "resumed_card_drafts": resumed_drafts,
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.transition",
                    session_id,
                    detail,
                )
                row = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if owns_connection:
                    connection.execute("COMMIT")
                return self._session(row)
            except Exception:
                if owns_connection:
                    connection.execute("ROLLBACK")
                raise

    async def finalize_session(
        self,
        session_id: str,
        actor_id: str,
        *,
        termination_type: str = "completed",
        reason: str = "",
        terminal_match: Mapping[str, Any] | None = None,
        trigger_revision: int = 0,
    ) -> dict[str, Any]:
        return await self._run(
            self._finalize_session,
            session_id,
            actor_id,
            termination_type,
            reason,
            terminal_match,
            trigger_revision,
        )

    def _finalize_existing_result(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        plan: Mapping[str, Any],
        *,
        decision: str,
    ) -> dict[str, Any]:
        """幂等重放/被其他终局取代时返回既有结果，不再生成第二份归档。"""

        row = connection.execute(
            """
            SELECT s.*, w.name AS world_name, w.slug AS world_slug,
                   srs.progress_json, srs.recovery_json,
                   sa.termination_type,
                   sa.reason AS archive_reason,
                   sa.final_snapshot_id, sa.ended_by, sa.ended_at,
                   sa.ending_ref, sa.ending_label, sa.readonly
            FROM sessions s
            JOIN worlds w ON w.id = s.world_id
            LEFT JOIN session_rule_states srs
              ON srs.session_id = s.id
            LEFT JOIN session_archives sa ON sa.session_id = s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        finalization = connection.execute(
            "SELECT * FROM session_finalizations WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        base = (
            self._session(row)
            if row is not None
            else {"id": session_id, "state": "finished"}
        )
        return {
            **base,
            "idempotency_key": str(plan.get("idempotency_key") or ""),
            "decision": decision,
            "projection": dict(plan.get("projection") or {}),
            "finalization": dict(finalization) if finalization else {},
        }

    async def delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        result = await self._run(
            self._delete_session,
            session_id,
            actor_id,
            confirm_name,
        )
        try:
            trashed = await asyncio.to_thread(
                self.storage.trash_relative_path,
                str(result.get("relative_path") or ""),
                label=str(result.get("instance_slug") or "story"),
            )
            result["trash_path"] = str(trashed or "")
        except Exception as exc:
            result["trash_error"] = str(exc)[:500]
        return result

    def _delete_session(
        self,
        session_id: str,
        actor_id: str,
        confirm_name: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT s.*, ss.relative_path
                    FROM sessions s
                    LEFT JOIN story_storage ss ON ss.session_id = s.id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("副本不存在")
                if row["state"] not in {
                    SESSION_CLOSED,
                    SESSION_FINISHED,
                }:
                    raise InvalidTransitionError(
                        "只能删除已关闭或已归档的副本"
                    )
                if str(confirm_name or "").strip() != str(
                    row["instance_name"]
                ):
                    raise ValueError("确认名称与副本名称不一致")
                detail = {
                    "instance_name": row["instance_name"],
                    "instance_slug": row["instance_slug"],
                    "relative_path": str(row["relative_path"] or ""),
                    "state": row["state"],
                }
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.delete",
                    session_id,
                    detail,
                )
                connection.execute(
                    """
                    DELETE FROM token_quota_policies
                    WHERE scope_type = 'session' AND scope_id = ?
                    """,
                    (session_id,),
                )
                connection.execute(
                    "DELETE FROM sessions WHERE id = ?",
                    (session_id,),
                )
                replacement = connection.execute(
                    """
                    SELECT id FROM sessions
                    WHERE platform_id = ? AND group_id = ?
                      AND state <> 'finished'
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (row["platform_id"], row["group_id"]),
                ).fetchone()
                if replacement:
                    connection.execute(
                        """
                        UPDATE sessions SET selected = CASE WHEN id = ? THEN 1 ELSE 0 END
                        WHERE platform_id = ? AND group_id = ?
                        """,
                        (
                            replacement["id"],
                            row["platform_id"],
                            row["group_id"],
                        ),
                    )
                connection.execute("COMMIT")
                return {
                    "deleted": True,
                    "session_id": session_id,
                    **detail,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise
