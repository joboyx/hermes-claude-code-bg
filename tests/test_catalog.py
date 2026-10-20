"""Catalog: model ids, aliases, context windows, effort map, list prices."""
from __future__ import annotations

import unittest

from claude_code_bg import catalog
from tests.support import set_env


class CatalogTest(unittest.TestCase):
    def test_resolve_aliases_and_default(self):
        self.assertEqual(catalog.resolve("opus"), "claude-opus-5-5")
        self.assertEqual(catalog.resolve("sonnet-5.5"), "claude-sonnet-5-5")
        self.assertEqual(catalog.resolve("haiku-5-5"), "claude-haiku-5-5")
        self.assertEqual(catalog.resolve("claude-haiku-5-5"), "claude-haiku-5-5")
        self.assertEqual(catalog.resolve(None), catalog.DEFAULT_MODEL)
        self.assertEqual(catalog.resolve(""), "claude-opus-5-5")
        for alias, model in catalog.ALIASES.items():
            self.assertIn(model, catalog.MODELS, alias)

    def test_unknown_id_passes_through(self):
        self.assertEqual(catalog.resolve("claude-future-9"), "claude-future-9")

    def test_context_window(self):
        for model in catalog.MODELS:
            self.assertEqual(catalog.context_window(model), 1_000_000)
        self.assertEqual(catalog.context_window("claude-future-9"), 200_000)

    def test_effort_map_and_env_default(self):
        set_env(self, CLAUDE_CODE_BG_EFFORT=None)
        cases = {"none": "low", "minimal": "low", "low": "low", "medium": "medium",
                 "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max"}
        for value, effort in cases.items():
            self.assertEqual(catalog.effort_of(value), effort, value)
        self.assertEqual(catalog.effort_of(None), "high")
        self.assertEqual(catalog.effort_of("bogus"), catalog.DEFAULT_EFFORT)
        set_env(self, CLAUDE_CODE_BG_EFFORT="medium")
        self.assertEqual(catalog.effort_of(None), "medium")
        self.assertEqual(catalog.effort_of("max"), "max")

    def test_list_cost_opus(self):
        usage = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.assertEqual(catalog.list_cost("claude-opus-5-5", usage), 4.0)
        mixed = {"input": 0, "output": 1_000_000, "cache_read": 1_000_000,
                 "cache_creation": 1_000_000}
        self.assertAlmostEqual(catalog.list_cost("claude-haiku-5-5", mixed), 0.71)

    def test_list_cost_unknown_model_none(self):
        usage = {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.assertIsNone(catalog.list_cost("claude-future-9", usage))


if __name__ == "__main__":
    unittest.main()
