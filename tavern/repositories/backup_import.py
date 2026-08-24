from __future__ import annotations

import json
from collections.abc import Mapping

from ..database_support import *
from ..constants import PLUGIN_VERSION
from ..contracts.narrative_document import (
    NARRATIVE_DOCUMENT_SCHEMA_ID,
    canonical_narrative_json,
    legacy_text_fallback,
    narrative_document_to_plain_text,
    narrative_text_sha256,
    parse_narrative_document,
)
from ..generation_reminders import (
    ACTIVE_REMINDER_OPERATION_STATUSES,
    TERMINAL_REMINDER_OPERATION_STATUSES,
    GenerationReminderConfig,
    parse_utc,
)
from .configuration_revision_security import (
    safe_configuration_revision_row,
    sanitize_configuration_revisions,
)
from ..recovery_ranges import RecoveryStateError, parse_recovery_json
from .generation_reminders import STORY_GENERATION_OPERATION_TYPES
from .backup_actor_fate import (
    ACTOR_FATE_BACKUP_COLUMNS,
    ACTOR_FATE_BACKUP_QUERIES,
    ACTOR_FATE_IMPORT_ORDER,
    ACTOR_FATE_REPLACE_DELETE_ORDER,
    validate_actor_fate_backup_rows,
    validate_actor_fate_merge_conflicts,
)


def _validate_story_document_against_event(
    document_row: Mapping[str, Any],
    event_row: Mapping[str, Any],
    *,
    source: str,
) -> None:
    if (
        str(event_row.get("session_id") or "")
        != str(document_row.get("session_id") or "")
        or str(event_row.get("role") or "") != "narrator"
        or int(event_row.get("turn_no") or 0)
        != int(document_row.get("turn_no") or 0)
    ):
        raise ValueError(f"{source}故事结构与事件归属不一致")
    if document_row.get("schema") != NARRATIVE_DOCUMENT_SCHEMA_ID:
        raise ValueError(f"{source}故事结构使用了不支持的 schema")
    document_json = document_row.get("document_json")
    if not isinstance(document_json, str):
        raise ValueError(f"{source}故事结构正文不是 JSON 文本")
    try:
        payload = json.loads(document_json)
        if not isinstance(payload, Mapping):
            raise ValueError("NarrativeDocument 必须为对象")
        document = parse_narrative_document(
            payload,
            dialogue_expected=False,
        )
    except Exception as exc:
        raise ValueError(
            f"{source}故事结构未通过 NarrativeDocument 校验"
        ) from exc
    canonical = canonical_narrative_json(document)
    plain_text = narrative_document_to_plain_text(document)
    if document_json != canonical:
        raise ValueError(f"{source}故事结构不是规范 JSON")
    if str(document_row.get("plain_text") or "") != plain_text:
        raise ValueError(f"{source}故事结构与确定性正文不一致")
    if (
        str(document_row.get("text_sha256") or "")
        != narrative_text_sha256(document)
    ):
        raise ValueError(f"{source}故事结构正文哈希不一致")
    if str(event_row.get("content") or "") != plain_text:
        raise ValueError(f"{source}故事结构与叙事事件正文不一致")


def _validate_story_document_backup_rows(
    data: Mapping[str, Any],
) -> None:
    events: dict[str, Mapping[str, Any]] = {}
    for raw in data.get("events", ()):
        if not isinstance(raw, Mapping):
            raise ValueError("备份事件表含非法记录")
        event_id = str(raw.get("id") or "")
        if not event_id or event_id in events:
            raise ValueError("备份事件表含缺失或重复的事件标识")
        events[event_id] = raw

    session_ids = {
        str(raw.get("id") or "")
        for raw in data.get("sessions", ())
        if isinstance(raw, Mapping) and str(raw.get("id") or "")
    }
    seen: set[str] = set()
    required = {
        "event_id", "session_id", "turn_no", "schema",
        "document_json", "plain_text", "text_sha256", "created_at",
    }
    for raw in data.get("story_documents", ()):
        if not isinstance(raw, Mapping):
            raise ValueError("备份故事结构表含非法记录")
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"备份故事结构缺少字段 {missing[0]}")
        event_id = str(raw.get("event_id") or "")
        session_id = str(raw.get("session_id") or "")
        if not event_id or event_id in seen:
            raise ValueError("备份故事结构含缺失或重复的事件标识")
        seen.add(event_id)
        if session_id not in session_ids:
            raise ValueError("备份故事结构引用了不存在的副本")
        event = events.get(event_id)
        if event is None:
            raise ValueError("备份故事结构引用了不存在的事件")
        _validate_story_document_against_event(
            raw,
            event,
            source="备份",
        )
    for event_id, event in events.items():
        if str(event.get("role") or "") != "narrator" or event_id in seen:
            continue
        meta = json_load(event.get("meta_json"), {})
        if isinstance(meta, Mapping) and meta.get("legacy_record") is True:
            try:
                legacy_text_fallback(
                    event.get("content"),
                    legacy_record=True,
                )
            except Exception as exc:
                raise ValueError("备份旧故事记录未通过安全校验") from exc
            continue
        raise ValueError("备份叙事事件缺少结构化故事正文")


def _validate_recovery_backup_rows(data: Mapping[str, Any]) -> None:
    for raw in data.get("session_rule_states", ()):
        if not isinstance(raw, Mapping):
            raise ValueError("备份副本规则状态含非法记录")
        try:
            parse_recovery_json(raw.get("recovery_json"))
        except RecoveryStateError as exc:
            raise ValueError("备份恢复状态无效") from exc


