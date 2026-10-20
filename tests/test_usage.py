"""`/usage`: plan limits from Claude Code's own `get_usage` control request (stub `claude`)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timezone

from claude_code_bg import usage
from tests.support import StubEnv, set_env

HERMES_AGENT_REPO = os.environ.get("HERMES_AGENT_REPO") or os.path.expanduser("~/.hermes/hermes-agent")
if HERMES_AGENT_REPO not in sys.path:
    sys.path.append(HERMES_AGENT_REPO)
try:
    import agent.account_usage  # noqa: F401
    HAS_HERMES = True
except ImportError:
    HAS_HERMES = False

# Payload shapes from the claude-subscription-directsdk account-usage tests.
LIMITS = {"limits": [
    {"kind": "session", "percent": 45, "resets_at": "2026-10-01T07:09:00Z"},
    {"kind": "weekly_all", "percent": 23, "resets_at": "2026-10-02T06:59:00Z"},
    {"kind": "weekly_model", "percent": 17, "resets_at": "2026-10-02T06:59:00Z",
     "scope": {"model": {"display_name": "Fable"}}},
    {"kind": "something_new", "percent": None, "resets_at": None},
    {"kind": ["odd"], "percent": float("nan"), "scope": "odd"},
]}
LEGACY = {"five_hour": {"utilization": 0.4, "resets_at": "2026-10-01T07:09:00Z"},
          "seven_day": {"utilization": 12}, "seven_day_opus": {"utilization": True},
          "seven_day_sonnet": "odd",
          "extra_usage": {"is_enabled": True, "used_credits": 1.5, "monthly_limit": 10,
                          "currency": "EUR"}}
SIMPLE = {"limits": [{"kind": "session", "percent": 45, "resets_at": "2026-10-01T07:09:00Z"}]}


def body(rate_limits=None, available=True) -> dict:
    return {"subscription_type": "max" if available else None,
            "rate_limits_available": available, "rate_limits": rate_limits,
            "session": {}, "behaviors": None}


@unittest.skipUnless(HAS_HERMES, "Hermes agent repo not importable (set HERMES_AGENT_REPO)")
class UsageWindowsTest(unittest.TestCase):
    def test_usage_windows_limits_and_legacy(self):
        windows, details = usage.usage_windows(LIMITS)
        self.assertEqual([(w.label, w.used_percent) for w in windows], [
            ("Current session", 45.0), ("Current week", 23.0), ("Fable week", 17.0),
            ("something_new", None), ("['odd']", None)])
        self.assertEqual(windows[0].reset_at, datetime(2026, 10, 1, 7, 9, tzinfo=timezone.utc))
        self.assertIsNone(windows[3].reset_at)
        self.assertEqual(details, [])

        windows, details = usage.usage_windows(LEGACY)
        self.assertEqual([(w.label, w.used_percent) for w in windows],
                         [("Current session", 40.0), ("Current week", 12.0)])
        self.assertEqual(details, ["Extra usage: 1.50 / 10.00 EUR"])

    def test_usage_windows_odd_shapes_degrade(self):
        self.assertEqual(usage.usage_windows({}), ([], []))
        self.assertEqual(usage.usage_windows({"limits": "odd", "five_hour": []}), ([], []))
        windows, _ = usage.usage_windows({"limits": [{"kind": "session", "resets_at": "not a date"}]})
        self.assertEqual([(w.label, w.used_percent, w.reset_at) for w in windows],
                         [("Current session", None, None)])


class FetchUsageTest(unittest.TestCase):
    def setUp(self):
        self.stub = self.enterContext(StubEnv())
        self.enterContext(self.stub.apply_env())
        set_env(self, ANTHROPIC_API_KEY="sk-test-placeholder", CLAUDECODE="1")

    def call(self) -> dict:
        calls = [c for c in self.stub.calls() if "-p" in c["argv"]]
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_limits_from_get_usage_control_request(self):
        self.stub.fx(usage_body=body(SIMPLE))
        limits, reason = usage.fetch_usage()
        self.assertEqual((limits, reason), (SIMPLE, None))
        self.assertEqual(self.stub.fx()["_requests"], [{
            "type": "control_request", "request_id": "hermes-usage",
            "request": {"subtype": "get_usage", "skip_behaviors": True}}])
        call = self.call()
        for flag in ("--input-format", "--output-format", "--no-session-persistence",
                     "--disable-slash-commands", "--strict-mcp-config"):
            self.assertIn(flag, call["argv"])
        # The CLI uses its own login: no API key, no child-session marker, no base URL override.
        for name in ("ANTHROPIC_API_KEY", "CLAUDECODE", "ANTHROPIC_BASE_URL"):
            self.assertNotIn(name, call["env_keys"])

    def test_explicit_command_wins(self):
        self.stub.fx(usage_body=body(SIMPLE))
        set_env(self, CLAUDE_CODE_BG_COMMAND="/no/such/claude")
        self.assertEqual(usage.fetch_usage(command=self.stub.command), (SIMPLE, None))

    def test_failures_name_the_reason(self):
        cases = [
            ({"usage_body": body(None, available=False)},
             "no Claude subscription login found (run `claude` and log in)"),
            ({"usage_body": body(None)}, "Claude Code returned no plan limits"),
            ({"usage_body": body(None), "usage_subtype": "error"},
             "Claude Code refused the usage request (needs a version with get_usage)"),
            ({"usage_raw": "not json"},
             "Claude Code gave no usage answer (needs a version with get_usage)"),
        ]
        for keys, reason in cases:
            with self.subTest(reason=reason):
                fixture = {k: v for k, v in self.stub.fx().items() if not k.startswith("usage_")}
                self.stub.fx_path.write_text(json.dumps({**fixture, **keys}))
                self.assertEqual(usage.fetch_usage(), (None, reason))

    def test_quiet_mode_says_why_limits_are_missing(self):
        set_env(self, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        self.stub.fx(usage_body=body(None))
        limits, reason = usage.fetch_usage()
        self.assertIsNone(limits)
        self.assertIn("nonessential traffic off", reason)

    def test_timeout(self):
        self.stub.fx(usage_body=body(SIMPLE), usage_delay=5)
        self.assertEqual(usage.fetch_usage(timeout=0.5),
                         (None, "Claude Code did not answer in time"))

    def test_missing_claude_code(self):
        missing = str(self.stub.root / "no-such-claude")
        limits, reason = usage.fetch_usage(command=missing)
        self.assertIsNone(limits)
        self.assertTrue(reason.startswith("Claude Code is not installed"), reason)
        self.assertEqual(self.stub.calls(), [])


if __name__ == "__main__":
    unittest.main()
