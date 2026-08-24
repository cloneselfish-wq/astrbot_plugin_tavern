from __future__ import annotations

from ...web_console_shared import *
from ...web_console_compat import (
    error_response,
    file_response,
    json_response,
    request,
    stream_response,
)




class RegistryRoutesMixin:
    def __init__(
        self,
        *,
        context: Any,
        plugin_config: Any,
        database: TavernDatabase,
        broker: EventBroker,
        data_dir: Path,
        logger: Any,
        allow_group: Any,
        config_lock: Any,
        extensions: Any = None,
        hooks: Any = None,
        engine: Any = None,
        modules: PluginModuleManager | None = None,
        world_twp: TwpPackageService | None = None,
    ) -> None:
        self.context = context
        self.plugin_config = plugin_config
        self.database = database
        self._tavern_engine = engine
        self.broker = broker
        self.data_dir = Path(data_dir)
        self.logger = logger
        self.allow_group = allow_group
        self.config_lock = config_lock
        # Do not name this attribute ``extensions``: that is also the public
        # async route handler.  The old collision registered the registry
        # object as an HTTP handler and caused GET /extensions to return 500.
        self._extension_registry = extensions
        self.hooks = hooks
        self.modules = modules or PluginModuleManager()
        self.world_twp = world_twp or TwpPackageService(self.data_dir)
        self.web_application_service = WebApplicationService(self.database)
        self.health_recovery_service = HealthRecoveryService(
            self.database,
            self.data_dir,
        )
        self.backup_recovery_service = BackupRecoveryService(
            self.data_dir,
            self.database,
        )
        self.provider_health_service = ProviderHealthService(
            context=self.context,
            repository=self.database,
            config_provider=lambda: TavernConfig.from_mapping(
                self.plugin_config
            ),
        )
        self.application_router = self._build_application_router()
        self.delivery_service = DeliveryService(
            context=self.context,
            repository=self.database,
            markdown_enabled=lambda target: (
                markdown_supported(
                    target.platform_instance_id or target.unified_origin
                )
                or TavernConfig.from_mapping(
                    self.plugin_config
                ).qqbot_markdown_enabled
            ),
        )
        # 0.11.1：按 (slug, revision) 缓存 RuleRuntime，避免每次
        # world_simulate 全量重建 EntityRegistry/CapabilityService/EventPipeline。
        # RuleRuntime 在 __init__ 后只读（resolve 不修改内部状态），可安全复用。
        self._rule_runtime_cache: "OrderedDict[tuple[str, int], RuleRuntime]" = (
            OrderedDict()
        )
        # v1.0-A2：缓存时间戳表（TTL 失效用），与 _rule_runtime_cache 同键。
        self._rule_runtime_timestamps: dict[tuple[str, int], float] = {}
        # C6：内置世界安装状态提供者与重试回调由宿主（main.py）注入。
        # 独立测试没有宿主时保持 None，worlds / builtin-status API 安全降级。
        self.builtin_world_status_provider: Any = None
        self.builtin_world_retry: Any = None
        self.builtin_world_ensure: Any = None
        self._standalone_routes: list[dict[str, Any]] = []
        self._register_routes()
    def _build_application_router(self) -> ApplicationRouter:
        router = ApplicationRouter()
        registrations = (
            (
                CommandSpec(
                    action="tendency.evidence.action",
                    mode="deterministic_write",
                    service="WebApplicationService.execute",
                    session_required=True,
                    expected_revision=True,
                    idempotency_required=True,
                ),
                self.web_application_service.execute,
            ),
            (
                CommandSpec(
                    action="author.job.create",
                    mode="deterministic_write",
                    service="WebApplicationService.execute",
                    capability="author",
                    expected_revision=True,
                    idempotency_required=True,
                ),
                self.web_application_service.execute,
            ),
            (
                CommandSpec(
                    action="author.job.action",
                    mode="deterministic_write",
                    service="WebApplicationService.execute",
                    capability="author",
                    expected_revision=True,
                    idempotency_required=True,
                ),
                self.web_application_service.execute,
            ),
        )
        for spec, handler in registrations:
            router.register(spec, handler)
        for action in sorted(HEALTH_ACTIONS):
            router.register(
                CommandSpec(
                    action=action,
                    mode="deterministic_write",
                    service="HealthRecoveryService.execute",
                    capability="admin",
                    idempotency_required=True,
                ),
                self.health_recovery_service.execute,
            )
        return router
    def _cached_rule_runtime(self, world: Mapping[str, Any]) -> RuleRuntime:
        slug = str(world.get("slug") or "").strip()
        revision = int(world.get("revision") or 0)
        if not slug:
            return RuleRuntime(world)
        key = (slug, revision)
        now = time.monotonic()
        cached = self._rule_runtime_cache.get(key)
        if cached is not None:
            created = self._rule_runtime_timestamps.get(key, 0.0)
            if now - created <= self._RUNTIME_CACHE_TTL_SECONDS:
                self._rule_runtime_cache.move_to_end(key)
                return cached
            self._rule_runtime_cache.pop(key, None)
            self._rule_runtime_timestamps.pop(key, None)
        cached = RuleRuntime(world)
        self._rule_runtime_cache[key] = cached
        self._rule_runtime_timestamps[key] = now
        if len(self._rule_runtime_cache) > 8:
            _, stale = self._rule_runtime_cache.popitem(last=False)
            self._rule_runtime_timestamps.pop(stale, None)
        return cached
    def _purge_rule_runtime(self, slug: str) -> None:
        """世界被编辑/归档/恢复后按 slug 失效对应缓存条目。"""
        slug = str(slug or "").strip()
        if not slug:
            return
        stale_keys = [
            key for key in list(self._rule_runtime_cache)
            if key[0] == slug
        ]
        for key in stale_keys:
            self._rule_runtime_cache.pop(key, None)
            self._rule_runtime_timestamps.pop(key, None)
    def _register(self, path: str, handler: Any, methods: list[str], desc: str) -> None:
        self._standalone_routes.append(
            {
                "path": str(path).strip("/"),
                "handler": handler,
                "methods": tuple(str(method).upper() for method in methods),
                "description": str(desc),
            }
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/{path}",
            handler,
            methods,
            desc,
        )
    def standalone_routes(self) -> tuple[dict[str, Any], ...]:
        """Routes reused by the authenticated self-hosted console."""

        return tuple(dict(item) for item in self._standalone_routes)
    def _register_routes(self) -> None:
        routes = [
            ("overview", self.overview, ["GET"], "Tavern overview"),
            ("worlds", self.worlds, ["GET"], "List worlds"),
            ("worlds/order", self.world_order, ["POST"], "Reorder world"),
            ("worlds/archive", self.world_archive, ["POST"], "Archive world"),
            ("worlds/restore", self.world_restore, ["POST"], "Restore world"),
            (
                "worlds/builtin-status",
                self.builtin_world_status_api,
                ["GET"],
                "Built-in world install status",
            ),
            (
                "worlds/builtin-retry",
                self.builtin_world_retry_api,
                ["POST"],
                "Retry a failed built-in world install",
            ),
            ("characters", self.characters, ["GET"], "List characters"),
            (
                "characters/save",
                self.character_save,
                ["POST"],
                "Save character",
            ),
            (
                "characters/delete",
                self.character_delete,
                ["POST"],
                "Delete character",
            ),
            (
                "characters/import",
                self.character_import,
                ["POST"],
                "Import resident NPCs/characters JSON",
            ),
            ("sessions", self.sessions, ["GET"], "List sessions"),
            (
                "sessions/detail",
                self.session_detail,
                ["GET"],
                "Session detail",
            ),
            (
                "console/sessions/detail",
                self.console_session_detail,
                ["GET"],
                "Console administrator session detail",
            ),
            (
                "console/sessions/shell",
                self.console_session_shell,
                ["GET"],
                "Lightweight console session shell",
            ),
            (
                "sessions/narrative-control",
                self.session_narrative_control_view,
                ["GET"],
                "Narrative control semantic view",
            ),
            (
                "sessions/characters",
                self.session_characters_view,
                ["GET"],
                "Session character semantic list",
            ),
            (
                "sessions/characters/detail",
                self.session_character_detail_view,
                ["GET"],
                "Session character staged detail",
            ),
            (
                "sessions/characters/supplements",
                self.session_character_supplements_view,
                ["GET"],
                "Session staged supplement semantic view",
            ),
            (
                "sessions/world-state",
                self.session_world_state_view,
                ["GET"],
                "Controlled world state semantic view",
            ),
            (
                "sessions/actor-fate",
                self.session_actor_fate_consent,
                ["GET", "POST"],
                "Principal-scoped actor fate preview consent",
            ),
            (
                "sessions/assets",
                self.session_assets_view,
                ["GET"],
                "Session assets semantic view",
            ),
            (
                "sessions/economy",
                self.session_economy_view,
                ["GET"],
                "Session economy semantic view",
            ),
            (
                "sessions/economy/transactions",
                self.session_economy_transactions,
                ["GET"],
                "Session economy transactions",
            ),
            (
                "sessions/economy/set-enabled",
                self.session_economy_set_enabled,
                ["POST"],
                "Enable or disable session economy",
            ),
            (
                "sessions/economy/migrate-world",
                self.session_economy_migrate_world,
                ["POST"],
                "Explicitly migrate a frozen world after backup",
            ),
            (
                "sessions/economy/adjust",
                self.session_economy_adjust,
                ["POST"],
                "Adjust session economy",
            ),
            (
                "sessions/operations",
                self.session_operations_view,
                ["GET"],
                "Session operation semantic view",
            ),
            (
                "sessions/operations/cancel",
                self.session_operation_cancel,
                ["POST"],
                "Cancel a stuck session operation",
            ),
            (
                "sessions/deliveries/view",
                self.session_deliveries_view,
                ["GET"],
                "Session delivery semantic view",
            ),
            (
                "sessions/deliveries/action",
                self.session_deliveries_action,
                ["POST"],
                "Retry or cancel a session delivery",
            ),
            (
                "sessions/diagnostics/view",
                self.session_diagnostics_view,
                ["GET"],
                "Session redacted diagnostic view",
            ),
            (
                "sessions/growth",
                self.session_growth_view,
                ["GET"],
                "Session skill growth view",
            ),
            (
                "sessions/growth/confirm",
                self.session_growth_confirm,
                ["POST"],
                "Confirm a skill growth preview",
            ),
            (
                "sessions/growth/evidence",
                self.session_growth_evidence,
                ["POST"],
                "Record skill growth evidence",
            ),
            (
                "sessions/tendencies/me",
                self.session_tendency_view,
                ["GET"],
                "Signed-in player tendency view",
            ),
            (
                "sessions/tendencies/action",
                self.session_tendency_action,
                ["POST"],
                "Ignore or restore player tendency evidence",
            ),
            (
                "author/jobs",
                self.author_jobs_view,
                ["GET"],
                "List author analysis jobs",
            ),
            (
                "author/jobs/create",
                self.author_job_create,
                ["POST"],
                "Create an author analysis job",
            ),
            (
                "author/jobs/action",
                self.author_job_action,
                ["POST"],
                "Cancel or retry an author analysis job",
            ),
            (
                "author/jobs/artifact",
                self.author_job_artifact,
                ["GET"],
                "Read an author analysis artifact",
            ),
            (
                "admin/health",
                self.health_view,
                ["GET"],
                "redacted health summary",
            ),
            (
                "admin/health/actions",
                self.health_action,
                ["POST"],
                "safe health recovery actions",
            ),
            (
                "admin/health/diagnostics/<token>",
                self.health_diagnostic,
                ["GET"],
                "Download one generated redacted health diagnostic",
            ),
            (
                "sessions/changes",
                self.session_changes,
                ["GET"],
                "Incremental session changes after sequence",
            ),
            (
                "sessions/card-source",
                self.session_card_source,
                ["GET"],
                "Admin-only raw card edit source",
            ),
            ("sessions/recovery", self.session_recovery, ["GET"], "Inspect workflow recovery state"),
            ("sessions/diagnostics", self.session_diagnostics, ["GET"], "Export redacted diagnostics"),
            ("sessions/rescue", self.session_rescue, ["POST"], "Run precise workflow recovery"),
            ("sessions/card-revisions", self.session_card_revisions, ["GET", "POST"], "Manage character card revisions"),
            (
                "sessions/action",
                self.session_action,
                ["POST"],
                "Session action",
            ),
            (
                "sessions/lifecycle",
                self.session_lifecycle,
                ["POST"],
                "Console administrator session lifecycle",
            ),
            (
                "sessions/state",
                self.session_state,
                ["POST"],
                "Edit session state",
            ),
            (
                "sessions/turn-order",
                self.session_turn_order,
                ["POST"],
                "Edit multiplayer turn order",
            ),
            (
                "sessions/time-rules",
                self.session_time_rules,
                ["POST"],
                "Edit instance timing rules",
            ),
            (
                "sessions/rules",
                self.session_rules,
                ["POST"],
                "Edit session rules and progress",
            ),
            (
                "sessions/npc",
                self.session_npc,
                ["POST"],
                "Create or edit a session NPC",
            ),
            (
                "sessions/timer",
                self.session_timer,
                ["POST"],
                "Control persistent timer",
            ),
            (
                "sessions/timer-policy",
                self.session_timer_policy,
                ["POST"],
                "Control countdown categories",
            ),
            (
                "sessions/token-quota",
                self.session_token_quota,
                ["POST"],
                "Control token quotas",
            ),
            (
                "sessions/card-review",
                self.session_card_review,
                ["POST"],
                "Review a character card",
            ),
            (
                "console/sessions/card-review",
                self.console_session_card_review,
                ["POST"],
                "Console administrator character-card review",
            ),
            (
                "sessions/permission",
                self.session_permission,
                ["POST"],
                "Grant instance role",
            ),
            (
                "sessions/participant",
                self.session_participant,
                ["POST"],
                "Manage participant status",
            ),
            (
                "supplements",
                self.supplements,
                ["GET"],
                "List staged supplement offers",
            ),
            (
                "supplements/action",
                self.supplement_action,
                ["POST"],
                "Player confirm or manage a supplement offer",
            ),
            (
                "groups/remark",
                self.group_remark,
                ["POST"],
                "Save a group remark",
            ),
            (
                "groups/token-usage",
                self.group_token_usage,
                ["GET"],
                "Read group token usage and quota",
            ),
            (
                "groups/token-quota",
                self.group_token_quota,
                ["POST"],
                "Control a group token quota",
            ),
            ("players", self.players, ["GET"], "List players"),
            ("players/save", self.player_save, ["POST"], "Save player"),
            (
                "players/delete",
                self.player_delete,
                ["POST"],
                "Delete player",
            ),
            ("memories", self.memories, ["GET"], "List memories"),
            ("memories/save", self.memory_save, ["POST"], "Save memory"),
            (
                "memories/delete",
                self.memory_delete,
                ["POST"],
                "Delete memory",
            ),
            ("snapshots", self.snapshots, ["GET"], "List snapshots"),
            (
                "snapshots/create",
                self.snapshot_create,
                ["POST"],
                "Create snapshot",
            ),
            (
                "snapshots/restore",
                self.snapshot_restore,
                ["POST"],
                "Restore snapshot",
            ),
            (
                "snapshots/delete",
                self.snapshot_delete,
                ["POST"],
                "Delete snapshot",
            ),
            (
                "archives/delete",
                self.archive_delete,
                ["POST"],
                "Delete independent save archive",
            ),
            ("audit", self.audit, ["GET"], "Audit log"),
            ("providers", self.providers, ["GET"], "List chat providers"),
            (
                "console/providers/health-check",
                self.provider_health_check,
                ["POST"],
                "Actively probe the configured narrative provider chain",
            ),
            ("settings", self.settings, ["GET"], "Tavern settings"),
            (
                "settings/save",
                self.settings_save,
                ["POST"],
                "Save Tavern settings",
            ),
            (
                "backup/export",
                self.backup_export,
                ["GET"],
                "Export Tavern backup",
            ),
            (
                "backup/import/<mode>",
                self.backup_import,
                ["POST"],
                "Import Tavern backup",
            ),
            (
                "backup/restore/preview/<mode>",
                self.backup_restore_preview,
                ["POST"],
                "Validate and preview a full backup restore",
            ),
            (
                "backup/restore/execute",
                self.backup_restore_execute,
                ["POST"],
                "Execute a confirmed full backup restore",
            ),
            (
                "dashboard/context",
                self.dashboard_context,
                ["GET"],
                "console safe plugin-page principal context",
            ),
            (
                "dashboard/events",
                self.dashboard_events,
                ["GET"],
                "console principal-scoped session event stream",
            ),
            (
                "dashboard/intents",
                self.dashboard_intent,
                ["POST"],
                "Explicit RC8 semantic write intents",
            ),
            (
                "dashboard/recovery-preview",
                self.dashboard_recovery_preview,
                ["POST"],
                "Preview a console opaque full-backup recovery",
            ),
            ("deliveries", self.deliveries, ["GET", "POST"], "Pending text deliveries"),
            # ── v1.0-A2：跑团现场 ──────────────────────────────
            (
                "dashboard/sessions",
                self.dashboard_sessions,
                ["GET"],
                "Session realtime overview",
            ),
            (
                "dashboard/session",
                self.dashboard_session,
                ["GET"],
                "Session realtime detail",
            ),
            (
                "dashboard/surfaces/dashboard",
                self.dashboard_surface_dashboard,
                ["GET"],
                "console dashboard workspace surface",
            ),
            (
                "dashboard/surfaces/tendencies",
                self.dashboard_surface_tendencies,
                ["GET"],
                "console player tendencies workspace surface",
            ),
            (
                "dashboard/surfaces/sessions",
                self.dashboard_surface_sessions,
                ["GET"],
                "console sessions workspace surface",
            ),
            (
                "dashboard/surfaces/characters",
                self.dashboard_surface_characters,
                ["GET"],
                "console characters workspace surface",
            ),
            (
                "dashboard/surfaces/memories",
                self.dashboard_surface_memories,
                ["GET"],
                "console memories workspace surface",
            ),
            (
                "dashboard/surfaces/worlds",
                self.dashboard_surface_worlds,
                ["GET"],
                "console worlds workspace surface",
            ),
            (
                "dashboard/surfaces/designer",
                self.dashboard_surface_designer,
                ["GET"],
                "console designer workspace surface",
            ),
            (
                "dashboard/surfaces/author_jobs",
                self.dashboard_surface_author_jobs,
                ["GET"],
                "console author jobs workspace surface",
            ),
            (
                "dashboard/surfaces/todo",
                self.dashboard_surface_todo,
                ["GET"],
                "console todo workspace surface",
            ),
            (
                "dashboard/surfaces/audit",
                self.dashboard_surface_audit,
                ["GET"],
                "console audit workspace surface",
            ),
            (
                "dashboard/surfaces/health",
                self.dashboard_surface_health,
                ["GET"],
                "console health workspace surface",
            ),
            (
                "dashboard/surfaces/settings",
                self.dashboard_surface_settings,
                ["GET"],
                "console settings workspace surface",
            ),
            (
                "dashboard/surfaces/modules",
                self.dashboard_surface_modules,
                ["GET"],
                "console modules workspace surface",
            ),
            (
                "dashboard/surfaces/about",
                self.dashboard_surface_about,
                ["GET"],
                "console about workspace surface",
            ),
            (
                "dashboard/session-summary",
                self.dashboard_session_summary,
                ["GET"],
                "console lazy session summary visual",
            ),
            (
                "dashboard/session-party",
                self.dashboard_session_party,
                ["GET"],
                "console lazy party visual",
            ),
            (
                "dashboard/session-world-visuals",
                self.dashboard_session_world_visuals,
                ["GET"],
                "console lazy world visuals",
            ),
            (
                "dashboard/session-history",
                self.dashboard_session_history,
                ["GET"],
                "console lazy history and delivery visuals",
            ),
            (
                "dashboard/session-generation",
                self.dashboard_session_generation,
                ["GET"],
                "console lazy generation waterfall",
            ),
            (
                "sessions/narrative-mode",
                self.session_narrative_mode,
                ["GET", "POST"],
                "console session narrative length mode",
            ),
            (
                "world-twp/readme",
                self.world_twp_readme,
                ["GET"],
                "audience-trimmed world README sections",
            ),
            (
                "sessions/narrative-style",
                self.session_narrative_style,
                ["GET", "POST"],
                "console session dialogue-description style",
            ),
            (
                "sessions/gameplay",
                self.session_gameplay_runtime,
                ["GET", "POST"],
                "console RC10 gameplay module PageModel",
            ),
            (
                "dashboard/timeline",
                self.dashboard_timeline,
                ["GET"],
                "Session event timeline",
            ),
            (
                "dashboard/timers",
                self.dashboard_timers_fast,
                ["GET"],
                "Session timers widget",
            ),
            (
                "dashboard/seed-quota",
                self.dashboard_seed_quota,
                ["POST"],
                "Seed default token quota",
            ),
            # ── A14：元素反应与通用接口 ─────────────────────────────
            (
                "worlds/element-reaction",
                self.world_element_reaction,
                ["POST"],
                "Resolve elemental reaction (dry-run)",
            ),
            (
                "worlds/element-table",
                self.world_element_table,
                ["GET"],
                "World elemental table",
            ),
            (
                "worlds/resolution-table",
                self.world_resolution_table,
                ["GET"],
                "World resolution tables",
            ),
            (
                "sessions/turn-preflight",
                self.session_turn_preflight,
                ["GET"],
                "Turn preflight (read-only)",
            ),
            (
                "sessions/context-compile",
                self.session_context_compile,
                ["GET"],
                "Compiled context debug snapshot",
            ),
            (
                "sessions/inject-fact",
                self.session_inject_fact,
                ["POST"],
                "Inject a world fact",
            ),
            (
                "sessions/apply-effect",
                self.session_apply_effect,
                ["POST"],
                "Validate/dry-run declared effects",
            ),
            (
                "sessions/advance-clock",
                self.session_advance_clock,
                ["POST"],
                "Advance a scene clock",
            ),
            (
                "sessions/pacing/preview",
                self.session_pacing_preview,
                ["POST"],
                "Preview a safe story pacing operation",
            ),
            (
                "sessions/pacing/commit",
                self.session_pacing_commit,
                ["POST"],
                "Commit a previously previewed story pacing operation",
            ),
            ("extensions", self.extensions, ["GET"], "Registered extensions"),
            ("hooks/events", self.hook_events, ["GET"], "Hook event catalog"),
            (
                "meta/capabilities",
                self.meta_capabilities,
                ["GET"],
                "Runtime capabilities",
            ),
            ("sessions/token-reset", self.session_token_reset, ["POST"], "Reset session token stats"),
            ("sessions/turn-command", self.session_turn_command, ["POST"], "Adjust turn order (DM)"),
            ("economy/summary", self.economy_summary, ["GET"], "Economy summary"),
            ("economy/set-enabled", self.economy_set_enabled, ["POST"], "Toggle economy"),
            ("economy/adjust", self.economy_adjust, ["POST"], "Adjust wallet balance"),
            (
                "economy/migrate-world",
                self.session_economy_migrate_world,
                ["POST"],
                "Explicitly migrate a frozen world after backup",
            ),
            ("economy/transactions", self.economy_transactions, ["GET"], "Economy transactions"),
            ("delegations/list", self.delegations_list, ["GET"], "List delegations"),
            ("delegations/grant", self.delegations_grant, ["POST"], "Grant delegation"),
            ("delegations/revoke", self.delegations_revoke, ["POST"], "Revoke delegation"),
            ("delegations/restore", self.delegations_restore, ["POST"], "Restore owner control"),
            ("delegations/forced-choose", self.delegations_forced_choose, ["POST"], "Forced choose"),
            ("delegations/forced-reroll", self.delegations_forced_reroll, ["POST"], "Forced reroll"),
            ("dm/command", self.dm_command, ["POST"], "DM command"),
            ("modules", self.plugin_modules, ["GET"], "Plugin module catalog"),
            ("modules/toggle", self.plugin_module_toggle, ["POST"], "Toggle optional plugin module"),
            ("worlds/twp/protocol", self.world_twp_protocol, ["GET"], "TWP protocol catalog"),
            ("worlds/twp/packages", self.world_twp_packages, ["GET"], "List installed TWP packages"),
            ("worlds/twp/preflight", self.world_twp_preflight, ["POST"], "Validate TWP ZIP package"),
            ("worlds/twp/import", self.world_twp_import, ["POST"], "Import TWP ZIP package"),
            ("worlds/twp/module", self.world_twp_module, ["POST"], "Toggle TWP world module"),
            (
                "sessions/ai-companions",
                self.session_ai_companions,
                ["GET"],
                "List AI companion actors",
            ),
            (
                "sessions/ai-companions/configure",
                self.session_ai_companions_configure,
                ["POST"],
                "Configure AI companion actors",
            ),
            (
                "sessions/ai-companions/decision",
                self.session_ai_companion_decision,
                ["POST"],
                "Confirm, reselect or pause an AI companion decision",
            ),
            (
                "sessions/opening",
                self.session_opening,
                ["GET"],
                "Read the frozen opening recommendation",
            ),
            (
                "sessions/opening/override",
                self.session_opening_override,
                ["POST"],
                "Override the opening before performance",
            ),
            ("worlds/twp/export/<package_id>", self.world_twp_export, ["GET"], "Export original TWP ZIP package"),
            (
                "worlds/twp/<package_id>/actor/preset-libraries",
                self.world_twp_preset_libraries,
                ["GET"],
                "TWP actor preset library catalog",
            ),
            ("worlds/twp/commands", self.world_twp_commands, ["GET"], "TWP command catalog"),
            ("worlds/twp/runtime", self.world_twp_runtime, ["GET"], "TWP runtime projection"),
            ("worlds/twp/command-preview", self.world_twp_command_preview, ["POST"], "Preview a TWP command"),
            ("worlds/twp/endings", self.world_twp_endings, ["GET"], "Ending readiness check"),
            ("worlds/twp/command", self.world_twp_command, ["POST"], "Execute a TWP command"),
            ("designer/health", self.designer_health, ["POST"], "Template health check"),
            ("designer/coverage", self.designer_coverage, ["POST"], "Profession coverage matrix"),
            ("designer/candidates", self.designer_candidates, ["POST"], "Authoritative candidates"),
            ("designer/simulate", self.designer_simulate, ["POST"], "Build simulation"),
            (
                "designer/session-actors",
                self.designer_session_actors,
                ["GET"],
                "Trusted minimal actor summaries for authoring",
            ),
            ("designer/effects", self.designer_effects, ["POST"], "Effect reducer preview"),
            ("designer/template-diff", self.designer_template_diff, ["POST"], "Template diff"),
            ("designer/card-diff", self.designer_card_diff, ["POST"], "Character card diff"),
            ("designer/card-groups", self.designer_card_groups, ["POST"], "Nine-group character card view"),
            ("worlds/twp/l10n-report", self.world_twp_l10n_report, ["POST"], "TWP localization report"),
            ("designer/distribution", self.designer_distribution, ["POST"], "Distribution info check"),
            ("worlds/twp/simulate", self.world_twp_simulate, ["POST"], "TWP deterministic simulation"),
            ("designer/preset-references", self.designer_preset_references, ["POST"], "Preset reference check"),
            ("designer/preset-save", self.designer_preset_save, ["POST"], "Create/update a preset"),
            ("designer/field-save", self.designer_field_save, ["POST"], "Create/update a card field"),
            ("designer/reorder", self.designer_reorder, ["POST"], "Reorder presets"),
            ("designer/revert", self.designer_revert, ["POST"], "Revert last designer edit"),
            ("designer/preset-delete", self.designer_preset_delete, ["POST"], "Physically delete preset after reference check"),
            ("worlds/github/scan", self.github_world_scan, ["POST"], "Scan a public GitHub repo for world packages"),
            ("worlds/github/import", self.github_world_import, ["POST"], "Download and import a world package from GitHub"),
            ("panel/status", self.panel_status, ["GET"], "Remote panel status"),
            ("panel/reset-password", self.panel_reset_password, ["POST"], "Reset remote panel password"),
        ]
        for route in routes:
            self._register(*route)
