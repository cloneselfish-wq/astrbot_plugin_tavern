"""Schema 29 identity, actor, and room-bound runtime catalog."""


SCHEMA_SQL = r"""
                    CREATE TABLE IF NOT EXISTS actors (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        actor_kind TEXT NOT NULL
                            CHECK(actor_kind IN (
                                'human', 'ai_companion', 'npc'
                            )),
                        participant_id TEXT REFERENCES participants(id)
                            ON DELETE SET NULL,
                        character_ref TEXT REFERENCES session_characters(id)
                            ON DELETE SET NULL,
                        display_name TEXT NOT NULL,
                        controller_kind TEXT NOT NULL DEFAULT 'none'
                            CHECK(controller_kind IN (
                                'participant', 'console', 'policy', 'none'
                            )),
                        controller_ref TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'paused', 'retired', 'archived'
                            )),
                        state_json TEXT NOT NULL DEFAULT
                            '{"schema":"tavern-actor-state/1.0.0-rc10","version":1}',
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK(
                            (actor_kind='human' AND participant_id IS NOT NULL)
                            OR
                            (actor_kind<>'human' AND participant_id IS NULL)
                        )
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_actors_participant
                    ON actors(session_id, participant_id)
                    WHERE participant_id IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS idx_actors_session_status
                    ON actors(session_id, actor_kind, status);

                    CREATE TABLE IF NOT EXISTS ai_companion_instances (
                        actor_id TEXT PRIMARY KEY REFERENCES actors(id)
                            ON DELETE CASCADE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        preset_id TEXT NOT NULL,
                        preset_version TEXT NOT NULL,
                        frozen_profile_json TEXT NOT NULL,
                        decision_policy_json TEXT NOT NULL DEFAULT '{}',
                        provider_policy_json TEXT NOT NULL DEFAULT '{}',
                        mode TEXT NOT NULL DEFAULT 'confirm'
                            CHECK(mode IN (
                                'automatic', 'confirm', 'paused'
                            )),
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'waiting', 'acting', 'retry_wait',
                                'paused', 'retired', 'error'
                            )),
                        current_operation_id TEXT NOT NULL DEFAULT '',
                        lease_owner TEXT NOT NULL DEFAULT '',
                        leased_at TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT NOT NULL DEFAULT '',
                        last_decision_receipt_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ai_companion_session_status
                    ON ai_companion_instances(session_id, status, actor_id);
                    CREATE INDEX IF NOT EXISTS idx_ai_companion_lease
                    ON ai_companion_instances(status, lease_expires_at);

                    CREATE TABLE IF NOT EXISTS ai_companion_decision_receipts (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        actor_id TEXT NOT NULL REFERENCES actors(id)
                            ON DELETE CASCADE,
                        operation_id TEXT NOT NULL,
                        choice_set_id TEXT REFERENCES choice_sets(id)
                            ON DELETE SET NULL,
                        actor_revision INTEGER NOT NULL,
                        session_revision INTEGER NOT NULL,
                        decision_json TEXT NOT NULL,
                        public_projection_json TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN (
                                'planned', 'awaiting_confirmation',
                                'submitted', 'discarded', 'failed'
                            )),
                        idempotency_key TEXT NOT NULL UNIQUE,
                        trace_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(operation_id, actor_id)
                    );

                    CREATE TABLE IF NOT EXISTS session_opening_decisions (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        world_id TEXT NOT NULL,
                        world_revision INTEGER NOT NULL
                            CHECK(world_revision >= 1),
                        algorithm_version TEXT NOT NULL,
                        seed TEXT NOT NULL,
                        candidates_json TEXT NOT NULL,
                        selected_scene_ref TEXT NOT NULL,
                        selected_reason TEXT NOT NULL,
                        selection_source TEXT NOT NULL
                            CHECK(selection_source IN (
                                'recommended', 'defaulted', 'admin_override',
                                'imported', 'cloned'
                            )),
                        overridden_by_principal_ref TEXT NOT NULL DEFAULT '',
                        frozen INTEGER NOT NULL DEFAULT 0
                            CHECK(frozen IN (0, 1)),
                        frozen_at TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS principal_bindings (
                        id TEXT PRIMARY KEY,
                        principal_kind TEXT NOT NULL
                            CHECK(principal_kind IN (
                                'console', 'platform', 'miniprogram'
                            )),
                        provider TEXT NOT NULL,
                        app_id TEXT NOT NULL DEFAULT '',
                        external_subject_hash TEXT NOT NULL,
                        encrypted_payload_json TEXT NOT NULL DEFAULT '{}',
                        local_user_ref TEXT NOT NULL DEFAULT '',
                        platform_instance_id TEXT NOT NULL DEFAULT '',
                        display_name TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'pending', 'active', 'rotated', 'revoked',
                                'expired'
                            )),
                        rotated_from_id TEXT REFERENCES principal_bindings(id)
                            ON DELETE SET NULL,
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        revoked_at TEXT NOT NULL DEFAULT '',
                        UNIQUE(
                            principal_kind, provider, app_id,
                            external_subject_hash
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_principal_bindings_status
                    ON principal_bindings(principal_kind, provider, status);

                    CREATE TABLE IF NOT EXISTS room_invites (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        code_hash TEXT NOT NULL UNIQUE,
                        created_by_binding_id TEXT
                            REFERENCES principal_bindings(id)
                            ON DELETE SET NULL,
                        max_uses INTEGER NOT NULL DEFAULT 1
                            CHECK(max_uses >= 1),
                        use_count INTEGER NOT NULL DEFAULT 0
                            CHECK(use_count >= 0),
                        failed_attempts INTEGER NOT NULL DEFAULT 0
                            CHECK(failed_attempts >= 0),
                        status TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN (
                                'active', 'consumed', 'expired', 'revoked',
                                'rate_limited'
                            )),
                        expires_at TEXT NOT NULL,
                        cooldown_until TEXT NOT NULL DEFAULT '',
                        last_used_at TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK(use_count <= max_uses)
                    );
                    CREATE INDEX IF NOT EXISTS idx_room_invites_session_status
                    ON room_invites(session_id, status, expires_at);

                    CREATE TABLE IF NOT EXISTS choice_recovery_receipts (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        choice_set_id TEXT REFERENCES choice_sets(id)
                            ON DELETE SET NULL,
                        operation_id TEXT NOT NULL,
                        provider_class TEXT NOT NULL DEFAULT '',
                        failure_kind TEXT NOT NULL,
                        repair_count INTEGER NOT NULL DEFAULT 0
                            CHECK(repair_count >= 0),
                        fallback_version TEXT NOT NULL DEFAULT '',
                        resolution_summary_json TEXT NOT NULL DEFAULT '{}',
                        public_message TEXT NOT NULL,
                        trace_id TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN (
                                'repaired', 'fallback', 'failed', 'cancelled'
                            )),
                        idempotency_key TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_choice_recovery_session
                    ON choice_recovery_receipts(session_id, created_at);

                    CREATE TABLE IF NOT EXISTS world_module_runtime_status (
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        module_id TEXT NOT NULL,
                        declared INTEGER NOT NULL DEFAULT 0
                            CHECK(declared IN (0, 1)),
                        definition_state TEXT NOT NULL
                            CHECK(definition_state IN (
                                'not_applicable', 'ready', 'error'
                            )),
                        runtime_state TEXT NOT NULL
                            CHECK(runtime_state IN (
                                'not_applicable', 'waiting', 'empty',
                                'ready', 'error'
                            )),
                        projection_state TEXT NOT NULL
                            CHECK(projection_state IN (
                                'not_applicable', 'waiting', 'empty',
                                'ready', 'error'
                            )),
                        issue_code TEXT NOT NULL DEFAULT '',
                        issue_message TEXT NOT NULL DEFAULT '',
                        last_success_at TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        revision INTEGER NOT NULL DEFAULT 1
                            CHECK(revision >= 1),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(session_id, module_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_world_module_runtime_state
                    ON world_module_runtime_status(
                        session_id, projection_state, module_id
                    );

                    CREATE INDEX IF NOT EXISTS idx_choice_actor
                    ON choice_sets(session_id, actor_id);
"""


