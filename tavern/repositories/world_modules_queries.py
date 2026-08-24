from __future__ import annotations

from .worlds_support import *


class WorldModulesQueriesRepositoryMixin:
    def _apply_v05_turn_ops(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        participant: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        source_event_id: str,
        workflow: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Apply validated v0.5 state operations inside the turn transaction."""

        result: dict[str, Any] = {
            "npc": [],
            "clocks": [],
            "ledger": [],
            "statuses": [],
            "assists": [],
        }
        session_id = str(session["id"])

        inspiration_mode = str(
            workflow.get("inspiration_mode") or ""
        ).lower()
        if inspiration_mode in {"advantage", "reroll"} and check_payload:
            runtime = connection.execute(
                """
                SELECT * FROM character_runtime_states
                WHERE session_id = ? AND participant_id = ?
                """,
                (session_id, participant["id"]),
            ).fetchone()
            if not runtime:
                raise InvalidTransitionError("角色缺少副本运行状态")
            state = json_load(runtime["state_json"], {})
            state = dict(state) if isinstance(state, Mapping) else {}
            balance = bounded_int(state.get("inspiration"), 1, 0, 3)
            if balance < 1:
                raise InvalidTransitionError("灵感点不足，本轮没有提交")
            operation_id = (
                f"inspiration:{workflow.get('choice_set_id')}:{inspiration_mode}"
            )
            existing = connection.execute(
                """
                SELECT balance_after FROM inspiration_transactions
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if not existing:
                balance -= 1
                state["inspiration"] = balance
                state["inspiration_max"] = bounded_int(
                    state.get("inspiration_max"),
                    3,
                    1,
                    10,
                )
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO inspiration_transactions(
                        id, session_id, participant_id, delta,
                        balance_after, reason, operation_id, created_at
                    ) VALUES (?, ?, ?, -1, ?, ?, ?, ?)
                    """,
                    (
                        new_id("inspire"),
                        session_id,
                        participant["id"],
                        balance,
                        (
                            "投骰前取得优势"
                            if inspiration_mode == "advantage"
                            else "预授权重投完整骰池"
                        ),
                        operation_id,
                        now,
                    ),
                )
            else:
                balance = int(existing["balance_after"])
            result["inspiration"] = {
                "mode": inspiration_mode,
                "balance": balance,
            }

        assist_token_id = str(
            workflow.get("assist_token_id") or ""
        ).strip()
        if assist_token_id and check_payload:
            consumed = connection.execute(
                """
                UPDATE assist_tokens SET status = 'consumed',
                    consumed_at = ?
                WHERE id = ? AND session_id = ? AND status = 'active'
                """,
                (now, assist_token_id, session_id),
            )
            if consumed.rowcount:
                result["consumed_assist_id"] = assist_token_id

        status_ops = workflow.get("status_ops")
        if isinstance(status_ops, Sequence) and not isinstance(
            status_ops,
            (str, bytes),
        ):
            for operation in status_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                name = clean_text(operation.get("name"), max_chars=100)
                if not target_ref or not name:
                    continue
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    )
                    ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END
                    LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target:
                    continue
                runtime = connection.execute(
                    """
                    SELECT * FROM character_runtime_states
                    WHERE session_id = ? AND participant_id = ?
                    """,
                    (session_id, target["id"]),
                ).fetchone()
                if not runtime:
                    continue
                state = json_load(runtime["state_json"], {})
                state = dict(state) if isinstance(state, Mapping) else {}
                statuses = [
                    dict(item)
                    for item in state.get("statuses", [])
                    if isinstance(item, Mapping)
                ]
                op = str(operation.get("op") or "add").lower()
                existing_index = next(
                    (
                        index
                        for index, item in enumerate(statuses)
                        if str(item.get("name") or "").casefold()
                        == name.casefold()
                    ),
                    -1,
                )
                if op == "remove":
                    if existing_index >= 0:
                        statuses.pop(existing_index)
                else:
                    entry = {
                        "name": name,
                        "severity": str(
                            operation.get("severity") or "minor"
                        ),
                        "affects": [
                            clean_text(item, max_chars=80)
                            for item in (
                                operation.get("affects")
                                if isinstance(
                                    operation.get("affects"),
                                    list,
                                )
                                else []
                            )[:12]
                            if clean_text(item, max_chars=80)
                        ],
                        "effect": clean_text(
                            operation.get("effect"),
                            max_chars=300,
                        ),
                        "removal": clean_text(
                            operation.get("removal"),
                            max_chars=300,
                        ),
                        "source_event_id": source_event_id,
                        "created_turn": new_turn,
                    }
                    if existing_index >= 0:
                        statuses[existing_index] = entry
                    else:
                        statuses.append(entry)
                state["statuses"] = statuses[:40]
                connection.execute(
                    """
                    UPDATE character_runtime_states SET
                        state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(state), now, runtime["id"]),
                )
                result["statuses"].append(
                    {
                        "target_id": target["id"],
                        "name": name,
                        "op": op,
                    }
                )

        config = connection.execute(
            """
            SELECT npc_policy_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        npc_policy = json_load(
            config["npc_policy_json"] if config else "",
            {},
        )
        max_new_npcs = bounded_int(
            npc_policy.get("max_new_per_turn"),
            3,
            0,
            3,
        )
        if not bool(npc_policy.get("enabled", True)):
            max_new_npcs = 0
        created_count = 0
        npc_ops = workflow.get("npc_ops")
        if isinstance(npc_ops, Sequence) and not isinstance(
            npc_ops,
            (str, bytes),
        ):
            for operation in npc_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "").lower()
                name = clean_text(operation.get("name"), max_chars=80)
                npc_id = clean_text(operation.get("npc_id"), max_chars=128)
                aliases = [
                    clean_text(item, max_chars=80)
                    for item in (
                        operation.get("aliases")
                        if isinstance(operation.get("aliases"), list)
                        else []
                    )[:12]
                    if clean_text(item, max_chars=80)
                ]
                npc = None
                matched_by_name = False
                if npc_id:
                    npc = connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE id = ? AND session_id = ?
                        """,
                        (npc_id, session_id),
                    ).fetchone()
                if not npc and name:
                    normalized_names = {
                        self._stable_key(name),
                        *(self._stable_key(item) for item in aliases),
                    }
                    for candidate in connection.execute(
                        """
                        SELECT * FROM session_characters
                        WHERE session_id = ?
                        """,
                        (session_id,),
                    ).fetchall():
                        candidate_names = {
                            self._stable_key(candidate["name"]),
                            *(
                                self._stable_key(item)
                                for item in json_load(
                                    candidate["aliases_json"],
                                    [],
                                )
                            ),
                        }
                        if normalized_names & candidate_names:
                            npc = candidate
                            matched_by_name = True
                            break
                if op == "create" and npc and matched_by_name:
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc["id"],),
                    ).fetchone()
                    raw_duplicate_state = json_load(
                        state_row["state_json"] if state_row else "",
                        {},
                    )
                    duplicate_state = (
                        dict(raw_duplicate_state)
                        if isinstance(raw_duplicate_state, Mapping)
                        else {}
                    )
                    proposals = list(
                        duplicate_state.get("duplicate_proposals") or []
                    )
                    proposals.append(
                        {
                            "name": name,
                            "aliases": aliases,
                            "public_profile": dict(
                                operation.get("public_profile") or {}
                            ),
                            "source_event_id": source_event_id,
                            "turn_no": new_turn,
                        }
                    )
                    duplicate_state["duplicate_proposals"] = proposals[-5:]
                    connection.execute(
                        """
                        UPDATE session_characters
                        SET review_status = 'duplicate',
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, npc["id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc["id"], json_dump(duplicate_state), now),
                    )
                    result["npc"].append(
                        {
                            "id": npc["id"],
                            "op": "duplicate_suspected",
                            "name": name,
                        }
                    )
                    continue
                if op == "create" and not npc:
                    registration_reasons = {
                        str(item)
                        for item in (
                            operation.get("registration_reasons") or []
                        )
                        if str(item)
                        in {
                            "direct_interaction",
                            "important_clue",
                            "long_term_memory",
                        }
                    }
                    if (
                        created_count >= max_new_npcs
                        or not name
                        or not bool(operation.get("persistent", True))
                        or not registration_reasons
                    ):
                        continue
                    created_count += 1
                    npc_id = new_id("snpc")
                    review_status = (
                        "pending"
                        if bool(
                            npc_policy.get(
                                "generated_requires_review",
                                True,
                            )
                        )
                        else "approved"
                    )
                    connection.execute(
                        """
                        INSERT INTO session_characters(
                            id, session_id, stable_key, name, aliases_json,
                            role_type, public_profile_json, known_facts_json,
                            misconceptions_json, source, review_status,
                            lifecycle_status, persistent, first_event_id,
                            last_event_id, first_turn, last_turn, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'model_generated', ?, 'active', 1,
                                  ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            npc_id,
                            session_id,
                            f"generated:{self._stable_key(name)}",
                            name,
                            json_dump(aliases),
                            clean_text(
                                operation.get("role_type") or "npc",
                                max_chars=40,
                            ),
                            json_dump(
                                dict(operation.get("public_profile") or {})
                            ),
                            json_dump(
                                list(operation.get("known_facts") or [])[:30]
                            ),
                            json_dump(
                                list(operation.get("misconceptions") or [])[:20]
                            ),
                            review_status,
                            source_event_id,
                            source_event_id,
                            new_turn,
                            new_turn,
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        """,
                        (
                            npc_id,
                            json_dump(
                                dict(operation.get("runtime_state") or {})
                            ),
                            now,
                        ),
                    )
                    result["npc"].append(
                        {"id": npc_id, "op": "create", "name": name}
                    )
                    continue
                if not npc:
                    continue
                npc_id = str(npc["id"])
                lifecycle_status = str(npc["lifecycle_status"])
                if op == "archive":
                    lifecycle_status = "archived"
                elif op == "depart":
                    lifecycle_status = "departed"
                elif op == "kill":
                    lifecycle_status = "dead"
                elif op in {"update", "create"}:
                    lifecycle_status = "active"
                profile = dict(
                    json_load(npc["public_profile_json"], {})
                )
                if isinstance(operation.get("public_profile"), Mapping):
                    profile.update(dict(operation["public_profile"]))
                known = list(json_load(npc["known_facts_json"], []))
                for fact in list(operation.get("known_facts") or [])[:30]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in known:
                        known.append(text)
                misconceptions = list(
                    json_load(npc["misconceptions_json"], [])
                )
                for fact in list(
                    operation.get("misconceptions") or []
                )[:20]:
                    text = clean_text(fact, max_chars=400)
                    if text and text not in misconceptions:
                        misconceptions.append(text)
                connection.execute(
                    """
                    UPDATE session_characters SET
                        public_profile_json = ?, known_facts_json = ?,
                        misconceptions_json = ?, lifecycle_status = ?,
                        last_event_id = ?, last_turn = ?,
                        revision = revision + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json_dump(profile),
                        json_dump(known[:60]),
                        json_dump(misconceptions[:40]),
                        lifecycle_status,
                        source_event_id,
                        new_turn,
                        now,
                        npc_id,
                    ),
                )
                if isinstance(operation.get("runtime_state"), Mapping):
                    state_row = connection.execute(
                        """
                        SELECT state_json FROM session_character_states
                        WHERE character_id = ?
                        """,
                        (npc_id,),
                    ).fetchone()
                    state = dict(
                        json_load(
                            state_row["state_json"] if state_row else "",
                            {},
                        )
                    )
                    state.update(dict(operation["runtime_state"]))
                    connection.execute(
                        """
                        INSERT INTO session_character_states(
                            character_id, state_json, revision, updated_at
                        ) VALUES (?, ?, 1, ?)
                        ON CONFLICT(character_id) DO UPDATE SET
                            state_json = excluded.state_json,
                            revision = revision + 1,
                            updated_at = excluded.updated_at
                        """,
                        (npc_id, json_dump(state), now),
                    )
                result["npc"].append(
                    {"id": npc_id, "op": op, "name": npc["name"]}
                )

        ledger_ops = workflow.get("ledger_ops")
        if isinstance(ledger_ops, Sequence) and not isinstance(
            ledger_ops,
            (str, bytes),
        ):
            for operation in ledger_ops[:16]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "update").lower()
                entry_id = clean_text(
                    operation.get("entry_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=160)
                row = None
                if entry_id:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE id = ? AND session_id = ?
                        """,
                        (entry_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM story_ledger
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                status = {
                    "complete": "completed",
                    "fail": "failed",
                    "archive": "archived",
                }.get(op, "active")
                kind = str(operation.get("kind") or "objective").lower()
                if kind not in {
                    "main",
                    "side",
                    "objective",
                    "clue",
                    "milestone",
                    "failed",
                }:
                    kind = "objective"
                if not row and op == "create" and title:
                    entry_id = new_id("ledger")
                    connection.execute(
                        """
                        INSERT INTO story_ledger(
                            id, session_id, stable_key, kind, title,
                            description, status, visibility,
                            source_event_id, completed_event_id,
                            revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, '',
                                  1, ?, ?)
                        """,
                        (
                            entry_id,
                            session_id,
                            self._stable_key(title),
                            kind,
                            title,
                            clean_text(
                                operation.get("description"),
                                max_chars=800,
                            ),
                            (
                                "host"
                                if str(
                                    operation.get("visibility") or ""
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            source_event_id,
                            now,
                            now,
                        ),
                    )
                elif row:
                    entry_id = str(row["id"])
                    connection.execute(
                        """
                        UPDATE story_ledger SET
                            kind = ?, title = ?, description = ?,
                            status = ?, visibility = ?,
                            completed_event_id = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            kind,
                            title or row["title"],
                            clean_text(
                                operation.get("description")
                                or row["description"],
                                max_chars=800,
                            ),
                            status,
                            (
                                "host"
                                if str(
                                    operation.get("visibility")
                                    or row["visibility"]
                                ).lower()
                                == "host"
                                else "public"
                            ),
                            (
                                source_event_id
                                if status in {"completed", "failed"}
                                else row["completed_event_id"]
                            ),
                            now,
                            entry_id,
                        ),
                    )
                else:
                    continue
                result["ledger"].append(
                    {"id": entry_id, "op": op, "status": status}
                )

        clock_ops = workflow.get("clock_ops")
        if isinstance(clock_ops, Sequence) and not isinstance(
            clock_ops,
            (str, bytes),
        ):
            for operation in clock_ops[:12]:
                if not isinstance(operation, Mapping):
                    continue
                op = str(operation.get("op") or "advance").lower()
                clock_id = clean_text(
                    operation.get("clock_id"),
                    max_chars=128,
                )
                title = clean_text(operation.get("title"), max_chars=100)
                row = None
                if clock_id:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE id = ? AND session_id = ?
                        """,
                        (clock_id, session_id),
                    ).fetchone()
                if not row and title:
                    row = connection.execute(
                        """
                        SELECT * FROM scene_clocks
                        WHERE session_id = ? AND stable_key = ?
                        """,
                        (session_id, self._stable_key(title)),
                    ).fetchone()
                if not row and op == "create" and title:
                    segments = bounded_int(
                        operation.get("segments"),
                        4,
                        4,
                        8,
                    )
                    if segments not in {4, 6, 8}:
                        segments = 4
                    clock_id = new_id("clock")
                    connection.execute(
                        """
                        INSERT INTO scene_clocks(
                            id, session_id, stable_key, title, segments,
                            current_value, visibility, trigger_text, status,
                            triggered_event_id, revision, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'active', '',
                                  1, ?, ?)
                        """,
                        (
                            clock_id,
                            session_id,
                            self._stable_key(title),
                            title,
                            segments,
                            str(operation.get("visibility") or "public"),
                            clean_text(
                                operation.get("trigger"),
                                max_chars=500,
                            ),
                            now,
                            now,
                        ),
                    )
                    current_value = 0
                    status = "active"
                elif row:
                    clock_id = str(row["id"])
                    segments = int(row["segments"])
                    current_value = int(row["current_value"])
                    if op == "advance":
                        current_value += bounded_int(
                            operation.get("delta"),
                            1,
                            -8,
                            8,
                        )
                    elif op == "set":
                        current_value = bounded_int(
                            operation.get("value"),
                            current_value,
                            0,
                            segments,
                        )
                    elif op == "complete":
                        current_value = segments
                    current_value = max(0, min(segments, current_value))
                    status = (
                        "archived"
                        if op == "archive"
                        else "completed"
                        if current_value >= segments
                        else "active"
                    )
                    triggered_event_id = str(row["triggered_event_id"] or "")
                    trigger_text = clean_text(
                        operation.get("trigger") or row["trigger_text"],
                        max_chars=500,
                    )
                    if (
                        status == "completed"
                        and not triggered_event_id
                    ):
                        triggered_event_id = new_id("event")
                        _insert_world_event(
                            connection,
                            event_id=triggered_event_id,
                            session_id=session_id,
                            turn_no=new_turn,
                            actor_id="clock",
                            actor_name="场景时钟",
                            content=(
                                trigger_text
                                or f"场景时钟「{row['title']}」已填满。"
                            ),
                            meta={
                                "kind": "scene_clock_trigger",
                                "clock_id": clock_id,
                            },
                            created_at=now,
                        )
                    connection.execute(
                        """
                        UPDATE scene_clocks SET
                            current_value = ?, status = ?,
                            triggered_event_id = ?, trigger_text = ?,
                            revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            current_value,
                            status,
                            triggered_event_id,
                            trigger_text,
                            now,
                            clock_id,
                        ),
                    )
                else:
                    continue
                result["clocks"].append(
                    {
                        "id": clock_id,
                        "op": op,
                        "current_value": current_value,
                        "segments": segments,
                        "status": status,
                    }
                )

        assist_ops = workflow.get("assist_ops")
        selected_text = str(
            (workflow.get("selected_choice") or {}).get("text")
            if isinstance(workflow.get("selected_choice"), Mapping)
            else ""
        )
        if (
            isinstance(assist_ops, Sequence)
            and not isinstance(assist_ops, (str, bytes))
            and any(word in selected_text for word in ("协助", "帮助", "支援"))
        ):
            for operation in assist_ops[:1]:
                if not isinstance(operation, Mapping):
                    continue
                target_ref = clean_text(
                    operation.get("target_id"),
                    max_chars=128,
                )
                method = clean_text(
                    operation.get("method"),
                    max_chars=300,
                )
                target = connection.execute(
                    """
                    SELECT * FROM participants
                    WHERE session_id = ? AND (
                        id = ? OR group_user_id = ? OR
                        lower(character_name) = lower(?) OR
                        lower(character_code) = lower(?)
                    ) LIMIT 1
                    """,
                    (
                        session_id,
                        target_ref,
                        target_ref,
                        target_ref,
                        target_ref,
                    ),
                ).fetchone()
                if not target or not method or target["id"] == participant["id"]:
                    continue
                connection.execute(
                    """
                    UPDATE assist_tokens SET status = 'expired'
                    WHERE session_id = ? AND target_participant_id = ?
                      AND status = 'active'
                    """,
                    (session_id, target["id"]),
                )
                token_id = new_id("assist")
                expires_round = bounded_int(
                    operation.get("expires_round"),
                    acting_round + 1,
                    acting_round,
                    acting_round + 1,
                )
                connection.execute(
                    """
                    INSERT INTO assist_tokens(
                        id, session_id, source_participant_id,
                        target_participant_id, stat, method, status,
                        expires_round, source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                    """,
                    (
                        token_id,
                        session_id,
                        participant["id"],
                        target["id"],
                        clean_text(operation.get("stat"), max_chars=40),
                        method,
                        expires_round,
                        source_event_id,
                        now,
                    ),
                )
                result["assists"].append(
                    {"id": token_id, "target_id": target["id"]}
                )

        connection.execute(
            """
            UPDATE assist_tokens SET status = 'expired'
            WHERE session_id = ? AND status = 'active'
              AND expires_round > 0 AND expires_round < ?
            """,
            (session_id, acting_round),
        )

        milestone = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END)
                    AS completed
            FROM story_ledger
            WHERE session_id = ? AND kind = 'milestone'
              AND status <> 'archived'
            """,
            (session_id,),
        ).fetchone()
        objective = connection.execute(
            """
            SELECT title FROM story_ledger
            WHERE session_id = ? AND status = 'active'
              AND kind IN ('main', 'objective')
            ORDER BY CASE kind WHEN 'main' THEN 0 ELSE 1 END,
                     updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        rule_row = connection.execute(
            """
            SELECT progress_json FROM session_rule_states
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if rule_row:
            progress = normalize_progress(
                json_load(rule_row["progress_json"], {})
            )
            if int(milestone["total"] or 0) > 0:
                progress["total_milestones"] = int(milestone["total"])
                progress["completed_milestones"] = int(
                    milestone["completed"] or 0
                )
            if objective:
                progress["current_objective"] = str(objective["title"])
            connection.execute(
                """
                UPDATE session_rule_states SET progress_json = ?,
                    revision = revision + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (json_dump(progress), now, session_id),
            )
            result["progress"] = progress
        return result
