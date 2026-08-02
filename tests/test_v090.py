from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from tavern.api import ExtensionRegistry, HookRegistry, TavernPublicAPI
from tavern.config import TavernConfig
from tavern.constants import (
    DATABASE_SCHEMA_VERSION,
    DEFAULT_WORLD,
    DEFAULT_WORLD_SLUG,
    PLUGIN_VERSION,
    SESSION_PREPARING,
    SESSION_RUNNING,
)
from tavern.database import TavernDatabase
from tavern.diagnostics import build_diagnostic_report
from tavern.engine import TavernBusyError, TavernEngine
from tavern.events import EventBroker
from tavern.narrative_quality import inspect_narrative
from tavern.operations import operation_key, recovery_summary
from tavern.world_contract import validate_world_contract
from tavern.world_migration import compare_world_contracts
from tavern.world_preflight import inspect_world_package


ROOT = Path(__file__).resolve().parents[1]


class FakeContext:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def get_current_chat_provider_id(self, *, umo: str) -> str:
        return "provider-test"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(completion_text=self.outputs.pop(0))


class V090ContractTests(unittest.TestCase):
    def test_versions_are_single_current_baseline(self) -> None:
        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text("utf-8"))
        self.assertEqual(PLUGIN_VERSION, "0.9.0")
        self.assertEqual(metadata["version"], "v0.9.0")
        self.assertEqual(DATABASE_SCHEMA_VERSION, 8)

    def test_builtin_world_passes_strict_preflight(self) -> None:
        report = inspect_world_package(DEFAULT_WORLD)
        self.assertTrue(report["compatible"])
        self.assertEqual(report["summary"]["schema_version"], 2)
        self.assertEqual(report["summary"]["stats_mode"], "preset")
        self.assertEqual(report["summary"]["migrations"], 0)

    def test_old_or_missing_world_protocol_is_rejected(self) -> None:
        for value in (None, 1):
            world = copy.deepcopy(DEFAULT_WORLD)
            if value is None:
                world.pop("world_schema_version", None)
                world["rules"].pop("world_schema_version", None)
            else:
                world["world_schema_version"] = value
                world["rules"]["world_schema_version"] = value
            with self.assertRaisesRegex(ValueError, "仅接受世界包协议 v2"):
                validate_world_contract(world)

    def test_world_migration_never_hot_replaces(self) -> None:
        candidate = copy.deepcopy(DEFAULT_WORLD)
        report = compare_world_contracts(DEFAULT_WORLD, candidate)
        self.assertFalse(report["safe_for_live_replace"])
        self.assertTrue(report["safe_for_clone"])

    def test_quality_and_recovery_guards_remain_active(self) -> None:
        quality = inspect_narrative("风吹过大厅。", [{"text": "观察"}, {"text": "观察"}])
        self.assertFalse(quality["passed"])
        recovery = recovery_summary(
            [], session_state="running", has_active_choices=False, has_active_vote=False
        )
        self.assertEqual(recovery["recommended_action"], "rebuild_choices")

    def test_operation_key_is_stable_without_plaintext(self) -> None:
        first = operation_key(
            "s", "turn", turn_no=2, actor_id="u", source_id="m", payload={"input": "秘密"}
        )
        second = operation_key(
            "s", "turn", turn_no=2, actor_id="u", source_id="m", payload={"input": "秘密"}
        )
        self.assertEqual(first, second)
        self.assertNotIn("秘密", first)

    def test_extension_registry_is_named_and_collision_safe(self) -> None:
        registry = ExtensionRegistry()
        provider = lambda payload: payload
        registry.register_dice_system("percentile", provider)
        self.assertIs(registry.resolve("dice_system", "percentile"), provider)
        with self.assertRaisesRegex(ValueError, "已注册"):
            registry.register_dice_system("percentile", provider)

    def test_logo_and_static_cache_version_are_current(self) -> None:
        html = (ROOT / "pages/console/index.html").read_text("utf-8")
        style = (ROOT / "pages/console/style.css").read_text("utf-8")
        logo = (ROOT / "logo.svg").read_text("utf-8")
        self.assertIn("style.css?v=0.9.0", html)
        self.assertIn("app.js?v=0.9.0", html)
        self.assertIn("button-secondary", html)
        self.assertIn(".button-secondary", style)
        self.assertIn("viewBox=\"0 0 256 256\"", logo)

    def test_readme_and_changelog_have_separate_roles(self) -> None:
        readme = (ROOT / "README.md").read_text("utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
        self.assertNotIn("## v0.8.0", readme)
        for section in changelog.split("\n## v")[1:]:
            self.assertIn("### 更新摘要", section)
            self.assertIn("### 详细更新说明", section)


class V090DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = TavernDatabase(self.root)
        self.session = await self.database.ensure_session(
            "qq", "group-v090", "qq:group-v090", DEFAULT_WORLD_SLUG, "admin"
        )

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_new_database_ignores_old_filename(self) -> None:
        self.assertEqual(self.database.path.name, "catalog_v090.sqlite3")
        self.assertFalse((self.root / "catalog.sqlite3").exists())
        with self.database._connect() as connection:
            schema = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(int(schema), 8)

    async def test_timer_policy_and_play_time_are_off_by_default(self) -> None:
        policy = await self.database.get_timer_policy(self.session["id"])
        self.assertFalse(policy["global_enabled"])
        self.assertTrue(all(not value for value in policy["switches"].values()))
        config = await self.database.get_instance_config(self.session["id"])
        rules = config["time_rules"]
        for key in (
            "preparation_timeout_seconds", "ready_timeout_seconds",
            "turn_timeout_seconds",
            "vote_round_one_seconds", "standby_timeout_seconds",
        ):
            self.assertIsNone(rules[key])
        self.assertFalse(rules["announce_timeouts"])

    async def test_enabling_all_timer_categories_is_explicit(self) -> None:
        policy = await self.database.set_timer_policy(
            self.session["id"], "all", True, "admin"
        )
        self.assertTrue(policy["global_enabled"])
        self.assertTrue(all(policy["effective"].values()))

    async def test_configured_reminder_fires_once(self) -> None:
        await self.database.set_timer_policy(self.session["id"], "all", True, "admin")
        with self.database._connect() as connection:
            timer_id = self.database._create_timer(
                connection,
                session_id=self.session["id"],
                participant_id="",
                timer_type="turn",
                timeout_seconds=120,
                reminder_seconds=30,
                action={"actor_user_id": "user"},
            )
            connection.execute(
                "UPDATE timer_instances SET reminder_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), timer_id),
            )
        first = await self.database.process_due_timers()
        second = await self.database.process_due_timers()
        self.assertEqual([item["kind"] for item in first], ["reminder"])
        self.assertEqual(second, [])

    async def test_current_backup_only_and_round_trip(self) -> None:
        bundle = await self.database.export_bundle()
        self.assertEqual(bundle["schema_version"], 8)
        old = copy.deepcopy(bundle)
        old["schema_version"] = 7
        with self.assertRaisesRegex(ValueError, "仅接受 Schema 8"):
            self.database.validate_bundle(old)
        self.database.validate_bundle(bundle)

        restored = TavernDatabase(self.root / "restored")
        result = await restored.import_bundle(bundle, "replace", "admin")
        self.assertEqual(result["sessions"], 1)
        recovered = await restored.get_session_by_group("qq", "group-v090")
        self.assertEqual(recovered["id"], self.session["id"])

    async def test_world_unknown_extensions_survive_editor_roundtrip(self) -> None:
        world = await self.database.get_world(DEFAULT_WORLD_SLUG)
        world["third_party_extension"] = {"mode": "custom", "order": [3, 1, 2]}
        saved = await self.database.save_world(world, "admin")
        stripped = {key: value for key, value in saved.items() if key != "third_party_extension"}
        stripped["description"] = "编辑器修改后的简介"
        saved_again = await self.database.save_world(stripped, "admin")
        self.assertEqual(
            saved_again["third_party_extension"],
            {"mode": "custom", "order": [3, 1, 2]},
        )

    async def test_operation_receipt_is_idempotent(self) -> None:
        created = await self.database.reserve_operation(
            "turn:test", self.session["id"], "turn", {"turn_no": 1}
        )
        duplicate = await self.database.reserve_operation(
            "turn:test", self.session["id"], "turn", {"turn_no": 1}
        )
        self.assertTrue(created["created"])
        self.assertFalse(duplicate["created"])

    async def test_diagnostics_redact_private_origin(self) -> None:
        report = await build_diagnostic_report(self.database, self.session["id"])
        self.assertEqual(report["session"]["unified_origin"], "[REDACTED]")
        self.assertEqual(report["database_schema_version"], 8)

    async def test_public_api_does_not_expose_connection(self) -> None:
        hooks = HookRegistry()
        api = TavernPublicAPI(self.database, hooks, ExtensionRegistry())
        session = await api.get_current_session("qq", "group-v090")
        self.assertEqual(session["id"], self.session["id"])
        self.assertFalse(hasattr(api, "connect"))

        report = await api.export_diagnostic(
            self.session["id"],
            {"api_key": "secret-value", "safe_note": "visible"},
        )
        self.assertEqual(report["extension_context"]["api_key"], "[REDACTED]")
        self.assertEqual(report["extension_context"]["safe_note"], "visible")

    async def test_character_revision_activates_only_after_review(self) -> None:
        await self.database.transition_session(
            self.session["id"], SESSION_PREPARING, "admin"
        )
        reserved = await self.database.reserve_participant(
            self.session["id"], "user-card", "测试玩家"
        )
        origin = "private:user-card"
        await self.database.bind_card_code(
            reserved["binding_code"], "user-card", origin
        )
        draft = await self.database.card_draft_for_private(origin)
        values = {
            "name": "灰鸦",
            "code": "HY",
            "profession": "骑士",
            "primary_attribute": "力量",
            "secondary_attribute": "意志",
        }
        for field in draft["template"]["fields"]:
            key = field["key"]
            value = values.get(key)
            if value is None and field.get("options"):
                first = field["options"][0]
                value = first.get("value") if isinstance(first, dict) else first
            if value is None:
                value = "原始资料"
            await self.database.fill_card_draft(origin, str(value))
        submitted = await self.database.confirm_card_draft(origin)
        await self.database.review_character_card(
            self.session["id"], submitted["id"], True, "admin"
        )
        before = await self.database.get_participant(
            self.session["id"], participant_ref=submitted["id"]
        )
        request = await self.database.request_card_revision(
            self.session["id"],
            submitted["id"],
            {"background": "修订后的冒险经历"},
            {},
            "user-card",
            "修正背景",
        )
        pending = await self.database.get_participant(
            self.session["id"], participant_ref=submitted["id"]
        )
        self.assertEqual(
            pending["character_version_id"], before["character_version_id"]
        )
        await self.database.review_card_revision(
            request["id"], True, "admin", "同意修改"
        )
        roster = await self.database.list_roster(self.session["id"])
        updated = next(item for item in roster if item["id"] == submitted["id"])
        self.assertEqual(updated["card_version_no"], 2)
        self.assertEqual(
            updated["card_profile"]["background"], "修订后的冒险经历"
        )

    async def test_committed_hook_isolated_from_failures(self) -> None:
        hooks = HookRegistry()
        seen: list[str] = []

        async def good(payload):
            seen.append(payload["session_id"])

        def bad(payload):
            raise RuntimeError("extension failed")

        hooks.subscribe("story_generated", bad)
        hooks.subscribe("story_generated", good)
        broker = EventBroker(hooks=hooks)
        await broker.publish(
            {"hook": "story_generated", "session_id": self.session["id"]}
        )
        self.assertEqual(seen, [self.session["id"]])

    async def test_transport_redelivery_consumes_one_turn(self) -> None:
        self.session = await self.database.transition_session(
            self.session["id"], SESSION_RUNNING, "admin"
        )
        output = json.dumps(
            {
                "mode": "resolve",
                "narrative": "只推进一次。",
                "check": None,
                "state_patch": {"scene_summary": "只推进一次。"},
                "memories": [],
                "director_note": "测试",
            },
            ensure_ascii=False,
        )
        context = FakeContext([output])
        engine = TavernEngine(
            context=context,
            database=self.database,
            config_provider=lambda: TavernConfig(user_cooldown_seconds=0, json_repair_attempts=0),
            broker=EventBroker(),
        )
        event = SimpleNamespace(
            unified_msg_origin="qq:group-v090", message_id="event-once"
        )
        first = await engine.process(
            event=event,
            session_id=self.session["id"],
            sender_id="user",
            sender_name="旅客",
            content="观察窗外",
        )
        self.assertEqual(first.session["turn_no"], 1)
        with self.assertRaises(TavernBusyError):
            await engine.process(
                event=event,
                session_id=self.session["id"],
                sender_id="user",
                sender_name="旅客",
                content="观察窗外",
            )
        self.assertEqual(len(context.calls), 1)


if __name__ == "__main__":
    unittest.main()
