from __future__ import annotations

from .workflow_support import *
from ..contracts.narrative_document import NarrativeDocument


class TurnQueueRepositoryMixin:
    async def active_vote(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self._run(self._active_vote, session_id)

    def _active_vote(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM group_votes
                WHERE session_id = ? AND status = 'open'
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if not row:
                return None
            result = self._vote(row)
            ballots = connection.execute(
                """
                SELECT user_id, option_key, created_at, updated_at
                FROM vote_ballots WHERE vote_id = ?
                ORDER BY created_at
                """,
                (row["id"],),
            ).fetchall()
            result["ballots"] = [dict(item) for item in ballots]
            tally = vote_result(
                eligible_count=len(result["eligible_user_ids"]),
                ballots=result["ballots"],
                option_keys=[
                    str(item.get("key")) for item in result["options"]
                ],
            )
            result["tally"] = tally
            return result

    async def create_group_vote(
        self,
        session_id: str,
        *,
        group_decision: Mapping[str, Any],
        suspended_user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        """0.11.2：为「全队行动」选项直接发起集体表决。

        旧流程要求叙事模型在 resolve 里自行生成 group_decision，
        模型未生成时整轮被拒（“该选项影响全队，但模型没有生成集体表决”）。
        现在由引擎在选项被选中时调用本方法，立即进入全员投票。
        """
        return await self._run(
            self._create_group_vote,
            session_id,
            dict(group_decision),
            suspended_user_id,
            actor_id,
        )

    def _create_group_vote(
        self,
        session_id: str,
        group_decision: dict[str, Any],
        suspended_user_id: str,
        actor_id: str,
    ) -> dict[str, Any]:
        question = clean_text(
            group_decision.get("question"),
            max_chars=500,
        )
        options = self._normalize_vote_options(
            group_decision.get("options")
        )
        if not question or len(options) < 2:
            raise ValueError("集体表决需要问题与至少两个选项")
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
                if not session:
                    raise DatabaseNotFoundError("会话不存在")
                if session["state"] != SESSION_RUNNING:
                    raise InvalidTransitionError("酒馆当前不在运行状态")
                if connection.execute(
                    """
                    SELECT 1 FROM group_votes
                    WHERE session_id = ? AND status = 'open'
                    """,
                    (session_id,),
                ).fetchone():
                    raise InvalidTransitionError("已有一场集体表决进行中")
                suspended_user_id = validate_platform_id(
                    suspended_user_id,
                    label="行动玩家 ID",
                )
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
                        (session_id,),
                    ).fetchall()
                ]
                config_row = connection.execute(
                    """
                    SELECT time_rules_json, world_snapshot_json
                    FROM instance_configs
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                world = json_load(
                    config_row["world_snapshot_json"]
                    if config_row
                    else "",
                    {},
                )
                ai_vote = self._ai_vote_projection_locked(
                    connection,
                    session_id=session_id,
                    world=world,
                )
                eligible.extend(ai_vote["eligible_refs"])
                time_rules = normalize_time_rules(
                    json_load(
                        config_row["time_rules_json"] if config_row else "",
                        {},
                    )
                )
                vote_id = new_id("vote")
                event_id = new_id("event")
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
                        session_id,
                        event_id,
                        question,
                        json_dump(options),
                        json_dump(eligible),
                        suspended_user_id,
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
                event_id = append_event(
                    connection,
                    event_id=event_id,
                    session_id=session_id,
                    turn_no=session["turn_no"],
                    role="system",
                    actor_id="vote",
                    actor_name="集体表决",
                    content=f"【集体表决】{question}",
                    meta={
                        "kind": "group_vote",
                        "vote_id": vote_id,
                        "status": "open",
                    },
                    created_at=now,
                )
                self._create_timer(
                    connection,
                    session_id=session_id,
                    participant_id="",
                    timer_type="vote",
                    timeout_seconds=time_rules[
                        "vote_round_one_seconds"
                    ],
                    reminder_seconds=time_rules[
                        "vote_reminder_seconds"
                    ],
                    action={"vote_id": vote_id, "stage": 1},
                )
                self._insert_audit(
                    connection,
                    session_id,
                    actor_id,
                    "vote.created",
                    vote_id,
                    {"question": question},
                )
                vote_row = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote_id,),
                ).fetchone()
                connection.execute("COMMIT")
                return self._vote(vote_row)
            except Exception:
                connection.execute("ROLLBACK")
                raise

    async def cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        return await self._run(
            self._cast_vote,
            session_id,
            user_id,
            option_key,
        )

    def _cast_vote(
        self,
        session_id: str,
        user_id: str,
        option_key: str,
    ) -> dict[str, Any]:
        key = str(option_key or "").strip().upper()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                vote_row = connection.execute(
                    """
                    SELECT * FROM group_votes
                    WHERE session_id = ? AND decision_status = 'collecting'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if not vote_row:
                    raise DatabaseNotFoundError("当前没有进行中的集体投票")
                vote = self._vote(vote_row)
                # A16：代理投票——被托管人可代替角色投票，票记在角色名下。
                operator_id = user_id
                effective_user_id = user_id
                now_check = utc_now()
                grant = connection.execute(
                    """
                    SELECT d.owner_user_id, d.source, d.permissions_json
                    FROM delegation_grants d
                    WHERE d.session_id = ? AND d.delegate_user_id = ?
                      AND d.status = 'active'
                      AND d.expires_at IN ('', ?)
                      AND 'vote' IN (
                          SELECT value FROM json_each(d.permissions_json)
                      )
                    ORDER BY d.created_at DESC LIMIT 1
                    """,
                    (session_id, user_id, now_check),
                ).fetchone()
                if grant and str(grant["owner_user_id"]) in vote["eligible_user_ids"]:
                    effective_user_id = str(grant["owner_user_id"])
                if effective_user_id not in vote["eligible_user_ids"]:
                    raise PermissionError("你不在本次投票的有效成员名单中")
                if effective_user_id != user_id:
                    self._insert_audit(
                        connection,
                        session_id,
                        user_id,
                        "delegation.vote",
                        str(vote_row["id"]),
                        {
                            "operator_user_id": operator_id,
                            "ballot_user_id": effective_user_id,
                            "source": str(grant["source"]),
                        },
                    )
                valid_keys = {
                    str(item.get("key")) for item in vote["options"]
                }
                if key not in valid_keys:
                    raise ValueError(
                        "请选择：" + " / ".join(sorted(valid_keys))
                    )
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO vote_ballots(
                        id, vote_id, user_id, option_key,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vote_id, user_id) DO UPDATE SET
                        option_key = excluded.option_key,
                        updated_at = excluded.updated_at
                    """,
                    (
                        new_id("ballot"),
                        vote["id"],
                        effective_user_id,
                        key,
                        now,
                        now,
                    ),
                )
                ballots = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT user_id, option_key FROM vote_ballots
                        WHERE vote_id = ?
                        """,
                        (vote["id"],),
                    ).fetchall()
                ]
                tally = vote_result(
                    eligible_count=len(vote["eligible_user_ids"]),
                    ballots=ballots,
                    option_keys=sorted(valid_keys),
                )
                status = "open"
                winner = str(tally["winner"] or "")
                stage = int(vote["stage"])
                options = vote["options"]
                if winner:
                    status = "decided"
                elif tally["all_voted"] and tally["quorum"]:
                    counts = tally["counts"]
                    ranking = sorted(
                        options,
                        key=lambda item: (
                            -int(counts.get(str(item.get("key")), 0)),
                            str(item.get("key")),
                        ),
                    )
                    if stage == 1 and len(ranking) > 2:
                        top_count = int(
                            counts.get(str(ranking[0].get("key")), 0)
                        )
                        tied_top = [
                            item
                            for item in ranking
                            if int(counts.get(str(item.get("key")), 0))
                            == top_count
                        ]
                        runoff = (
                            tied_top[:2]
                            if len(tied_top) >= 2
                            else ranking[:2]
                        )
                        connection.execute(
                            "DELETE FROM vote_ballots WHERE vote_id = ?",
                            (vote["id"],),
                        )
                        config = connection.execute(
                            """
                            SELECT time_rules_json FROM instance_configs
                            WHERE session_id = ?
                            """,
                            (session_id,),
                        ).fetchone()
                        time_rules = normalize_time_rules(
                            json_load(
                                config["time_rules_json"] if config else "",
                                {},
                            )
                        )
                        new_deadline = deadline_after(
                            time_rules["vote_round_two_seconds"]
                        )
                        connection.execute(
                            """
                            UPDATE group_votes SET
                                options_json = ?, stage = 2,
                                deadline_at = ?, result_json = ?,
                                updated_at = ?
                            WHERE id = ?
                            """,
                            (
                                json_dump(runoff),
                                new_deadline,
                                json_dump(
                                    {
                                        "round_one": tally,
                                        "reason": "runoff",
                                    }
                                ),
                                now,
                                vote["id"],
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE timer_instances
                            SET status = 'completed', updated_at = ?
                            WHERE session_id = ? AND timer_type = 'vote'
                              AND status = 'active'
                            """,
                            (now, session_id),
                        )
                        self._create_timer(
                            connection,
                            session_id=session_id,
                            participant_id="",
                            timer_type="vote",
                            timeout_seconds=time_rules[
                                "vote_round_two_seconds"
                            ],
                            reminder_seconds=time_rules[
                                "vote_reminder_seconds"
                            ],
                            action={"vote_id": vote["id"], "stage": 2},
                        )
                        self._insert_audit(
                            connection,
                            session_id,
                            user_id,
                            "vote.runoff",
                            vote["id"],
                            {"tally": tally},
                        )
                        updated_vote = connection.execute(
                            "SELECT * FROM group_votes WHERE id = ?",
                            (vote["id"],),
                        ).fetchone()
                        connection.execute("COMMIT")
                        return {
                            "vote": self._vote(updated_vote),
                            "tally": tally,
                            "resolved": False,
                            "runoff": True,
                        }
                    status = "rejected"

                if status != "open":
                    session = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                    if session is None:
                        raise DatabaseNotFoundError("会话不存在")
                    operation_id = (
                        f"vote-resolution:{vote['id']}"
                        if status == "decided"
                        else ""
                    )
                    resolution_status = (
                        "pending" if status == "decided" else "not_started"
                    )
                    decision_status = (
                        "decided" if status == "decided" else "rejected"
                    )
                    connection.execute(
                        """
                        UPDATE group_votes SET
                            status = ?, winner_key = ?,
                            decision_status = ?, resolution_status = ?,
                            resolution_operation_id = ?,
                            decision_revision = ?, decided_at = ?,
                            result_json = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            status,
                            winner,
                            decision_status,
                            resolution_status,
                            operation_id,
                            int(session["revision"] or 0),
                            now,
                            json_dump(tally),
                            now,
                            vote["id"],
                        ),
                    )
                    if operation_id:
                        request_payload = {
                            "vote_id": str(vote["id"]),
                            "winner_key": winner,
                            "decision_revision": int(session["revision"] or 0),
                            "suspended_user_id": str(
                                vote.get("suspended_user_id") or ""
                            ),
                        }
                        connection.execute(
                            """
                            INSERT INTO operation_receipts(
                                operation_id, session_id, operation_type,
                                request_json, result_json, status, phase,
                                lease_expires_at, input_hash,
                                created_at, updated_at
                            ) VALUES (?, ?, 'vote_resolution', ?, ?,
                                      'reserved', 'decision_locked', '', ?, ?, ?)
                            ON CONFLICT(operation_id) DO NOTHING
                            """,
                            (
                                operation_id,
                                session_id,
                                json_dump(request_payload),
                                json_dump(
                                    {
                                        "phase": "decision_locked",
                                        "vote_id": str(vote["id"]),
                                    }
                                ),
                                content_hash(request_payload),
                                now,
                                now,
                            ),
                        )
                    connection.execute(
                        """
                        UPDATE timer_instances
                        SET status = 'completed', updated_at = ?
                        WHERE session_id = ? AND timer_type = 'vote'
                          AND status = 'active'
                        """,
                        (now, session_id),
                    )
                    winning_text = ""
                    for option in vote["options"]:
                        if str(option.get("key")) == winner:
                            winning_text = str(option.get("text") or "")
                            break
                    event_text = (
                        f"【集体决定】{winning_text}"
                        if status == "decided"
                        else "【集体决定】本次表决未形成多数，队伍维持现状。"
                    )
                    append_event(
                        connection,
                        session_id=session_id,
                        turn_no=session["turn_no"],
                        role="system",
                        actor_id="vote",
                        actor_name="集体表决",
                        content=event_text,
                        meta={
                            "kind": "group_vote",
                            "vote_id": vote["id"],
                            "status": status,
                            "winner": winner,
                        },
                        created_at=now,
                    )
                    self._resume_after_vote(
                        connection,
                        session=session,
                        vote=vote,
                        now=now,
                    )
                    self._apply_return_vote_result(
                        connection,
                        vote_id=vote["id"],
                        passed=status == "decided",
                        now=now,
                    )
                self._insert_audit(
                    connection,
                    session_id,
                    user_id,
                    "vote.cast",
                    vote["id"],
                    {
                        "option": key,
                        "status": status,
                        "tally": tally,
                    },
                )
                updated_vote = connection.execute(
                    "SELECT * FROM group_votes WHERE id = ?",
                    (vote["id"],),
                ).fetchone()
                connection.execute("COMMIT")
                return {
                    "vote": self._vote(updated_vote),
                    "tally": tally,
                    "resolved": status != "open",
                    "runoff": False,
                }
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _resume_after_vote(
        self,
        connection: sqlite3.Connection,
        *,
        session: sqlite3.Row,
        vote: Mapping[str, Any],
        now: str,
    ) -> None:
        user_id = str(vote.get("suspended_user_id") or "")
        if not user_id:
            return
        participant = connection.execute(
            """
            SELECT * FROM participants
            WHERE session_id = ? AND group_user_id = ?
              AND participation_status = 'active'
              AND card_status = 'approved'
            """,
            (session["id"], user_id),
        ).fetchone()
        if not participant:
            return
        # 0.11.2：投票已结束，作废残留的 active 选项集。
        # 旧实现因残留选项存在而直接 return，导致表决通过后
        # 既不生成新选项、旧选项又一直挂在 WebUI 上。
        connection.execute(
            """
            UPDATE choice_sets SET status = 'superseded'
            WHERE session_id = ? AND status = 'active'
            """,
            (session["id"],),
        )
        latest = connection.execute(
            "SELECT decision_status, winner_key FROM group_votes WHERE id = ?",
            (vote["id"],),
        ).fetchone()
        declines = False
        if latest and latest["decision_status"] == "decided":
            winner_key = str(latest["winner_key"] or "")
            for option in (vote.get("options") or []):
                if (
                    isinstance(option, Mapping)
                    and str(option.get("key")) == winner_key
                    and bool(option.get("declines_action"))
                ):
                    declines = True
                    break
            if not declines:
                # 表决通过（非暂缓）：新叙事与新选项由上层
                # process_vote_resolution 生成，这里只恢复行动权
                # （行动指针未动，保持由被挂起玩家继续）。
                return
        # 表决「未通过」或「通过但选择暂缓」时，立即为被挂起
        # 玩家恢复一组兜底选项（幂等），避免任何失败窗口造成软锁。
        self._insert_fallback_choices(
            connection,
            session=session,
            participant=participant,
            now=now,
            idempotency_key=f"post-vote:{vote['id']}",
        )

    async def commit_vote_resolution(
        self,
        session_id: str,
        *,
        expected_revision: int,
        narrative: str,
        narrative_document: NarrativeDocument | Mapping[str, Any],
        world_state: Mapping[str, Any],
        memories: Sequence[Mapping[str, Any]] = (),
        model_payload: Mapping[str, Any] | None = None,
        workflow: Mapping[str, Any] | None = None,
        vote_id: str = "",
        item_ops: Sequence[Mapping[str, Any]] | None = None,
        economy_ops: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """0.11.2：把「集体表决通过」的落实叙事落库，并生成下一组选项。

        与 DM 推进类似：不移动玩家指针、不消耗个人行动机会；
        表决结果作为已定事实推进剧情。C6：模型提议的经济操作与表决
        落库在同一事务内应用；任一失败整笔回滚，不产生半提交资产。
        """
        return await self._run(
            self._commit_vote_resolution,
            session_id,
            expected_revision,
            narrative,
            (
                narrative_document.to_dict()
                if isinstance(narrative_document, NarrativeDocument)
                else dict(narrative_document)
            ),
            dict(world_state),
            [dict(item) for item in memories],
            dict(model_payload or {}),
            dict(workflow or {}),
            vote_id,
            [dict(op) for op in (item_ops or ())],
            [dict(op) for op in (economy_ops or ())],
        )
