from __future__ import annotations

import json
import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from tavern.config import TavernConfig
from tavern.constants import (
    DEFAULT_WORLD_SLUG,
    SESSION_PAUSED,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import TavernDatabase
from tavern.engine import TavernEngine
from tavern.events import EventBroker
from tavern.lifecycle import fallback_choices


class FakeNarratorContext:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise AssertionError("模型被额外调用")
        return SimpleNamespace(completion_text=self.outputs.pop(0))


class VNextWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.database = TavernDatabase(self.data_dir)
        self.session = await self.database.ensure_session(
            "qq",
            "vnext-group",
            "qq:vnext-group",
            DEFAULT_WORLD_SLUG,
            "admin",
        )
        self.session = await self.database.transition_session(
            self.session["id"],
            SESSION_PREPARING,
            "admin",
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _make_character(
        self,
        user_id: str,
        name: str,
        code: str,
    ) -> dict:
        reserved = await self.database.reserve_participant(
            self.session["id"],
            user_id,
            name,
        )
        origin = f"qq-private:{user_id}"
        await self.database.bind_card_code(
            reserved["binding_code"],
            f"private-{user_id}",
            origin,
        )
        draft = await self.database.card_draft_for_private(origin)
        self.assertIsNotNone(draft)
        stat_values = {
            "stat_body": "3",
            "stat_agility": "3",
            "stat_will": "2",
            "stat_knowledge": "2",
        }
        fixed_values = {
            "name": name,
            "code": code,
            "appearance": f"{name}穿着便于旅行的旧外套。",
            "background": f"{name}来自边境商路，了解基本旅行常识。",
            "personality": "谨慎、克制，不会替同伴作决定。",
            "goal": "调查酒馆附近的异常并保护同行者。",
            "weakness": "不擅长强行对抗，也缺乏贵族人脉。",
            "knowledge_boundary": "只知道边境常识，不知道隐秘魔法真相。",
        }
        for field in draft["template"]["fields"]:
            value = stat_values.get(
                field["key"],
                fixed_values.get(field["key"], "无"),
            )
            await self.database.fill_card_draft(origin, value)
        confirmed = await self.database.confirm_card_draft(origin)
        if not confirmed["auto_approved"]:
            confirmed = await self.database.review_character_card(
                self.session["id"],
                confirmed["id"],
                True,
                "admin",
                "测试审核",
            )
        return await self.database.set_participant_ready(
            self.session["id"],
            user_id,
        )

    async def _activate_two(self) -> tuple[dict, dict, dict]:
        first = await self._make_character("user-1", "白鸦", "BY")
        second = await self._make_character("user-2", "梅林", "ML")
        result = await self.database.activate_story(
            self.session["id"],
            "admin",
        )
        self.assertTrue(result["started"])
        self.session = result["session"]
        return first, second, result

    async def _commit_current(
        self,
        *,
        group_decision: dict | None = None,
        return_progress: dict | None = None,
    ) -> dict:
        session = await self.database.get_session(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        self.assertIsNotNone(choice)
        participant = choice["participant"]
        state = dict(session["world_state"])
        state["scene_summary"] = "测试行动已经得到裁定。"
        workflow = {
            "choice_set_id": choice["id"],
            "selected_key": "A",
            "flavor_text": "",
            "next_choices": fallback_choices(state),
            "group_decision": group_decision,
            "return_progress": return_progress,
        }
        result = await self.database.commit_turn(
            session_id=session["id"],
            expected_revision=session["revision"],
            player_id=participant["player_id"],
            player_user_id=participant["group_user_id"],
            player_name=participant["character_name"],
            player_input="选择 A",
            narrative="测试行动已经得到裁定。",
            world_state=state,
            memories=[],
            check_payload=None,
            model_payload={"mode": "resolve"},
            director_note="测试",
            auto_snapshot_interval=5,
            store_model_payload=False,
            workflow=workflow,
        )
        self.session = result
        return result

    async def test_opening_is_two_stage_and_creates_four_choices(self) -> None:
        self.assertEqual(self.session["state"], SESSION_PREPARING)
        preflight = await self.database.opening_preflight(self.session["id"])
        self.assertFalse(preflight["ok"])
        self.assertTrue(preflight["blockers"])

        _, _, result = await self._activate_two()
        self.assertEqual(result["session"]["state"], SESSION_RUNNING)
        self.assertEqual(
            [item["key"] for item in result["choice_set"]["choices"]],
            ["A", "B", "C", "D"],
        )
        timers = await self.database.list_timers(self.session["id"])
        self.assertTrue(
            any(
                item["timer_type"] == "turn"
                and item["status"] == "active"
                for item in timers
            )
        )

    async def test_required_choice_recovers_when_model_skips_check(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        required_choices = fallback_choices(
            {
                "location": "断桥前",
                "scene_summary": "队伍需要越过断桥",
            }
        )
        required_choices[2].update(
            {
                "text": "借助绳索翻越断桥",
                "risk": "dangerous",
                "requires_check": True,
                "check_type": "standard",
                "check_stat": "敏捷",
                "difficulty": 15,
                "known_consequences": "失败可能跌落并受伤",
                "advantage_sources": ["装备：绳索"],
            }
        )
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            required_choices,
            actor_id=choice["participant"]["group_user_id"],
        )
        choice = await self.database.active_choice_set(self.session["id"])
        participant = choice["participant"]
        premature_resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦未经检定便越过了断桥。",
                "check": None,
                "state_patch": {
                    "location": "断桥另一侧",
                    "scene_summary": "白鸦已经越过断桥。",
                },
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "断桥另一侧"}
                ),
                "director_note": "错误地跳过了检定。",
            },
            ensure_ascii=False,
        )
        checked_resolution = json.dumps(
            {
                "mode": "resolve",
                "narrative": "白鸦依据检定结果完成了这次尝试。",
                "check": None,
                "state_patch": {
                    "scene_summary": "断桥尝试已经得到裁定。",
                },
                "memories": [],
                "next_choices": fallback_choices(
                    {"location": "断桥前"}
                ),
                "director_note": "遵循插件锁定的权威骰点。",
            },
            ensure_ascii=False,
        )
        context = FakeNarratorContext(
            [premature_resolution, checked_resolution]
        )
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(
                user_cooldown_seconds=0,
                json_repair_attempts=0,
                request_timeout_seconds=5,
                store_model_payloads=True,
            ),
            broker=EventBroker(),
        )
        reply = await engine.process_choice(
            event=SimpleNamespace(
                unified_msg_origin="qq:vnext-group"
            ),
            session_id=self.session["id"],
            sender_id=participant["group_user_id"],
            sender_name=participant["character_name"],
            choice_key="C",
        )

        self.assertIsNotNone(reply.dice)
        self.assertEqual(reply.dice.difficulty, 15)
        self.assertEqual(reply.session["turn_no"], 1)
        self.assertNotEqual(
            reply.session["world_state"].get("location"),
            "断桥另一侧",
        )
        self.assertEqual(len(context.calls), 2)
        self.assertIn(
            "本条行动来自插件已锁定的必检选项",
            context.calls[0]["prompt"],
        )
        self.assertIn(
            '"requires_check": true',
            context.calls[0]["prompt"],
        )
        self.assertIn(
            "<authoritative_check>",
            context.calls[1]["prompt"],
        )
        self.assertNotIn("未经检定便越过", reply.text)

    async def test_private_card_draft_survives_database_reload(self) -> None:
        reserved = await self.database.reserve_participant(
            self.session["id"],
            "draft-user",
            "草稿玩家",
        )
        origin = "qq-private:draft-user"
        await self.database.bind_card_code(
            reserved["binding_code"],
            "private-draft-user",
            origin,
        )
        await self.database.fill_card_draft(origin, "草稿角色")

        reloaded = TavernDatabase(self.data_dir)
        draft = await reloaded.card_draft_for_private(origin)
        self.assertIsNotNone(draft)
        self.assertEqual(draft["fields"]["name"], "草稿角色")
        self.assertEqual(draft["current_step"], 1)

    async def test_seat_limit_rejects_fifth_player(self) -> None:
        for index in range(4):
            await self.database.reserve_participant(
                self.session["id"],
                f"seat-{index}",
                f"席位{index}",
            )
        with self.assertRaisesRegex(ValueError, "已满"):
            await self.database.reserve_participant(
                self.session["id"],
                "seat-5",
                "第五人",
            )

    async def test_authoritative_stat_modifier_comes_from_card(self) -> None:
        await self._make_character("user-1", "白鸦", "BY")
        modifier = await self.database.authoritative_modifier(
            self.session["id"],
            "user-1",
            "体魄",
        )
        self.assertTrue(modifier["matched"])
        self.assertEqual(modifier["modifier"], 0)
        unknown = await self.database.authoritative_modifier(
            self.session["id"],
            "user-1",
            "凭空创造",
        )
        self.assertFalse(unknown["matched"])
        self.assertEqual(unknown["modifier"], 0)

    async def test_group_vote_requires_majority_and_preserves_turn(self) -> None:
        await self._activate_two()
        before = await self.database.get_turn_status(self.session["id"])
        await self._commit_current(
            group_decision={
                "question": "是否前往北方废墟？",
                "options": [
                    {"key": "A", "text": "前往北方废墟"},
                    {"key": "B", "text": "留在酒馆调查"},
                    {"key": "C", "text": "先休整"},
                    {"key": "D", "text": "放弃路线"},
                ],
            }
        )
        vote = await self.database.active_vote(self.session["id"])
        self.assertIsNotNone(vote)
        suspended = vote["suspended_user_id"]
        self.assertEqual(suspended, before["current_user_id"])

        first = await self.database.cast_vote(
            self.session["id"],
            "user-1",
            "A",
        )
        self.assertFalse(first["resolved"])
        second = await self.database.cast_vote(
            self.session["id"],
            "user-2",
            "A",
        )
        self.assertTrue(second["resolved"])
        self.assertEqual(second["vote"]["winner_key"], "A")
        choice = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(
            choice["participant"]["group_user_id"],
            suspended,
        )
        session = await self.database.get_session(self.session["id"])
        self.assertIn(
            "队伍多数决定：前往北方废墟",
            session["world_state"]["facts"],
        )

    async def test_snapshot_restores_inflight_choice_and_pauses_timer(self) -> None:
        await self._activate_two()
        choice = await self.database.active_choice_set(self.session["id"])
        await self.database.create_snapshot(
            self.session["id"],
            "保留中的选项",
            "admin",
        )
        await self.database.replace_active_choices(
            self.session["id"],
            choice["participant_id"],
            fallback_choices({"location": "改变后的地点"}),
            actor_id="user-1",
        )
        restored = await self.database.restore_snapshot(
            self.session["id"],
            "保留中的选项",
            "admin",
        )
        self.assertEqual(restored["state"], SESSION_PAUSED)
        restored_choice = await self.database.active_choice_set(
            self.session["id"]
        )
        self.assertEqual(restored_choice["id"], choice["id"])
        timers = await self.database.list_timers(self.session["id"])
        self.assertTrue(
            any(
                item["timer_type"] == "turn"
                and item["status"] == "paused"
                for item in timers
            )
        )

    async def test_turn_timeout_can_hold_original_action_right(self) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "turn_timeout_seconds": 60,
                "turn_timeout_action": "hold",
                "all_idle_pause_seconds": None,
            },
            "admin",
        )
        await self._activate_two()
        before = await self.database.get_turn_status(self.session["id"])
        choice = await self.database.active_choice_set(self.session["id"])
        timer = next(
            item
            for item in await self.database.list_timers(self.session["id"])
            if item["timer_type"] == "turn" and item["status"] == "active"
        )
        await self.database.control_timer(
            timer["id"],
            "expire",
            "admin",
        )
        after = await self.database.get_turn_status(self.session["id"])
        active_choice = await self.database.active_choice_set(self.session["id"])
        self.assertEqual(
            after["current_user_id"],
            before["current_user_id"],
        )
        self.assertEqual(active_choice["id"], choice["id"])

    async def test_all_idle_timeout_pauses_and_freezes_timers(self) -> None:
        config = await self.database.get_instance_config(self.session["id"])
        await self.database.save_instance_time_rules(
            self.session["id"],
            {
                **config["time_rules"],
                "turn_timeout_seconds": None,
                "all_idle_pause_seconds": 1,
            },
            "admin",
        )
        await self._activate_two()
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT phase_meta_json FROM instance_configs
                WHERE session_id = ?
                """,
                (self.session["id"],),
            ).fetchone()
            phase_meta = json.loads(row["phase_meta_json"])
            phase_meta["started_at"] = "2000-01-01T00:00:00+00:00"
            connection.execute(
                """
                UPDATE instance_configs SET phase_meta_json = ?
                WHERE session_id = ?
                """,
                (
                    json.dumps(phase_meta, ensure_ascii=False),
                    self.session["id"],
                ),
            )
        notices = await self.database.process_due_timers()
        session = await self.database.get_session(self.session["id"])
        self.assertEqual(session["state"], SESSION_PAUSED)
        self.assertTrue(any(item["kind"] == "idle_pause" for item in notices))
        timers = await self.database.list_timers(self.session["id"])
        self.assertFalse(any(item["status"] == "active" for item in timers))

    async def test_delegation_expires_when_owner_returns(self) -> None:
        _, _, result = await self._activate_two()
        owner = result["current_participant"]
        await self.database.grant_delegation(
            self.session["id"],
            owner["group_user_id"],
            "delegate-user",
            owner["group_user_id"],
            duration_seconds=3600,
        )
        delegated = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            "delegate-user",
            "choose",
        )
        self.assertTrue(delegated["authorized"])
        returned = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            owner["group_user_id"],
            "choose",
        )
        self.assertEqual(returned["mode"], "owner")
        revoked = await self.database.authorize_participant_control(
            self.session["id"],
            owner["id"],
            "delegate-user",
            "choose",
        )
        self.assertFalse(revoked["authorized"])

    async def test_ban_atomically_retires_and_keeps_history(self) -> None:
        await self._activate_two()
        result = await self.database.create_ban(
            self.session["id"],
            "BY",
            "admin",
            duration_seconds=3600,
            reason="测试封禁",
        )
        self.assertIn("白鸦", result["narrative"])
        participant = await self.database.get_participant(
            self.session["id"],
            participant_ref="BY",
        )
        self.assertEqual(participant["participation_status"], "retired")
        bans = await self.database.list_bans(self.session["id"])
        self.assertEqual(len(bans), 1)
        events = await self.database.recent_events(self.session["id"], 20)
        self.assertTrue(
            any(item["meta"].get("kind") == "safe_exit" for item in events)
        )

    async def test_return_requires_vote_then_story_progress(self) -> None:
        await self._activate_two()
        await self.database.retire_participant(
            self.session["id"],
            "BY",
            "admin",
            forced=True,
            reason="测试退场",
        )
        request = await self.database.request_return(
            self.session["id"],
            "user-1",
        )
        vote = await self.database.active_vote(self.session["id"])
        self.assertEqual(vote["id"], request["vote_id"])
        result = await self.database.cast_vote(
            self.session["id"],
            "user-2",
            "A",
        )
        self.assertTrue(result["resolved"])
        requests = await self.database.list_return_requests(
            self.session["id"]
        )
        self.assertEqual(requests[0]["status"], "quest_active")

        await self.database.designate_turn(
            self.session["id"],
            "user-2",
            "admin",
        )
        await self._commit_current(
            return_progress={
                "request_id": request["request_id"],
                "evidence": "队伍找到离场者留下的信物并完成会合条件。",
                "completed": True,
            }
        )
        requests = await self.database.list_return_requests(
            self.session["id"]
        )
        self.assertEqual(requests[0]["status"], "completed")
        participant = await self.database.get_participant(
            self.session["id"],
            participant_ref="BY",
        )
        self.assertEqual(participant["participation_status"], "active")

    async def test_schema_upgrade_creates_consistent_backup(self) -> None:
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE tavern_meta SET value = '2'
                WHERE key = 'schema_version'
                """
            )
        upgraded = TavernDatabase(self.data_dir)
        self.assertIsNotNone(upgraded.migration_backup_path)
        self.assertTrue(upgraded.migration_backup_path.exists())
        with sqlite3.connect(upgraded.migration_backup_path) as connection:
            version = connection.execute(
                """
                SELECT value FROM tavern_meta
                WHERE key = 'schema_version'
                """
            ).fetchone()[0]
        self.assertEqual(version, "2")
