from .common import *

class RowProjectionMixin:

    @staticmethod
    def _world(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "slug": row["slug"],
            "display_no": int(row["display_no"]),
            "sort_order": int(row["sort_order"]),
            "name": row["name"],
            "description": row["description"],
            "system_prompt": row["system_prompt"],
            "rules": json_load(row["rules_json"], {}),
            "ui_profile": json_load(row["ui_profile_json"], {}),
            "opening_scene": row["opening_scene"],
            "initial_state": json_load(row["initial_state_json"], {}),
            "archived": bool(row["archived"]),
            "revision": row["revision"],
            "source_package_id": row["source_package_id"],
            "package_format": int(row["package_format"]),
            "content_version": row["content_version"],
            "source_kind": row["source_kind"],
            "is_modified": bool(row["is_modified"]),
            "previous_content_version": row["previous_content_version"],
            "migration_status": row["migration_status"],
            "source_artifact_hash": row["source_artifact_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        extensions = json_load(row["extensions_json"], {})
        if isinstance(extensions, Mapping):
            for key, value in extensions.items():
                # Author ui_schema is compile-time input only. Runtime callers
                # consume the closed, resolved ui_profile stored in its own
                # column and must never receive the raw author contract.
                if key != "ui_schema" and key not in result:
                    result[str(key)] = value
        result["internal_world_model_revision"] = int(
            result.get("internal_world_model_revision")
            or result["rules"].get("internal_world_model_revision", 0)
        )
        result["capabilities"] = dict(result["rules"].get("capabilities") or {})
        result["player_limits"] = player_limits(result)
        result["card_template"] = card_template(result)
        result["time_rules"] = world_time_rules(result)
        rules = result["rules"]
        result["choice_mode"] = (
            "strict_abcd"
            if bool(rules.get("strict_choices", True))
            else "free_text"
        )
        result["check_density"] = str(
            rules.get("check_density", "standard")
        )
        return result

    @staticmethod
    def _character(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "world_id": row["world_id"],
            "slug": row["slug"],
            "name": row["name"],
            "role": row["role"],
            "profile": json_load(row["profile_json"], {}),
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "sort_order": row["sort_order"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _session(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        stored_world_state = json_load(row["world_state_json"], {})
        result = {
            "id": row["id"],
            "platform_id": row["platform_id"],
            "group_id": row["group_id"],
            "unified_origin": row["unified_origin"],
            "instance_slug": (
                row["instance_slug"]
                if "instance_slug" in keys
                else row["world_slug"]
            ),
            "instance_name": (
                row["instance_name"]
                if "instance_name" in keys
                else row["world_name"]
            ),
            "selected": bool(row["selected"]) if "selected" in keys else True,
            "world_id": row["world_id"],
            "state": row["state"],
            "turn_no": row["turn_no"],
            "revision": row["revision"],
            "input_locked": (
                bool(row["input_locked"])
                if "input_locked" in keys
                else False
            ),
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "history_floor_seq": row["history_floor_seq"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if "world_name" in keys:
            result["world_name"] = row["world_name"]
        if "world_slug" in keys:
            result["world_slug"] = row["world_slug"]
        if "world_description" in keys:
            result["world_description"] = str(
                row["world_description"] or ""
            )
        if "group_remark" in keys:
            result["group_remark"] = str(row["group_remark"] or "")
        if "group_revision" in keys:
            result["group_revision"] = int(row["group_revision"] or 1)
        if "storage_relative_path" in keys:
            result["storage_relative_path"] = str(
                row["storage_relative_path"] or ""
            )
        if "storage_sync_status" in keys:
            result["storage_sync_status"] = str(
                row["storage_sync_status"] or "pending"
            )
        if "storage_last_error" in keys:
            result["storage_last_error"] = str(
                row["storage_last_error"] or ""
            )
        if "playthrough_no" in keys:
            result["playthrough_no"] = int(row["playthrough_no"] or 1)
        if "player_count" in keys:
            result["player_count"] = row["player_count"]
        for key in (
            "ready_count",
            "memory_count",
            "snapshot_count",
            "npc_count",
            "active_timer_count",
        ):
            if key in keys:
                result[key] = int(row[key] or 0)
        if "progress_json" in keys:
            result["progress"] = normalize_progress(
                json_load(row["progress_json"], {})
            )
        if "recovery_json" in keys:
            result["recovery"] = json_load(row["recovery_json"], {})
        if "termination_type" in keys:
            result["archive"] = (
                {
                    "termination_type": row["termination_type"],
                    "reason": row["archive_reason"],
                    "final_snapshot_id": row["final_snapshot_id"],
                    "ended_by": row["ended_by"],
                    "ended_at": row["ended_at"],
                    "readonly": bool(row["readonly"]),
                }
                if row["termination_type"]
                else None
            )
            result["readonly"] = bool(row["readonly"])
        if "protocol_archive_schema" in keys and row["protocol_archive_schema"]:
            receipt = json_load(row["protocol_archive_result_json"], {})
            receipt = dict(receipt) if isinstance(receipt, Mapping) else {}
            result["protocol_archive"] = {
                "status": str(receipt.get("status") or "readonly"),
                "reason": str(receipt.get("reason") or "旧世界协议副本只读保留"),
                "automatic_action": str(receipt.get("automatic_action") or "系统已阻止后续写入"),
                "next_step": str(receipt.get("next_step") or "在 RC10 世界中新建副本"),
                "source_database_schema": int(row["protocol_source_database_schema"] or 0),
                "source_world_schema": int(row["protocol_source_world_schema"] or 0),
                "source_protocol": str(row["protocol_source_protocol"] or ""),
            }
            result["readonly"] = True
        if "waiting_for" in keys:
            result["waiting_for"] = str(row["waiting_for"] or "")
        if "active_deadline_at" in keys:
            result["active_deadline_at"] = str(
                row["active_deadline_at"] or ""
            )
        progress = result.get("progress")
        if isinstance(progress, Mapping):
            total = int(progress.get("total_milestones") or 0)
            completed = int(progress.get("completed_milestones") or 0)
            result["progress_percent"] = (
                round(completed * 100 / total) if total > 0 else None
            )
        return result

    @staticmethod
    def _player(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "user_id": row["user_id"],
            "display_name": row["display_name"],
            "character_name": row["character_name"],
            "profile": json_load(row["profile_json"], {}),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "id": row["id"],
            "session_id": row["session_id"],
            "turn_no": row["turn_no"],
            "role": row["role"],
            "actor_id": row["actor_id"],
            "actor_name": row["actor_name"],
            "content": row["content"],
            "meta": json_load(row["meta_json"], {}),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _memory(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "scope": row["scope"],
            "scope_id": row["scope_id"],
            "kind": row["kind"],
            "content": row["content"],
            "importance": row["importance"],
            "salience": row["salience"],
            "tags": json_load(row["tags_json"], []),
            "source_event_id": row["source_event_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_accessed_at": row["last_accessed_at"],
            "visibility": (
                row["governance_visibility"]
                if "governance_visibility" in keys
                and row["governance_visibility"]
                else "public"
            ),
            "locked": bool(
                row["governance_locked"]
                if "governance_locked" in keys else 0
            ),
            "pinned": bool(
                row["governance_pinned"]
                if "governance_pinned" in keys else 0
            ),
            "invalidated": bool(
                row["governance_invalidated"]
                if "governance_invalidated" in keys else 0
            ),
            "supersedes_id": (
                row["governance_supersedes_id"]
                if "governance_supersedes_id" in keys else ""
            ),
            "conflict_status": (
                row["governance_conflict_status"]
                if "governance_conflict_status" in keys
                and row["governance_conflict_status"]
                else "clear"
            ),
            "governance_note": (
                row["governance_note"]
                if "governance_note" in keys else ""
            ),
        }

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        stored_world_state = json_load(row["world_state_json"], {})
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "name": row["name"],
            "kind": row["kind"],
            "turn_no": row["turn_no"],
            "session_revision": row["session_revision"],
            "world_id": row["world_id"],
            "world_state": public_world_state(stored_world_state),
            "turn_state": turn_state_from_world(stored_world_state),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _session_character(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "name": row["name"],
            "aliases": json_load(row["aliases_json"], []),
            "role_type": row["role_type"],
            "public_profile": json_load(row["public_profile_json"], {}),
            "known_facts": json_load(row["known_facts_json"], []),
            "misconceptions": json_load(row["misconceptions_json"], []),
            "source": row["source"],
            "review_status": row["review_status"],
            "lifecycle_status": row["lifecycle_status"],
            "persistent": bool(row["persistent"]),
            "first_event_id": row["first_event_id"],
            "last_event_id": row["last_event_id"],
            "first_turn": row["first_turn"],
            "last_turn": row["last_turn"],
            "revision": row["revision"],
            "state": (
                json_load(row["state_json"], {})
                if "state_json" in keys
                else {}
            ),
            "state_revision": (
                int(row["state_revision"] or 0)
                if "state_revision" in keys
                else 0
            ),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _ledger_entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "kind": row["kind"],
            "title": row["title"],
            "description": row["description"],
            "status": row["status"],
            "visibility": row["visibility"],
            "source_event_id": row["source_event_id"],
            "completed_event_id": row["completed_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _scene_clock(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "stable_key": row["stable_key"],
            "title": row["title"],
            "segments": row["segments"],
            "current_value": row["current_value"],
            "visibility": row["visibility"],
            "trigger_text": row["trigger_text"],
            "status": row["status"],
            "triggered_event_id": row["triggered_event_id"],
            "revision": row["revision"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _assert_session_writable(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> None:
        maintenance = connection.execute(
            """
            SELECT value FROM tavern_meta
            WHERE key='maintenance_mode'
            """
        ).fetchone()
        if maintenance is not None and str(maintenance["value"] or "") == "1":
            raise InvalidTransitionError(
                "系统正在执行安全维护，暂时拒绝新的写入操作"
            )
        row = connection.execute(
            """
            SELECT s.state, sa.readonly,
                   COALESCE(par.readonly, 0) AS protocol_readonly
            FROM sessions s
            LEFT JOIN session_archives sa ON sa.session_id = s.id
            LEFT JOIN protocol_archive_receipts par
              ON par.target_kind='session' AND par.target_id=s.id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            raise DatabaseNotFoundError("会话不存在")
        if bool(row["protocol_readonly"]):
            raise InvalidTransitionError(
                "该副本来自旧世界协议，系统已完成可校验备份并保持只读；"
                "请导出查看，或在 RC10 世界中新建副本和角色"
            )
        if row["state"] == SESSION_FINISHED or bool(row["readonly"]):
            raise InvalidTransitionError(
                "该副本已永久归档并处于只读状态"
            )
