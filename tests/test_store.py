"""Store: conversation state files, locks, event log, paths."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claude_code_bg import store
from tests.support import set_env

REPO = Path(__file__).resolve().parent.parent


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="ccbg-store-")).resolve()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        set_env(self, HERMES_HOME=str(self.home), CLAUDE_CODE_BG_CWD=None)

    def test_hermes_home_env_then_default(self):
        self.assertEqual(store.hermes_home(), self.home)
        self.assertEqual(store.base_dir(), self.home / "claude-code-bg")
        set_env(self, HERMES_HOME=None)
        self.assertEqual(store.hermes_home(), Path.home() / ".hermes")

    def test_load_missing_has_turns(self):
        self.assertEqual(store.load("conv-a"), {"turns": {}})
        state = {"claude_id": "abc12345"}
        store.save("conv-b", state)
        self.assertEqual(store.load("conv-b"), {"claude_id": "abc12345", "turns": {}})

    def test_save_is_atomic_roundtrip(self):
        first = {"claude_id": "abc12345", "turns": {"k1": {"state": "sent", "offset": 10}}}
        store.save("conv-a", first)
        path = self.home / "claude-code-bg" / "conv" / "conv-a.json"
        self.assertEqual(json.loads(path.read_text()), first)
        with open(path) as old:
            second = {"claude_id": "abc12345", "turns": {"k1": {"state": "done", "text": "ok"}}}
            store.save("conv-a", second)
            # The old file handle still sees the old content: save replaced
            # the file instead of rewriting it in place.
            self.assertEqual(json.loads(old.read()), first)
        self.assertEqual(store.load("conv-a"), second)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["conv-a.json"])

    def test_all_conversations(self):
        self.assertEqual(store.all_conversations(), [])
        store.save("conv-b", {"turns": {}})
        store.save("conv-a", {"turns": {}})
        with store.FileLock("conv-c"):
            pass
        self.assertEqual(store.all_conversations(), ["conv-a", "conv-b"])

    def test_thread_lock_same_object_per_key(self):
        lock = store.thread_lock("conv-a")
        self.assertIs(store.thread_lock("conv-a"), lock)
        self.assertIsNot(store.thread_lock("conv-b"), lock)
        self.assertIsInstance(lock, type(threading.Lock()))

    def test_file_lock_excludes_second_process(self):
        out = self.home / "child.log"
        with store.FileLock("conv-a"):
            self.assertTrue((self.home / "claude-code-bg" / "conv" / "conv-a.lock").exists())
            child = subprocess.Popen(
                [sys.executable, "-m", "tests.lock_child", "conv-a", str(out)],
                cwd=REPO, env=dict(os.environ))
            self.addCleanup(child.kill)
            deadline = time.time() + 10
            while "started" not in (out.read_text() if out.exists() else ""):
                self.assertLess(time.time(), deadline, "child did not start")
                time.sleep(0.05)
            time.sleep(0.5)
            self.assertNotIn("acquired", out.read_text())
            self.assertIsNone(child.poll())
            released = time.time()
        self.assertEqual(child.wait(timeout=10), 0)
        marks = dict(line.split() for line in out.read_text().splitlines())
        self.assertGreaterEqual(float(marks["acquired"]), released)

    def test_file_lock_non_blocking_raises_when_held(self):
        lock_path = self.home / "claude-code-bg" / "conv" / "conv-a.lock"
        with store.FileLock("conv-a"):
            # flock conflicts between two open files, also in one process.
            with self.assertRaises(BlockingIOError):
                with store.FileLock("conv-a", blocking=False):
                    self.fail("entered a held lock")
        with store.FileLock("conv-a", blocking=False):
            self.assertTrue(lock_path.exists())
        self.assertTrue(lock_path.exists())

    def test_event_appends_jsonl(self):
        before = time.time()
        store.event({"kind": "request", "conv": "conv-a"})
        store.event({"kind": "done", "conv": "conv-a", "ms": 12})
        lines = (self.home / "claude-code-bg" / "events.jsonl").read_text().splitlines()
        rows = [json.loads(line) for line in lines]
        self.assertEqual([r["kind"] for r in rows], ["request", "done"])
        self.assertEqual(rows[1]["ms"], 12)
        for row in rows:
            self.assertGreaterEqual(row["t"], round(before, 2) - 0.01)

    def test_event_swallows_oserror(self):
        # HERMES_HOME is a regular file, so the log directory cannot exist.
        blocker = self.home / "not-a-dir"
        blocker.write_text("")
        set_env(self, HERMES_HOME=str(blocker))
        store.event({"kind": "request"})

    def test_default_cwd_env_override(self):
        self.assertEqual(store.default_cwd(), str(self.home / "claude-code-bg" / "cwd"))
        set_env(self, CLAUDE_CODE_BG_CWD="/srv/placeholder/workspace")
        self.assertEqual(store.default_cwd(), "/srv/placeholder/workspace")


if __name__ == "__main__":
    unittest.main()
