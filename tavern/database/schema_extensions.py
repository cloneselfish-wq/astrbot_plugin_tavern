SCHEMA_SQL = r"""                    CREATE INDEX IF NOT EXISTS idx_item_instances_actor
                    ON item_instances(session_id, actor_id)
                    WHERE actor_id IS NOT NULL;
                    CREATE TABLE IF NOT EXISTS world_edit_undo (
                        id TEXT PRIMARY KEY,
                        world_id TEXT NOT NULL REFERENCES worlds(id)
                            ON DELETE CASCADE,
                        revision INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_world_edit_undo
                    ON world_edit_undo(world_id, created_at DESC);
                    CREATE TABLE IF NOT EXISTS session_narrative_styles (
                        session_id TEXT PRIMARY KEY REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        preset_id TEXT NOT NULL,
                        custom_expectation TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 0,
                        source_world_style_sha TEXT NOT NULL,
                        updated_by TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gameplay_states (
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        module_id TEXT NOT NULL,
                        state_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        visibility TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, module_id, state_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_gameplay_states_module
                    ON gameplay_states(session_id, module_id, visibility);
                    CREATE TABLE IF NOT EXISTS gameplay_receipts (
                        operation_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id)
                            ON DELETE CASCADE,
                        module_id TEXT NOT NULL,
                        intent TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        input_sha256 TEXT NOT NULL,
                        revision_before INTEGER NOT NULL,
                        revision_after INTEGER NOT NULL,
                        result_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(session_id, module_id, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS idx_gameplay_receipts_session
                    ON gameplay_receipts(session_id, created_at DESC);
"""
