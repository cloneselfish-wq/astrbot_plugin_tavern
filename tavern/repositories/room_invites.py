"""persistent domain services.

The methods in this mixin are host-independent.  They expose transaction
boundaries for actors, AI companion configuration, miniprogram bindings and
room joins, module runtime status, and public choice-recovery receipts.
"""

from __future__ import annotations

import re

from ..database_support import *
from ..lifecycle import (
    attribute_maps,
    find_profession_preset,
    semantic_field_key,
)


def _public_ref(kind: str, value: object) -> str:
    digest = hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12].upper()
    return f"public:{kind}:{digest}"


def _binding_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tavern-principal-binding/1.0.0-rc10",
        "binding_ref": _public_ref("binding", row.get("id")),
        "principal_kind": str(row.get("principal_kind") or ""),
        "provider": str(row.get("provider") or ""),
        "display_name": str(row.get("display_name") or ""),
        "status": str(row.get("status") or ""),
        "revision": int(row.get("revision") or 0),
    }


def _actor_view(row: Mapping[str, Any]) -> dict[str, Any]:
    actor_status = str(
        row.get("actor_status")
        if row.get("actor_status") is not None
        else row.get("status") or ""
    )
    action_status = str(row.get("action_status") or actor_status)
    revision = int(
        row.get("instance_revision")
        or row.get("revision")
        or row.get("actor_revision")
        or 0
    )
    updated_at = max(
        (
            str(value or "")
            for value in (
                row.get("instance_updated_at"),
                row.get("actor_updated_at"),
                row.get("updated_at"),
            )
            if value
        ),
        default="",
    )
    return {
        "schema": "tavern-actor/1.0.0-rc10",
        "actor_ref": _public_ref(
            "actor", row.get("actor_id_internal") or row.get("id")
        ),
        "kind": str(row.get("actor_kind") or ""),
        "display_name": str(row.get("display_name") or ""),
        "status": actor_status,
        "action_status": action_status,
        "mode": str(row.get("mode") or ""),
        "revision": revision,
        "updated_at": updated_at,
    }


_AI_VISUAL_PRIVATE_KEYS = frozenset(
    {
        "actor_id",
        "current_operation_id",
        "decision_policy",
        "decision_policy_json",
        "lease_owner",
        "operation_id",
        "preset_id",
        "provider_id",
        "provider_policy",
        "provider_policy_json",
        "session_id",
        "system_prompt",
        "trace_id",
    }
)


def _ai_visual_input(value: Any) -> Any:
    """Remove technical AI material before semantic projection receives it."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).strip().lower()
            if (
                name in _AI_VISUAL_PRIVATE_KEYS
                or "provider" in name
                or "prompt" in name
            ):
                continue
            result[str(key)] = _ai_visual_input(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_ai_visual_input(item) for item in value]
    return value


def _ai_visual_state_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the authoritative in-process state needed by the console party lens.

    The repository deliberately excludes preset/provider/decision/lease data.
    ``state`` and ``profile`` remain internal inputs: the console semantic projector
    applies role visibility and emits only labels, values and safe statuses.
    """

    actor = _actor_view(row)
    return {
        **actor,
        "state": _ai_visual_input(
            json_load(row.get("actor_state_json", "{}"), {})
        ),
        "profile": _ai_visual_input(
            json_load(row.get("frozen_profile_json", "{}"), {})
        ),
        "awaiting_confirmation": bool(row.get("awaiting_confirmation")),
        "inventory_supported": True,
    }


def _decision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "tavern-ai-decision/1.0.0-rc10",
        "actor_ref": _public_ref("actor", row.get("actor_id")),
        "operation_ref": _public_ref("operation", row.get("operation_id")),
        "choice_set_ref": (
            _public_ref("choice", row.get("choice_set_id"))
            if row.get("choice_set_id")
            else ""
        ),
        "status": str(row.get("status") or ""),
        "decision": _ai_visual_input(
            json_load(row.get("public_projection_json", "{}"), {})
        ),
        "updated_at": str(row.get("updated_at") or ""),
    }


