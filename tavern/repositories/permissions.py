from __future__ import annotations

from ..database_support import *
from ..constants import PLUGIN_VERSION


class PermissionsRepositoryMixin:
    async def authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._authorize_participant_control,
            session_id,
            participant_id,
            controller_user_id,
            permission,
        )

    def _authorize_participant_control(
        self,
        session_id: str,
        participant_id: str,
        controller_user_id: str,
        permission: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE id = ? AND session_id = ?
                    """,
                    (participant_id, session_id),
                ).fetchone()
                if not participant:
                    raise DatabaseNotFoundError("回合角色不存在")
                now = utc_now()
                self._expire_delegations_locked(connection, session_id, now)
                owner_id = str(participant["group_user_id"])
                if controller_user_id == owner_id:
                    connection.execute(
                        """
                        UPDATE delegation_grants
                        SET status = 'revoked', updated_at = ?
                        WHERE participant_id = ? AND status = 'active'
                        """,
                        (now, participant_id),
                    )
                    connection.execute("COMMIT")
                    return {
                        "authorized": True,
                        "mode": "owner",
                        "controller_user_id": controller_user_id,
                        "source": "owner",
                        "forced": False,
                    }
                rows = connection.execute(
                    """
                    SELECT * FROM delegation_grants
                    WHERE participant_id = ? AND delegate_user_id = ?
                      AND status = 'active'
                    ORDER BY created_at DESC
                    """,
                    (participant_id, controller_user_id),
                ).fetchall()
                active_row = rows[0] if rows else None
                authorized = bool(
                    active_row
                    and permission
                    in json_load(active_row["permissions_json"], [])
                )
                connection.execute("COMMIT")
                return {
                    "authorized": authorized,
                    "mode": "delegate" if authorized else "none",
                    "owner_user_id": owner_id,
                    "controller_user_id": controller_user_id,
                    "source": str(active_row["source"]) if active_row else "",
                    "forced": bool(
                        active_row
                        and str(active_row["source"]) in {"admin", "dm"}
                    ),
                    "expiry_kind": str(active_row["expiry_kind"]) if active_row else "",
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        return await self._run(
            self._list_permission_grants,
            session_id,
        )

    def _list_permission_grants(
        self,
        session_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    async def grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._grant_permission,
            session_id,
            user_id,
            role,
            actor_id,
        )

    def _grant_permission(
        self,
        session_id: str,
        user_id: str,
        role: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if role not in {"host", "moderator"}:
            raise ValueError("权限角色必须是 host 或 moderator")
        user_id = validate_platform_id(user_id, label="用户 ID")
        with self._connect() as connection:
            self._assert_session_writable(connection, session_id)
            ai_actor_ids = {
                str(row["id"])
                for row in connection.execute(
                    """
                    SELECT id FROM actors
                    WHERE session_id=? AND actor_kind='ai_companion'
                    """,
                    (session_id,),
                ).fetchall()
            }
            ai_actor_refs = {
                "public:actor:"
                + hashlib.sha256(actor_id.encode("utf-8"))
                .hexdigest()[:12]
                .upper()
                for actor_id in ai_actor_ids
            }
            if (
                user_id.startswith("public:actor:")
                or user_id in ai_actor_ids
                or user_id in ai_actor_refs
            ):
                raise ValueError(
                    "AI 队友不是平台账号，不能授予永久 host 或 moderator 权限"
                )
            now = utc_now()
            connection.execute(
                """
                INSERT INTO permission_grants(
                    id, session_id, user_id, role, granted_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, user_id, role) DO UPDATE SET
                    granted_by = excluded.granted_by,
                    created_at = excluded.created_at
                """,
                (
                    new_id("permission"),
                    session_id,
                    user_id,
                    role,
                    actor_id,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM permission_grants
                WHERE session_id = ? AND user_id = ? AND role = ?
                """,
                (session_id, user_id, role),
            ).fetchone()
            return dict(row)

    async def permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        return await self._run(
            self._permission_roles,
            session_id,
            user_id,
        )

    def _permission_roles(
        self,
        session_id: str,
        user_id: str,
    ) -> set[str]:
        with self._connect() as connection:
            return {
                str(row["role"])
                for row in connection.execute(
                    """
                    SELECT role FROM permission_grants
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (session_id, user_id),
                ).fetchall()
            }
