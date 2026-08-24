"""RC8 explicit write-intent boundary.

The console client never supplies an HTTP method, route, database identifier, or
stable domain reference.  It submits one allow-listed semantic intent plus a
principal-scoped surface handle.  Each handler resolves that handle, repeats
the real authorisation check, and delegates to an existing transactional
repository/application service with optimistic concurrency and a durable
idempotency key.
"""


from __future__ import annotations


import asyncio


import json


from collections.abc import Mapping


from typing import Any


from ...database_support import (
    DatabaseConflictError,
    DatabaseNotFoundError,
    InvalidTransitionError,
)


from ...config import TavernConfig

from ...generation_reminders import (
    GenerationReminderConfigError,
    validate_reminder_interval,
)


from ... import github_worlds as github_service


from ...visualization import visual_envelope


from ...visualization.envelopes import problem_from_exception


from ..errors import WebApiError


from ..routes import (
    actor_id,
    flag,
    mapping,
    require_admin,
    require_author,
    require_login,
    text,
    to_int,
)


from ..routes.tendencies import (
    author_job_action,
    author_job_create,
    health_action,
    tendency_action,
)


from ..routes.snapshot_intents import (
    create_snapshot_intent,
    delete_snapshot_intent,
    restore_snapshot_intent,
    trash_archive_intent,
)


from ..routes.authoring_intents import (
    designer_field_save_intent,
    designer_preset_save_intent,
    twp_module_toggle_intent,
)


from ..routes.character_intents import (
    retire_resident_character_intent,
    save_resident_character_intent,
)


from ..routes.github_imports import (
    commit_github_world_import,
    preview_github_world_import,
)


from ..routes.worlds import archive_world_intent


from ..routes.sessions import require_member


from ..surfaces.registry import (
    health_component_revision,
    issue_surface_key,
    resolve_surface_key,
)


_SESSION_ACTIONS: Mapping[str, tuple[str, str, str]] = {
    "session.lifecycle.finish": ("C01", "finish", "故事已经完结"),
    "session.lifecycle.abort": ("C02", "abort", "本轮已经归档"),
    "session.lifecycle.close": ("C03", "close", "副本已经关闭"),
    "session.lifecycle.reopen": ("C04", "reopen", "副本已经重新开放"),
}


_CARD_ACTIONS: Mapping[str, tuple[str, bool, str]] = {
    "card.review.approve": ("C28", True, "角色审核已经通过"),
}


_AUTHOR_JOB_ACTIONS: Mapping[str, tuple[str, str, str]] = {
    "author_job.cancel": ("C32", "cancel", "作者任务取消请求已记录"),
    "author_job.retry": ("C32", "retry", "新的重试任务已经排队"),
}


_AUTHOR_JOB_CREATE_ACTIONS = frozenset({"author_job.create"})


_TENDENCY_ACTIONS: Mapping[str, tuple[str, str, str]] = {
    "tendency.evidence.ignore": ("tendency.evidence.visibility", "ignore", "这条依据已经忽略"),
    "tendency.evidence.restore": ("tendency.evidence.visibility", "restore", "这条依据已经恢复"),
}


_HEALTH_ACTIONS: Mapping[str, tuple[str, str]] = {
    "health.backup.create": ("health.recover", "新备份已经创建并校验"),
    "health.outbox.retry": ("health.recover", "失败项目已经进入安全重试队列"),
    "health.lease.release_expired": ("health.recover", "过期租约已经释放"),
}


_WORLD_MIGRATION_ACTIONS = frozenset({"session.world.migrate"})


_PACING_ACTIONS = frozenset({"session.pacing.preview", "session.pacing.commit"})


_DESIGNER_ACTIONS = frozenset({"designer.simulate"})


_RECOVERY_ACTIONS = frozenset({"backup.restore.execute"})


_MEMORY_ACTIONS = frozenset({"memory.govern"})


_OPERATION_ACTIONS = frozenset({"operation.cancel.request"})


_TIMER_ACTIONS = frozenset({"timer.control"})


_PARTICIPANT_ACTIONS = frozenset(
    {"participant.retire", "participants.force_ready"}
)


_SESSION_CLONE_ACTIONS = frozenset({"session.clone"})


_SESSION_CONFIGURATION_ACTIONS = frozenset(
    {"session.generation_reminder.save", "session.narrative_mode.save"}
)


_ACTOR_FATE_PREVIEW_ACTIONS = frozenset(
    {"actor_fate.preview.accept", "actor_fate.preview.refuse"}
)


_SETTINGS_ACTIONS = frozenset({"settings.group.save"})


_TOKEN_QUOTA_ACTIONS = frozenset({"session.token_quota.set"})


_SNAPSHOT_ACTIONS = frozenset(
    {
        "snapshot.create",
        "snapshot.replace",
        "snapshot.restore",
        "snapshot.delete",
        "archive.trash",
    }
)


_WORLD_AUTHORING_ACTIONS = frozenset(
    {
        "designer.field.save",
        "designer.preset.save",
        "world.module.toggle",
        "world.archive",
    }
)


_RESIDENT_CHARACTER_ACTIONS = frozenset(
    {
        "resident_character.create",
        "resident_character.update",
        "resident_character.retire",
    }
)


_GITHUB_IMPORT_ACTIONS = frozenset(
    {"github.world.preview", "github.world.commit"}
)