def _materialize_ai_profile(
    world: Mapping[str, Any],
    preset: Mapping[str, Any],
) -> dict[str, Any]:
    frozen = dict(preset)
    if isinstance(frozen.get("stats"), Mapping):
        return frozen
    try:
        template = card_template(world)
        profession_field = semantic_field_key(
            template,
            "actor.identity.profession",
        )
        primary_field = semantic_field_key(
            template,
            "actor.stats.primary",
        )
        secondary_field = semantic_field_key(
            template,
            "actor.stats.secondary",
        )
        profession_ref = str(preset.get("profession_ref") or "")
        profession = find_profession_preset(template, profession_ref)
        base = profession.get("base_attributes") or profession.get(
            "attributes"
        )
        base = base if isinstance(base, Mapping) else {}
        _label_to_key, key_to_label = attribute_maps(template)
        ranked = sorted(
            (
                (int(value), str(key))
                for key, value in base.items()
                if str(key) in key_to_label
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if (
            not profession_field
            or not primary_field
            or not secondary_field
            or len(ranked) < 2
        ):
            return frozen
        fields = {
            profession_field: profession_ref,
            primary_field: key_to_label[ranked[0][1]],
            secondary_field: key_to_label[ranked[1][1]],
        }
        frozen["stats"] = resolve_profession_stats(
            template,
            fields,
            require_complete=True,
        )
        frozen["stat_selection"] = {
            "primary": ranked[0][1],
            "secondary": ranked[1][1],
            "source": "deterministic_profession_profile",
        }
    except (KeyError, TypeError, ValueError):
        # Worlds without an attribute contract remain valid AI worlds, but
        # check choices will be rejected instead of inventing modifiers.
        pass
    return frozen


class RoomInviteRepositoryMixin:
    async def create_room_invite(
        self,
        *,
        session_id: str,
        binding_ref: str,
        max_uses: int,
        expires_in_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._create_room_invite,
            session_id,
            binding_ref,
            max_uses,
            expires_in_seconds,
            idempotency_key,
        )

    def _create_room_invite(
        self,
        session_id: str,
        binding_ref: str,
        max_uses: int,
        expires_in_seconds: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("创建邀请需要 idempotency_key")
        max_uses = max(1, min(50, int(max_uses or 1)))
        ttl = max(60, min(86400, int(expires_in_seconds or 1800)))
        invite_id = "invite_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM room_invites WHERE id=?", (invite_id,)
                ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return {
                        "schema": "tavern-room-invite/1.0.0-rc10",
                        "invite_ref": _public_ref("invite", invite_id),
                        "code": "",
                        "code_reissued": False,
                        "status": str(existing["status"]),
                        "expires_at": str(existing["expires_at"]),
                        "revision": int(existing["revision"]),
                    }
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if str(session["state"]) in {SESSION_FINISHED}:
                    raise InvalidTransitionError("已归档副本不能创建邀请")
                binding = self._binding_by_public_ref(connection, binding_ref)
                if binding is None or str(binding["status"]) != "active":
                    raise PermissionError("创建邀请需要有效的小程序绑定")
                code = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
                code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
                now = datetime.now(timezone.utc)
                expires_at = (now + timedelta(seconds=ttl)).isoformat(timespec="seconds")
                now_text = now.isoformat(timespec="seconds")
                connection.execute(
                    """
                    INSERT INTO room_invites(
                        id, session_id, code_hash, created_by_binding_id,
                        max_uses, use_count, status, expires_at,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 'active', ?, 1, ?, ?)
                    """,
                    (
                        invite_id,
                        session_id,
                        code_hash,
                        binding["id"],
                        max_uses,
                        expires_at,
                        now_text,
                        now_text,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "schema": "tavern-room-invite/1.0.0-rc10",
            "invite_ref": _public_ref("invite", invite_id),
            "code": code,
            "code_reissued": True,
            "status": "active",
            "expires_at": expires_at,
            "revision": 1,
        }

    async def join_room_with_invite(
        self,
        *,
        binding_ref: str,
        invite_code: str,
        display_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._join_room_with_invite,
            binding_ref,
            invite_code,
            display_name,
            idempotency_key,
        )

    def _join_room_with_invite(
        self,
        binding_ref: str,
        invite_code: str,
        display_name: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(idempotency_key or "").strip():
            raise ValueError("加入房间需要 idempotency_key")
        code = str(invite_code or "").strip()
        if not code:
            raise ValueError("请输入房间邀请码")
        public_name = clean_text(display_name, max_chars=100)
        if not public_name:
            raise ValueError("加入房间失败：平台没有提供可公开显示的名称")
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                binding = self._binding_by_public_ref(connection, binding_ref)
                if binding is None or str(binding["status"]) != "active":
                    raise PermissionError("小程序身份尚未绑定或已失效")
                invite = connection.execute(
                    "SELECT * FROM room_invites WHERE code_hash=?", (code_hash,)
                ).fetchone()
                if invite is None:
                    raise DatabaseNotFoundError("邀请码无效或已失效")
                user_key = f"miniprogram:{binding['id']}"
                participant = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id=? AND group_user_id=?
                    """,
                    (invite["session_id"], user_key),
                ).fetchone()
                if participant is not None:
                    participant_id = str(participant["id"])
                    session = connection.execute(
                        "SELECT * FROM sessions WHERE id=?", (invite["session_id"],)
                    ).fetchone()
                    new_status = str(invite["status"])
                    connection.execute("COMMIT")
                    return {
                        "schema": "tavern-room-membership/1.0.0-rc10",
                        "session_ref": _public_ref("session", session["id"]),
                        "participant_ref": _public_ref("participant", participant_id),
                        "session_revision": int(session["revision"]),
                        "invite_status": new_status,
                        "joined": False,
                    }
                now = utc_now()
                if str(invite["status"]) != "active":
                    raise InvalidTransitionError("邀请码已失效")
                if str(invite["expires_at"]) <= now:
                    connection.execute(
                        """
                        UPDATE room_invites SET status='expired',
                            revision=revision+1, updated_at=? WHERE id=?
                        """,
                        (now, invite["id"]),
                    )
                    raise InvalidTransitionError("邀请码已过期")
                if int(invite["use_count"]) >= int(invite["max_uses"]):
                    raise InvalidTransitionError("邀请码使用次数已满")
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id=?", (invite["session_id"],)
                ).fetchone()
                if session is None:
                    raise DatabaseNotFoundError("副本不存在")
                if str(session["state"]) != SESSION_PREPARING:
                    raise InvalidTransitionError("当前副本未开放加入")
                participant_id = new_id("participant")
                connection.execute(
                    """
                    INSERT INTO participants(
                        id, session_id, group_user_id, display_name,
                        participation_status, seat_reserved_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        participant_id,
                        session["id"],
                        user_key,
                        public_name,
                        now,
                        now,
                        now,
                    ),
                )
                new_count = int(invite["use_count"]) + 1
                new_status = (
                    "consumed"
                    if new_count >= int(invite["max_uses"])
                    else "active"
                )
                connection.execute(
                    """
                    UPDATE room_invites SET
                        use_count=?, status=?, last_used_at=?,
                        revision=revision+1, updated_at=?
                    WHERE id=?
                    """,
                    (new_count, new_status, now, now, invite["id"]),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "schema": "tavern-room-membership/1.0.0-rc10",
            "session_ref": _public_ref("session", session["id"]),
            "participant_ref": _public_ref("participant", participant_id),
            "session_revision": int(session["revision"]),
            "invite_status": new_status,
            "joined": True,
        }


__all__ = ["RoomInviteRepositoryMixin"]
