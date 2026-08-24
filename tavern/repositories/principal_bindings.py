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


class PrincipalBindingRepositoryMixin:
    def _actor_by_public_ref(
        self,
        connection: sqlite3.Connection,
        actor_ref: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            "SELECT * FROM actors ORDER BY created_at, id"
        ).fetchall()
        row = next(
            (
                item
                for item in rows
                if _public_ref("actor", item["id"]) == str(actor_ref)
            ),
            None,
        )
        if row is None:
            raise DatabaseNotFoundError("AI 队友不存在")
        return row

    async def bind_miniprogram_principal(
        self,
        *,
        provider: str,
        app_id: str,
        external_subject_hash: str,
        local_user_ref: str = "",
        display_name: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._bind_miniprogram_principal,
            provider,
            app_id,
            external_subject_hash,
            local_user_ref,
            display_name,
        )

    def _bind_miniprogram_principal(
        self,
        provider: str,
        app_id: str,
        external_subject_hash: str,
        local_user_ref: str,
        display_name: str,
    ) -> dict[str, Any]:
        provider = str(provider or "").strip().lower()
        if provider not in {"wechat", "qq"}:
            raise ValueError("小程序 provider 只能是 wechat 或 qq")
        subject_hash = str(external_subject_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", subject_hash):
            raise ValueError("外部身份必须先转换为 64 位不可逆哈希")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM principal_bindings
                    WHERE principal_kind='miniprogram'
                      AND provider=? AND app_id=? AND external_subject_hash=?
                    """,
                    (provider, str(app_id or ""), subject_hash),
                ).fetchone()
                if row is None:
                    binding_id = new_id("binding")
                    connection.execute(
                        """
                        INSERT INTO principal_bindings(
                            id, principal_kind, provider, app_id,
                            external_subject_hash, local_user_ref,
                            display_name, status, revision, created_at, updated_at
                        ) VALUES (
                            ?, 'miniprogram', ?, ?, ?, ?, ?, 'active', 1, ?, ?
                        )
                        """,
                        (
                            binding_id,
                            provider,
                            str(app_id or ""),
                            subject_hash,
                            str(local_user_ref or ""),
                            clean_text(display_name, max_chars=100),
                            now,
                            now,
                        ),
                    )
                else:
                    binding_id = str(row["id"])
                    if str(row["status"]) in {"revoked", "expired"}:
                        raise DatabaseConflictError("该绑定已失效，请重新完成身份验证")
                    connection.execute(
                        """
                        UPDATE principal_bindings SET
                            local_user_ref=CASE WHEN ?<>'' THEN ? ELSE local_user_ref END,
                            display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            str(local_user_ref or ""),
                            str(local_user_ref or ""),
                            clean_text(display_name, max_chars=100),
                            clean_text(display_name, max_chars=100),
                            now,
                            binding_id,
                        ),
                    )
                result = connection.execute(
                    "SELECT * FROM principal_bindings WHERE id=?",
                    (binding_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _binding_view(dict(result))

    async def revoke_principal_binding(
        self,
        binding_ref: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        return await self._run(
            self._revoke_principal_binding,
            binding_ref,
            expected_revision,
        )

    def _binding_by_public_ref(
        self,
        connection: sqlite3.Connection,
        binding_ref: str,
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            "SELECT * FROM principal_bindings ORDER BY created_at, id"
        ).fetchall()
        return next(
            (
                row
                for row in rows
                if _public_ref("binding", row["id"]) == str(binding_ref)
            ),
            None,
        )

    def _revoke_principal_binding(
        self,
        binding_ref: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._binding_by_public_ref(connection, binding_ref)
                if row is None:
                    raise DatabaseNotFoundError("身份绑定不存在")
                if int(row["revision"]) != int(expected_revision):
                    raise DatabaseConflictError("身份绑定已更新，请刷新后重试")
                now = utc_now()
                connection.execute(
                    """
                    UPDATE principal_bindings SET
                        status='revoked', revision=revision+1,
                        revoked_at=?, updated_at=?
                    WHERE id=? AND revision=?
                    """,
                    (now, now, row["id"], expected_revision),
                )
                result = connection.execute(
                    "SELECT * FROM principal_bindings WHERE id=?", (row["id"],)
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return _binding_view(dict(result))


__all__ = ["PrincipalBindingRepositoryMixin"]