def _validate_generation_reminder_backup_rows(
    data: Mapping[str, Any],
) -> None:
    required = {
        "status", "request_json", "reminder_acknowledged", "reminder_enabled",
        "reminder_interval_seconds", "reminder_sequence",
        "reminder_config_revision", "reminder_source_revision",
        "reminder_last_at", "reminder_next_at",
    }
    for raw in data.get("operation_receipts", ()):
        if not isinstance(raw, Mapping):
            raise ValueError("备份操作回执表含非法记录")
        if "operation_type" not in raw:
            raise ValueError("备份操作回执缺少字段 operation_type")
        operation_type = str(raw.get("operation_type") or "")
        if operation_type not in STORY_GENERATION_OPERATION_TYPES:
            continue
        missing = sorted(required - set(raw))
        if missing:
            raise ValueError(f"备份操作回执缺少字段 {missing[0]}")
        status = str(raw.get("status") or "")
        if status not in (
            ACTIVE_REMINDER_OPERATION_STATUSES
            | TERMINAL_REMINDER_OPERATION_STATUSES
        ):
            raise ValueError("备份故事操作状态无效")
        acknowledged = raw.get("reminder_acknowledged")
        enabled = raw.get("reminder_enabled")
        sequence = raw.get("reminder_sequence")
        if (
            type(acknowledged) is not int
            or acknowledged not in {0, 1}
            or type(enabled) is not int
            or enabled not in {0, 1}
            or type(sequence) is not int
            or sequence < 0
        ):
            raise ValueError("备份故事提醒状态无效")
        interval = raw.get("reminder_interval_seconds")
        revisions = (
            raw.get("reminder_config_revision"),
            raw.get("reminder_source_revision"),
        )
        if (
            type(interval) is not int
            or interval < 30
            or interval > 600
            or interval % 15
            or any(type(value) is not int or value < 0 for value in revisions)
        ):
            raise ValueError("备份故事提醒配置无效")
        for field in ("reminder_last_at", "reminder_next_at"):
            timestamp = raw.get(field)
            if timestamp in (None, ""):
                continue
            try:
                parse_utc(str(timestamp))
            except Exception as exc:
                raise ValueError("备份故事提醒时间无效") from exc
        request_json = raw.get("request_json")
        if not isinstance(request_json, str):
            raise ValueError("备份故事操作请求快照无效")
        try:
            request = json.loads(request_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("备份故事操作请求快照无效") from exc
        if not isinstance(request, Mapping):
            raise ValueError("备份故事操作请求快照无效")
        if "reminder_config" not in request:
            unarmed = (
                status == "reserved"
                and acknowledged == 0
                and enabled == 1
                and interval == 60
                and sequence == 0
                and revisions == (0, 0)
                and raw.get("reminder_last_at") == ""
                and raw.get("reminder_next_at") == ""
            )
            if not unarmed:
                raise ValueError("未冻结的故事提醒含非默认持久化状态")
            continue
        snapshot = request.get("reminder_config")
        if not isinstance(snapshot, Mapping):
            raise ValueError("备份故事提醒快照无效")
        try:
            config = GenerationReminderConfig.from_mapping(snapshot)
        except Exception as exc:
            raise ValueError("备份故事提醒快照无效") from exc
        safely_stopped = (
            enabled == 0
            and str(raw.get("reminder_next_at") or "") == ""
            and str(raw.get("last_error_code") or "")
            == "generation.reminder_state_invalid"
        )
        if (
            (int(config.enabled) != enabled and not safely_stopped)
            or config.interval_seconds != interval
            or config.revision != revisions[0]
            or config.source_revision != revisions[1]
        ):
            raise ValueError("备份故事提醒快照与持久化列不一致")
        next_at = str(raw.get("reminder_next_at") or "")
        if safely_stopped:
            continue
        if not config.enabled and next_at:
            raise ValueError("已关闭的故事提醒仍含下次调度时间")
        if status in TERMINAL_REMINDER_OPERATION_STATUSES and next_at:
            raise ValueError("已终止的故事操作仍含下次提醒时间")
        if (
            config.enabled
            and status in ACTIVE_REMINDER_OPERATION_STATUSES
            and not next_at
        ):
            raise ValueError("活动故事操作缺少下次提醒时间")


class BackupImportRepositoryMixin:
    async def cleanup(self, audit_retention_days: int) -> dict[str, int]:
        return await self._run(self._cleanup, audit_retention_days)

    def _cleanup(self, audit_retention_days: int) -> dict[str, int]:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=max(1, audit_retention_days))
        ).isoformat(timespec="seconds")
        with self._connect() as connection:
            audit_cursor = connection.execute(
                "DELETE FROM audit_logs WHERE created_at < ?",
                (cutoff,),
            )
            now = utc_now()
            code_cursor = connection.execute(
                """
                UPDATE card_binding_codes SET status = 'expired'
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now,),
            )
            draft_cursor = connection.execute(
                """
                UPDATE character_card_drafts SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            delegation_cursor = connection.execute(
                """
                UPDATE delegation_grants SET
                    status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            ban_cursor = connection.execute(
                """
                UPDATE ban_records SET status = 'expired', updated_at = ?
                WHERE status = 'active' AND expires_at <> ''
                  AND expires_at <= ?
                """,
                (now, now),
            )
            card_receipt_cursor = connection.execute(
                """
                DELETE FROM card_review_receipts
                WHERE created_at < ?
                  AND session_id IN (
                      SELECT id FROM sessions
                      WHERE state = 'finished' AND updated_at < ?
                  )
                """,
                (cutoff, cutoff),
            )
            supplement_receipt_cursor = connection.execute(
                """
                DELETE FROM supplement_action_receipts
                WHERE created_at < ?
                  AND session_id IN (
                      SELECT id FROM sessions
                      WHERE state = 'finished' AND updated_at < ?
                  )
                """,
                (cutoff, cutoff),
            )
            return {
                "audit_logs": audit_cursor.rowcount,
                "card_codes": code_cursor.rowcount,
                "card_drafts": draft_cursor.rowcount,
                "delegations": delegation_cursor.rowcount,
                "bans": ban_cursor.rowcount,
                "card_review_receipts": card_receipt_cursor.rowcount,
                "supplement_action_receipts": (
                    supplement_receipt_cursor.rowcount
                ),
            }

    async def export_bundle(self) -> dict[str, Any]:
        return await self._run(self._export_bundle)

    def _export_bundle(self) -> dict[str, Any]:
        with self._connect() as connection:
            tables = {
                "worlds": "SELECT * FROM worlds ORDER BY created_at",
                "characters": "SELECT * FROM characters ORDER BY created_at",
                "sessions": "SELECT * FROM sessions ORDER BY created_at",
                "players": "SELECT * FROM players ORDER BY created_at",
                "events": "SELECT * FROM events ORDER BY seq",
                "memories": "SELECT * FROM memories ORDER BY created_at",
                "snapshots": "SELECT * FROM snapshots ORDER BY created_at",
                "audit_logs": "SELECT * FROM audit_logs ORDER BY id",
                "instance_configs": (
                    "SELECT * FROM instance_configs ORDER BY created_at"
                ),
                "character_cards": (
                    "SELECT * FROM character_cards ORDER BY created_at"
                ),
                "character_card_versions": (
                    "SELECT * FROM character_card_versions ORDER BY created_at"
                ),
                "card_revision_requests": (
                    "SELECT * FROM card_revision_requests ORDER BY created_at"
                ),
                "card_review_receipts": (
                    "SELECT * FROM card_review_receipts ORDER BY created_at"
                ),
                "supplement_action_receipts": (
                    "SELECT * FROM supplement_action_receipts ORDER BY created_at"
                ),
                "participants": (
                    "SELECT * FROM participants ORDER BY created_at"
                ),
                "character_runtime_states": (
                    "SELECT * FROM character_runtime_states ORDER BY created_at"
                ),
                "character_card_drafts": (
                    "SELECT * FROM character_card_drafts ORDER BY created_at"
                ),
                "card_binding_codes": (
                    "SELECT * FROM card_binding_codes ORDER BY created_at"
                ),
                "choice_sets": (
                    "SELECT * FROM choice_sets ORDER BY created_at"
                ),
                "rolls": "SELECT * FROM rolls ORDER BY created_at",
                "group_votes": (
                    "SELECT * FROM group_votes ORDER BY created_at"
                ),
                "vote_ballots": (
                    "SELECT * FROM vote_ballots ORDER BY created_at"
                ),
                "selected_world_events": (
                    "SELECT * FROM selected_world_events ORDER BY created_at"
                ),
                "timer_instances": (
                    "SELECT * FROM timer_instances ORDER BY created_at"
                ),
                "delegation_grants": (
                    "SELECT * FROM delegation_grants ORDER BY created_at"
                ),
                "permission_grants": (
                    "SELECT * FROM permission_grants ORDER BY created_at"
                ),
                "ban_records": (
                    "SELECT * FROM ban_records ORDER BY created_at"
                ),
                "return_requests": (
                    "SELECT * FROM return_requests ORDER BY created_at"
                ),
                "snapshot_workflows": (
                    "SELECT * FROM snapshot_workflows ORDER BY snapshot_id"
                ),
                "session_archives": (
                    "SELECT * FROM session_archives ORDER BY ended_at"
                ),
                "session_rule_states": (
                    "SELECT * FROM session_rule_states ORDER BY created_at"
                ),
                "dm_control_states": (
                    "SELECT * FROM dm_control_states ORDER BY created_at"
                ),
                "session_characters": (
                    "SELECT * FROM session_characters ORDER BY created_at"
                ),
                "session_character_states": (
                    "SELECT * FROM session_character_states ORDER BY updated_at"
                ),
                "story_ledger": (
                    "SELECT * FROM story_ledger ORDER BY created_at"
                ),
                "scene_clocks": (
                    "SELECT * FROM scene_clocks ORDER BY created_at"
                ),
                "memory_governance": (
                    "SELECT * FROM memory_governance ORDER BY updated_at"
                ),
                "assist_tokens": (
                    "SELECT * FROM assist_tokens ORDER BY created_at"
                ),
                "roll_revisions": (
                    "SELECT * FROM roll_revisions ORDER BY created_at"
                ),
                "inspiration_transactions": (
                    "SELECT * FROM inspiration_transactions ORDER BY created_at"
                ),
                "provider_health": (
                    "SELECT * FROM provider_health ORDER BY updated_at"
                ),
                "configuration_revisions": (
                    "SELECT * FROM configuration_revisions ORDER BY id"
                ),
                "operation_receipts": (
                    "SELECT * FROM operation_receipts ORDER BY created_at"
                ),
                "story_documents": (
                    "SELECT * FROM story_documents ORDER BY session_id, turn_no, created_at"
                ),
                "world_feature_versions": (
                    "SELECT * FROM world_feature_versions ORDER BY world_id, world_revision, feature_name"
                ),
                "world_entity_registry": (
                    "SELECT * FROM world_entity_registry ORDER BY world_id, world_revision, entity_ref"
                ),
                "world_rule_revisions": (
                    "SELECT * FROM world_rule_revisions ORDER BY created_at"
                ),
                "world_snapshots": (
                    "SELECT * FROM world_snapshots ORDER BY created_at"
                ),
                "actor_capability_instances": (
                    "SELECT * FROM actor_capability_instances ORDER BY created_at"
                ),
                "runtime_effect_instances": (
                    "SELECT * FROM runtime_effect_instances ORDER BY created_at"
                ),
                "operation_commits": (
                    "SELECT * FROM operation_commits ORDER BY created_at"
                ),
                "resolution_receipts": (
                    "SELECT * FROM resolution_receipts ORDER BY created_at"
                ),
                "migration_receipts": (
                    "SELECT * FROM migration_receipts ORDER BY created_at"
                ),
                "group_registry": (
                    "SELECT * FROM group_registry ORDER BY created_at"
                ),
                "story_storage": (
                    "SELECT * FROM story_storage ORDER BY created_at"
                ),
                "timer_policies": (
                    "SELECT * FROM timer_policies ORDER BY updated_at"
                ),
                "token_usage": (
                    "SELECT * FROM token_usage ORDER BY created_at"
                ),
                "token_quota_policies": (
                    "SELECT * FROM token_quota_policies ORDER BY updated_at"
                ),
            }
            tables.update(ACTOR_FATE_BACKUP_QUERIES)
            data: dict[str, list[dict[str, Any]]] = {}
            for name, query in tables.items():
                rows = connection.execute(query).fetchall()
                if name == "configuration_revisions":
                    data[name] = [
                        safe_configuration_revision_row(row) for row in rows
                    ]
                else:
                    data[name] = [dict(row) for row in rows]
            return {
                "format": "astrbot-tavern-backup",
                # Legacy JSON projection remains round-trippable through
                # validate_bundle()/import_bundle().  The physical ZIP writer
                # upgrades its own manifest to format 2 after it has copied
                # and verified the complete SQLite catalog.
                "format_version": 1,
                "plugin_version": PLUGIN_VERSION,
                "schema_version": DATABASE_SCHEMA_VERSION,
                "created_at": utc_now(),
                "data_scope": "readable_legacy_projection",
                "data": data,
            }

    @staticmethod
    def validate_bundle(bundle: Mapping[str, Any]) -> None:
        if bundle.get("format") != "astrbot-tavern-backup":
            raise ValueError("不是有效的 321开团备份")
        format_version = int(bundle.get("format_version", 0))
        if format_version not in {1, 2}:
            raise ValueError("不支持的备份格式版本")
        if format_version == 2:
            raise ValueError(
                "格式 2 必须通过完整 ZIP 的 staging 恢复，"
                "不能使用逐表 JSON 导入"
            )
        try:
            schema_version = int(bundle.get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("备份数据库版本无效") from exc
        if schema_version != DATABASE_SCHEMA_VERSION:
            raise ValueError(
                f"v{PLUGIN_VERSION} 只接受 Schema {DATABASE_SCHEMA_VERSION} 备份；"
                f"当前为 Schema {schema_version}，请使用对应旧插件恢复旧备份"
            )
        data = bundle.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("备份缺少 data")
        required = {
            "worlds",
            "characters",
            "sessions",
            "players",
            "events",
            "memories",
            "snapshots",
            "audit_logs",
            "instance_configs",
            "character_cards",
            "character_card_versions",
            "card_revision_requests",
            "card_review_receipts",
            "supplement_action_receipts",
            "participants",
            "character_runtime_states",
            "character_card_drafts",
            "card_binding_codes",
            "choice_sets",
            "rolls",
            "group_votes",
            "vote_ballots",
            "selected_world_events",
            "timer_instances",
            "delegation_grants",
            "permission_grants",
            "ban_records",
            "return_requests",
            "snapshot_workflows",
            "session_archives",
            "session_rule_states",
            "dm_control_states",
            "session_characters",
            "session_character_states",
            "story_ledger",
            "scene_clocks",
            "memory_governance",
            "assist_tokens",
            "roll_revisions",
            "inspiration_transactions",
            "provider_health",
            "configuration_revisions",
            "operation_receipts",
            "story_documents",
            "group_registry",
            "story_storage",
            "timer_policies",
            "token_usage",
            "token_quota_policies",
        }
        if not required.issubset(data.keys()):
            raise ValueError("备份数据表不完整")
        for table in required:
            if not isinstance(data[table], list):
                raise ValueError(f"备份表 {table} 格式错误")
            if len(data[table]) > 1_000_000:
                raise ValueError(f"备份表 {table} 记录数异常")
        required_runtime = {
            "world_feature_versions", "world_entity_registry",
            "world_rule_revisions", "world_snapshots",
            "actor_capability_instances", "runtime_effect_instances",
            "operation_commits", "resolution_receipts", "migration_receipts",
            "story_documents", *ACTOR_FATE_IMPORT_ORDER,
        }
        if not required_runtime.issubset(data.keys()):
            raise ValueError("备份缺少规则、运行态与回执数据表")
        _validate_story_document_backup_rows(data)
        _validate_recovery_backup_rows(data)
        _validate_generation_reminder_backup_rows(data)
        validate_actor_fate_backup_rows(data)

    async def import_bundle(
        self,
        bundle: Mapping[str, Any],
        mode: str,
        actor_id: str,
    ) -> dict[str, int]:
        return await self._run(
            self._import_bundle,
            dict(bundle),
            mode,
            actor_id,
        )

    def _import_bundle(self, bundle: dict[str, Any], mode: str, actor_id: str) -> dict[str, int]:
        self.validate_bundle(bundle)
        if mode not in {'merge', 'replace'}:
            raise ValueError('导入模式必须为 merge 或 replace')
        data = {table: [dict(row) for row in rows] for table, rows in bundle['data'].items()}
        for table in (
            'world_feature_versions', 'world_entity_registry', 'world_rule_revisions',
            'world_snapshots', 'actor_capability_instances', 'runtime_effect_instances',
            'operation_commits', 'resolution_receipts', 'migration_receipts',
            'story_documents',
            *ACTOR_FATE_IMPORT_ORDER,
        ):
            data.setdefault(table, [])
        policy_tables = ('timer_policies', 'token_usage', 'token_quota_policies')
        counts: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if mode == 'merge':
                    sanitize_configuration_revisions(
                        connection,
                        abandon_reserved=True,
                    )
                    self._validate_merge_conflicts(connection, data)
                if mode == 'replace':
                    connection.execute('DELETE FROM dm_control_states')
                    for table in ACTOR_FATE_REPLACE_DELETE_ORDER:
                        connection.execute(f'DELETE FROM {table}')
                    for table in ('migration_receipts', 'resolution_receipts', 'operation_commits', 'runtime_effect_instances', 'actor_capability_instances', 'world_entity_registry', 'world_feature_versions', 'world_snapshots', 'world_rule_revisions', 'story_documents', 'audit_logs', 'token_usage', 'token_quota_policies', 'timer_policies', 'story_storage', 'group_registry', 'operation_receipts', 'card_review_receipts', 'supplement_action_receipts', 'card_revision_requests', 'configuration_revisions', 'provider_health', 'session_archives', 'inspiration_transactions', 'roll_revisions', 'assist_tokens', 'memory_governance', 'session_character_states', 'scene_clocks', 'story_ledger', 'session_characters', 'session_rule_states', 'snapshot_workflows', 'return_requests', 'ban_records', 'permission_grants', 'delegation_grants', 'timer_instances', 'selected_world_events', 'vote_ballots', 'group_votes', 'rolls', 'choice_sets', 'card_binding_codes', 'character_card_drafts', 'character_runtime_states', 'participants', 'character_card_versions', 'character_cards', 'instance_configs', 'snapshots', 'memories', 'events', 'players', 'sessions', 'characters', 'worlds'):
                        connection.execute(f'DELETE FROM {table}')
                used_numbers = {
                    int(row[0]) for row in connection.execute(
                        'SELECT display_no FROM worlds WHERE display_no IS NOT NULL'
                    ).fetchall()
                }
                next_number = max(used_numbers, default=0) + 1
                ordered_worlds = sorted(
                    data['worlds'], key=lambda item: (str(item.get('created_at') or ''), str(item.get('id') or ''))
                )
                for world_row in ordered_worlds:
                    desired = int(world_row.get('display_no') or 0)
                    existing_same = connection.execute(
                        'SELECT display_no FROM worlds WHERE id=?', (world_row.get('id'),)
                    ).fetchone()
                    if existing_same:
                        desired = int(existing_same[0])
                    elif desired <= 0 or (mode == 'merge' and desired in used_numbers):
                        while next_number in used_numbers:
                            next_number += 1
                        desired = next_number
                        next_number += 1
                    used_numbers.add(desired)
                    world_row['display_no'] = desired
                    world_row['sort_order'] = int(world_row.get('sort_order') or desired)
                self._import_rows(connection, 'worlds', data['worlds'], ('id', 'slug', 'display_no', 'sort_order', 'name', 'description', 'system_prompt', 'rules_json', 'extensions_json', 'ui_profile_json', 'opening_scene', 'initial_state_json', 'archived', 'revision', 'source_package_id', 'package_format', 'content_version', 'source_kind', 'is_modified', 'previous_content_version', 'migration_status', 'source_artifact_hash', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'characters', data['characters'], ('id', 'world_id', 'slug', 'name', 'role', 'profile_json', 'prompt', 'enabled', 'sort_order', 'revision', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'sessions', data['sessions'], ('id', 'platform_id', 'group_id', 'unified_origin', 'instance_slug', 'instance_name', 'selected', 'world_id', 'state', 'turn_no', 'revision', 'world_state_json', 'history_floor_seq', 'input_locked', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'players', data['players'], ('id', 'session_id', 'user_id', 'display_name', 'character_name', 'profile_json', 'enabled', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'events', data['events'], ('seq', 'id', 'session_id', 'turn_no', 'role', 'actor_id', 'actor_name', 'content', 'meta_json', 'created_at'), merge=mode == 'merge')
                self._import_rows(connection, 'memories', data['memories'], ('id', 'session_id', 'scope', 'scope_id', 'kind', 'content', 'importance', 'salience', 'tags_json', 'fingerprint', 'source_event_id', 'created_at', 'updated_at', 'last_accessed_at'), merge=mode == 'merge')
                self._import_rows(connection, 'snapshots', data['snapshots'], ('id', 'session_id', 'name', 'kind', 'turn_no', 'session_revision', 'world_id', 'world_state_json', 'created_by', 'created_at'), merge=mode == 'merge')
                runtime_columns: dict[str, tuple[str, ...]] = {'instance_configs': ('session_id', 'world_revision', 'world_snapshot_json', 'time_rules_json', 'phase_meta_json', 'created_at', 'updated_at'), 'character_cards': ('id', 'owner_user_id', 'world_id', 'display_name', 'archived', 'deleted', 'current_version', 'created_at', 'updated_at'), 'character_card_versions': ('id', 'character_card_id', 'version_no', 'template_version', 'profile_json', 'stats_json', 'status', 'review_note', 'reviewed_by', 'created_at'), 'participants': ('id', 'session_id', 'player_id', 'group_user_id', 'private_user_id', 'private_origin', 'display_name', 'character_card_id', 'character_version_id', 'character_name', 'character_code', 'aliases_json', 'card_status', 'ready', 'participation_status', 'seat_reserved_at', 'joined_round', 'consecutive_timeouts', 'action_locked', 'exit_reason', 'created_at', 'updated_at'), 'character_runtime_states': ('id', 'session_id', 'participant_id', 'character_card_id', 'state_json', 'revision', 'created_at', 'updated_at'), 'character_card_drafts': ('id', 'participant_id', 'generation', 'template_version', 'template_revision', 'world_revision', 'fields_json', 'current_step', 'status', 'cancel_reason', 'superseded_by', 'expires_at', 'created_at', 'updated_at'), 'card_binding_codes': ('id', 'participant_id', 'code', 'status', 'expires_at', 'private_user_id', 'private_origin', 'replaced_by', 'failure_reason', 'created_at', 'used_at'), 'choice_sets': ('id', 'session_id', 'participant_id', 'round_no', 'session_revision', 'choices_json', 'status', 'reroll_count', 'selected_key', 'flavor_text', 'idempotency_key', 'created_at', 'updated_at'), 'rolls': ('id', 'session_id', 'choice_set_id', 'participant_id', 'roll_json', 'created_at'), 'group_votes': ('id', 'session_id', 'source_event_id', 'question', 'options_json', 'eligible_user_ids_json', 'stage', 'status', 'winner_key', 'suspended_user_id', 'deadline_at', 'result_json', 'created_at', 'updated_at'), 'vote_ballots': ('id', 'vote_id', 'user_id', 'option_key', 'created_at', 'updated_at'), 'selected_world_events': ('id', 'session_id', 'round_no', 'pool_item_id', 'payload_json', 'status', 'narrative', 'created_at', 'resolved_at'), 'timer_instances': ('id', 'session_id', 'participant_id', 'timer_type', 'status', 'deadline_at', 'remaining_seconds', 'reminder_at', 'reminder_sent', 'action_json', 'created_at', 'updated_at'), 'delegation_grants': ('id', 'session_id', 'participant_id', 'owner_user_id', 'delegate_user_id', 'permissions_json', 'status', 'expires_at', 'expiry_kind', 'expires_round', 'auto_restore', 'source', 'granted_by', 'created_at', 'updated_at'), 'permission_grants': ('id', 'session_id', 'user_id', 'role', 'granted_by', 'created_at'), 'ban_records': ('id', 'session_id', 'platform_id', 'group_id', 'user_id', 'participant_id', 'scope', 'reason', 'actor_id', 'status', 'expires_at', 'created_at', 'updated_at'), 'return_requests': ('id', 'session_id', 'participant_id', 'requested_by', 'status', 'exit_type', 'objective', 'progress_json', 'vote_id', 'created_at', 'updated_at'), 'snapshot_workflows': ('snapshot_id', 'workflow_json')}
                runtime_columns['instance_configs'] = (
                    'session_id', 'world_revision', 'world_snapshot_json',
                    'ui_profile_json', 'time_rules_json', 'phase_meta_json',
                    'created_at', 'updated_at',
                )
                for table in ('instance_configs', 'character_cards', 'character_card_versions', 'participants', 'character_runtime_states', 'character_card_drafts', 'card_binding_codes', 'choice_sets', 'rolls', 'group_votes', 'vote_ballots', 'selected_world_events', 'timer_instances', 'delegation_grants', 'permission_grants', 'ban_records', 'return_requests', 'snapshot_workflows'):
                    self._import_rows(connection, table, data[table], runtime_columns[table], merge=mode == 'merge')
                domain_columns: dict[str, tuple[str, ...]] = {'session_archives': ('session_id', 'termination_type', 'reason', 'final_snapshot_id', 'ended_by', 'ended_at', 'readonly'), 'session_rule_states': ('session_id', 'progress_json', 'content_boundaries_json', 'npc_policy_json', 'context_budget_json', 'dice_rules_json', 'recovery_json', 'revision', 'created_at', 'updated_at'), 'session_characters': ('id', 'session_id', 'stable_key', 'name', 'aliases_json', 'role_type', 'public_profile_json', 'known_facts_json', 'misconceptions_json', 'source', 'review_status', 'lifecycle_status', 'persistent', 'first_event_id', 'last_event_id', 'first_turn', 'last_turn', 'revision', 'created_at', 'updated_at'), 'session_character_states': ('character_id', 'state_json', 'revision', 'updated_at'), 'story_ledger': ('id', 'session_id', 'stable_key', 'kind', 'title', 'description', 'status', 'visibility', 'source_event_id', 'completed_event_id', 'revision', 'created_at', 'updated_at'), 'scene_clocks': ('id', 'session_id', 'stable_key', 'title', 'segments', 'current_value', 'visibility', 'trigger_text', 'status', 'triggered_event_id', 'revision', 'created_at', 'updated_at'), 'memory_governance': ('memory_id', 'visibility', 'locked', 'pinned', 'invalidated', 'supersedes_id', 'conflict_status', 'note', 'updated_by', 'updated_at'), 'assist_tokens': ('id', 'session_id', 'source_participant_id', 'target_participant_id', 'stat', 'method', 'status', 'expires_round', 'source_event_id', 'created_at', 'consumed_at'), 'roll_revisions': ('id', 'roll_id', 'revision_no', 'reason', 'previous_json', 'revised_json', 'actor_id', 'created_at'), 'inspiration_transactions': ('id', 'session_id', 'participant_id', 'delta', 'balance_after', 'reason', 'operation_id', 'created_at'), 'provider_health': ('provider_id', 'status', 'consecutive_failures', 'last_failure_reason', 'last_failure_at', 'last_success_at', 'circuit_until', 'updated_at'), 'configuration_revisions': ('id', 'fingerprint', 'payload_json', 'saved_by', 'saved_at'), 'operation_receipts': ('operation_id', 'session_id', 'operation_type', 'request_json', 'result_json', 'status', 'created_at', 'updated_at')}
                domain_columns['operation_receipts'] = (
                    'operation_id', 'session_id', 'operation_type',
                    'request_json', 'result_json', 'status', 'phase',
                    'retry_count', 'lease_expires_at', 'plan_json',
                    'rollback_json', 'last_error_code', 'input_hash',
                    'cancel_requested_at', 'cancel_requested_by',
                    'last_progress_stage', 'last_progress_at',
                    'reminder_acknowledged', 'reminder_enabled',
                    'reminder_interval_seconds', 'reminder_sequence',
                    'reminder_config_revision', 'reminder_source_revision',
                    'reminder_last_at',
                    'reminder_next_at', 'committed_revision',
                    'created_at', 'updated_at',
                )
                for table in ('session_rule_states', 'session_characters', 'session_character_states', 'story_ledger', 'scene_clocks', 'memory_governance', 'assist_tokens', 'roll_revisions', 'inspiration_transactions', 'provider_health', 'configuration_revisions', 'operation_receipts', 'session_archives'):
                    self._import_rows(connection, table, data[table], domain_columns[table], merge=mode == 'merge')
                for table in ACTOR_FATE_IMPORT_ORDER:
                    self._import_rows(
                        connection,
                        table,
                        data[table],
                        ACTOR_FATE_BACKUP_COLUMNS[table],
                        merge=mode == 'merge',
                    )
                v10_columns: dict[str, tuple[str, ...]] = {
                    'world_feature_versions': ('world_id', 'world_revision', 'feature_name', 'feature_version', 'required', 'created_at'),
                    'world_entity_registry': ('world_id', 'world_revision', 'entity_ref', 'entity_type', 'label', 'definition_json', 'content_hash', 'visibility', 'created_at'),
                    'world_rule_revisions': ('id', 'world_id', 'world_revision', 'content_hash', 'rules_json', 'created_at'),
                    'world_snapshots': ('id', 'world_id', 'world_revision', 'content_hash', 'snapshot_json', 'created_at'),
                    'actor_capability_instances': ('id', 'session_id', 'actor_ref', 'capability_ref', 'definition_version', 'source_ref', 'state_json', 'persistence_scope', 'available', 'created_at', 'updated_at'),
                    'runtime_effect_instances': ('id', 'session_id', 'target_ref', 'effect_ref', 'source_ref', 'state_json', 'duration_json', 'persistence_scope', 'status', 'created_at', 'updated_at'),
                    'operation_commits': ('operation_id', 'session_id', 'input_hash', 'status', 'result_json', 'rollback_json', 'created_at', 'updated_at'),
                    'resolution_receipts': ('receipt_id', 'operation_id', 'session_id', 'world_snapshot_id', 'content_hash', 'receipt_json', 'public_projection_json', 'created_at'),
                    'migration_receipts': ('id', 'migration_type', 'source_version', 'target_version', 'world_id', 'session_id', 'operation_id', 'receipt_json', 'confirmed_by', 'created_at'),
                    'story_documents': ('event_id', 'session_id', 'turn_no', 'schema', 'document_json', 'plain_text', 'text_sha256', 'created_at'),
                }
                for table, columns in v10_columns.items():
                    self._import_rows(connection, table, data[table], columns, merge=mode == 'merge')
                self._import_rows(
                    connection,
                    'dm_control_states',
                    data['dm_control_states'],
                    (
                        'session_id', 'mode', 'active_dm_user_id', 'phase',
                        'directive', 'beat_no', 'current_actor_type',
                        'current_actor_ref', 'preserved_turn_json', 'revision',
                        'created_at', 'updated_at',
                    ),
                    merge=mode == 'merge',
                )
                self._import_rows(connection, 'group_registry', data['group_registry'], ('id', 'platform_id', 'group_id', 'remark', 'revision', 'created_at', 'updated_at'), merge=mode == 'merge')
                data['story_storage'] = []
                policy_columns: dict[str, tuple[str, ...]] = {'timer_policies': ('session_id', 'global_enabled', 'switches_json', 'revision', 'updated_by', 'updated_at'), 'token_usage': ('id', 'session_id', 'group_id', 'request_type', 'provider_id', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'total_tokens', 'reserved_tokens', 'usage_source', 'status', 'created_at', 'settled_at'), 'token_quota_policies': ('id', 'scope_type', 'scope_id', 'window_seconds', 'token_limit', 'enabled', 'revision', 'updated_by', 'updated_at')}
                for table in policy_tables:
                    self._import_rows(connection, table, data[table], policy_columns[table], merge=mode == 'merge')
                self._import_rows(connection, 'card_revision_requests', data['card_revision_requests'], ('id', 'session_id', 'participant_id', 'character_card_id', 'base_version_id', 'candidate_version_id', 'status', 'request_note', 'review_note', 'requested_by', 'reviewed_by', 'created_at', 'updated_at'), merge=mode == 'merge')
                self._import_rows(connection, 'card_review_receipts', data['card_review_receipts'], ('idempotency_key', 'session_id', 'participant_id', 'card_version_id', 'revision_request_id', 'action', 'request_fingerprint', 'event_id', 'result_json', 'created_at'), merge=mode == 'merge')
                self._import_rows(connection, 'supplement_action_receipts', data['supplement_action_receipts'], ('idempotency_key', 'session_id', 'participant_id', 'offer_id', 'action', 'expected_revision', 'request_fingerprint', 'event_id', 'result_json', 'created_at'), merge=mode == 'merge')
                if mode == 'replace':
                    self._import_rows(connection, 'audit_logs', data['audit_logs'], ('id', 'session_id', 'actor_id', 'action', 'target', 'detail_json', 'created_at'))
                sanitize_configuration_revisions(
                    connection,
                    abandon_reserved=True,
                )
                for table, rows in data.items():
                    counts[table] = len(rows) if table != 'audit_logs' or mode == 'replace' else 0
                self._insert_audit(connection, '', actor_id, 'backup.import', mode, counts)
                connection.execute('COMMIT')
                return counts
            except Exception:
                connection.execute('ROLLBACK')
                raise


    @staticmethod
    def _validate_merge_conflicts(
        connection: sqlite3.Connection,
        data: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        """Reject ambiguous identities before a non-destructive merge.

        A backup row may update an existing entity only when its stable ID is
        unchanged. If a natural unique key points at another ID, merging would
        incorrectly combine unrelated worlds, groups, players, or timelines.
        """

        validate_actor_fate_merge_conflicts(connection, data)
        identity_specs: dict[str, tuple[str, ...]] = {
            "worlds": ("slug",),
            "characters": ("world_id", "slug"),
            "sessions": (
                "platform_id",
                "group_id",
                "instance_slug",
            ),
            "players": ("session_id", "user_id"),
            "events": ("seq",),
            "memories": ("session_id", "fingerprint"),
            "snapshots": ("session_id", "name"),
            "group_registry": ("platform_id", "group_id"),
        }
        for table, identity_columns in identity_specs.items():
            seen_ids: set[str] = set()
            seen_identities: dict[tuple[Any, ...], str] = {}
            for row in data.get(table, ()):
                if not isinstance(row, Mapping):
                    raise ValueError(f"备份表 {table} 含非法记录")
                required = ("id", *identity_columns)
                missing = [column for column in required if column not in row]
                if missing:
                    raise ValueError(
                        f"备份表 {table} 缺少字段 {missing[0]}"
                    )
                row_id = str(row["id"])
                if row_id in seen_ids:
                    raise ValueError(
                        f"备份表 {table} 含重复 ID：{row_id}"
                    )
                seen_ids.add(row_id)

                identity = tuple(row[column] for column in identity_columns)
                previous_id = seen_identities.get(identity)
                if previous_id and previous_id != row_id:
                    raise ValueError(
                        f"备份表 {table} 含重复唯一标识，"
                        "请检查备份或改用覆盖导入"
                    )
                seen_identities[identity] = row_id

                where = " AND ".join(
                    f"{column} = ?" for column in identity_columns
                )
                existing = connection.execute(
                    f"SELECT id FROM {table} WHERE {where}",
                    identity,
                ).fetchone()
                if existing and str(existing["id"]) != row_id:
                    raise ValueError(
                        f"备份表 {table} 的唯一标识已属于其他记录，"
                        "为避免串档已取消合并"
                    )

                if table == "events":
                    by_id = connection.execute(
                        "SELECT seq FROM events WHERE id = ?",
                        (row_id,),
                    ).fetchone()
                    if by_id and int(by_id["seq"]) != int(row["seq"]):
                        raise ValueError(
                            "时间线事件 ID 与序号不一致，"
                            "为避免历史错位已取消合并"
                        )

        for document in data.get("story_documents", ()):
            if not isinstance(document, Mapping):
                raise ValueError("备份故事结构表含非法记录")
            event_id = str(document.get("event_id") or "")
            live_event = connection.execute(
                "SELECT * FROM events WHERE id=?",
                (event_id,),
            ).fetchone()
            if live_event is None:
                continue
            live_document = connection.execute(
                "SELECT * FROM story_documents WHERE event_id=?",
                (event_id,),
            ).fetchone()
            retained = (
                dict(live_document)
                if live_document is not None
                else document
            )
            _validate_story_document_against_event(
                retained,
                dict(live_event),
                source="现有" if live_document is not None else "备份",
            )

    @staticmethod
    def _import_rows(
        connection: sqlite3.Connection,
        table: str,
        rows: Sequence[Mapping[str, Any]],
        columns: Sequence[str],
        *,
        merge: bool = False,
    ) -> None:
        placeholders = ",".join("?" for _ in columns)
        column_sql = ",".join(columns)
        insert_sql = (
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
        )
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"备份表 {table} 含非法记录")
            if table == "configuration_revisions":
                row = safe_configuration_revision_row(row)
            values: list[Any] = []
            for column in columns:
                if column not in row:
                    raise ValueError(f"备份表 {table} 缺少字段 {column}")
                values.append(row[column])
            existing = None
            if merge:
                identity_columns = {
                    "instance_configs": "session_id",
                    "snapshot_workflows": "snapshot_id",
                    "session_archives": "session_id",
                    "session_rule_states": "session_id",
                    "dm_control_states": "session_id",
                    "session_character_states": "character_id",
                    "memory_governance": "memory_id",
                    "provider_health": "provider_id",
                    "configuration_revisions": "id",
                    "operation_receipts": "operation_id",
                    "story_documents": "event_id",
                    "group_registry": "id",
                    "story_storage": "session_id",
                    "timer_policies": "session_id",
                    "world_feature_versions": ("world_id", "world_revision", "feature_name"),
                    "world_entity_registry": ("world_id", "world_revision", "entity_ref"),
                    "operation_commits": "operation_id",
                    "resolution_receipts": "receipt_id",
                    "migration_receipts": "id",
                    "actor_fate_states": "character_id",
                }.get(table, "id")
                if isinstance(identity_columns, str):
                    identity_columns = (identity_columns,)
                missing_identity = [column for column in identity_columns if column not in row]
                if missing_identity:
                    raise ValueError(f"备份表 {table} 缺少字段 {missing_identity[0]}")
                where = " AND ".join(f"{column}=?" for column in identity_columns)
                existing = connection.execute(
                    f"SELECT 1 FROM {table} WHERE {where}",
                    tuple(row[column] for column in identity_columns),
                ).fetchone()
            if existing:
                # Merge is deliberately insert-only. Existing records are the
                # authoritative live copy; restoring an older backup must not
                # silently roll back a session, player profile, or event.
                continue
            connection.execute(insert_sql, values)
