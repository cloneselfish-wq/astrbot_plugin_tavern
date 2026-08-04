from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import yaml

from tavern.constants import (
    CHARACTER_CARD_TEMPLATE_VERSION,
    DATABASE_SCHEMA_VERSION,
    NPC_IMPORT_TEMPLATE_VERSION,
    PLUGIN_VERSION,
    TEMPLATE_BUNDLE_VERSION,
)
from tavern.database import TavernDatabase
from tavern.prompts import system_prompt
from tavern.rule_runtime import RuleRuntime
from tavern.world_contract import validate_world_contract
from tavern.world_import import world_import_payload
from tavern.world_preflight import inspect_world_package


ROOT = Path(__file__).resolve().parents[1]


def load_json(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class V011ContractTests(unittest.TestCase):
    def test_release_versions_are_synchronized(self) -> None:
        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        manifest = load_json("templates/template-manifest.json")
        self.assertEqual(PLUGIN_VERSION, "0.11.2")
        self.assertEqual(metadata["version"], "v0.11.2")
        self.assertEqual(DATABASE_SCHEMA_VERSION, 10)
        self.assertEqual(TEMPLATE_BUNDLE_VERSION, "3.0.0")
        self.assertEqual(CHARACTER_CARD_TEMPLATE_VERSION, 6)
        self.assertEqual(NPC_IMPORT_TEMPLATE_VERSION, 2)
        self.assertEqual(manifest["compatible_plugin_version"], PLUGIN_VERSION)

    def test_v5_packages_and_aelvion_pass_strict_preflight(self) -> None:
        for relative in (
            "worlds/aelvion-ashen-crown.json",
            "templates/world-package-capabilities.template.json",
            "templates/world-package-interaction-rules.template.json",
            "templates/world-package-v5-full-example.json",
        ):
            report = inspect_world_package(load_json(relative))
            self.assertTrue(report["compatible"], (relative, report["issues"]))
            self.assertGreater(report["summary"]["entity_count"], 0)

    def test_v2_v3_v4_remain_supported(self) -> None:
        for relative in (
            "templates/world-package.template.json",
            "templates/world-package-preset-stack.template.json",
            "tests/fixtures/where-winds-meet-tideless-script.world.json",
        ):
            validate_world_contract(load_json(relative))

    def test_capability_progression_cycle_is_rejected(self) -> None:
        world = load_json("templates/world-package-capabilities.template.json")
        world["rules"]["capabilities"]["transitions"].append(
            {
                "transition_id": "cycle_back",
                "from": ["capability:advanced"],
                "operations": [
                    {"op": "grant_reference", "target_ref": "capability:basic"}
                ],
            }
        )
        with self.assertRaisesRegex(ValueError, "环"):
            validate_world_contract(world)

    def test_interaction_rules_are_world_defined_and_dry_run_is_side_effect_free(self) -> None:
        world = load_json("templates/world-package-interaction-rules.template.json")
        runtime = RuleRuntime(world)
        context = {
            "action": {"refs": {"custom:source.kind": "author_defined_source"}},
            "target": {"refs": {"custom:target.kind": "author_defined_target"}},
            "actor": {"capabilities": []},
            "state": {"refs": {"custom:check_modifier": 0}},
        }
        result = runtime.resolve_action_intent(
            {
                "actor_ref": "character:test",
                "action_type": "freeform",
                "declared_intent": "测试世界自定义关系",
            },
            context,
            dry_run=True,
        )
        self.assertEqual(result["state"], context["state"])
        self.assertIn("world_defined_relation", {
            item["rule_id"] for item in result["receipt"]["matched_rules"]
        })
        self.assertTrue(result["narrative_projection"])
        self.assertEqual(result["changes"][0]["after"], 2)

    def test_capability_projection_and_cost_commit(self) -> None:
        world = load_json("templates/world-package-v5-full-example.json")
        runtime = RuleRuntime(world)
        context = {
            "actor": {
                "capabilities": [
                    {"capability_ref": "capability:basic", "available": True}
                ],
                "refs": {"resource:focus": 3},
            },
            "state": {"refs": {"resource:focus": 3}},
        }
        projection = runtime.capability_projection(context)
        self.assertEqual(projection[0]["capability_ref"], "capability:basic")
        prompt = system_prompt(world, capability_projection=projection)
        self.assertIn("<available_capabilities>", prompt)
        result = runtime.resolve_action_intent(
            {
                "actor_ref": "character:test",
                "action_type": "freeform",
                "capability_ref": "capability:basic",
                "declared_intent": "使用能力",
            },
            context,
            dry_run=False,
        )
        self.assertEqual(result["state"]["refs"]["resource:focus"], 2)
        self.assertEqual(result["receipt"]["status"], "completed")

    def test_portable_hash_shape_excludes_catalog_number_and_order(self) -> None:
        world = load_json("worlds/aelvion-ashen-crown.json")
        world.update({"display_no": 81, "sort_order": -5, "created_at": "x"})
        portable = world_import_payload(world)
        self.assertNotIn("display_no", portable)
        self.assertNotIn("sort_order", portable)
        self.assertNotIn("created_at", portable)


class V011DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = TavernDatabase(Path(self.temp.name))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_stable_display_number_and_independent_sort_order(self) -> None:
        base = load_json("templates/world-package-v5-full-example.json")
        base.update({"slug": "stable-number-one", "name": "编号一"})
        first = await self.database.save_world(base, "admin")
        self.assertEqual(world_import_payload(base), world_import_payload(first))
        original_no = first["display_no"]
        edited = await self.database.save_world(
            {**first, "description": "编辑不改变编号"}, "admin"
        )
        self.assertEqual(edited["display_no"], original_no)
        moved = await self.database.set_world_sort_order(
            first["id"], 100, "admin"
        )
        self.assertEqual(moved["display_no"], original_no)
        self.assertEqual(moved["sort_order"], 100)
        await self.database.archive_world(first["id"], "admin")
        base.update({"slug": "stable-number-two", "name": "编号二"})
        second = await self.database.save_world(base, "admin")
        self.assertGreater(second["display_no"], original_no)

    async def test_action_commit_is_idempotent_and_receipted(self) -> None:
        session = await self.database.ensure_session(
            "test", "group", "test:group", "aelvion-ashen-crown", "admin",
            "receipt-test", "凭证测试",
        )
        intent = {
            "actor_ref": "character:test",
            "action_type": "freeform",
            "declared_intent": "观察现场",
        }
        first = await self.database.resolve_action_intent(
            session["id"], intent, {}, dry_run=False,
            operation_id="idempotent-v011", actor_id="admin",
        )
        second = await self.database.resolve_action_intent(
            session["id"], intent, {}, dry_run=False,
            operation_id="idempotent-v011", actor_id="admin",
        )
        self.assertEqual(first["receipt"]["receipt_id"], second["receipt"]["receipt_id"])
        receipt = await self.database.get_resolution_receipt(
            first["receipt"]["receipt_id"]
        )
        self.assertEqual(receipt["operation_id"], "idempotent-v011")
        self.assertEqual(len(receipt["content_hash"]), 64)

    async def test_running_contract_stays_frozen_and_upgrade_uses_new_clone(self) -> None:
        session = await self.database.ensure_session(
            "test", "upgrade-group", "test:upgrade", "aelvion-ashen-crown", "admin",
            "original", "原副本",
        )
        frozen = await self.database.get_instance_config(session["id"])
        current = await self.database.get_world("aelvion-ashen-crown")
        updated = await self.database.save_world(
            {**current, "description": current["description"] + "（新修订）"},
            "admin",
        )
        unchanged = await self.database.get_instance_config(session["id"])
        self.assertEqual(unchanged["world_revision"], frozen["world_revision"])
        clone = await self.database.clone_session(
            session["id"], "admin",
            instance_slug="upgraded", instance_name="升级分支",
            candidate_world_ref=updated["id"],
        )
        clone_config = await self.database.get_instance_config(clone["id"])
        self.assertEqual(clone["state"], "closed")
        self.assertEqual(clone_config["world_revision"], updated["revision"])
        self.assertEqual(
            clone_config["phase_meta"]["branched_from_session_id"], session["id"]
        )

    async def test_schema9_upgrade_creates_backup_and_deterministic_backfill(self) -> None:
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir()
        path = legacy_dir / "catalog_v090.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                """
                CREATE TABLE tavern_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO tavern_meta(key, value) VALUES ('schema_version', '9');
                CREATE TABLE worlds (
                    id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL,
                    system_prompt TEXT NOT NULL, rules_json TEXT NOT NULL,
                    extensions_json TEXT NOT NULL DEFAULT '{}',
                    opening_scene TEXT NOT NULL, initial_state_json TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO worlds VALUES (
                    'world_legacy', 'legacy-v4', '旧世界', '', '规则',
                    '{"world_schema_version":4}', '{}', '', '{}', 0, 1,
                    '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
                );
                """
            )
        upgraded = TavernDatabase(legacy_dir)
        self.assertIsNotNone(upgraded.migration_backup_path)
        self.assertTrue(upgraded.migration_backup_path.is_file())
        with closing(sqlite3.connect(path)) as connection:
            schema = connection.execute(
                "SELECT value FROM tavern_meta WHERE key='schema_version'"
            ).fetchone()[0]
            number, order = connection.execute(
                "SELECT display_no, sort_order FROM worlds WHERE id='world_legacy'"
            ).fetchone()
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM world_snapshots"
            ).fetchone()[0]
        self.assertEqual(schema, "10")
        self.assertEqual((number, order), (1, 1))
        self.assertEqual(snapshots, 2)


if __name__ == "__main__":
    unittest.main()
