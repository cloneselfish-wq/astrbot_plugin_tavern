from __future__ import annotations

import copy
import unittest

from tavern.constants import DEFAULT_WORLD, DEFAULT_WORLD_SLUG, PLUGIN_VERSION
from tavern.lifecycle import (
    card_template,
    format_choices,
    normalize_choices,
    resolve_profession_stats,
    validate_card_template_config,
)
from tavern.world_contract import validate_world_contract


class V070ContractTests(unittest.TestCase):
    def test_builtin_aelvion_contract_and_description(self) -> None:
        self.assertEqual(PLUGIN_VERSION, "0.7.0")
        self.assertEqual(DEFAULT_WORLD_SLUG, "aelvion-ashen-crown")
        self.assertEqual(DEFAULT_WORLD["name"], "阿尔维恩：灰烬王冠")
        self.assertNotIn("第26", DEFAULT_WORLD["description"])
        contract = validate_world_contract(DEFAULT_WORLD)
        self.assertEqual(contract["stats"]["mode"], "preset")
        self.assertEqual(contract["resolution"]["mode"], "attribute")

    def test_preset_formula_comes_from_world(self) -> None:
        template = card_template(DEFAULT_WORLD)
        validate_card_template_config(template)
        resolved = resolve_profession_stats(
            template,
            {
                "profession": "knight",
                "primary_attribute": "力量",
                "secondary_attribute": "意志",
            },
        )
        self.assertEqual(resolved["raw"]["strength"], 15)
        self.assertEqual(resolved["raw"]["willpower"], 9)
        self.assertEqual(resolved["effective_total"], 60)

    def test_danger_does_not_force_check_and_display_is_compact(self) -> None:
        choices = normalize_choices(
            [
                {"key": "A", "text": "观察", "danger_id": "safe"},
                {"key": "B", "text": "交涉", "danger_id": "dangerous"},
                {
                    "key": "C",
                    "text": "辨认符文",
                    "danger_id": "controlled",
                    "check": {
                        "required": True,
                        "attribute_id": "intelligence",
                        "difficulty": 12,
                    },
                },
                {"key": "D", "text": "等待", "danger_id": "safe"},
            ],
            DEFAULT_WORLD,
        )
        self.assertFalse(choices[1]["requires_check"])
        rendered = format_choices("测试者", choices)
        self.assertIn('需“智力”检定', rendered)
        self.assertNotIn("已知后果", rendered)

    def test_none_stats_skip_attributes_and_reject_attribute_resolution(self) -> None:
        world = copy.deepcopy(DEFAULT_WORLD)
        world["rules"]["character_card"]["stats"] = {"mode": "none"}
        world["rules"]["resolution"] = {"mode": "none"}
        world["capabilities"]["character_stats"] = False
        world["capabilities"]["attribute_checks"] = False
        world["capabilities"]["dice_resolution"] = False
        world["rules"]["capabilities"] = world["capabilities"]
        template = card_template(world)
        self.assertEqual(template["stats"]["mode"], "none")
        self.assertEqual(template["stats"]["attributes"], [])
        self.assertFalse(
            any(item["key"].startswith("stat_") for item in template["fields"])
        )
        validate_card_template_config(template)

        world["rules"]["resolution"] = {"mode": "attribute"}
        with self.assertRaisesRegex(ValueError, "无数值世界"):
            validate_world_contract(world)


if __name__ == "__main__":
    unittest.main()
