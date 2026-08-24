"""Schema 29 tendency, knowledge, authoring, and event outbox catalog."""


SCHEMA_SQL = r"""
                    CREATE TABLE IF NOT EXISTS player_tendency_evidence (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        participant_id TEXT NOT NULL REFERENCES participants(id)
                            ON DELETE CASCADE,
                        event_id TEXT NOT NULL REFERENCES session_events(event_id),
                        dimension TEXT NOT NULL
                            CHECK(dimension IN (
                                'risk', 'cooperation', 'mercy', 'curiosity',
                                'authority', 'planning'
                            )),
                        direction INTEGER NOT NULL
                            CHECK(direction IN (-1, 1)),
                        weight INTEGER NOT NULL
                            CHECK(weight BETWEEN 1 AND 5),
                        confidence REAL NOT NULL
                            CHECK(confidence >= 0 AND confidence <= 1),
                        rationale TEXT NOT NULL,
                        action_summary TEXT NOT NULL,
                        source_kind TEXT NOT NULL
                            CHECK(source_kind IN (
                                'action', 'vote', 'quest', 'host_correction'
                            )),
                        created_at TEXT NOT NULL,
                        revoked_at TEXT NOT NULL DEFAULT '',
                        revoked_by TEXT NOT NULL DEFAULT '',
                        revoke_reason TEXT NOT NULL DEFAULT '',
                        UNIQUE(participant_id, event_id, dimension)
                    );
                    CREATE INDEX IF NOT EXISTS idx_tendency_evidence_active
                    ON player_tendency_evidence(
                        participant_id, revoked_at, created_at DESC
                    );

                    CREATE TABLE IF NOT EXISTS player_tendency_profiles (
                        participant_id TEXT PRIMARY KEY
                            REFERENCES participants(id) ON DELETE CASCADE,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        source_last_seq INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        summary_json TEXT NOT NULL DEFAULT '{}',
                        evidence_count INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_tendency_profiles_session
                    ON player_tendency_profiles(session_id, updated_at DESC);

                    CREATE TABLE IF NOT EXISTS npc_knowledge_evidence (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        character_id TEXT NOT NULL
                            REFERENCES session_characters(id) ON DELETE CASCADE,
                        fact_ref TEXT NOT NULL,
                        fact_text TEXT NOT NULL DEFAULT '',
                        belief_kind TEXT NOT NULL
                            CHECK(belief_kind IN (
                                'known', 'misconception'
                            )),
                        source_event_id TEXT NOT NULL
                            REFERENCES session_events(event_id),
                        source_kind TEXT NOT NULL
                            CHECK(source_kind IN (
                                'witnessed', 'told', 'document', 'inference',
                                'world_preset'
                            )),
                        confidence REAL NOT NULL
                            CHECK(confidence >= 0 AND confidence <= 1),
                        visibility TEXT NOT NULL
                            CHECK(visibility IN (
                                'public', 'party', 'host', 'secret'
                            )),
                        known_since TEXT NOT NULL,
                        expires_at TEXT NOT NULL DEFAULT '',
                        revoked_at TEXT NOT NULL DEFAULT '',
                        revoked_by_event_id TEXT NOT NULL DEFAULT '',
                        UNIQUE(
                            character_id, fact_ref, source_event_id,
                            belief_kind
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_npc_knowledge_active
                    ON npc_knowledge_evidence(
                        character_id, revoked_at, expires_at, known_since
                    );

                    CREATE TABLE IF NOT EXISTS author_jobs (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL
                            CHECK(job_type IN (
                                'playtest', 'semantic_diff', 'full_preflight'
                            )),
                        world_id TEXT NOT NULL DEFAULT '',
                        world_revision INTEGER NOT NULL DEFAULT 0,
                        input_hash TEXT NOT NULL,
                        request_json TEXT NOT NULL,
                        status TEXT NOT NULL
                            CHECK(status IN (
                                'queued', 'leased', 'running', 'retry_wait',
                                'succeeded', 'permanently_failed',
                                'cancel_requested', 'cancelled'
                            )),
                        progress_current INTEGER NOT NULL DEFAULT 0,
                        progress_total INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        lease_owner TEXT NOT NULL DEFAULT '',
                        leased_at TEXT NOT NULL DEFAULT '',
                        lease_expires_at TEXT NOT NULL DEFAULT '',
                        next_retry_at TEXT NOT NULL DEFAULT '',
                        result_summary_json TEXT NOT NULL DEFAULT '{}',
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_error TEXT NOT NULL DEFAULT '',
                        created_by TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finished_at TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_author_jobs_dedupe
                    ON author_jobs(
                        job_type, world_id, world_revision, input_hash
                    )
                    WHERE status IN (
                        'queued', 'leased', 'running', 'retry_wait',
                        'succeeded'
                    );
                    CREATE INDEX IF NOT EXISTS idx_author_jobs_due
                    ON author_jobs(status, next_retry_at, created_at);

                    CREATE TABLE IF NOT EXISTS world_analysis_artifacts (
                        id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES author_jobs(id)
                            ON DELETE CASCADE,
                        artifact_type TEXT NOT NULL
                            CHECK(artifact_type IN (
                                'playtest_report', 'semantic_diff',
                                'preflight_report', 'coverage_matrix'
                            )),
                        schema_id TEXT NOT NULL,
                        content_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(job_id, artifact_type)
                    );

                    CREATE TABLE IF NOT EXISTS event_outbox (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL DEFAULT '',
                        event_id TEXT NOT NULL DEFAULT '',
                        topic TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        audience TEXT NOT NULL DEFAULT 'internal',
                        dedupe_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL
                            CHECK(status IN (
                                'pending', 'leased', 'retry_wait', 'delivered',
                                'permanently_failed', 'cancelled'
                            )),
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
                        delivered_at TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS idx_event_outbox_due
                    ON event_outbox(status, next_retry_at, created_at);
"""
