SCHEMA_SQL = r"""
                    CREATE TABLE IF NOT EXISTS tavern_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS worlds (
                        id TEXT PRIMARY KEY,
                        slug TEXT NOT NULL UNIQUE,
                        display_no INTEGER NOT NULL,
                        sort_order INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        system_prompt TEXT NOT NULL,
                        rules_json TEXT NOT NULL DEFAULT '{}',
                        extensions_json TEXT NOT NULL DEFAULT '{}',
                        ui_profile_json TEXT NOT NULL DEFAULT '{}'
                            CHECK(json_valid(ui_profile_json)),
                        opening_scene TEXT NOT NULL DEFAULT '',
                        initial_state_json TEXT NOT NULL DEFAULT '{}',
                        archived INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        source_package_id TEXT NOT NULL DEFAULT '',
                        package_format INTEGER NOT NULL DEFAULT 0,
                        content_version TEXT NOT NULL DEFAULT '',
                        source_kind TEXT NOT NULL DEFAULT 'user',
                        is_modified INTEGER NOT NULL DEFAULT 0,
                        previous_content_version TEXT NOT NULL DEFAULT '',
                        migration_status TEXT NOT NULL DEFAULT '',
                        source_artifact_hash TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS characters (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id)
                            ON DELETE CASCADE,
                        slug TEXT NOT NULL,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'npc',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        prompt TEXT NOT NULL DEFAULT '',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(world_id, slug)
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        instance_slug TEXT NOT NULL,
                        instance_name TEXT NOT NULL,
                        selected INTEGER NOT NULL DEFAULT 0,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        state TEXT NOT NULL DEFAULT 'closed'
                            CHECK(state IN (
                                'closed', 'preparing', 'running', 'paused',
                                'finished', 'maintenance'
                            )),
                        turn_no INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        world_state_json TEXT NOT NULL DEFAULT '{}',
                        history_floor_seq INTEGER NOT NULL DEFAULT 0,
                        input_locked INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id, instance_slug)
                    );
                    CREATE TABLE IF NOT EXISTS players (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        turn_no INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        actor_name TEXT NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_session_seq
                        ON events(session_id, seq DESC);
                    -- 0.11.1���ָ��浵�� MAX(seq)��AND created_at<=? ��ѯ
                    -- ��ǰ�޷����� session ������������ȫɨ��
                    CREATE INDEX IF NOT EXISTS idx_events_created_at
                        ON events(created_at);
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        scope TEXT NOT NULL DEFAULT 'world',
                        scope_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL DEFAULT 'fact',
                        content TEXT NOT NULL,
                        importance INTEGER NOT NULL DEFAULT 3,
                        salience REAL NOT NULL DEFAULT 1,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        fingerprint TEXT NOT NULL,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_accessed_at TEXT NOT NULL,
                        UNIQUE(session_id, fingerprint)
                    );
                    CREATE INDEX IF NOT EXISTS idx_memories_session
                        ON memories(session_id, importance DESC, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS snapshots (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'manual',
                        turn_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        world_id TEXT NOT NULL,
                        world_state_json TEXT NOT NULL,
                        created_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, name)
                    );
                    CREATE INDEX IF NOT EXISTS idx_snapshots_session
                        ON snapshots(session_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL DEFAULT '',
                        action TEXT NOT NULL,
                        target TEXT NOT NULL DEFAULT '',
                        detail_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_audit_session
                        ON audit_logs(session_id, id DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_selected_group
                    ON sessions(platform_id, group_id)
                    WHERE selected = 1;
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_sessions_running_group
                    ON sessions(platform_id, group_id)
                    WHERE state = 'running';
                    CREATE INDEX IF NOT EXISTS idx_sessions_group
                    ON sessions(platform_id, group_id, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS instance_configs (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL DEFAULT 1,
                        world_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        ui_profile_json TEXT NOT NULL DEFAULT '{}'
                            CHECK(json_valid(ui_profile_json)),
                        time_rules_json TEXT NOT NULL DEFAULT '{}',
                        phase_meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS character_cards (
                        id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        world_id TEXT NOT NULL REFERENCES worlds(id),
                        display_name TEXT NOT NULL,
                        archived INTEGER NOT NULL DEFAULT 0,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        current_version INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_character_cards_owner
                    ON character_cards(owner_user_id, world_id, archived);
                    CREATE TABLE IF NOT EXISTS character_card_versions (
                        id TEXT PRIMARY KEY,
                        character_card_id TEXT NOT NULL
                            REFERENCES character_cards(id) ON DELETE CASCADE,
                        version_no INTEGER NOT NULL,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        stats_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending_review'
                            CHECK(status IN (
                                'draft', 'pending_review', 'approved',
                                'rejected', 'superseded'
                            )),
                        review_note TEXT NOT NULL DEFAULT '',
                        reviewed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        UNIQUE(character_card_id, version_no)
                    );
                    CREATE TABLE IF NOT EXISTS participants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        player_id TEXT REFERENCES players(id)
                            ON DELETE SET NULL,
                        group_user_id TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        display_name TEXT NOT NULL,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        character_version_id TEXT
                            REFERENCES character_card_versions(id)
                            ON DELETE SET NULL,
                        character_name TEXT NOT NULL DEFAULT '',
                        character_code TEXT NOT NULL DEFAULT '',
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        card_status TEXT NOT NULL DEFAULT 'uncreated'
                            CHECK(card_status IN (
                                'uncreated', 'draft', 'pending_review',
                                'approved', 'rejected'
                            )),
                        -- D1 Schema 20���ֽ׶ν����ĳ־û��׶Σ�16 ��6����
                        -- δ����/��ģ����� ''���� lifecycle.resolve_card_stage
                        -- �����������־û�ֵ���ȡ�
                        card_stage TEXT NOT NULL DEFAULT ''
                            CHECK(card_stage IN (
                                '', 'incomplete', 'core_ready',
                                'staged_pending', 'stage_locked', 'complete'
                            )),
                        ready INTEGER NOT NULL DEFAULT 0,
                        participation_status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(participation_status IN (
                                'reserved', 'active', 'standby', 'away',
                                'retired', 'archived'
                            )),
                        seat_reserved_at TEXT NOT NULL,
                        joined_round INTEGER NOT NULL DEFAULT 1,
                        consecutive_timeouts INTEGER NOT NULL DEFAULT 0,
                        action_locked INTEGER NOT NULL DEFAULT 0,
                        exit_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, group_user_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_participant_code_session
                    ON participants(session_id, character_code)
                    WHERE character_code <> ''
                      AND participation_status NOT IN ('retired', 'archived');
                    CREATE TABLE IF NOT EXISTS character_runtime_states (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        character_card_id TEXT REFERENCES character_cards(id)
                            ON DELETE SET NULL,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, participant_id)
                    );
                    CREATE TABLE IF NOT EXISTS character_card_drafts (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        generation INTEGER NOT NULL DEFAULT 1,
                        template_version INTEGER NOT NULL DEFAULT 1,
                        template_revision TEXT NOT NULL DEFAULT '',
                        world_revision INTEGER NOT NULL DEFAULT 1,
                        fields_json TEXT NOT NULL DEFAULT '{}',
                        current_step INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'cancelled', 'superseded',
                                'submitted', 'expired'
                            )),
                        cancel_reason TEXT NOT NULL DEFAULT '',
                        superseded_by TEXT NOT NULL DEFAULT '',
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(participant_id, generation)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_character_card_drafts_one_active
                    ON character_card_drafts(participant_id)
                    WHERE status = 'active';
                    CREATE TABLE IF NOT EXISTS card_binding_codes (
                        id TEXT PRIMARY KEY,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        code TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'used', 'expired', 'cancelled',
                                'replaced', 'revoked'
                            )),
                        expires_at TEXT NOT NULL,
                        private_user_id TEXT NOT NULL DEFAULT '',
                        private_origin TEXT NOT NULL DEFAULT '',
                        replaced_by TEXT NOT NULL DEFAULT '',
                        failure_reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        used_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS choice_sets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT REFERENCES participants(id)
                            ON DELETE CASCADE,
                        actor_id TEXT REFERENCES actors(id)
                            ON DELETE SET NULL,
                        round_no INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        choices_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'selected', 'superseded',
                                'cancelled'
                            )),
                        reroll_count INTEGER NOT NULL DEFAULT 0,
                        selected_key TEXT NOT NULL DEFAULT '',
                        flavor_text TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK(participant_id IS NOT NULL OR actor_id IS NOT NULL)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_choice_active_session
                    ON choice_sets(session_id)
                    WHERE status = 'active';
                    CREATE TABLE IF NOT EXISTS rolls (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        choice_set_id TEXT REFERENCES choice_sets(id)
                            ON DELETE SET NULL,
                        participant_id TEXT REFERENCES participants(id)
                            ON DELETE SET NULL,
                        roll_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS group_votes (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        question TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        eligible_user_ids_json TEXT NOT NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL DEFAULT 'open'
                            CHECK(status IN (
                                'open', 'decided', 'resolved', 'rejected',
                                'cancelled', 'needs_recovery'
                            )),
                        decision_status TEXT NOT NULL DEFAULT 'collecting'
                            CHECK(decision_status IN (
                                'collecting', 'decided', 'rejected',
                                'cancelled'
                            )),
                        resolution_status TEXT NOT NULL DEFAULT 'not_started'
                            CHECK(resolution_status IN (
                                'not_started', 'pending', 'generating',
                                'failed_retryable', 'committed', 'cancelled',
                                'needs_recovery'
                            )),
                        resolution_operation_id TEXT NOT NULL DEFAULT '',
                        decision_revision INTEGER NOT NULL DEFAULT 0,
                        decided_at TEXT NOT NULL DEFAULT '',
                        resolved_at TEXT NOT NULL DEFAULT '',
                        committed_event_id TEXT NOT NULL DEFAULT '',
                        winner_key TEXT NOT NULL DEFAULT '',
                        suspended_user_id TEXT NOT NULL DEFAULT '',
                        deadline_at TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_group_vote_unfinished_session
                    ON group_votes(session_id)
                    WHERE decision_status = 'collecting'
                       OR (
                            decision_status = 'decided'
                            AND resolution_status IN (
                                'not_started', 'pending', 'generating',
                                'failed_retryable'
                            )
                       );
                    CREATE TABLE IF NOT EXISTS vote_ballots (
                        id TEXT PRIMARY KEY,
                        vote_id TEXT NOT NULL REFERENCES group_votes(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        option_key TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(vote_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS selected_world_events (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        round_no INTEGER NOT NULL,
                        pool_item_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'selected'
                            CHECK(status IN (
                                'selected', 'narrated', 'cancelled'
                            )),
                        narrative TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        resolved_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(session_id, round_no)
                    );
                    CREATE TABLE IF NOT EXISTS timer_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        participant_id TEXT NOT NULL DEFAULT '',
                        timer_type TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'paused', 'expired', 'cancelled',
                                'completed'
                            )),
                        deadline_at TEXT NOT NULL DEFAULT '',
                        remaining_seconds INTEGER,
                        reminder_at TEXT NOT NULL DEFAULT '',
                        reminder_sent INTEGER NOT NULL DEFAULT 0,
                        action_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_timers_due
                    ON timer_instances(status, deadline_at);
                    CREATE TABLE IF NOT EXISTS delegation_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        owner_user_id TEXT NOT NULL,
                        delegate_user_id TEXT NOT NULL,
                        permissions_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'expired', 'revoked'
                            )),
                        expires_at TEXT NOT NULL DEFAULT '',
                        expiry_kind TEXT NOT NULL DEFAULT 'none',
                        expires_round INTEGER NOT NULL DEFAULT 0,
                        auto_restore INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT 'player',
                        granted_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS permission_grants (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        user_id TEXT NOT NULL,
                        role TEXT NOT NULL
                            CHECK(role IN ('host', 'moderator')),
                        granted_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, user_id, role)
                    );
                    CREATE TABLE IF NOT EXISTS ban_records (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        platform_id TEXT NOT NULL DEFAULT '',
                        group_id TEXT NOT NULL DEFAULT '',
                        user_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL DEFAULT '',
                        scope TEXT NOT NULL
                            CHECK(scope IN ('instance', 'group', 'global')),
                        reason TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'expired', 'revoked')),
                        expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_bans_target
                    ON ban_records(user_id, status, scope);
                    CREATE TABLE IF NOT EXISTS return_requests (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        requested_by TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'requested'
                            CHECK(status IN (
                                'requested', 'voting', 'quest_active',
                                'completed', 'rejected', 'cancelled'
                            )),
                        exit_type TEXT NOT NULL DEFAULT 'departure',
                        objective TEXT NOT NULL DEFAULT '',
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        vote_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS snapshot_workflows (
                        snapshot_id TEXT PRIMARY KEY REFERENCES snapshots(id)
                            ON DELETE CASCADE,
                        workflow_json TEXT NOT NULL DEFAULT '{}'
                    );
                    CREATE TABLE IF NOT EXISTS session_archives (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        termination_type TEXT NOT NULL
                            CHECK(termination_type IN (
                                'completed', 'failed', 'aborted'
                            )),
                        reason TEXT NOT NULL DEFAULT '',
                        final_snapshot_id TEXT NOT NULL
                            REFERENCES snapshots(id),
                        ended_by TEXT NOT NULL,
                        ended_at TEXT NOT NULL,
                        readonly INTEGER NOT NULL DEFAULT 1,
                        ending_ref TEXT NOT NULL DEFAULT '',
                        ending_label TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS session_rule_states (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        progress_json TEXT NOT NULL DEFAULT '{}',
                        content_boundaries_json TEXT NOT NULL DEFAULT '{}',
                        npc_policy_json TEXT NOT NULL DEFAULT '{}',
                        context_budget_json TEXT NOT NULL DEFAULT '{}',
                        dice_rules_json TEXT NOT NULL DEFAULT '{}',
                        recovery_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS dm_control_states (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        mode TEXT NOT NULL DEFAULT 'auto'
                            CHECK(mode IN ('auto', 'dm')),
                        active_dm_user_id TEXT NOT NULL DEFAULT '',
                        phase TEXT NOT NULL DEFAULT 'auto'
                            CHECK(phase IN (
                                'auto', 'awaiting_dm', 'generating',
                                'player_handoff', 'npc_handoff'
                            )),
                        directive TEXT NOT NULL DEFAULT '',
                        beat_no INTEGER NOT NULL DEFAULT 0,
                        current_actor_type TEXT NOT NULL DEFAULT '',
                        current_actor_ref TEXT NOT NULL DEFAULT '',
                        preserved_turn_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS session_characters (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        name TEXT NOT NULL,
                        aliases_json TEXT NOT NULL DEFAULT '[]',
                        role_type TEXT NOT NULL DEFAULT 'npc',
                        public_profile_json TEXT NOT NULL DEFAULT '{}',
                        known_facts_json TEXT NOT NULL DEFAULT '[]',
                        misconceptions_json TEXT NOT NULL DEFAULT '[]',
                        source TEXT NOT NULL DEFAULT 'model_generated'
                            CHECK(source IN (
                                'world_preset', 'model_generated', 'admin'
                            )),
                        review_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(review_status IN (
                                'pending', 'approved', 'rejected',
                                'duplicate'
                            )),
                        lifecycle_status TEXT NOT NULL DEFAULT 'active'
                            CHECK(lifecycle_status IN (
                                'active', 'departed', 'dead', 'archived'
                            )),
                        persistent INTEGER NOT NULL DEFAULT 1,
                        first_event_id TEXT NOT NULL DEFAULT '',
                        last_event_id TEXT NOT NULL DEFAULT '',
                        first_turn INTEGER NOT NULL DEFAULT 0,
                        last_turn INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_characters_context
                    ON session_characters(
                        session_id, lifecycle_status, last_turn DESC
                    );
                    CREATE TABLE IF NOT EXISTS session_character_states (
                        character_id TEXT PRIMARY KEY
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS story_ledger (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'objective'
                            CHECK(kind IN (
                                'main', 'side', 'objective', 'clue',
                                'milestone', 'failed'
                            )),
                        title TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'failed', 'archived'
                            )),
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host')),
                        source_event_id TEXT NOT NULL DEFAULT '',
                        completed_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );
                    CREATE TABLE IF NOT EXISTS scene_clocks (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        stable_key TEXT NOT NULL,
                        title TEXT NOT NULL,
                        segments INTEGER NOT NULL
                            CHECK(segments IN (4, 6, 8)),
                        current_value INTEGER NOT NULL DEFAULT 0,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'vague', 'hidden')),
                        trigger_text TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'completed', 'archived'
                            )),
                        triggered_event_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, stable_key)
                    );
                    CREATE TABLE IF NOT EXISTS memory_governance (
                        memory_id TEXT PRIMARY KEY REFERENCES memories(id)
                            ON DELETE CASCADE,
                        visibility TEXT NOT NULL DEFAULT 'public'
                            CHECK(visibility IN ('public', 'host', 'private')),
                        locked INTEGER NOT NULL DEFAULT 0,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        invalidated INTEGER NOT NULL DEFAULT 0,
                        supersedes_id TEXT NOT NULL DEFAULT '',
                        conflict_status TEXT NOT NULL DEFAULT 'clear'
                            CHECK(conflict_status IN (
                                'clear', 'conflict', 'resolved'
                            )),
                        note TEXT NOT NULL DEFAULT '',
                        updated_by TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS assist_tokens (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        target_participant_id TEXT NOT NULL
                            REFERENCES participants(id) ON DELETE CASCADE,
                        stat TEXT NOT NULL DEFAULT '',
                        method TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'consumed', 'expired')),
                        expires_round INTEGER NOT NULL DEFAULT 0,
                        source_event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        consumed_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE TABLE IF NOT EXISTS roll_revisions (
                        id TEXT PRIMARY KEY,
                        roll_id TEXT NOT NULL REFERENCES rolls(id)
                            ON DELETE CASCADE,
                        revision_no INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        previous_json TEXT NOT NULL,
                        revised_json TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(roll_id, revision_no)
                    );
                    CREATE TABLE IF NOT EXISTS inspiration_transactions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        delta INTEGER NOT NULL,
                        balance_after INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        operation_id TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS provider_health (
                        provider_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'healthy'
                            CHECK(status IN ('healthy', 'open', 'half_open')),
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_failure_reason TEXT NOT NULL DEFAULT '',
                        last_failure_at TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        circuit_until TEXT NOT NULL DEFAULT '',
                        probe_status TEXT NOT NULL DEFAULT 'never',
                        last_probe_at TEXT NOT NULL DEFAULT '',
                        last_probe_latency_ms INTEGER NOT NULL DEFAULT 0,
                        last_probe_error_code TEXT NOT NULL DEFAULT '',
                        probe_expires_at TEXT NOT NULL DEFAULT '',
                        probe_idempotency_key TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS configuration_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fingerprint TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        saved_by TEXT NOT NULL,
                        saved_at TEXT NOT NULL
                    );
"""
