"""Driver: `claude --bg` launch argv, follow-up sequence, deny keys, ledger."""
from __future__ import annotations

import json
import os
import unittest

from claude_code_bg import driver
from tests.support import StubEnv, set_env

SID = "abc12345-0000-4000-8000-000000000001"
COPY_SID = "def67890-0000-4000-8000-000000000002"
SPEC_SETTINGS = {"autoMemoryEnabled": False, "remoteControlAtStartup": False,
                 "skipDangerousModePermissionPrompt": True, "autoCompactWindow": 200000}
STRIPPED = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
GUIDANCE = ("Not allowed in chat mode: no one can approve this prompt. Choose a safer way, "
            "or explain in your reply what you need and stop.")


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.stub = self.enterContext(StubEnv())
        self.enterContext(self.stub.apply_env())
        set_env(self, CLAUDE_CODE_BG_SLEEP_SCALE="0", CLAUDE_CODE_BG_SETTINGS=None)
        self.cwd = self.stub.root / "work"
        self.cwd.mkdir()

    def dispatch(self, prompt: str = "hello", persona: str | None = None) -> driver.Session:
        return driver.dispatch(prompt, name="hb-0123456789", cwd=str(self.cwd),
                               model="haiku", effort="low", persona=persona,
                               hermes_session="20261021_000000_abcd")

    def follow_up(self, s: driver.Session, prompt: str = "again") -> driver.Session:
        return driver.follow_up(s, prompt, name="hb-0123456789", cwd=str(self.cwd),
                                model="claude-haiku-5-5", effort="low")

    def launches(self) -> list[dict]:
        return [c for c in self.stub.calls() if "--bg" in c["argv"]]

    def ledger(self, claude_id: str) -> dict:
        path = self.stub.hermes_home / "claude-watch" / f"{claude_id}.json"
        return json.loads(path.read_text())

    def test_dispatch_argv_and_settings(self):
        set_env(self, **{name: "1" for name in STRIPPED})
        s = self.dispatch("hello there")
        self.assertEqual(s, driver.Session("abc12345", SID, copied=False))
        [call] = self.launches()
        argv = call["argv"]
        self.assertIn("--bg", argv)
        for flag, value in (("--name", "hb-0123456789"), ("--model", "claude-haiku-5-5"),
                            ("--effort", "low"), ("--permission-mode", "bypassPermissions"),
                            ("--disallowedTools", "AskUserQuestion")):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), SPEC_SETTINGS)
        self.assertEqual(argv[-2:], ["--", "hello there"])
        self.assertNotIn("--append-system-prompt", argv)
        self.assertEqual(call["cwd"], str(self.cwd))
        for name in STRIPPED:
            self.assertNotIn(name, call["env_keys"])

    def test_settings_env_merges_over_spec(self):
        set_env(self, CLAUDE_CODE_BG_SETTINGS='{"autoCompactWindow": 100000, "x": 1}')
        self.assertEqual(driver.launch_settings(),
                         {**SPEC_SETTINGS, "autoCompactWindow": 100000, "x": 1})

    def test_dispatch_persona_flag_only_on_dispatch(self):
        s = self.dispatch(persona="Be brief.")
        self.follow_up(s)
        first, second = self.launches()
        self.assertEqual(first["argv"][first["argv"].index("--append-system-prompt") + 1],
                         "Be brief.")
        self.assertLess(first["argv"].index("--append-system-prompt"), first["argv"].index("--"))
        self.assertNotIn("--append-system-prompt", second["argv"])
        self.assertEqual(second["argv"][:2], ["--resume", SID])
        self.assertEqual(second["argv"][-2:], ["--", "again"])

    def test_dispatch_writes_chat_ledger(self):
        self.dispatch()
        rec = self.ledger("abc12345")
        self.assertEqual({k: rec[k] for k in ("mode", "claude_id", "session_id", "name", "cwd",
                                              "state", "hermes_session")},
                         {"mode": "chat", "claude_id": "abc12345", "session_id": SID,
                          "name": "hb-0123456789", "cwd": str(self.cwd), "state": "running",
                          "hermes_session": "20261021_000000_abcd"})
        self.assertTrue(rec["started"])
        self.assertTrue(rec["purpose"])

    def test_dispatch_untrusted_raises(self):
        self.stub.fx(fail_dispatch="Workspace not trusted. Run claude in /placeholder once.")
        with self.assertRaisesRegex(driver.DriverError, "not trusted"):
            self.dispatch()

    def test_dispatch_without_session_id_raises(self):
        self.stub.fx(bg_session=None)
        with self.assertRaisesRegex(driver.DriverError, "abc12345"):
            self.dispatch()
        self.assertEqual(self.ledger("abc12345")["state"], "abandoned")

    def test_follow_up_waits_for_pid_exit(self):
        set_env(self, CLAUDE_CODE_BG_SLEEP_SCALE="0.2")
        s = self.dispatch()
        self.stub.fx(pid_alive_after_stop=1.0)
        again = self.follow_up(s)
        self.assertEqual(again, driver.Session("abc12345", SID, copied=False))
        verbs = [c["argv"][0] for c in self.stub.calls() if c["argv"][0] in ("stop", "rm", "--resume")]
        self.assertEqual(verbs, ["stop", "rm", "--resume"])
        rm = next(c for c in self.stub.calls() if c["argv"][0] == "rm")
        resume = next(c for c in self.stub.calls() if c["argv"][0] == "--resume")
        self.assertIs(rm["prev_pid_alive"], False)
        self.assertIs(resume["prev_pid_alive"], False)
        self.assertEqual(rm["argv"], ["rm", "abc12345"])

    def test_follow_up_adopts_copy(self):
        s = self.dispatch()
        self.stub.fx(copy_as="def67890", copy_session=COPY_SID)
        again = self.follow_up(s)
        self.assertEqual(again, driver.Session("def67890", COPY_SID, copied=True))
        rec = self.ledger("def67890")
        self.assertEqual((rec["mode"], rec["claude_id"], rec["session_id"], rec["state"]),
                         ("chat", "def67890", COPY_SID, "running"))
        self.assertEqual(rec["hermes_session"], "20261021_000000_abcd")
        self.assertEqual(self.ledger("abc12345")["state"], "abandoned")

    def test_follow_up_resume_failure_raises(self):
        s = self.dispatch()
        self.stub.fx(fail_resume="error: session not found")
        with self.assertRaisesRegex(driver.DriverError, "session not found"):
            self.follow_up(s)

    def test_follow_up_without_row_skips_stop_and_rm(self):
        s = driver.Session("abc12345", SID)
        self.follow_up(s)
        self.assertEqual([c["argv"][0] for c in self.stub.calls() if c["argv"][0] != "agents"],
                         ["--resume"])

    def test_wait_exit_returns_after_bound(self):
        self.stub.fx(rows=[{"id": "abc12345", "sessionId": SID, "state": "working",
                            "status": "busy"}])
        driver.wait_exit("abc12345", None, tries=3, interval=0.5)
        self.assertEqual(len([c for c in self.stub.calls() if c["argv"][0] == "agents"]), 3)

    def test_agents_unreadable_is_empty(self):
        self.stub.fx(rows="not a list")
        self.assertEqual(driver.agents(), {})

    def test_pid_alive(self):
        self.assertTrue(driver.pid_alive(os.getpid()))
        for pid in (None, 0, -1, True, "123", 1.5):
            self.assertFalse(driver.pid_alive(pid), pid)

    def test_deny_key_sequence(self):
        s = self.dispatch()
        self.assertTrue(driver.deny(s.claude_id, GUIDANCE))
        calls = self.stub.tmux_calls()
        new = calls[0]
        self.assertEqual(new[:4], ["new-session", "-d", "-s", "ccbg-abc12345"])
        self.assertEqual(new[-1], f"{self.stub.command} attach abc12345")
        keys = [c[c.index("send-keys") + 3:] for c in calls if c[0] == "send-keys"]
        self.assertEqual(keys, [["Down"], ["Tab"], ["-l", GUIDANCE], ["Enter"], ["C-z"]])
        self.assertEqual(calls[-1][0], "kill-session")
        self.assertIn("=ccbg-abc12345", calls[-1])
        self.assertNotIn("stop", [c["argv"][0] for c in self.stub.calls()])

    def test_deny_without_prompt_on_screen_stops(self):
        s = self.dispatch()
        self.stub.tmux_screen.write_text("Claude is working\n")
        self.assertFalse(driver.deny(s.claude_id, GUIDANCE))
        keys = [c[3:] for c in self.stub.tmux_calls() if c[0] == "send-keys"]
        self.assertEqual(keys, [["C-z"]])
        self.assertEqual(self.stub.tmux_calls()[-1][0], "kill-session")
        self.assertIn(["stop", "abc12345"], [c["argv"] for c in self.stub.calls()])

    def test_deny_without_tmux_stops_and_returns_false(self):
        s = self.dispatch()
        set_env(self, CLAUDE_CODE_BG_TMUX=str(self.stub.root / "no-such-tmux"))
        self.assertFalse(driver.deny(s.claude_id, GUIDANCE))
        self.assertIn(["stop", "abc12345"], [c["argv"] for c in self.stub.calls()])
        self.assertEqual(self.stub.tmux_calls(), [])

    def test_nap_scale_zero_does_not_sleep(self):
        import time
        start = time.monotonic()
        driver.nap(30)
        self.assertLess(time.monotonic() - start, 1)


if __name__ == "__main__":
    unittest.main()
