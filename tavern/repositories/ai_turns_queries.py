from __future__ import annotations

from .workflow_support import *


class AiTurnsQueriesRepositoryMixin:
    def _revoke_ban(
        self,
        session_id: str,
        user_id: str,
        actor_id: str,
    ) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE ban_records
                    SET status = 'revoked', updated_at = ?
                    WHERE user_id = ? AND status = 'active'
                    """,
                    (now, user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "ban.revoke",
                    user_id,
                    {"count": cursor.rowcount},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_bans(
        self,
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        return await self._run(self._list_bans, session_id)

    def _list_bans(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            now = utc_now()
            connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> '' AND expires_at <= ?
                """,
                (now, now),
            )
            if session_id:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' AND (
                           scope = 'global'
                        OR (scope = 'group'
                            AND platform_id = ? AND group_id = ?)
                        OR (scope = 'instance' AND session_id = ?)
                    )
                    ORDER BY created_at DESC
                    """,
                    (
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM ban_records
                    WHERE status = 'active' ORDER BY created_at DESC
                    """
                ).fetchall()
            return [dict(row) for row in rows]

    async def request_return(
        self,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._request_return,
            session_id,
            user_id,
        )

    def _request_return(
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
                    raise DatabaseNotFoundError("没有可返场的历史角色")
                if participant["participation_status"] != PARTICIPANT_RETIRED:
                    raise ValueError("只有已经正式退场的角色可以申请返场")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if self._active_ban_for(
                    connection,
                    session=session,
                    user_id=user_id,
                ):
                    raise PermissionError("封禁尚未解除，不能申请返场")
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
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
                    (session_id, *sorted(SEAT_HOLDING_STATUSES)),
                ).fetchone()[0]
                if occupied >= limits["maximum"]:
                    raise ValueError("当前没有空余席位，暂时无法申请返场")
                existing = connection.execute(
                    """
                    SELECT * FROM return_requests
                    WHERE participant_id = ?
                      AND status IN ('requested', 'voting', 'quest_active')
                    """,
                    (participant["id"],),
                ).fetchone()
                if existing:
                    raise ValueError("该角色已经有进行中的返场流程")
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        """,
                        (session_id,),
                    ).fetchall()
                ]
                if not eligible:
                    raise ValueError("当前没有可参与返场表决的在场玩家")
                name = (
                    participant["character_name"]
                    or participant["display_name"]
                )
                objective = (
                    f"沿着{name}离场时留下的线索，完成一次合理的寻找、"
                    "营救、解除困境或约定会合剧情。"
                )
                now = utc_now()
                vote_id = new_id("vote")
                options = [
                    {"key": "A", "text": f"同意开启{name}的返场支线"},
                    {"key": "B", "text": "暂不开启返场支线"},
                ]
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, question, options_json,
                        eligible_user_ids_json, stage, status,
                        suspended_user_id, deadline_at, result_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, 'open', '', ?, '{}', ?, ?)
                    """,
                    (
                        vote_id,
                        session_id,
                        f"是否为{name}开启一条需要通过剧情完成的返场支线？",
                        json_dump(options),
                        json_dump(eligible),
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        now,
                        now,
                    ),
                )
                request_id = new_id("return")
                connection.execute(
                    """
                    INSERT INTO return_requests(
                        id, session_id, participant_id, requested_by,
                        status, exit_type, objective, progress_json,
                        vote_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'voting', 'departure', ?,
                              '{}', ?, ?, ?)
                    """,
                    (
                        request_id,
                        session_id,
                        participant["id"],
                        user_id,
                        objective,
                        vote_id,
                        now,
                        now,
                    ),
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "return_request_id": request_id},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "return.request",
                    request_id,
                    {"vote_id": vote_id, "objective": objective},
                )
                connection.execute("COMMIT")
                return {
                    "request_id": request_id,
                    "vote_id": vote_id,
                    "objective": objective,
                    "character_name": name,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    # ── A17：后台调整回合顺序前作废旧活跃选项 ─────────────────────