SCHEMA_SQL = r"""                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        operation_type TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'completed'
                            CHECK(status IN (
                                'pending', 'reserved', 'generating',
                                'dice_locked', 'ready_to_commit',
                                'cancel_requested',
                                'completed', 'failed',
                                'failed_retryable', 'needs_recovery',
                                'compensated', 'cancelled'
                            )),
                        phase TEXT NOT NULL DEFAULT '',
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        lease_expires_at TEXT NOT NULL DEFAULT '',
                        plan_json TEXT NOT NULL DEFAULT '{}',
                        rollback_json TEXT NOT NULL DEFAULT '{}',
                        last_error_code TEXT NOT NULL DEFAULT '',
                        input_hash TEXT NOT NULL DEFAULT '',
                        cancel_requested_at TEXT NOT NULL DEFAULT '',
                        cancel_requested_by TEXT NOT NULL DEFAULT '',
                        last_progress_stage TEXT NOT NULL DEFAULT '',
                        last_progress_at TEXT NOT NULL DEFAULT '',
                        reminder_acknowledged INTEGER NOT NULL DEFAULT 0
                            CHECK(reminder_acknowledged IN (0, 1)),
                        reminder_enabled INTEGER NOT NULL DEFAULT 1
                            CHECK(reminder_enabled IN (0, 1)),
                        reminder_interval_seconds INTEGER NOT NULL DEFAULT 60
                            CHECK(
                                reminder_interval_seconds BETWEEN 30 AND 600
                                AND reminder_interval_seconds % 15 = 0
                            ),
                        reminder_sequence INTEGER NOT NULL DEFAULT 0
                            CHECK(reminder_sequence >= 0),
                        reminder_config_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(reminder_config_revision >= 0),
                        reminder_source_revision INTEGER NOT NULL DEFAULT 0
                            CHECK(reminder_source_revision >= 0),
                        reminder_last_at TEXT NOT NULL DEFAULT '',
                        reminder_next_at TEXT NOT NULL DEFAULT '',
                        committed_revision INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_operations_session_status
                    ON operation_receipts(session_id, status, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_operations_reminder_due
                    ON operation_receipts(
                        reminder_enabled, reminder_next_at, status
                    ) WHERE reminder_next_at <> '';
                    CREATE TABLE IF NOT EXISTS story_documents (
                        event_id TEXT PRIMARY KEY REFERENCES events(id)
                            ON DELETE CASCADE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        turn_no INTEGER NOT NULL CHECK(turn_no >= 0),
                        schema TEXT NOT NULL
                            CHECK(schema = 'tavern-narrative-document/1.0.0'),
                        document_json TEXT NOT NULL
                            CHECK(json_valid(document_json)),
                        plain_text TEXT NOT NULL,
                        text_sha256 TEXT NOT NULL CHECK(length(text_sha256) = 64),
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_story_documents_session_turn
                    ON story_documents(session_id, turn_no, created_at);
                    -- C6���洢ͬ����վ���С�д�뷽��ͬһ DB ���������У�
                    -- ��������drain�����Ѻ�ɾ��������������������������
                    CREATE TABLE IF NOT EXISTS storage_sync_outbox (
                        session_id TEXT NOT NULL,
                        kind TEXT NOT NULL
                            CHECK(kind IN (
                                'sync', 'archive_save', 'archive_backup'
                            )),
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'leased', 'retry_wait',
                                'permanently_failed'
                            )),
                        desired_generation INTEGER NOT NULL DEFAULT 1,
                        leased_generation INTEGER NOT NULL DEFAULT 0,
                        completed_generation INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 8,
                        lease_owner TEXT NOT NULL DEFAULT '',
                        leased_at TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT NOT NULL DEFAULT '',
                        next_retry_at TEXT NOT NULL DEFAULT '',
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, kind)
                    );
                    CREATE INDEX IF NOT EXISTS idx_storage_sync_outbox_due
                    ON storage_sync_outbox(status, next_retry_at, created_at);
                    CREATE TABLE IF NOT EXISTS world_feature_versions (
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        feature_name TEXT NOT NULL,
                        feature_version TEXT NOT NULL,
                        required INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(world_id, world_revision, feature_name)
                    );
                    CREATE TABLE IF NOT EXISTS world_entity_registry (
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        entity_ref TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        definition_json TEXT NOT NULL DEFAULT '{}',
                        content_hash TEXT NOT NULL,
                        visibility TEXT NOT NULL DEFAULT 'world',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(world_id, world_revision, entity_ref)
                    );
                    CREATE TABLE IF NOT EXISTS world_rule_revisions (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
                        world_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        rules_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(world_id, world_revision)
                    );
                    CREATE TABLE IF NOT EXISTS world_snapshots (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE RESTRICT,
                        world_revision INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(world_id, world_revision, content_hash)
                    );
                    CREATE TABLE IF NOT EXISTS actor_capability_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        actor_ref TEXT NOT NULL,
                        capability_ref TEXT NOT NULL,
                        definition_version INTEGER NOT NULL DEFAULT 1,
                        source_ref TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        persistence_scope TEXT NOT NULL DEFAULT 'campaign',
                        available INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, actor_ref, capability_ref, source_ref)
                    );
                    CREATE TABLE IF NOT EXISTS runtime_effect_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        target_ref TEXT NOT NULL,
                        effect_ref TEXT NOT NULL,
                        source_ref TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        duration_json TEXT NOT NULL DEFAULT '{}',
                        persistence_scope TEXT NOT NULL DEFAULT 'session',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS operation_commits (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        input_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        rollback_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pacing_previews (
                        plan_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        expected_session_revision INTEGER NOT NULL,
                        preview_hash TEXT NOT NULL,
                        plan_json TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_pacing_previews_session
                    ON pacing_previews(session_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS resolution_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        operation_id TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL DEFAULT '',
                        world_snapshot_id TEXT NOT NULL DEFAULT '',
                        content_hash TEXT NOT NULL,
                        receipt_json TEXT NOT NULL,
                        public_projection_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS migration_receipts (
                        id TEXT PRIMARY KEY,
                        migration_type TEXT NOT NULL,
                        source_version TEXT NOT NULL DEFAULT '',
                        target_version TEXT NOT NULL DEFAULT '',
                        world_id TEXT NOT NULL DEFAULT '',
                        session_id TEXT NOT NULL DEFAULT '',
                        operation_id TEXT NOT NULL DEFAULT '',
                        receipt_json TEXT NOT NULL,
                        confirmed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_world_features_revision
                    ON world_feature_versions(world_id, world_revision);
                    CREATE INDEX IF NOT EXISTS idx_world_entities_type
                    ON world_entity_registry(world_id, world_revision, entity_type);
                    CREATE INDEX IF NOT EXISTS idx_actor_capabilities
                    ON actor_capability_instances(session_id, actor_ref, available);
                    CREATE INDEX IF NOT EXISTS idx_runtime_effect_target
                    ON runtime_effect_instances(session_id, target_ref, status);
                    CREATE INDEX IF NOT EXISTS idx_resolution_receipts_session
                    ON resolution_receipts(session_id, created_at DESC);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_migration_operation
                    ON migration_receipts(operation_id) WHERE operation_id <> '';
                    CREATE TABLE IF NOT EXISTS card_revision_requests (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        character_card_id TEXT NOT NULL
                            REFERENCES character_cards(id) ON DELETE CASCADE,
                        base_version_id TEXT NOT NULL
                            REFERENCES character_card_versions(id),
                        candidate_version_id TEXT NOT NULL
                            REFERENCES character_card_versions(id),
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')),
                        request_note TEXT NOT NULL DEFAULT '',
                        review_note TEXT NOT NULL DEFAULT '',
                        requested_by TEXT NOT NULL,
                        reviewed_by TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_card_revision_session
                    ON card_revision_requests(session_id, status, created_at DESC);
                    CREATE TABLE IF NOT EXISTS card_review_receipts (
                        idempotency_key TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL,
                        card_version_id TEXT NOT NULL,
                        revision_request_id TEXT,
                        action TEXT NOT NULL CHECK(action IN (
                            'approve', 'reject', 'cancel'
                        )),
                        request_fingerprint TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_card_review_receipts_target
                    ON card_review_receipts(card_version_id, created_at);
                    CREATE TABLE IF NOT EXISTS supplement_action_receipts (
                        idempotency_key TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        participant_id TEXT NOT NULL,
                        offer_id TEXT NOT NULL,
                        action TEXT NOT NULL CHECK(action IN (
                            'confirm', 'postpone', 'reject', 'cancel'
                        )),
                        expected_revision INTEGER NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_supplement_receipts_offer
                    ON supplement_action_receipts(offer_id, created_at);
                    CREATE TABLE IF NOT EXISTS group_registry (
                        id TEXT PRIMARY KEY,
                        platform_id TEXT NOT NULL,
                        group_id TEXT NOT NULL,
                        remark TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_id, group_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_group_registry_remark
                    ON group_registry(remark, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS story_storage (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_registry_id TEXT NOT NULL
                            REFERENCES group_registry(id) ON DELETE CASCADE,
                        relative_path TEXT NOT NULL UNIQUE,
                        playthrough_no INTEGER NOT NULL DEFAULT 1,
                        created_stamp TEXT NOT NULL,
                        last_synced_revision INTEGER NOT NULL DEFAULT 0,
                        last_checksum TEXT NOT NULL DEFAULT '',
                        sync_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(sync_status IN (
                                'pending', 'ready', 'error'
                            )),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_story_storage_group
                    ON story_storage(group_registry_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS timer_policies (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        global_enabled INTEGER NOT NULL DEFAULT 0,
                        switches_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL DEFAULT 'system',
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS token_usage (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        group_id TEXT NOT NULL,
                        request_type TEXT NOT NULL,
                        provider_id TEXT NOT NULL DEFAULT '',
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        total_tokens INTEGER NOT NULL DEFAULT 0,
                        reserved_tokens INTEGER NOT NULL DEFAULT 0,
                        usage_source TEXT NOT NULL DEFAULT 'estimated',
                        status TEXT NOT NULL DEFAULT 'reserved'
                            CHECK(status IN (
                                'reserved', 'completed', 'failed'
                            )),
                        created_at TEXT NOT NULL,
                        settled_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_token_usage_session_time
                    ON token_usage(session_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_token_usage_group_time
                    ON token_usage(group_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS token_quota_policies (
                        id TEXT PRIMARY KEY,
                        scope_type TEXT NOT NULL
                            CHECK(scope_type IN ('group', 'session')),
                        scope_id TEXT NOT NULL,
                        window_seconds INTEGER NOT NULL,
                        token_limit INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(scope_type, scope_id)
                    );
                    -- v0.12.0-A16������ϵͳ����ѡ������� economy ��������
                    CREATE TABLE IF NOT EXISTS economy_state (
                        session_id TEXT PRIMARY KEY
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        enabled INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS economy_currencies (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        currency_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        short_name TEXT NOT NULL DEFAULT '',
                        icon TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        precision INTEGER NOT NULL DEFAULT 0,
                        allow_negative INTEGER NOT NULL DEFAULT 0,
                        transferable INTEGER NOT NULL DEFAULT 1,
                        exchangeable INTEGER NOT NULL DEFAULT 0,
                        public INTEGER NOT NULL DEFAULT 1,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        extensions_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, currency_id)
                    );
                    CREATE TABLE IF NOT EXISTS economy_wallets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        owner_type TEXT NOT NULL,
                        owner_ref TEXT NOT NULL,
                        currency_id TEXT NOT NULL,
                        balance INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, owner_type, owner_ref, currency_id)
                    );
                    CREATE TABLE IF NOT EXISTS economy_transactions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        operation_id TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        currency_id TEXT NOT NULL,
                        from_owner_type TEXT NOT NULL DEFAULT '',
                        from_owner_ref TEXT NOT NULL DEFAULT '',
                        to_owner_type TEXT NOT NULL DEFAULT '',
                        to_owner_ref TEXT NOT NULL DEFAULT '',
                        amount INTEGER NOT NULL DEFAULT 0,
                        balance_before INTEGER NOT NULL DEFAULT 0,
                        balance_after INTEGER NOT NULL DEFAULT 0,
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        actor_id TEXT NOT NULL DEFAULT '',
                        target_ref TEXT NOT NULL DEFAULT '',
                        event_id TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'committed'
                            CHECK(status IN (
                                'committed', 'reverted', 'failed'
                            )),
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS economy_exchange_rules (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        from_currency TEXT NOT NULL,
                        to_currency TEXT NOT NULL,
                        rate_numerator INTEGER NOT NULL DEFAULT 1,
                        rate_denominator INTEGER NOT NULL DEFAULT 1,
                        fee INTEGER NOT NULL DEFAULT 0,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, from_currency, to_currency)
                    );
                    CREATE INDEX IF NOT EXISTS idx_economy_tx_session_time
                    ON economy_transactions(session_id, created_at DESC);
                    -- v0.12.0-A16��ͳһ�ж����������ģ��ݵ�/���/�طŷ�����
                    CREATE TABLE IF NOT EXISTS action_operations (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        operator_id TEXT NOT NULL DEFAULT '',
                        context_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'committed', 'failed', 'cancelled'
                            )),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_action_ops_session
                    ON action_operations(session_id, created_at DESC);
                    -- Durable, resumable delivery of the ordered turn bundle.
                    -- These tables are part of the fresh Schema 29 catalog;
                    -- The current schema never relies on an old migration to create them.
                    CREATE TABLE IF NOT EXISTS turn_delivery_runs (
                        id TEXT PRIMARY KEY,
                        run_key TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        operation_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL DEFAULT '',
                        state_revision TEXT NOT NULL DEFAULT '',
                        origin TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'sending', 'partially_sent',
                                'retry_wait', 'delivered', 'cancelled'
                            )),
                        next_part_index INTEGER NOT NULL DEFAULT 0
                            CHECK(next_part_index >= 0),
                        total_parts INTEGER NOT NULL CHECK(total_parts >= 1),
                        attempt_count INTEGER NOT NULL DEFAULT 0
                            CHECK(attempt_count >= 0),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        delivered_at TEXT NOT NULL DEFAULT '',
                        cancelled_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_turn_delivery_session
                    ON turn_delivery_runs(session_id, updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_turn_delivery_resume
                    ON turn_delivery_runs(status, updated_at, next_part_index);

                    CREATE TABLE IF NOT EXISTS turn_delivery_parts (
                        run_id TEXT NOT NULL
                            REFERENCES turn_delivery_runs(id) ON DELETE CASCADE,
                        part_index INTEGER NOT NULL CHECK(part_index >= 0),
                        kind TEXT NOT NULL,
                        message_type TEXT NOT NULL DEFAULT '',
                        dedupe_key TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL,
                        rendered_text TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'sending', 'delivered',
                                'failed', 'skipped'
                            )),
                        attempts INTEGER NOT NULL DEFAULT 0
                            CHECK(attempts >= 0),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        delivered_at TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY(run_id, part_index)
                    );
                    CREATE INDEX IF NOT EXISTS idx_turn_delivery_parts_status
                    ON turn_delivery_parts(run_id, status, part_index);
                    -- D1 Schema 20������Ͷ�ݳ־û����У�D1-DEL-005/006����
                    -- C6 �� notification_outbox ���ٱ��������а�����Լ��
                    -- �˱����ԡ���Ƭ�αꡢĿ�����������״̬����
                    CREATE TABLE IF NOT EXISTS delivery_outbox (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        origin TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'notice',
                        text TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'leased', 'partially_sent',
                                'retry_wait', 'delivered',
                                'permanently_failed', 'cancelled',
                                'webui_only', 'sent', 'delivered_on_reply',
                                'dismissed'
                            )),
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        last_error_code TEXT NOT NULL DEFAULT '',
                        dedupe_key TEXT NOT NULL DEFAULT '',
                        priority INTEGER NOT NULL DEFAULT 100,
                        audience TEXT NOT NULL DEFAULT 'player',
                        target_snapshot_json TEXT NOT NULL DEFAULT '{}',
                        projection_snapshot TEXT NOT NULL DEFAULT '',
                        rendered_parts_json TEXT NOT NULL DEFAULT '[]',
                        next_part_index INTEGER NOT NULL DEFAULT 0,
                        total_parts INTEGER NOT NULL DEFAULT 1,
                        shard_cursor_json TEXT NOT NULL DEFAULT '{}',
                        lease_owner TEXT NOT NULL DEFAULT '',
                        leased_at TEXT NOT NULL DEFAULT '',
                        next_retry_at TEXT NOT NULL DEFAULT '',
                        max_attempts INTEGER NOT NULL DEFAULT 8,
                        meta_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        delivered_at TEXT NOT NULL DEFAULT '',
                        cancelled_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_delivery_outbox_pending
                    ON delivery_outbox(status, origin, created_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_outbox_dedupe
                    ON delivery_outbox(dedupe_key)
                    WHERE dedupe_key <> '' AND status = 'pending';
                    CREATE INDEX IF NOT EXISTS idx_delivery_outbox_due
                    ON delivery_outbox(status, priority, next_retry_at, created_at);
                    CREATE INDEX IF NOT EXISTS idx_delivery_outbox_lease
                    ON delivery_outbox(lease_owner, leased_at);
                    -- D1 Schema 20����ɫ��������Դ��D1-DATA-006����
                    -- ����ȷ��ʱ����ɫд�룻�� actor_capability_instances ��
                    -- ����ʱ�����Էֿ���������ȷ��ʱ������Ȩ����⡣
                    CREATE TABLE IF NOT EXISTS character_capabilities (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        character_id TEXT NOT NULL,
                        capability_ref TEXT NOT NULL,
                        source_ref TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        available INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, character_id, capability_ref)
                    );
                    CREATE INDEX IF NOT EXISTS idx_character_capabilities_session
                    ON character_capabilities(session_id, character_id, available);
                    -- D1 Schema 20��ְҵ��Դ��D1-DATA-005/006����
                    CREATE TABLE IF NOT EXISTS character_resources (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        character_id TEXT NOT NULL,
                        resource_ref TEXT NOT NULL,
                        label TEXT NOT NULL DEFAULT '',
                        current INTEGER NOT NULL DEFAULT 0,
                        maximum INTEGER NOT NULL DEFAULT 0,
                        state_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, character_id, resource_ref)
                    );
                    CREATE INDEX IF NOT EXISTS idx_character_resources_session
                    ON character_resources(session_id, character_id);
                    -- ��ɫ���˾�Ԯ���ڡ�
                    -- ����/���/����ȫ���ݵȣ�ͬһ��ɫͬһ����ֻ����һ��
                    -- open �У���ɻ���ں������ٴο�����
                    CREATE TABLE IF NOT EXISTS rescue_windows (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        character_id TEXT NOT NULL
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        kind TEXT NOT NULL DEFAULT 'default',
                        status TEXT NOT NULL DEFAULT 'open'
                            CHECK(status IN (
                                'open', 'succeeded', 'failed', 'cancelled'
                            )),
                        opened_at TEXT NOT NULL,
                        expires_on TEXT NOT NULL DEFAULT '',
                        allowed_rescue_commands_json TEXT NOT NULL DEFAULT '[]',
                        success_transition_json TEXT NOT NULL DEFAULT '[]',
                        failure_transition_json TEXT NOT NULL DEFAULT '[]',
                        command_labels_json TEXT NOT NULL DEFAULT '{}',
                        command TEXT NOT NULL DEFAULT '',
                        outcome TEXT NOT NULL DEFAULT '',
                        completed_at TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_rescue_windows_open
                    ON rescue_windows(session_id, character_id, kind)
                    WHERE status = 'open';
                    CREATE INDEX IF NOT EXISTS idx_rescue_windows_session
                    ON rescue_windows(session_id, character_id, status, expires_on);
                    -- D1 Schema 20�������¼���־���������������� seq����
                    -- events �����Ǿ�ȫ�� AUTOINCREMENT ����ʱ���ߣ�
                    -- session_events �� TWP ����/�վ�/Ͷ�������¼���Ȩ��
                    -- ������Դ��D1-RUN-006 / WP-11�����ɼ���д�븨��ά����
                    CREATE TABLE IF NOT EXISTS session_events (
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        seq INTEGER NOT NULL,
                        event_id TEXT NOT NULL,
                        type TEXT NOT NULL,
                        actor_ref TEXT NOT NULL DEFAULT '',
                        command_id TEXT NOT NULL DEFAULT '',
                        causation_id TEXT NOT NULL DEFAULT '',
                        correlation_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        visibility TEXT NOT NULL DEFAULT 'public',
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, seq)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_events_event_id
                    ON session_events(event_id);
                    CREATE INDEX IF NOT EXISTS idx_session_events_created_at
                    ON session_events(session_id, created_at);
                    -- D1 Schema 20������ͶӰ���㣨���߻ָ�/�����ؽ�ê�㣩��
                    CREATE TABLE IF NOT EXISTS projection_checkpoints (
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        projection_name TEXT NOT NULL,
                        last_seq INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, projection_name)
                    );
                    -- D1 Schema 20������Ͷ��Ŀ�꣨ƽ̨ʵ����һ�߽磬
                    -- D1-DEL-011����ֹֻ�� user_id ȫ��ƥ�䣩��
                    CREATE TABLE IF NOT EXISTS delivery_targets (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        platform_instance_id TEXT NOT NULL,
                        message_type TEXT NOT NULL DEFAULT 'private'
                            CHECK(message_type IN (
                                'group', 'private', 'channel', 'webui_only'
                            )),
                        target_id TEXT NOT NULL,
                        unified_origin TEXT NOT NULL DEFAULT '',
                        target_kind TEXT NOT NULL DEFAULT 'player'
                            CHECK(target_kind IN (
                                'player', 'dm', 'admin', 'system'
                            )),
                        verified_binding INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(platform_instance_id, message_type, target_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_delivery_targets_session
                    ON delivery_targets(session_id);
                    -- ��ɫ����״̬��
                    CREATE TABLE IF NOT EXISTS actor_fate_states (
                        character_id TEXT PRIMARY KEY
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        state TEXT NOT NULL DEFAULT 'healthy',
                        state_label TEXT NOT NULL DEFAULT '',
                        can_act INTEGER NOT NULL DEFAULT 1,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        transitioned_at TEXT NOT NULL DEFAULT '',
                        rescue_window_until TEXT NOT NULL DEFAULT '',
                        rescue_window_kind TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_actor_fate_session
                    ON actor_fate_states(session_id, terminal, can_act);
                    CREATE TABLE IF NOT EXISTS actor_fate_transitions (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        character_id TEXT NOT NULL
                            REFERENCES session_characters(id)
                            ON DELETE CASCADE,
                        from_state TEXT NOT NULL,
                        to_state TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        reversible INTEGER NOT NULL DEFAULT 0,
                        rescue_window INTEGER NOT NULL DEFAULT 0,
                        protection_consumed INTEGER NOT NULL DEFAULT 0,
                        event_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_actor_fate_transitions_session
                    ON actor_fate_transitions(session_id, created_at);
                    -- �վ��������л�ִ��
                    -- D1-DATA-006 ���� terminal_receipts����
                    CREATE TABLE IF NOT EXISTS terminal_receipts (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        condition_id TEXT NOT NULL,
                        condition_label TEXT NOT NULL DEFAULT '',
                        priority INTEGER NOT NULL DEFAULT 0,
                        ending_ref TEXT NOT NULL DEFAULT '',
                        termination_type TEXT NOT NULL DEFAULT 'completed'
                            CHECK(termination_type IN (
                                'completed', 'failed', 'aborted'
                            )),
                        archive_policy TEXT NOT NULL DEFAULT 'automatic_readonly',
                        trigger_revision INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'finalizing', 'finalized',
                                'cancelled'
                            )),
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_terminal_receipts_idempotency
                    ON terminal_receipts(idempotency_key)
                    WHERE idempotency_key <> '';
                    -- D1 Schema 20���վ����ջ�״̬���ɻָ���
                    -- finalization_pending��D1-RUN-013 ������ǰ�ᣩ��
                    CREATE TABLE IF NOT EXISTS session_finalizations (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN (
                                'pending', 'finalized', 'failed', 'cancelled'
                            )),
                        termination_type TEXT NOT NULL DEFAULT 'completed'
                            CHECK(termination_type IN (
                                'completed', 'failed', 'aborted'
                            )),
                        ending_ref TEXT NOT NULL DEFAULT '',
                        ending_label TEXT NOT NULL DEFAULT '',
                        archive_policy TEXT NOT NULL DEFAULT 'automatic_readonly',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        input_locked INTEGER NOT NULL DEFAULT 1,
                        snapshot_status TEXT NOT NULL DEFAULT 'pending'
                            CHECK(snapshot_status IN (
                                'pending', 'completed', 'failed'
                            )),
                        final_snapshot_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_finalization_idempotency
                    ON session_finalizations(idempotency_key)
                    WHERE idempotency_key <> '';
                    CREATE TABLE IF NOT EXISTS item_instances (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        owner_type TEXT NOT NULL DEFAULT 'character'
                            CHECK(owner_type IN ('character','party','actor')),
                        owner_ref TEXT NOT NULL,
                        actor_id TEXT REFERENCES actors(id) ON DELETE CASCADE,
                        item_id TEXT NOT NULL,
                        quantity INTEGER NOT NULL DEFAULT 1,
                        quality TEXT NOT NULL DEFAULT 'standard',
                        durability INTEGER NOT NULL DEFAULT 0,
                        charges INTEGER NOT NULL DEFAULT 0,
                        binding TEXT NOT NULL DEFAULT 'none',
                        container TEXT NOT NULL DEFAULT '',
                        source TEXT NOT NULL DEFAULT '',
                        state_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_id, owner_ref, item_id, container),
                        CHECK(
                            (owner_type='actor' AND actor_id IS NOT NULL
                                AND owner_ref LIKE 'public:actor:%')
                            OR
                            (owner_type IN ('character','party')
                                AND actor_id IS NULL
                                AND owner_ref NOT LIKE 'public:actor:%')
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_item_instances_owner
                    ON item_instances(session_id, owner_ref);
"""
