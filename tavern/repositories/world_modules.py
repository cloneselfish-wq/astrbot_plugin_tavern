from __future__ import annotations

from .worlds_support import *


class WorldModulesRepositoryMixin:
    def _commit_vnext_workflow(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        new_turn: int,
        acting_round: int,
        next_turn_state: Mapping[str, Any],
        player_user_id: str,
        player_event_id: str,
        narrator_event_id: str,
        world_state: Mapping[str, Any],
        check_payload: Mapping[str, Any],
        workflow: Mapping[str, Any],
        world: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        """Persist choices, votes, rolls, events and timers in the turn TX."""

        result: dict[str, Any] = {}
        if not workflow:
            return result

        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
            """,
            (session["id"], player_user_id),
        ).fetchone()
        workflow_actor_id = str(workflow.get("actor_id") or "")
        actor = (
            connection.execute(
                """
                SELECT * FROM actors
                WHERE id=? AND session_id=? AND actor_kind='ai_companion'
                  AND status='active'
                """,
                (workflow_actor_id, session["id"]),
            ).fetchone()
            if workflow_actor_id
            else None
        )
        if not participant and not actor:
            raise InvalidTransitionError("当前行动 actor 没有有效的副本记录")

        choice_set_id = str(workflow.get("choice_set_id") or "")
        selected_key = str(workflow.get("selected_key") or "").upper()
        flavor_text = clean_text(
            workflow.get("flavor_text"),
            max_chars=160,
        )
        if not choice_set_id or selected_key not in CHOICE_KEYS:
            raise ValueError("缺少有效的选项提交信息")
        choice_row = connection.execute(
            """
            SELECT * FROM choice_sets
            WHERE id = ? AND session_id = ? AND status = 'active'
            """,
            (choice_set_id, session["id"]),
        ).fetchone()
        if not choice_row:
            raise DatabaseConflictError("当前选项已经失效，请重新查看回合")
        if participant is not None:
            if choice_row["participant_id"] != participant["id"]:
                raise PermissionError("该选项不属于当前玩家")
        elif choice_row["actor_id"] != actor["id"]:
            raise PermissionError("该选项不属于当前 AI 队友")
        if int(choice_row["session_revision"]) != int(session["revision"]):
            raise DatabaseConflictError("场景已变化，旧选项不能继续使用")
        choices = normalize_choices(json_load(choice_row["choices_json"], []))
        selected = next(
            item for item in choices if item["key"] == selected_key
        )
        connection.execute(
            """
            UPDATE choice_sets SET
                status = 'selected', selected_key = ?,
                flavor_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (selected_key, flavor_text, now, choice_set_id),
        )
        connection.execute(
            """
            UPDATE timer_instances
            SET status = 'completed', updated_at = ?
            WHERE session_id = ? AND participant_id = ?
              AND timer_type = 'turn' AND status = 'active'
            """,
            (
                now,
                session["id"],
                str(participant["id"]) if participant is not None else "",
            ),
        )
        if participant is not None:
            connection.execute(
                """
                UPDATE participants
                SET consecutive_timeouts = 0, updated_at = ?
                WHERE id = ?
                """,
                (now, participant["id"]),
            )
        result["choice"] = {
            "choice_set_id": choice_set_id,
            "key": selected_key,
            "text": selected["text"],
        }

        if check_payload:
            roll_id = new_id("roll")
            connection.execute(
                """
                INSERT INTO rolls(
                    id, session_id, choice_set_id, participant_id,
                    roll_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    roll_id,
                    session["id"],
                    choice_set_id,
                    (
                        participant["id"]
                        if participant is not None
                        else None
                    ),
                    json_dump(dict(check_payload)),
                    now,
                ),
            )
            result["roll_id"] = roll_id

        config = connection.execute(
            """
            SELECT * FROM instance_configs WHERE session_id = ?
            """,
            (session["id"],),
        ).fetchone()
        world = json_load(
            config["world_snapshot_json"] if config else "",
            {},
        )
        time_rules = normalize_time_rules(
            json_load(config["time_rules_json"] if config else "", {})
        )
        round_completed = int(next_turn_state["round_no"]) > acting_round
        if round_completed:
            selected_event = self._select_world_event(
                connection,
                session_id=session["id"],
                round_no=acting_round,
                world=world,
                turn_no=new_turn,
                now=now,
            )
            if selected_event:
                result["world_event"] = selected_event

        return_progress = workflow.get("return_progress")
        if isinstance(return_progress, Mapping):
            request_id = str(return_progress.get("request_id") or "")
            evidence = clean_text(
                return_progress.get("evidence"),
                max_chars=500,
            )
            if request_id and evidence:
                progress_result = self._record_return_progress(
                    connection,
                    session_id=session["id"],
                    request_id=request_id,
                    evidence=evidence,
                    completed=bool(return_progress.get("completed", False)),
                    round_no=int(next_turn_state["round_no"]),
                    turn_no=new_turn,
                    now=now,
                )
                if progress_result:
                    result["return_progress"] = progress_result

        next_user_id = str(next_turn_state["current_user_id"] or "")
        next_participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], next_user_id),
        ).fetchone()
        next_actor = None
        if not next_participant:
            for candidate in connection.execute(
                """
                SELECT a.*, i.frozen_profile_json, i.mode,
                       i.status AS instance_status
                FROM actors a
                JOIN ai_companion_instances i ON i.actor_id=a.id
                WHERE a.session_id=? AND a.actor_kind='ai_companion'
                  AND a.status='active' AND i.status<>'retired'
                """,
                (session["id"],),
            ).fetchall():
                candidate_ref = (
                    "public:actor:"
                    + hashlib.sha256(
                        str(candidate["id"]).encode("utf-8")
                    ).hexdigest()[:12].upper()
                )
                if candidate_ref == next_user_id:
                    next_actor = candidate
                    break
        if not next_participant and not next_actor:
            result["next_choice_set_id"] = ""
            return result

        group_decision = workflow.get("group_decision")
        if isinstance(group_decision, Mapping):
            question = clean_text(
                group_decision.get("question"),
                max_chars=500,
            )
            options = self._normalize_vote_options(
                group_decision.get("options")
            )
            if question and len(options) >= 2:
                eligible = [
                    str(row["group_user_id"])
                    for row in connection.execute(
                        """
                        SELECT group_user_id FROM participants
                        WHERE session_id = ?
                          AND participation_status = 'active'
                          AND card_status = 'approved'
                        GROUP BY group_user_id
                        ORDER BY MIN(created_at)
                        """,
                        (session["id"],),
                    ).fetchall()
                ]
                ai_vote = self._ai_vote_projection_locked(
                    connection,
                    session_id=session["id"],
                    world=world,
                )
                eligible.extend(ai_vote["eligible_refs"])
                vote_id = new_id("vote")
                initial_result = {
                    "ai_vote_policy": ai_vote["policy"],
                }
                if ai_vote.get("advisory"):
                    initial_result["ai_advisory"] = ai_vote["advisory"]
                connection.execute(
                    """
                    INSERT INTO group_votes(
                        id, session_id, source_event_id, question,
                        options_json, eligible_user_ids_json, stage,
                        status, suspended_user_id, deadline_at,
                        result_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?, ?, ?, ?)
                    """,
                    (
                        vote_id,
                        session["id"],
                        narrator_event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        next_user_id,
                        deadline_after(
                            time_rules["vote_round_one_seconds"]
                        ),
                        json_dump(initial_result),
                        now,
                        now,
                    ),
                )
                for ballot in ai_vote["automatic_ballots"]:
                    connection.execute(
                        """
                        INSERT INTO vote_ballots(
                            id, vote_id, user_id, option_key,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(vote_id, user_id) DO NOTHING
                        """,
                        (
                            new_id("ballot"),
                            vote_id,
                            ballot["user_id"],
                            ballot["option_key"],
                            now,
                            now,
                        ),
                    )
                self._create_timer(
                    connection,
                    session_id=session["id"],
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules["vote_round_one_seconds"],
                    reminder_seconds=time_rules["vote_reminder_seconds"],
                    action={"vote_id": vote_id, "stage": 1},
                )
                result["vote_id"] = vote_id
                return result

        next_choices_raw = workflow.get("next_choices")
        recovery = (
            dict(workflow.get("choice_recovery_receipt") or {})
            if isinstance(workflow.get("choice_recovery_receipt"), Mapping)
            else {}
        )
        try:
            next_choices = normalize_choices(next_choices_raw, world)
        except ValueError:
            next_choices = fallback_choices(world_state, world)
            result["choice_fallback"] = True
            if not recovery:
                operation_id = str(workflow.get("operation_id") or "")
                recovery = {
                    "status": "fallback",
                    "failure_kind": "commit_validation_failed",
                    "repair_count": 0,
                    "fallback_version": "choices-fallback/1.0.0-rc10",
                    "provider_class": "none",
                    "message": (
                        "选项在提交前未通过校验，系统已改用安全兜底；"
                        "世界状态仍按本轮原子事务提交。"
                    ),
                    "trace_id": hashlib.sha256(
                        operation_id.encode("utf-8")
                    ).hexdigest()[:8].upper(),
                    "idempotency_key": f"{operation_id}:choice-recovery",
                    "resolution_summary": {"choice_count": len(next_choices)},
                }
        choice_id = new_id("choices")
        connection.execute(
            """
            INSERT INTO choice_sets(
                id, session_id, participant_id, actor_id, round_no,
                session_revision, choices_json, status, reroll_count,
                idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?)
            """,
            (
                choice_id,
                session["id"],
                (
                    next_participant["id"]
                    if next_participant is not None
                    else None
                ),
                next_actor["id"] if next_actor is not None else None,
                next_turn_state["round_no"],
                int(session["revision"]) + 1,
                json_dump(next_choices),
                f"turn:{session['id']}:{new_turn + 1}",
                now,
                now,
            ),
        )
        self._create_timer(
            connection,
            session_id=session["id"],
            participant_id=(
                str(next_participant["id"])
                if next_participant is not None
                else ""
            ),
            timer_type="turn",
            timeout_seconds=time_rules["turn_timeout_seconds"],
            reminder_seconds=time_rules["turn_reminder_seconds"],
            action={
                "choice_set_id": choice_id,
                "user_id": next_user_id,
                "actor_ref": next_user_id if next_actor is not None else "",
            },
        )
        result["next_choice_set_id"] = choice_id
        result["next_participant_id"] = (
            next_participant["id"] if next_participant is not None else ""
        )
        result["next_actor_id"] = (
            next_actor["id"] if next_actor is not None else ""
        )
        if recovery:
            operation_id = str(
                recovery.get("operation_id")
                or workflow.get("operation_id")
                or ""
            )
            result["choice_recovery_receipt"] = self._choice_recovery_view(
                self._insert_choice_recovery_locked(
                    connection,
                    session_id=str(session["id"]),
                    choice_set_id=choice_id,
                    operation_id=operation_id,
                    recovery=recovery,
                    now=now,
                )
            )
        return result

    @staticmethod
    def _normalize_vote_options(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes),
        ):
            return []
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, item in enumerate(value[:4]):
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("key") or CHOICE_KEYS[index]).upper()
            text = clean_text(item.get("text"), max_chars=240)
            if key not in CHOICE_KEYS or key in seen or not text:
                continue
            seen.add(key)
            entry: dict[str, Any] = {"key": key, "text": text}
            # 0.11.4：透传「同意执行」选项上声明的检定定义，
            # 供表决通过后按该检定执行（如全队行动的 魔力 DC17）。
            if isinstance(item.get("check"), Mapping):
                entry["check"] = dict(item["check"])
            # 透传「暂缓」标记——表决通过但选择暂缓时，
            # 引擎不调用模型，直接把行动权与兜底选项归还被挂起玩家。
            if bool(item.get("declines_action")):
                entry["declines_action"] = True
            result.append(entry)
        return result

    def _select_world_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        round_no: int,
        world: Mapping[str, Any],
        turn_no: int,
        now: str,
    ) -> dict[str, Any] | None:
        if connection.execute(
            """
            SELECT 1 FROM selected_world_events
            WHERE session_id = ? AND round_no = ?
            """,
            (session_id, round_no),
        ).fetchone():
            return None
        rules = world.get("rules")
        rules = rules if isinstance(rules, Mapping) else {}
        pool = rules.get("event_pool")
        if not isinstance(pool, Sequence) or isinstance(pool, (str, bytes)):
            return None
        session_row = connection.execute(
            """
            SELECT world_state_json FROM sessions WHERE id = ?
            """,
            (session_id,),
        ).fetchone()
        state = json_load(
            session_row["world_state_json"] if session_row else "",
            {},
        )
        location = str(state.get("location") or "").casefold()
        facts = {
            (
                str(item.get("text") or item.get("content") or item.get("fact") or "")
                if isinstance(item, dict)
                else str(item)
            ).casefold()
            for item in (
                state.get("facts")
                if isinstance(state.get("facts"), list)
                else []
            )
        }
        active_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM participants
                WHERE session_id = ? AND participation_status = 'active'
                  AND card_status = 'approved'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        candidates: list[tuple[dict[str, Any], int]] = []
        for raw in pool[:200]:
            if not isinstance(raw, Mapping):
                continue
            item_id = clean_text(raw.get("id"), max_chars=80)
            description = clean_text(
                raw.get("description"),
                max_chars=1000,
            )
            if not item_id or not description:
                continue
            minimum_round = bounded_int(
                raw.get("minimum_round"),
                1,
                1,
                1_000_000,
            )
            if round_no < minimum_round:
                continue
            conditions = raw.get("conditions")
            conditions = (
                conditions if isinstance(conditions, Mapping) else {}
            )
            allowed_locations = conditions.get("locations")
            if isinstance(allowed_locations, Sequence) and not isinstance(
                allowed_locations,
                (str, bytes),
            ):
                normalized_locations = {
                    str(item).casefold()
                    for item in allowed_locations
                    if str(item).strip()
                }
                if normalized_locations and location not in normalized_locations:
                    continue
            required_facts = conditions.get("required_facts")
            if isinstance(required_facts, Sequence) and not isinstance(
                required_facts,
                (str, bytes),
            ):
                required = {
                    str(item).casefold()
                    for item in required_facts
                    if str(item).strip()
                }
                if not required.issubset(facts):
                    continue
            excluded_facts = conditions.get("excluded_facts")
            if isinstance(excluded_facts, Sequence) and not isinstance(
                excluded_facts,
                (str, bytes),
            ):
                excluded = {
                    str(item).casefold()
                    for item in excluded_facts
                    if str(item).strip()
                }
                if excluded.intersection(facts):
                    continue
            minimum_players = bounded_int(
                conditions.get("minimum_players"),
                0,
                0,
                40,
            )
            if active_count < minimum_players:
                continue
            maximum_players = conditions.get("maximum_players")
            if (
                maximum_players not in {None, ""}
                and active_count
                > bounded_int(maximum_players, 40, 0, 40)
            ):
                continue
            previous = connection.execute(
                """
                SELECT round_no FROM selected_world_events
                WHERE session_id = ? AND pool_item_id = ?
                ORDER BY round_no DESC LIMIT 1
                """,
                (session_id, item_id),
            ).fetchone()
            if previous and bool(raw.get("once", False)):
                continue
            cooldown = bounded_int(
                raw.get("cooldown_rounds"),
                0,
                0,
                1_000_000,
            )
            if previous and round_no - int(previous["round_no"]) <= cooldown:
                continue
            weight = bounded_int(raw.get("weight"), 1, 1, 1000)
            candidates.append((dict(raw), weight))
        if not candidates:
            return None
        total = sum(weight for _, weight in candidates)
        pick = secrets.randbelow(total)
        selected = candidates[-1][0]
        for item, weight in candidates:
            if pick < weight:
                selected = item
                break
            pick -= weight
        event_id = new_id("worldevent")
        item_id = clean_text(selected.get("id"), max_chars=80)
        description = clean_text(
            selected.get("description"),
            max_chars=1000,
        )
        payload = {
            "id": item_id,
            "title": clean_text(selected.get("title"), max_chars=120),
            "description": description,
            "severity": clean_text(
                selected.get("severity") or "standard",
                max_chars=30,
            ),
        }
        connection.execute(
            """
            INSERT INTO selected_world_events(
                id, session_id, round_no, pool_item_id, payload_json,
                status, narrative, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'narrated', ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                round_no,
                item_id,
                json_dump(payload),
                description,
                now,
                now,
            ),
        )
        _insert_world_event(
            connection,
            event_id=new_id("event"),
            session_id=session_id,
            turn_no=turn_no,
            actor_id="world",
            actor_name="世界脉冲",
            content=description,
            meta={
                "kind": "world_pulse",
                "selected_world_event_id": event_id,
                "round_no": round_no,
            },
            created_at=now,
        )
        return {"id": event_id, **payload}
