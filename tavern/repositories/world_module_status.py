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


class WorldModuleStatusRepositoryMixin:
    async def upsert_world_module_status(
        self,
        *,
        session_id: str,
        module_id: str,
        declared: bool,
        definition_state: str,
        runtime_state: str,
        projection_state: str,
        issue_code: str = "",
        issue_message: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._upsert_world_module_status,
            session_id,
            module_id,
            declared,
            definition_state,
            runtime_state,
            projection_state,
            issue_code,
            issue_message,
        )

    def _upsert_world_module_status(
        self,
        session_id: str,
        module_id: str,
        declared: bool,
        definition_state: str,
        runtime_state: str,
        projection_state: str,
        issue_code: str,
        issue_message: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO world_module_runtime_status(
                    session_id, module_id, declared, definition_state,
                    runtime_state, projection_state, issue_code,
                    issue_message, last_success_at, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(session_id,module_id) DO UPDATE SET
                    declared=excluded.declared,
                    definition_state=excluded.definition_state,
                    runtime_state=excluded.runtime_state,
                    projection_state=excluded.projection_state,
                    issue_code=excluded.issue_code,
                    issue_message=excluded.issue_message,
                    last_success_at=excluded.last_success_at,
                    revision=world_module_runtime_status.revision+1,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    str(module_id),
                    1 if declared else 0,
                    definition_state,
                    runtime_state,
                    projection_state,
                    clean_text(issue_code, max_chars=100),
                    clean_text(issue_message, max_chars=300),
                    now if projection_state in {"empty", "ready"} else "",
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM world_module_runtime_status
                WHERE session_id=? AND module_id=?
                """,
                (session_id, str(module_id)),
            ).fetchone()
        return dict(row)


__all__ = ["WorldModuleStatusRepositoryMixin"]
