from __future__ import annotations

from .workflow_support import *


class WorkflowOperationsRepositoryMixin:
    async def activate_story(
        self,
        session_id: str,
        actor_id: str,
        *,
        resume: bool = False,
        choices: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = await self.get_session(session_id)
        if not resume and int(session.get("turn_no") or 0) > 0:
            raise InvalidTransitionError(
                "该副本已有剧情进度，不能再次开演；请使用 /团 继续"
            )
        preflight = await self.opening_preflight(session_id)
        if not preflight["ok"]:
            return {"started": False, **preflight}
        return await self._run(
            self._activate_story,
            session_id,
            actor_id,
            resume,
            [dict(item) for item in (choices or ())],
        )

    def _activate_story(
        self,
        session_id: str,
        actor_id: str,
        resume: bool,
        supplied_choices: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_PREPARING:
                    raise InvalidTransitionError("副本当前不在准备阶段")
                if not resume and int(session["turn_no"] or 0) > 0:
                    raise InvalidTransitionError(
                        "该副本已有剧情进度，不能再次开演；请使用 /团 继续"
                    )
                config = connection.execute(
                    """
                    SELECT * FROM instance_configs WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(config["world_snapshot_json"], {})
                limits = player_limits(world)
                participants = connection.execute(
                    """
                    SELECT pt.*, cv.profile_json AS card_profile_json
                    FROM participants pt
                    LEFT JOIN character_card_versions cv
                      ON cv.id = pt.character_version_id
                    WHERE session_id = ? AND card_status = 'approved'
                      AND ready = 1 AND participation_status = 'active'
                    ORDER BY pt.created_at
                    """,
                    (session_id,),
                ).fetchall()
                blockers: list[str] = []
                if len(participants) < limits["minimum_start"]:
                    blockers.append(
                        f"有效出场人数不足：{len(participants)}/{limits['minimum_start']}"
                    )
                if blockers:
                    connection.execute("ROLLBACK")
                    return {
                        "started": False,
                        "ok": False,
                        "blockers": blockers,
                        "participants": [
                            self._participant(row) for row in participants
                        ],
                        "limits": limits,
                    }

                ai_rows = connection.execute(
                    """
                    SELECT a.*, i.mode, i.status AS instance_status,
                           i.frozen_profile_json
                    FROM actors a
                    JOIN ai_companion_instances i ON i.actor_id=a.id
                    WHERE a.session_id=? AND a.actor_kind='ai_companion'
                      AND a.status='active' AND i.status<>'retired'
                    ORDER BY a.created_at, a.id
                    """,
                    (session_id,),
                ).fetchall()
                ai_actors = [
                    self._ai_turn_actor(dict(row)) for row in ai_rows
                ]
                order = [
                    str(row["group_user_id"]) for row in participants
                ] + [str(item["actor_ref"]) for item in ai_actors]
                stored_state = json_load(session["world_state_json"], {})
                decision = connection.execute(
                    "SELECT * FROM session_opening_decisions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                squad = [
                    {
                        **self._participant(row),
                        "card_profile": json_load(
                            row["card_profile_json"] if "card_profile_json" in row.keys() else "",
                            {},
                        ),
                    }
                    for row in participants
                ]
                if not resume and decision is None:
                    seed = int(
                        hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16],
                        16,
                    )
                    selected = select_opening_scenario(world, squad, seed=seed)
                    candidates = recommend_opening_scenarios(world, squad)
                    scene_ref = str(selected.get("opening_scene_ref") or "")
                    reasons = list(selected.get("opening_selection_reasons") or [])
                    now = utc_now()
                    connection.execute(
                        """
                        INSERT INTO session_opening_decisions(
                            session_id, world_id, world_revision,
                            algorithm_version, seed, candidates_json,
                            selected_scene_ref, selected_reason,
                            selection_source, frozen, revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'profile-score-v1', ?, ?, ?, ?, ?, 0, 1, ?, ?)
                        """,
                        (
                            session_id,
                            session["world_id"],
                            int(config["world_revision"] or 1),
                            str(seed),
                            json_dump(candidates),
                            scene_ref,
                            "；".join(str(item) for item in reasons),
                            "recommended" if candidates else "defaulted",
                            now,
                            now,
                        ),
                    )
                    decision = connection.execute(
                        "SELECT * FROM session_opening_decisions WHERE session_id=?",
                        (session_id,),
                    ).fetchone()
                if decision is not None:
                    selected_scene_ref = str(decision["selected_scene_ref"] or "")
                    if selected_scene_ref:
                        stored_state["current_scene"] = selected_scene_ref
                existing_turn = turn_state_from_world(
                    stored_state,
                    allowed_user_ids=set(order),
                )
                if resume:
                    existing_order = [
                        item for item in existing_turn["order"] if item in order
                    ]
                    order = existing_order + [
                        item for item in order if item not in existing_order
                    ]
                turn_state = replace_turn_order(existing_turn, order)
                turn_state["current_user_id"] = (
                    existing_turn["current_user_id"]
                    if resume
                    and existing_turn["current_user_id"] in order
                    else order[0]
                )
                persisted_state = embed_turn_state(
                    public_world_state(stored_state),
                    turn_state,
                )
                current_user_id = turn_state["current_user_id"]
                current_participant = next(
                    (
                        row
                        for row in participants
                        if row["group_user_id"] == current_user_id
                    ),
                    None,
                )
                current_ai = next(
                    (
                        item
                        for item in ai_actors
                        if item["actor_ref"] == current_user_id
                    ),
                    None,
                )
                current_actor = (
                    {
                        **self._participant(current_participant),
                        "actor_kind": "human",
                        "actor_ref": "",
                    }
                    if current_participant is not None
                    else current_ai
                )
                if current_actor is None:
                    raise InvalidTransitionError("当前行动 actor 不存在")
                preserved_choice = (
                    connection.execute(
                        """
                        SELECT * FROM choice_sets
                        WHERE session_id = ?
                          AND (
                            participant_id IS ?
                            OR actor_id IS ?
                          )
                          AND status = 'active'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (
                            session_id,
                            (
                                current_participant["id"]
                                if current_participant is not None
                                else None
                            ),
                            (
                                current_ai["actor_id"]
                                if current_ai is not None
                                else None
                            ),
                        ),
                    ).fetchone()
                    if resume
                    else None
                )
                preserved_vote = (
                    connection.execute(
                        """
                        SELECT * FROM group_votes
                        WHERE session_id = ? AND status = 'open'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    if resume
                    else None
                )
                selected_choices: list[dict[str, Any]] = []
                if not preserved_choice and not preserved_vote:
                    selected_choices = (
                        normalize_choices(supplied_choices)
                        if supplied_choices
                        else (
                            fallback_choices(stored_state)
                            if resume
                            else opening_choices(world)
                        )
                    )
                now = utc_now()
                connection.execute(
                    """
                    UPDATE timer_instances
                    SET status = 'cancelled', updated_at = ?
                    WHERE session_id = ? AND timer_type = 'preparation'
                      AND status IN ('active', 'paused')
                    """,
                    (now, session_id),
                )
                connection.execute(
                    """
                    UPDATE sessions SET
                        state = CASE
                            WHEN state = 'running' THEN 'paused'
                            ELSE state
                        END,
                        selected = 0,
                        revision = CASE
                            WHEN state = 'running' THEN revision + 1
                            ELSE revision
                        END,
                        updated_at = CASE
                            WHEN state = 'running' THEN ?
                            ELSE updated_at
                        END
                    WHERE platform_id = ? AND group_id = ? AND id <> ?
                    """,
                    (
                        now,
                        session["platform_id"],
                        session["group_id"],
                        session_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE sessions SET
                        state = 'running', selected = 1,
                        world_state_json = ?, revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (json_dump(persisted_state), now, session_id),
                )
                connection.execute(
                    """
                    UPDATE session_opening_decisions SET
                        frozen=1,
                        frozen_at=CASE
                            WHEN frozen_at='' THEN ? ELSE frozen_at
                        END,
                        updated_at=?
                    WHERE session_id=?
                    """,
                    (now, now, session_id),
                )
                new_revision = int(session["revision"]) + 1
                time_rules = normalize_time_rules(
                    json_load(config["time_rules_json"], {})
                )
                choice_id = ""
                choice_row: sqlite3.Row | None = None
                if preserved_vote:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                elif preserved_choice:
                    choice_id = str(preserved_choice["id"])
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET session_revision = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_revision, now, choice_id),
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                else:
                    connection.execute(
                        """
                        UPDATE choice_sets
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ? AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    choice_id = new_id("choices")
                    connection.execute(
                        """
                        INSERT INTO choice_sets(
                            id, session_id, participant_id, actor_id, round_no,
                            session_revision, choices_json, status,
                            reroll_count, idempotency_key, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
                        """,
                        (
                            choice_id,
                            session_id,
                            (
                                current_participant["id"]
                                if current_participant is not None
                                else None
                            ),
                            (
                                current_ai["actor_id"]
                                if current_ai is not None
                                else None
                            ),
                            turn_state["round_no"],
                            new_revision,
                            json_dump(selected_choices),
                            f"opening:{session_id}:{new_revision}",
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'cancelled', updated_at = ?
                        WHERE session_id = ?
                          AND status IN ('active', 'paused')
                        """,
                        (now, session_id),
                    )
                    self._create_timer(
                        connection,
                        session_id=session_id,
                        participant_id=(
                            str(current_participant["id"])
                            if current_participant is not None
                            else ""
                        ),
                        timer_type="turn",
                        timeout_seconds=time_rules["turn_timeout_seconds"],
                        reminder_seconds=time_rules["turn_reminder_seconds"],
                        action={
                            "choice_set_id": choice_id,
                            "user_id": current_user_id,
                            "actor_ref": str(
                                (current_ai or {}).get("actor_ref") or ""
                            ),
                        },
                    )
                    choice_row = connection.execute(
                        "SELECT * FROM choice_sets WHERE id = ?",
                        (choice_id,),
                    ).fetchone()
                opening_projection = project_opening_scene(
                    world,
                    stored_state,
                    squad=squad,
                )
                opening = (
                    clean_text(
                        opening_projection.get("opening_text")
                        or world.get("opening_scene"),
                        max_chars=6000,
                    )
                    if not resume and session["turn_no"] == 0
                    else ""
                )
                if opening:
                    append_event(
                        connection,
                        session_id=session_id,
                        turn_no=session["turn_no"],
                        role="system",
                        actor_id="system",
                        actor_name="开团系统",
                        content=opening,
                        meta={"kind": "opening"},
                        created_at=now,
                    )
                phase_meta = json_load(config["phase_meta_json"], {})
                phase_meta.update(
                    {
                        "resume_mode": bool(resume),
                        "started_at": now,
                        "frozen_roster": [
                            str(row["id"]) for row in participants
                        ],
                    }
                )
                connection.execute(
                    """
                    UPDATE instance_configs
                    SET phase_meta_json = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (json_dump(phase_meta), now, session_id),
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "session.perform" if not resume else "session.continue",
                    session_id,
                    {
                        "roster": order,
                        "choice_set_id": choice_id,
                    },
                )
                updated = connection.execute(
                    """
                    SELECT s.*, w.name AS world_name, w.slug AS world_slug
                    FROM sessions s JOIN worlds w ON w.id = s.world_id
                    WHERE s.id = ?
                    """,
                    (session_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "started": True,
                    "ok": True,
                    "session": self._session(updated),
                    "choice_set": (
                        self._choice_set(choice_row)
                        if choice_row
                        else None
                    ),
                    "vote": (
                        self._vote(preserved_vote)
                        if preserved_vote
                        else None
                    ),
                    "current_participant": current_actor,
                    "current_actor": current_actor,
                    "participants": [
                        self._participant(row) for row in participants
                    ],
                    "actors": [
                        *[
                            {
                                **self._participant(row),
                                "actor_kind": "human",
                                "actor_ref": "",
                            }
                            for row in participants
                        ],
                        *ai_actors,
                    ],
                    "gameplay_brief": (
                        dict(world.get("gameplay_brief"))
                        if isinstance(world.get("gameplay_brief"), Mapping)
                        else None
                    ),
                    "opening_decision": (
                        self._opening_public_view(dict(decision))
                        if decision is not None
                        else None
                    ),
                    "opening": opening,
                }
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