_SETTINGS_FIELDS: Mapping[str, Mapping[str, tuple[str, str]]] = {
    "permissions": {
        "admin_ids": ("security.admin_ids", "csv"),
        "allowed_group_ids": ("security.allowed_group_ids", "csv"),
        "restrict_groups": ("security.require_group_whitelist", "bool"),
        "unauthorized_behavior": (
            "security.unauthorized_command_behavior",
            "behavior",
        ),
        "public_status": ("security.public_status", "bool"),
    },
    "time": {
        "request_timeout_seconds": ("model.request_timeout_seconds", "int"),
        "generation_budget_total_seconds": (
            "model.generation_budget_total_seconds",
            "int",
        ),
        "generation_budget_per_call_seconds": (
            "model.generation_budget_per_call_seconds",
            "int",
        ),
        "user_cooldown_seconds": ("runtime.user_cooldown_seconds", "float"),
        "story_generation_reminder_enabled": (
            "runtime.story_generation_reminder_enabled",
            "bool",
        ),
        "story_generation_reminder_interval_seconds": (
            "runtime.story_generation_reminder_interval_seconds",
            "int",
        ),
    },
    "model": {
        "provider_id": ("model.provider_id", "text"),
        "fallback_provider_1_id": ("model.fallback_provider_1_id", "text"),
        "fallback_provider_2_id": ("model.fallback_provider_2_id", "text"),
        "fallback_provider_3_id": ("model.fallback_provider_3_id", "text"),
        "fallback_provider_4_id": ("model.fallback_provider_4_id", "text"),
        "image_caption_provider_id": ("model.image_caption_provider_id", "text"),
        "image_caption_prompt": ("model.image_caption_prompt", "text"),
        "max_images_per_turn": ("model.max_images_per_turn", "int"),
        "temperature": ("model.temperature", "float"),
        "max_tokens": ("model.max_tokens", "int"),
        "request_timeout_seconds": ("model.request_timeout_seconds", "int"),
        "json_repair_attempts": ("model.json_repair_attempts", "int"),
        "generation_budget_total_seconds": (
            "model.generation_budget_total_seconds",
            "int",
        ),
        "generation_budget_max_calls": (
            "model.generation_budget_max_calls",
            "int",
        ),
        "generation_budget_per_call_seconds": (
            "model.generation_budget_per_call_seconds",
            "int",
        ),
        "generation_budget_max_fallbacks": (
            "model.generation_budget_max_fallbacks",
            "int",
        ),
    },
    "context": {
        "default_world_slug": ("runtime.default_world_slug", "text"),
        "trigger_prefix": ("runtime.trigger_prefix", "text"),
        "qqbot_markdown_enabled": ("runtime.qqbot_markdown_enabled", "bool"),
        "max_input_chars": ("runtime.max_input_chars", "int"),
        "max_output_chars": ("runtime.max_output_chars", "int"),
        "recent_turns": ("runtime.recent_turns", "int"),
        "memory_limit": ("runtime.memory_limit", "int"),
    },
    "recovery": {
        "token_quota_enabled": ("token_quota.enabled", "bool"),
        "token_quota_window_seconds": ("token_quota.window_seconds", "int"),
        "token_quota_limit": ("token_quota.token_limit", "int"),
        "two_phase_checks": ("runtime.two_phase_checks", "bool"),
        "auto_snapshot_interval": ("runtime.auto_snapshot_interval", "int"),
        "audit_retention_days": ("advanced.audit_retention_days", "int"),
        "store_model_payloads": ("advanced.store_model_payloads", "bool"),
    },
    "panel": {
        "panel_enabled": ("remote_panel.enabled", "bool"),
        "panel_host": ("remote_panel.host", "text"),
        "panel_port": ("remote_panel.port", "int"),
        "allow_insecure_http": ("remote_panel.allow_insecure_http", "bool"),
        "secure_cookie": ("remote_panel.secure_cookie", "bool"),
    },
}


INTENT_ALLOWLIST = frozenset(
    (
        *_SESSION_ACTIONS,
        *_CARD_ACTIONS,
        *_AUTHOR_JOB_ACTIONS,
        *_AUTHOR_JOB_CREATE_ACTIONS,
        *_TENDENCY_ACTIONS,
        *_HEALTH_ACTIONS,
        *_WORLD_MIGRATION_ACTIONS,
        *_PACING_ACTIONS,
        *_DESIGNER_ACTIONS,
        *_RECOVERY_ACTIONS,
        *_MEMORY_ACTIONS,
        *_OPERATION_ACTIONS,
        *_TIMER_ACTIONS,
        *_PARTICIPANT_ACTIONS,
        *_SESSION_CLONE_ACTIONS,
        *_SESSION_CONFIGURATION_ACTIONS,
        *_ACTOR_FATE_PREVIEW_ACTIONS,
        *_SETTINGS_ACTIONS,
        *_TOKEN_QUOTA_ACTIONS,
        *_SNAPSHOT_ACTIONS,
        *_WORLD_AUTHORING_ACTIONS,
        *_RESIDENT_CHARACTER_ACTIONS,
        *_GITHUB_IMPORT_ACTIONS,
    )
)


_REQUEST_FIELDS = frozenset(
    {"intent", "target_key", "expected_revision", "input"}
)


_FORBIDDEN_TRANSPORT_FIELDS = frozenset(
    {
        "method",
        "path",
        "url",
        "endpoint",
        "session_id",
        "world_ref",
        "participant_ref",
        "job_ref",
        "operation_id",
        "delivery_id",
    }
)


_PACING_LABELS: Mapping[str, str] = {
    "host_beat": "主持推进一拍",
    "close_scene": "结束当前场景",
    "skip_routine": "跳过无风险过程",
    "transition": "转入下一场景",
    "next_clue": "开放下一条调查线索",
    "advance_chapter": "推进到下一章节",
}


COMMON_DECLARED_EXPORTS = [
    "INTENT_ALLOWLIST",
    "execute_intent",
    "project_recovery_preview",
]



__all__ = [name for name in globals() if not name.startswith('__')]

