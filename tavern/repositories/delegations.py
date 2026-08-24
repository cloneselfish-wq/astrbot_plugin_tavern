from __future__ import annotations

from ..database_support import *
from ..constants import PLUGIN_VERSION


class DelegationsRepositoryMixin:
    async def grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        *,
        duration_seconds: int | None = None,
        permissions: list[str] | None = None,
        expiry_kind: str = "none",
        expires_round: int = 0,
        auto_restore: bool = False,
        source: str = "player",
    ) -> dict[str, Any]:
        """A16：授予角色代控权。

        source=player 仅允许本人授权；source=admin/dm 允许管理员/人工 DM
        强制托管（由上层权限判断决定）。
        """
        return await self._run(
            self._grant_delegation,
            session_id,
            owner_user_id,
            delegate_user_id,
            actor_id,
            duration_seconds,
            list(permissions or []) if permissions else None,
            str(expiry_kind or "none").strip(),
            int(expires_round or 0),
            bool(auto_restore),
            str(source or "player").strip(),
        )

    def _grant_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        delegate_user_id: str,
        actor_id: str,
        duration_seconds: int | None,
        permissions: list[str] | None,
        expiry_kind: str,
        expires_round: int,
        auto_restore: bool,
        source: str,
    ) -> dict[str, Any]:
        owner_user_id = validate_platform_id(
            owner_user_id, label="角色拥有者 ID"
        )
        delegate_user_id = validate_platform_id(
            delegate_user_id, label="代控用户 ID"
        )
        if source not in {"player", "admin", "dm", "system"}:
            raise ValueError("托管来源必须为 player/admin/dm/system")
        if source == "player" and actor_id != owner_user_id:
            raise PermissionError("代控只能由角色本人授权")
        if owner_user_id == delegate_user_id:
            raise ValueError("不能把自己的角色授权给自己")
        if expiry_kind not in {"none", "datetime", "round", "instance"}:
            raise ValueError("托管期限类型必须为 none/datetime/round/instance")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND group_user_id = ?
                    """,
                    (session_id, owner_user_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("你尚未加入当前副本")
                if participant["participation_status"] in {
                    PARTICIPANT_RETIRED,
                    PARTICIPANT_ARCHIVED,
                }:
                    raise ValueError("已经退场的角色不能授权代控")
                if duration_seconds is None and expiry_kind == "datetime":
                    config = connection.execute(
                        """
                        SELECT time_rules_json FROM instance_configs
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchone()
                    rules = normalize_time_rules(
                        json_load(
                            config["time_rules_json"] if config else "",
                            {},
                        )
                    )
                    duration_seconds = rules["delegation_ttl_seconds"]
                now = utc_now()
                connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE participant_id = ? AND status = 'active'
                    """,
                    (now, participant["id"]),
                )
                grant_id = new_id("delegation")
                expires_at = (
                    deadline_after(duration_seconds)
                    if expiry_kind == "datetime" and duration_seconds
                    else ""
                )
                if expiry_kind == "round" and expires_round <= 0:
                    session_row = connection.execute(
                        "SELECT world_state_json, turn_no FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if session_row:
                        stored = json_load(session_row["world_state_json"], {})
                        expires_round = max(
                            1,
                            int(turn_state_from_world(stored).get("round_no") or 0),
                            int(session_row["turn_no"] or 0),
                        )
                default_permissions = ["choose", "reroll", "skip"]
                granted = permissions if permissions else default_permissions
                allowed = {
                    "choose", "vote", "reroll", "skip", "free_action", "check", "combat",
                    "view_private", "modify_temp", "modify_permanent",
                }
                granted = [p for p in granted if p in allowed]
                if not granted:
                    raise ValueError("托管权限列表为空或包含非法权限")
                connection.execute(
                    """
                    INSERT INTO delegation_grants(
                        id, session_id, participant_id, owner_user_id,
                        delegate_user_id, permissions_json, status,
                        expires_at, created_at, updated_at,
                        expiry_kind, expires_round, auto_restore, source,
                        granted_by
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        grant_id,
                        session_id,
                        participant["id"],
                        owner_user_id,
                        delegate_user_id,
                        json_dump(granted),
                        expires_at,
                        now,
                        now,
                        expiry_kind,
                        expires_round,
                        int(auto_restore),
                        source,
                        actor_id,
                    ),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.grant",
                    grant_id,
                    {
                        "participant_id": participant["id"],
                        "owner_user_id": owner_user_id,
                        "delegate_user_id": delegate_user_id,
                        "permissions": granted,
                        "expiry_kind": expiry_kind,
                        "expires_at": expires_at,
                        "expires_round": expires_round,
                        "auto_restore": auto_restore,
                        "source": source,
                    },
                )
                row = connection.execute(
                    "SELECT * FROM delegation_grants WHERE id = ?",
                    (grant_id,),
                ).fetchone()
                connection.execute("COMMIT")
                result = dict(row)
                result["permissions"] = json_load(
                    result.pop("permissions_json"), []
                )
                return result
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
        *,
        force: bool = False,
    ) -> int:
        return await self._run(
            self._revoke_delegation,
            session_id,
            owner_user_id,
            actor_id,
            bool(force),
        )

    def _revoke_delegation(
        self,
        session_id: str,
        owner_user_id: str,
        actor_id: str,
        force: bool,
    ) -> int:
        if actor_id != owner_user_id and not force:
            raise PermissionError("代控只能由角色本人撤销")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                now = utc_now()
                cursor = connection.execute(
                    """
                    UPDATE delegation_grants
                    SET status = 'revoked', updated_at = ?
                    WHERE session_id = ? AND owner_user_id = ?
                      AND status = 'active'
                    """,
                    (now, session_id, owner_user_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "delegation.revoke",
                    owner_user_id,
                    {"count": cursor.rowcount, "forced": force},
                )
                connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def active_controller(
        self,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._active_controller, session_id, participant_id
        )

    def _active_controller(
        self,
        session_id: str,
        participant_id: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            participant = connection.execute(
                """
                SELECT group_user_id, display_name, character_name
                FROM participants WHERE id = ? AND session_id = ?
                """,
                (participant_id, session_id),
            ).fetchone()
            if not participant:
                raise DatabaseNotFoundError("回合角色不存在")
            self._expire_delegations_locked(connection, session_id, utc_now())
            row = connection.execute(
                """
                SELECT * FROM delegation_grants
                WHERE participant_id = ? AND status = 'active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (participant_id,),
            ).fetchone()
        owner_user_id = str(participant["group_user_id"] or "")
        if not row:
            return {
                "participant_id": participant_id,
                "owner_user_id": owner_user_id,
                "controller_user_id": owner_user_id,
                "mode": "owner",
                "grant": None,
            }
        return {
            "participant_id": participant_id,
            "owner_user_id": owner_user_id,
            "controller_user_id": str(row["delegate_user_id"]),
            "mode": "delegate",
            "grant": dict(row),
        }

    async def expire_due_delegations(self, session_id: str) -> int:
        return await self._run(self._expire_due_delegations, session_id)

    def _expire_due_delegations(self, session_id: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                count = self._expire_delegations_locked(
                    connection, session_id, utc_now()
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return count

    def _expire_delegations_locked(
        self,
        connection: Any,
        session_id: str,
        now: str,
    ) -> int:
        session_row = connection.execute(
            "SELECT state, world_state_json, turn_no FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        current_round = 0
        session_state = ""
        if session_row:
            session_state = str(session_row["state"] or "")
            stored = json_load(session_row["world_state_json"], {})
            # 会话列与世界状态应同步推进；读取时仍取二者较大值，以便旧存档、
            # 中断恢复或迁移途中只有一侧先更新时不会让已结束的“当前回合”托管复活。
            current_round = max(
                int(turn_state_from_world(stored).get("round_no") or 0),
                int(session_row["turn_no"] or 0),
            )
        due_rows = connection.execute(
            """
            SELECT id, participant_id, owner_user_id, delegate_user_id,
                   expiry_kind, expires_round, expires_at, auto_restore
            FROM delegation_grants
            WHERE session_id = ? AND status = 'active'
              AND (
                (expires_at <> '' AND expires_at <= ?)
                OR (expiry_kind = 'round' AND expires_round > 0 AND ? > expires_round)
                OR (expiry_kind = 'instance' AND ? IN ('closed', 'finished', 'archived'))
              )
            """,
            (session_id, now, current_round, session_state),
        ).fetchall()
        if not due_rows:
            return 0
        ids = [str(row["id"]) for row in due_rows]
        placeholders = ",".join("?" for _ in ids)
        cursor = connection.execute(
            f"""
            UPDATE delegation_grants SET status = 'expired', updated_at = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (now, *ids),
        )
        for row in due_rows:
            self._insert_audit(
                connection,
                session_id,
                "system",
                "delegation.expire",
                str(row["id"]),
                {
                    "participant_id": str(row["participant_id"]),
                    "owner_user_id": str(row["owner_user_id"]),
                    "delegate_user_id": str(row["delegate_user_id"]),
                    "expiry_kind": str(row["expiry_kind"]),
                    "expires_round": int(row["expires_round"] or 0),
                    "current_round": current_round,
                    "auto_restore": bool(row["auto_restore"]),
                },
            )
        return cursor.rowcount

    async def list_delegations(self, session_id: str) -> list[dict[str, Any]]:
        return await self._run(self._list_delegations, session_id)

    def _list_delegations(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_delegations_locked(connection, session_id, utc_now())
            rows = connection.execute(
                """
                SELECT d.*, pt.display_name AS participant_display,
                       pt.character_name AS participant_character
                FROM delegation_grants d
                JOIN participants pt ON pt.id = d.participant_id
                WHERE d.session_id = ? AND d.status = 'active'
                ORDER BY d.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            connection.execute("COMMIT")
        result = []
        for row in rows:
            item = dict(row)
            item["permissions"] = json_load(item.pop("permissions_json"), [])
            result.append(item)
        return result


    async def list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_return_requests,
            session_id,
        )

    def _list_return_requests(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rr.*, pt.character_name, pt.display_name
                FROM return_requests rr
                JOIN participants pt ON pt.id = rr.participant_id
                WHERE rr.session_id = ?
                ORDER BY rr.created_at DESC
                """,
                (session_id,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["progress"] = json_load(item.pop("progress_json"), {})
                result.append(item)
            return result