POST_OPERATIONS_SQL = r"""
                    CREATE INDEX IF NOT EXISTS idx_item_instances_actor
                    ON item_instances(session_id, actor_id)
                    WHERE actor_id IS NOT NULL;

                    CREATE TRIGGER IF NOT EXISTS
                        trg_item_instances_actor_insert
                    BEFORE INSERT ON item_instances
                    WHEN NEW.owner_type='actor' AND NOT EXISTS (
                        SELECT 1 FROM actors a
                        WHERE a.id=NEW.actor_id
                          AND a.session_id=NEW.session_id
                          AND a.actor_kind='ai_companion'
                          AND a.status NOT IN ('retired', 'archived')
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT, 'AI actor inventory owner is invalid'
                        );
                    END;

                    CREATE TRIGGER IF NOT EXISTS
                        trg_item_instances_actor_update
                    BEFORE UPDATE OF
                        session_id, owner_type, owner_ref, actor_id
                    ON item_instances
                    WHEN NEW.owner_type='actor' AND NOT EXISTS (
                        SELECT 1 FROM actors a
                        WHERE a.id=NEW.actor_id
                          AND a.session_id=NEW.session_id
                          AND a.actor_kind='ai_companion'
                          AND a.status NOT IN ('retired', 'archived')
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT, 'AI actor inventory owner is invalid'
                        );
                    END;
"""
