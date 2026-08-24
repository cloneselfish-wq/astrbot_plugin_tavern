from __future__ import annotations


SETTING_ACTION_FIELDS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "permissions": (
        ("admin_ids", "security.admin_ids", "text"),
        ("allowed_group_ids", "security.allowed_group_ids", "text"),
        ("restrict_groups", "security.require_group_whitelist", "checkbox"),
        (
            "unauthorized_behavior",
            "security.unauthorized_command_behavior",
            "select",
        ),
        ("public_status", "security.public_status", "checkbox"),
    ),
    "time": (
        ("request_timeout_seconds", "model.request_timeout_seconds", "number"),
        (
            "generation_budget_total_seconds",
            "model.generation_budget_total_seconds",
            "number",
        ),
        (
            "generation_budget_per_call_seconds",
            "model.generation_budget_per_call_seconds",
            "number",
        ),
        ("user_cooldown_seconds", "runtime.user_cooldown_seconds", "number"),
        (
            "story_generation_reminder_enabled",
            "runtime.story_generation_reminder_enabled",
            "checkbox",
        ),
        (
            "story_generation_reminder_interval_seconds",
            "runtime.story_generation_reminder_interval_seconds",
            "number",
        ),
    ),
    "model": (
        ("provider_id", "model.provider_id", "select"),
        ("fallback_provider_1_id", "model.fallback_provider_1_id", "select"),
        ("fallback_provider_2_id", "model.fallback_provider_2_id", "select"),
        ("fallback_provider_3_id", "model.fallback_provider_3_id", "select"),
        ("fallback_provider_4_id", "model.fallback_provider_4_id", "select"),
        ("image_caption_provider_id", "model.image_caption_provider_id", "select"),
        ("image_caption_prompt", "model.image_caption_prompt", "textarea"),
        ("max_images_per_turn", "model.max_images_per_turn", "number"),
        ("temperature", "model.temperature", "number"),
        ("max_tokens", "model.max_tokens", "number"),
        ("request_timeout_seconds", "model.request_timeout_seconds", "number"),
        ("json_repair_attempts", "model.json_repair_attempts", "number"),
        (
            "generation_budget_total_seconds",
            "model.generation_budget_total_seconds",
            "number",
        ),
        (
            "generation_budget_max_calls",
            "model.generation_budget_max_calls",
            "number",
        ),
        (
            "generation_budget_per_call_seconds",
            "model.generation_budget_per_call_seconds",
            "number",
        ),
        (
            "generation_budget_max_fallbacks",
            "model.generation_budget_max_fallbacks",
            "number",
        ),
    ),
    "context": (
        ("default_world_slug", "runtime.default_world_slug", "text"),
        ("trigger_prefix", "runtime.trigger_prefix", "text"),
        ("qqbot_markdown_enabled", "runtime.qqbot_markdown_enabled", "checkbox"),
        ("max_input_chars", "runtime.max_input_chars", "number"),
        ("max_output_chars", "runtime.max_output_chars", "number"),
        ("recent_turns", "runtime.recent_turns", "number"),
        ("memory_limit", "runtime.memory_limit", "number"),
    ),
    "recovery": (
        ("token_quota_enabled", "token_quota.enabled", "checkbox"),
        ("token_quota_window_seconds", "token_quota.window_seconds", "number"),
        ("token_quota_limit", "token_quota.token_limit", "number"),
        ("two_phase_checks", "runtime.two_phase_checks", "checkbox"),
        ("auto_snapshot_interval", "runtime.auto_snapshot_interval", "number"),
        ("audit_retention_days", "advanced.audit_retention_days", "number"),
        ("store_model_payloads", "advanced.store_model_payloads", "checkbox"),
    ),
    "panel": (
        ("panel_enabled", "remote_panel.enabled", "checkbox"),
        ("panel_host", "remote_panel.host", "text"),
        ("panel_port", "remote_panel.port", "number"),
        ("allow_insecure_http", "remote_panel.allow_insecure_http", "checkbox"),
        ("secure_cookie", "remote_panel.secure_cookie", "checkbox"),
    ),
}


__all__ = ["SETTING_ACTION_FIELDS"]
