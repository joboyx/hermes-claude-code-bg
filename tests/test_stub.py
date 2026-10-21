"""Stub `claude`: what each CLI call prints, records and changes."""
from __future__ import annotations

import json
import os
import subprocess
import time
import unittest

from tests.support import StubEnv

SID = "abc12345-0000-4000-8000-000000000001"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int, bound: float = 5.0) -> bool:
    end = time.monotonic() + bound
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class StubClaudeTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubEnv()
        self.stub.__enter__()
        self.addCleanup(self.stub.__exit__, None, None, None)
        self.cwd = self.stub.root / "work"
        self.cwd.mkdir()

    def run_claude(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        done = subprocess.run([self.stub.command, *argv], env=self.stub.env(), cwd=self.cwd,
                              capture_output=True, text=True, timeout=30)
        if check:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done

    def rows(self) -> dict[str, dict]:
        return {r["id"]: r for r in json.loads(self.run_claude("agents", "--json", "--all").stdout)}

    def test_dispatch_prints_id_and_adds_row(self):
        out = self.run_claude("--bg", "--name", "n", "--", "hi").stdout
        self.assertIn("backgrounded · abc12345", out)
        row = self.rows()["abc12345"]
        self.assertEqual(row["sessionId"], SID)
        self.assertEqual(row["name"], "n")
        self.assertEqual((row["state"], row["status"]), ("done", "idle"))
        self.assertEqual(row["cwd"], str(self.cwd.resolve()))
        self.assertTrue(pid_alive(row["pid"]))

    def test_rows_fixture_is_listed(self):
        self.stub.fx(rows=[{"id": "other001", "sessionId": "s-1", "state": "working", "status": "busy"}])
        self.assertEqual(self.rows()["other001"]["state"], "working")

    def test_stop_then_rm_removes_row(self):
        self.run_claude("--bg", "--name", "n", "--", "hi")
        pid = self.rows()["abc12345"]["pid"]
        self.run_claude("stop", "abc12345")
        row = self.rows()["abc12345"]
        self.assertEqual((row["state"], row["status"]), ("stopped", None))
        self.assertFalse(pid_alive(pid))
        self.run_claude("rm", "abc12345")
        self.assertNotIn("abc12345", self.rows())

    def test_exit_after_keeps_status_for_n_agents_calls(self):
        self.stub.fx(exit_after=2)
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("stop", "abc12345")
        statuses = [self.rows()["abc12345"]["status"] for _ in range(3)]
        self.assertEqual(statuses, ["idle", "idle", None])

    def test_pid_alive_after_stop(self):
        self.stub.fx(pid_alive_after_stop=0.6)
        self.run_claude("--bg", "--name", "n", "--", "hi")
        pid = self.rows()["abc12345"]["pid"]
        stopped_at = time.monotonic()
        self.run_claude("stop", "abc12345")
        self.assertTrue(pid_alive(pid))
        self.assertTrue(wait_dead(pid))
        self.assertGreaterEqual(time.monotonic() - stopped_at, 0.5)

    def test_resume_same_session_keeps_id(self):
        self.run_claude("--bg", "--name", "n", "--", "hi")
        out = self.run_claude("--resume", SID, "--bg", "--name", "n", "--", "again").stdout
        self.assertIn("backgrounded · abc12345", out)
        self.assertEqual(self.rows()["abc12345"]["sessionId"], SID)

    def test_resume_without_transcript_fails_the_row(self):
        out = self.run_claude("--resume", SID, "--bg", "--name", "n", "--", "again").stdout
        self.assertIn("backgrounded · abc12345", out)
        row = self.rows()["abc12345"]
        self.assertEqual((row["state"], row["status"], row["pid"]), ("failed", None, None))
        self.assertFalse((self.stub.projects / "stub" / f"{SID}.jsonl").exists())

    def test_resume_copy_note(self):
        self.stub.fx(copy_as="def67890", copy_session="def67890-0000-4000-8000-000000000002")
        self.run_claude("--bg", "--name", "n", "--", "hi")
        lines = self.run_claude("--resume", SID, "--bg", "--name", "n", "--", "again").stdout.splitlines()
        self.assertEqual(lines[0], "note: abc12345 is still running; started a copy as def67890")
        self.assertIn("backgrounded · def67890", lines[1])
        self.assertEqual(self.rows()["def67890"]["sessionId"], "def67890-0000-4000-8000-000000000002")

    def test_fail_resume(self):
        self.stub.fx(fail_resume="error: session not found")
        done = self.run_claude("--resume", SID, "--bg", "--name", "n", "--", "again", check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("error: session not found", done.stderr)
        self.assertEqual(self.rows(), {})

    def test_transcript_appended_with_prompt(self):
        entry = {"type": "user", "message": {"role": "user", "content": "{{prompt}}"}}
        self.stub.fx(append_transcript=[entry])
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("--resume", SID, "--bg", "--name", "n", "--", 'say "x"')
        path = self.stub.projects / "stub" / f"{SID}.jsonl"
        contents = [json.loads(line)["message"]["content"] for line in path.read_text().splitlines()]
        self.assertEqual(contents, ["hi", 'say "x"'])

    def test_jobs_state_written(self):
        self.stub.fx(jobs_state={"inFlight": {"tasks": 1}})
        self.run_claude("--bg", "--name", "n", "--", "hi")
        state = json.loads((self.stub.jobs / "abc12345" / "state.json").read_text())
        self.assertEqual(state, {"inFlight": {"tasks": 1}})

    def test_calls_logged(self):
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("agents", "--json", "--all")
        calls = self.stub.calls()
        self.assertEqual([c["argv"] for c in calls],
                         [["--bg", "--name", "n", "--", "hi"], ["agents", "--json", "--all"]])
        self.assertEqual(calls[0]["cwd"], str(self.cwd.resolve()))
        self.assertIn("STUB_FX", calls[0]["env_keys"])

    def test_prev_pid_alive_logged_on_rm_and_resume(self):
        self.stub.fx(pid_alive_after_stop=5)
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("stop", "abc12345")
        self.run_claude("rm", "abc12345")
        self.run_claude("--resume", "ffff0000-x", "--bg", "--name", "n", "--", "other")
        calls = self.stub.calls()
        self.assertIs(calls[2]["prev_pid_alive"], True)
        self.assertIsNone(calls[3]["prev_pid_alive"])
        self.assertNotIn("prev_pid_alive", calls[0])

    def test_fail_dispatch(self):
        self.stub.fx(fail_dispatch="Workspace not trusted. Run claude in /placeholder once.")
        done = self.run_claude("--bg", "--name", "n", "--", "hi", check=False)
        self.assertEqual(done.returncode, 1)
        self.assertIn("Workspace not trusted", done.stderr)
        self.assertEqual(self.rows(), {})

    def test_rm_and_relaunch_clear_exit_countdown(self):
        self.stub.fx(exit_after=5)
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("stop", "abc12345")
        self.run_claude("rm", "abc12345")
        self.assertEqual(self.stub.fx()["_exiting"], {})
        self.run_claude("stop", "abc12345")  # no row: no countdown
        self.run_claude("--bg", "--name", "n", "--", "hi")
        self.run_claude("stop", "abc12345")
        self.run_claude("--resume", SID, "--bg", "--name", "n", "--", "again")
        self.assertEqual(self.stub.fx()["_exiting"], {})

    def test_fake_tmux_logs_and_prints_screen(self):
        env = self.stub.env()
        out = subprocess.run([self.stub.tmux, "capture-pane", "-t", "=x:", "-p"], env=env,
                             capture_output=True, text=True, timeout=30).stdout
        self.assertIn("Do you want to proceed?", out)
        self.stub.tmux_screen.write_text("plain screen\n")
        out = subprocess.run([self.stub.tmux, "capture-pane"], env=env,
                             capture_output=True, text=True, timeout=30).stdout
        self.assertEqual(out, "plain screen\n")
        self.assertEqual(self.stub.tmux_calls(),
                         [["capture-pane", "-t", "=x:", "-p"], ["capture-pane"]])

    def test_env_points_at_stub_dirs(self):
        env = self.stub.env()
        self.assertEqual(env["HERMES_HOME"], str(self.stub.hermes_home))
        self.assertEqual(env["CLAUDE_PROJECTS"], str(self.stub.projects))
        self.assertEqual(env["CLAUDE_JOBS"], str(self.stub.jobs))
        self.assertEqual(env["CLAUDE_CODE_BG_COMMAND"], self.stub.command)
        self.assertTrue(os.access(self.stub.command, os.X_OK))

    def test_apply_env_sets_and_restores(self):
        before = os.environ.get("CLAUDE_CODE_BG_COMMAND")
        with self.stub.apply_env():
            self.assertEqual(os.environ["CLAUDE_CODE_BG_COMMAND"], self.stub.command)
        self.assertEqual(os.environ.get("CLAUDE_CODE_BG_COMMAND"), before)

    def test_exit_kills_live_children(self):
        self.run_claude("--bg", "--name", "n", "--", "hi")
        pid = self.rows()["abc12345"]["pid"]
        self.stub.__exit__(None, None, None)
        self.assertTrue(wait_dead(pid))


if __name__ == "__main__":
    unittest.main()
