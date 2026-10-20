"""Registration: the provider profile and its hooks, loaded the way Hermes loads an installed plugin.

Needs the Hermes agent source: `$HERMES_AGENT_REPO` (default `~/.hermes/hermes-agent`). The module
is skipped when `providers` cannot be imported.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

HERMES_AGENT_REPO = os.environ.get("HERMES_AGENT_REPO") or os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_AGENT_REPO not in sys.path:
    sys.path.append(HERMES_AGENT_REPO)
try:
    import providers
except ImportError as exc:
    raise unittest.SkipTest(f"Hermes agent repo not importable (set HERMES_AGENT_REPO): {exc}")

from claude_code_bg import catalog
from claude_code_bg.client import CANCELLED, AuxiliaryRefused
from tests.support import StubEnv, set_env

NAME = "claude-code-bg"
PACKAGE = Path(__file__).resolve().parent.parent / "claude_code_bg"
COST_NOTE = "list-price equivalent; not a subscription invoice"
STATE = {}


def install(home: Path) -> None:
    """Install the plugin into `home` the way the install script does: a symlink under plugins/."""
    plugins = home / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    link = plugins / NAME
    if not link.exists():
        link.symlink_to(PACKAGE, target_is_directory=True)


def setUpModule():
    STATE["home"] = Path(tempfile.mkdtemp(prefix="ccbg-profile-")).resolve()
    STATE["saved_home"] = os.environ.get("HERMES_HOME")
    install(STATE["home"])
    os.environ["HERMES_HOME"] = str(STATE["home"])


def tearDownModule():
    if STATE["saved_home"] is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = STATE["saved_home"]
    shutil.rmtree(STATE["home"], ignore_errors=True)


def profile():
    return providers.get_provider_profile(NAME)


class ProfileTest(unittest.TestCase):
    def test_profile_registered(self):
        p = profile()
        self.assertIsNotNone(p)
        self.assertEqual(providers.provider_source(NAME), "user")
        self.assertIn(p, providers.list_providers())
        self.assertEqual((p.name, p.api_mode, p.auth_type, p.base_url, p.process_command),
                         (NAME, "chat_completions", "external_process", "process://claude-code-bg",
                          "claude"))
        self.assertFalse(p.supports_health_check)
        self.assertTrue(p.supports_vision)
        self.assertIsNotNone(p.classify_api_error)
        self.assertEqual(p.fetch_models(), list(catalog.MODELS))

    def test_installed_plugin_imports_without_repo_on_path(self):
        # Hermes imports the plugin dir under its own module name, with only the Hermes repo on
        # sys.path. Importing it must register the profile, start no thread and write no file.
        home = Path(tempfile.mkdtemp(prefix="ccbg-install-")).resolve()
        self.addCleanup(shutil.rmtree, home, True)
        install(home)
        script = (
            "import sys, threading\n"
            "from providers import get_provider_profile\n"
            f"p = get_provider_profile({NAME!r})\n"
            "client = sys.modules['claude_code_bg.client']\n"
            "print(type(p).__module__)\n"
            "print(p.name)\n"
            "print(p.classify_api_error(client.AuxiliaryRefused('x')) is not None)\n"
            "print([t.name for t in threading.enumerate() if 'claude' in t.name])\n"
        )
        env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": HERMES_AGENT_REPO}
        run = subprocess.run([sys.executable, "-c", script], cwd=home, env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stderr)
        module, name, classified, threads = run.stdout.splitlines()[-4:]
        self.assertTrue(module.startswith("_hermes_user_provider_"), module)
        self.assertEqual((name, classified, threads), (NAME, "True", "[]"))
        # Hermes creates its own home dirs on import; the plugin's state dirs must not appear.
        self.assertFalse((home / "claude-code-bg").exists())
        self.assertFalse((home / "claude-watch").exists())

    def test_extras_chat_vs_auxiliary(self):
        p = profile()
        self.assertEqual(
            p.build_api_kwargs_extras(reasoning_config={"effort": "low"}, session_id="s-1",
                                      model="claude-opus-5-5"),
            ({}, {"hermes_session_id": "s-1", "hermes_reasoning_effort": "low"}))
        # Auxiliary calls (titles, compression, vision) carry no session_id at all.
        self.assertEqual(p.build_api_kwargs_extras(reasoning_config=None, task="title_generation"),
                         ({}, {"hermes_auxiliary": True}))
        self.assertEqual(p.build_api_kwargs_extras(session_id=None), ({}, {}))
        self.assertEqual(p.build_api_kwargs_extras(reasoning_config={"enabled": False},
                                                   session_id="s-1"),
                         ({}, {"hermes_session_id": "s-1"}))

    def test_context_length(self):
        p = profile()
        for model in catalog.MODELS:
            self.assertEqual(p.get_model_context_length(model), 1_000_000)
        self.assertEqual(p.get_model_context_length("opus"), 1_000_000)
        self.assertEqual(p.get_model_context_length("claude-unknown-1"), 200_000)

    def test_model_capabilities_vision(self):
        from agent.models_dev import get_model_capabilities
        p = profile()
        self.assertEqual(set(p.model_capabilities), set(catalog.MODELS))
        for model in catalog.MODELS:
            self.assertEqual(p.model_capabilities[model],
                             {"supports_vision": True, "context_window": 1_000_000})
            caps = get_model_capabilities(NAME, model)
            self.assertTrue(caps.supports_vision)
            self.assertEqual(caps.context_window, 1_000_000)

    def test_classify_cancel_and_aux(self):
        from agent.error_classifier import FailoverReason, classify_api_error
        p = profile()
        self.assertEqual(p.classify_api_error(RuntimeError(CANCELLED), status_code=None),
                         {"reason": "timeout"})
        self.assertEqual(p.classify_api_error(AuxiliaryRefused("aux")),
                         {"reason": "format_error", "retryable": False, "should_fallback": True})
        self.assertIsNone(p.classify_api_error(RuntimeError("something else")))
        self.assertIsNone(p.classify_api_error(ValueError(CANCELLED)))
        # Through Hermes' own classifier, which consults the profile for this provider.
        verdict = classify_api_error(RuntimeError(CANCELLED), provider=NAME, model="claude-opus-5-5")
        self.assertEqual(verdict.reason, FailoverReason.timeout)
        verdict = classify_api_error(AuxiliaryRefused("aux"), provider=NAME, model="claude-opus-5-5")
        self.assertEqual(verdict.reason, FailoverReason.format_error)
        self.assertFalse(verdict.retryable)
        self.assertTrue(verdict.should_fallback)

    def test_get_usage_cost_estimate(self):
        from agent.usage_pricing import estimate_usage_cost, normalize_usage
        # The usage block the client returns: prompt buckets of the LAST call, sums in turn_usage.
        response_usage = {
            "prompt_tokens": 50_000 + 10, "completion_tokens": 500, "total_tokens": 50_510,
            "prompt_tokens_details": {"cached_tokens": 50_000, "cache_write_tokens": 0},
            "cache_creation_input_tokens": 0,
            "turn_usage": {"input": 1000, "cache_read": 100_000, "cache_creation": 2000,
                           "output": 500, "api_calls": 2},
        }
        usage = normalize_usage(response_usage, provider=NAME, api_mode="chat_completions")
        cost = estimate_usage_cost("claude-opus-5-5", usage, provider=NAME)
        # (1000 * 4 + 500 * 20 + 2000 * 8 + 100000 * 0.20) / 1e6 USD
        self.assertEqual(cost.amount_usd, Decimal("0.05"))
        self.assertEqual((cost.status, cost.label), ("estimated", "~$0.05"))
        self.assertIn(COST_NOTE, cost.notes)

        # A summed usage (no raw block) falls back to the canonical buckets.
        summed = usage + usage
        cost = estimate_usage_cost("claude-opus-5-5", summed, provider=NAME)
        # input 2 * 10, cache_read 2 * 50000, output 2 * 500
        self.assertEqual(cost.amount_usd, Decimal("0.04008"))

        cost = estimate_usage_cost("claude-unknown-1", usage, provider=NAME)
        self.assertEqual((cost.status, cost.amount_usd), ("unknown", None))


class AccountUsageTest(unittest.TestCase):
    def setUp(self):
        self.stub = self.enterContext(StubEnv())
        self.enterContext(self.stub.apply_env())
        install(self.stub.hermes_home)

    def usage(self):
        from agent.account_usage import fetch_account_usage, render_account_usage_lines
        snapshot = fetch_account_usage(NAME)
        return snapshot, render_account_usage_lines(snapshot)

    def test_usage_command_shows_plan_limits(self):
        limits = {"limits": [{"kind": "session", "percent": 45, "resets_at": "2026-10-01T07:09:00Z"},
                             {"kind": "weekly_all", "percent": 23}]}
        self.stub.fx(usage_body={"rate_limits_available": True, "rate_limits": limits})
        snapshot, lines = self.usage()
        self.assertEqual([(w.label, w.used_percent) for w in snapshot.windows],
                         [("Current session", 45.0), ("Current week", 23.0)])
        self.assertEqual(lines[:2], ["\U0001F4C8 Claude plan limits", f"Provider: {NAME}"])
        self.assertTrue(lines[2].startswith("Current session: 55% remaining (45% used)"), lines)
        self.assertIsNone(snapshot.unavailable_reason)

    def test_usage_failure_is_an_unavailable_line(self):
        self.stub.fx(usage_raw="not json")
        snapshot, lines = self.usage()
        self.assertEqual(snapshot.windows, ())
        self.assertEqual(lines[-1], "Unavailable: Claude Code gave no usage answer "
                                    "(needs a version with get_usage)")

    def test_usage_with_claude_missing(self):
        set_env(self, CLAUDE_CODE_BG_COMMAND=str(self.stub.root / "no-such-claude"))
        _, lines = self.usage()
        self.assertTrue(lines[-1].startswith("Unavailable: Claude Code is not installed"), lines)


if __name__ == "__main__":
    unittest.main()
