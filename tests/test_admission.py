"""Admission (live-session caps, host memory) and the idle reaper."""
from __future__ import annotations

import json
import threading
import time
import unittest

from claude_code_bg import admission, driver, store
from tests.support import StubEnv, set_env

BUSY = ("Claude Code is busy on this host right now (3 live sessions, 1000 MB free). "
        "Send your message again in a few minutes.")


def meminfo(available_kb: int | None) -> str:
    lines = ["MemTotal:       16384000 kB", "MemFree:          512000 kB"]
    if available_kb is not None:
        lines.append(f"MemAvailable:   {available_kb} kB")
    return "\n".join(lines + ["Buffers:          100000 kB", ""])


class AdmissionTest(unittest.TestCase):
    def setUp(self):
        self.stub = self.enterContext(StubEnv())
        self.enterContext(self.stub.apply_env())
        self.meminfo = self.stub.root / "meminfo"
        self.set_mem_mb(8000)
        set_env(self, CLAUDE_CODE_BG_SLEEP_SCALE="0", CLAUDE_CODE_BG_SETTINGS=None,
                CLAUDE_CODE_BG_MEMINFO=str(self.meminfo), CLAUDE_CODE_BG_MAX_LIVE=None,
                CLAUDE_CODE_BG_MAX_CHATS=None, CLAUDE_CODE_BG_MIN_MEM_MB=None,
                CLAUDE_CODE_BG_IDLE_STOP_S=None)
        self.cwd = self.stub.root / "work"
        self.cwd.mkdir()

    def set_mem_mb(self, mb: int) -> None:
        self.meminfo.write_text(meminfo(mb * 1024))

    def session(self, cid: str, conv: str | None = None, *, chat: bool = True,
                done_ago: float | None = None, turns: dict | None = None, **row) -> None:
        """Start a stub row with a live pid; own chat ledger and conversation state optional."""
        sid = f"{cid}-0000-4000-8000-000000000000"
        self.stub.fx(bg_id=cid, bg_session=sid)
        driver.dispatch("hello", name=f"hb-{cid}", cwd=str(self.cwd), model="haiku",
                        effort="low", persona=None, hermes_session=None)
        if not chat:
            (self.stub.hermes_home / "claude-watch" / f"{cid}.json").unlink()
        if row:
            rows = self.stub.fx()["rows"]
            for r in rows:
                if r["id"] == cid:
                    r.update(row)
            self.stub.fx(rows=rows)
        if conv:
            last = time.time() - done_ago if done_ago is not None else None
            store.save(conv, {"claude_id": cid, "session_id": sid, "turns": turns or {},
                              "last_done_at": last})

    def stops(self) -> list[str]:
        return [c["argv"][1] for c in self.stub.calls() if c["argv"][:1] == ["stop"]]

    def events(self) -> list[dict]:
        path = self.stub.hermes_home / "claude-code-bg" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    # memory

    def test_mem_available_reads_meminfo(self):
        self.assertEqual(admission.mem_available_mb(), 8000)
        self.meminfo.write_text(meminfo(None))
        self.assertIsNone(admission.mem_available_mb())
        set_env(self, CLAUDE_CODE_BG_MEMINFO=str(self.stub.root / "missing"))
        self.assertIsNone(admission.mem_available_mb())

    def test_idle_seconds_low_memory(self):
        self.assertEqual(admission.idle_stop_seconds(), 600)
        self.set_mem_mb(1200)
        self.assertEqual(admission.idle_stop_seconds(), 120)
        self.set_mem_mb(1228)
        self.assertEqual(admission.idle_stop_seconds(), 600)
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S="300")
        self.assertEqual(admission.idle_stop_seconds(), 300)
        self.set_mem_mb(1000)
        self.assertEqual(admission.idle_stop_seconds(), 120)
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S="0")
        self.assertEqual(admission.idle_stop_seconds(), 0)
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S=None,
                CLAUDE_CODE_BG_MEMINFO=str(self.stub.root / "missing"))
        self.assertEqual(admission.idle_stop_seconds(), 600)

    # admission

    def test_admit_ok_when_under_caps(self):
        self.session("bbbb0001", chat=False)
        self.session("cccc0001", "conv-other", done_ago=30)
        self.assertEqual(admission.admit("conv-me"),
                         admission.Verdict(ok=True, live=2, mem_mb=8000))
        self.assertEqual(self.stops(), [])

    def test_admit_refuses_over_live_cap_then_reaps_idle_chat(self):
        set_env(self, CLAUDE_CODE_BG_MAX_LIVE="3")
        self.session("cccc0001", "conv-old", done_ago=900)
        self.session("cccc0002", "conv-new", done_ago=100)
        self.session("bbbb0001", chat=False)
        verdict = admission.admit("conv-me")
        self.assertEqual(verdict, admission.Verdict(ok=True, live=2, mem_mb=8000))
        self.assertEqual(self.stops(), ["cccc0001"])
        self.assertIsNotNone(store.load("conv-old").get("reaped_at"))
        self.assertIsNone(store.load("conv-new").get("reaped_at"))

    def test_admit_refuses_over_chat_cap_when_no_chat_is_idle(self):
        set_env(self, CLAUDE_CODE_BG_MAX_LIVE="10")
        sent = {"k1": {"state": "sent"}}
        self.session("cccc0001", "conv-a", done_ago=900, turns=sent, state="working",
                     status="busy")
        self.session("cccc0002", "conv-b", done_ago=900, state="working", status="busy")
        verdict = admission.admit("conv-me")
        self.assertEqual(verdict, admission.Verdict(ok=False, live=2, mem_mb=8000))
        self.assertEqual(self.stops(), [])
        self.assertIn("admission", [e.get("kind") for e in self.events()])

    def test_admit_refuses_when_memory_low(self):
        self.set_mem_mb(1000)
        self.session("bbbb0001", chat=False)
        self.session("bbbb0002", chat=False)
        self.session("cccc0001", "conv-idle", done_ago=60)
        verdict = admission.admit("conv-me")
        # The idle chat is stopped first; memory stays low, so the turn is refused.
        self.assertEqual(self.stops(), ["cccc0001"])
        self.assertEqual(verdict, admission.Verdict(ok=False, live=2, mem_mb=1000))
        self.assertEqual(admission.busy_reply(admission.Verdict(False, 3, 1000)), BUSY)
        self.assertEqual(admission.BUSY_REPLY.format(live=3, mem_mb=1000), BUSY)

    def test_resume_of_live_conversation_skips_memory_gate(self):
        self.set_mem_mb(1000)
        self.session("cccc0001", "conv-me", done_ago=60)
        self.session("bbbb0001", chat=False)
        self.assertEqual(admission.admit("conv-me"),
                         admission.Verdict(ok=True, live=2, mem_mb=1000))
        self.assertEqual(self.stops(), [])
        # A conversation without a live process still meets the gate.
        self.assertFalse(admission.admit("conv-new").ok)

    def test_resume_of_live_conversation_not_counted_twice(self):
        set_env(self, CLAUDE_CODE_BG_MAX_LIVE="3")
        self.session("cccc0001", "conv-me", done_ago=900)
        self.session("cccc0002", "conv-b", done_ago=900, turns={"k1": {"state": "sent"}},
                     state="working", status="busy")
        self.session("bbbb0001", chat=False)
        self.assertEqual(admission.admit("conv-me"),
                         admission.Verdict(ok=True, live=3, mem_mb=8000))
        self.assertEqual(self.stops(), [])
        # A new conversation counts all three, so the idle chat is stopped for it.
        self.assertTrue(admission.admit("conv-new").ok)
        self.assertEqual(self.stops(), ["cccc0001"])

    def test_admit_evicts_idle_chat_with_sent_turn(self):
        # A turn that failed or timed out stays `sent`; its idle process must not hold a slot.
        set_env(self, CLAUDE_CODE_BG_MAX_LIVE="10", CLAUDE_CODE_BG_MAX_CHATS="3")
        sent = {"k1": {"state": "sent", "sent_at": time.time() - 900}}
        self.session("cccc0001", "conv-stuck", done_ago=900, turns=sent)
        self.session("cccc0002", "conv-busy", done_ago=950, turns=dict(sent),
                     state="working", status="busy")
        self.session("cccc0003", "conv-recent", done_ago=10)
        verdict = admission.admit("conv-me")
        self.assertEqual(verdict, admission.Verdict(ok=True, live=2, mem_mb=8000))
        # The working conversation is older but not a candidate; the idle stuck one goes first.
        self.assertEqual(self.stops(), ["cccc0001"])
        self.assertIsNotNone(store.load("conv-stuck").get("reaped_at"))

    # reaper

    def test_reap_once_stops_idle_row_with_sent_turn(self):
        old = {"k1": {"state": "sent", "sent_at": time.time() - 700}}
        self.session("cccc0001", "conv-stuck", done_ago=900, turns=old)
        self.session("cccc0002", "conv-first-failed", turns=dict(old))  # never finished a turn
        self.session("cccc0003", "conv-working", done_ago=900, turns=dict(old),
                     state="working", status="busy")
        self.session("cccc0004", "conv-just-sent", done_ago=900,
                     turns={"k1": {"state": "sent", "sent_at": time.time() - 30}})
        self.assertEqual(sorted(admission.reap_once()), ["cccc0001", "cccc0002"])
        self.assertEqual(sorted(self.stops()), ["cccc0001", "cccc0002"])
        self.assertEqual(admission.reap_once(), [])

    def test_reap_once_stops_idle_only(self):
        self.session("cccc0001", "conv-idle", done_ago=700)
        self.session("cccc0002", "conv-sent", done_ago=700, turns={"k1": {"state": "sent"}},
                     state="working", status="busy")
        self.session("cccc0003", "conv-recent", done_ago=30)
        self.assertEqual(admission.reap_once(), ["cccc0001"])
        self.assertEqual(self.stops(), ["cccc0001"])
        state = store.load("conv-idle")
        self.assertGreaterEqual(state["reaped_at"], state["last_done_at"])
        self.assertNotIn("reaped_at", store.load("conv-sent"))
        self.assertIn("reaped", [e.get("kind") for e in self.events()])
        # Already reaped: the next pass leaves it alone.
        self.assertEqual(admission.reap_once(), [])

    def test_reap_once_skips_working_row_dead_pid_and_held_locks(self):
        self.session("cccc0001", "conv-working", done_ago=700, state="working", status="busy")
        self.session("cccc0002", "conv-dead", done_ago=700, pid=999999999)
        self.session("cccc0003", "conv-thread", done_ago=700)
        self.session("cccc0004", "conv-file", done_ago=700)
        lock = store.thread_lock("conv-thread")
        with lock, store.FileLock("conv-file"):
            self.assertEqual(admission.reap_once(), [])
        self.assertEqual(self.stops(), [])
        self.assertEqual(admission.reap_once(), ["cccc0004", "cccc0003"])
        self.assertTrue((self.stub.hermes_home / "claude-code-bg" / "conv" /
                         "conv-file.lock").exists())

    def test_reap_once_corrupt_state_does_not_stop_others(self):
        self.session("cccc0002", "conv-b", done_ago=700)
        (self.stub.hermes_home / "claude-code-bg" / "conv" / "conv-a.json").write_text("{bad")
        self.assertEqual(admission.reap_once(), ["cccc0002"])
        errors = [e for e in self.events() if e.get("kind") == "reaper_error"]
        self.assertEqual([e["conv"] for e in errors], ["conv-a"])

    def test_reap_once_off_when_idle_zero(self):
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S="0")
        self.session("cccc0001", "conv-idle", done_ago=7000)
        self.assertEqual(admission.reap_once(), [])
        self.assertEqual(self.stops(), [])

    def test_reaper_tick_logs_error_and_returns(self):
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S="not-a-number")
        admission._reap_tick()
        self.assertIn("reaper_error", [e.get("kind") for e in self.events()])

    def test_start_reaper_disabled_starts_no_thread(self):
        set_env(self, CLAUDE_CODE_BG_IDLE_STOP_S="0")
        admission.start_reaper()
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn(admission.REAPER_THREAD, names)


if __name__ == "__main__":
    unittest.main()
