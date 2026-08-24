from __future__ import annotations

from .sessions_support import *


class ParticipantsQueriesRepositoryMixin:
    def _save_player(
        self,
        payload: dict[str, Any],
        actor_id: str,
    ) -> dict[str, Any]:
        session_id = validate_platform_id(
            payload.get("session_id"),
            label="会话 ID",
        )
        user_id = validate_platform_id(
            payload.get("user_id"),
            label="用户 ID",
        )
        display_name = clean_text(
            payload.get("display_name"),
            max_chars=100,
        )
        if not display_name:
            raise ValueError("显示名称不能为空")
        character_name = clean_text(
            payload.get("character_name"),
            max_chars=100,
        )
        profile = payload.get("profile")
        if not isinstance(profile, Mapping):
            raise ValueError("玩家资料必须是 JSON 对象")
        enabled = int(bool(payload.get("enabled", True)))
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_session_writable(connection, session_id)
                existing = connection.execute(
                    """
                    SELECT * FROM players
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchone()
                if existing:
                    connection.execute(
                        """
                        UPDATE players SET
                            display_name = ?, character_name = ?,
                            profile_json = ?, enabled = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            existing["id"],
                        ),
                    )
                    # A16.3：玩家资料里的角色名/显示名同步到 participants，
                    # 避免「回合与行动者(players)」与「阵容/行动卡(participants)」名字不一致。
                    if character_name:
                        connection.execute(
                            """
                            UPDATE participants SET
                                character_name = ?, display_name = ?,
                                updated_at = ?
                            WHERE session_id = ? AND group_user_id = ?
                            """,
                            (character_name, display_name, now, session_id, user_id),
                        )
                    player_id = existing["id"]
                    action = "player.update"
                else:
                    player_id = new_id("player")
                    connection.execute(
                        """
                        INSERT INTO players(
                            id, session_id, user_id, display_name,
                            character_name, profile_json, enabled,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            player_id,
                            session_id,
                            user_id,
                            display_name,
                            character_name,
                            json_dump(dict(profile)),
                            enabled,
                            now,
                            now,
                        ),
                    )
                    action = "player.create"
                if not enabled:
                    self._remove_turn_member(
                        connection,
                        session_id,
                        user_id,
                        updated_at=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    action,
                    player_id,
                    {"user_id": user_id, "display_name": display_name},
                )
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._player(row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def delete_player(
        self,
        player_id: str,
        actor_id: str,
    ) -> None:
        await self._run(self._delete_player, player_id, actor_id)

    def _delete_player(self, player_id: str, actor_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM players WHERE id = ?",
                    (player_id,),
                ).fetchone()
                if not row:
                    raise DatabaseNotFoundError("玩家不存在")
                self._assert_session_writable(
                    connection,
                    row["session_id"],
                )
                connection.execute(
                    "DELETE FROM players WHERE id = ?",
                    (player_id,),
                )
                self._remove_turn_member(
                    connection,
                    row["session_id"],
                    row["user_id"],
                    updated_at=utc_now(),
                )
                self._insert_audit(
                    connection,
                    row["session_id"],
                    actor_id,
                    "player.delete",
                    player_id,
                    {"user_id": row["user_id"]},
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
