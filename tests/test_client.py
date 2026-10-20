"""Client: the OpenAI-shaped turn flow Hermes calls (spec "Client -> Turn flow")."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import unittest

from claude_code_bg import client, messages, store
from claude_code_bg.client import CANCELLED, AuxiliaryRefused, Client
from tests.support import StubEnv, set_env

SESSION = "20261021_000000_abcd"
SID = "abc12345-0000-4000-8000-000000000001"
COPY_ID = "def67890"
COPY_SID = "def67890-0000-4000-8000-000000000002"
LATE = "(Reply to an earlier message that timed out:)"
GUIDANCE = ("Not allowed in chat mode: no one can approve this prompt. Choose a safer way, "
            "or explain in your reply what you need and stop.")


def user_entry(content) -> dict:
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": content}}


def assistant_entry(msg_id, text, stop_reason="end_turn", usage=None) -> dict:
    return {"type": "assistant", "isSidechain": False,
            "message": {"id": msg_id, "role": "assistant", "stop_reason": stop_reason,
                        "content": [{"type": "text", "text": text}], "usage": usage or {}}}


def answer_of(prompt: str) -> str:
    return f"answer to {prompt}"


# On each launch the stub writes our prompt and a finished reply ("answer to <prompt>").
ANSWER = [user_entry("{{prompt}}"), assistant_entry("msg-{{prompt}}", "answer to {{prompt}}")]
PROMPT_ONLY = [user_entry("{{prompt}}")]


def meminfo(available_mb: int) -> str:
    return f"MemTotal: 16384000 kB\nMemAvailable: {available_mb * 1024} kB\n"


def msgs(*texts: str) -> list[dict]:
    """A Hermes message list: system prompt, then user / assistant turns (odd count ends on user)."""
    out = [{"role": "system", "content": "You are Hermes."}]
    for i, text in enumerate(texts):
        out.append({"role": "user" if i % 2 == 0 else "assistant", "content": text})
    return out


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.stub = self.enterContext(StubEnv())
        self.enterContext(self.stub.apply_env())
        self.cwd = self.stub.root / "work"
        self.meminfo = self.stub.root / "meminfo"
        self.meminfo.write_text(meminfo(8000))
        set_env(self, CLAUDE_CODE_BG_SLEEP_SCALE="0", CLAUDE_CODE_BG_IDLE_STOP_S="0",
                CLAUDE_CODE_BG_MEMINFO=str(self.meminfo), CLAUDE_CODE_BG_CWD=str(self.cwd),
                CLAUDE_CODE_BG_SETTINGS=None, CLAUDE_CODE_BG_MAX_LIVE=None,
                CLAUDE_CODE_BG_MAX_CHATS=None, CLAUDE_CODE_BG_MIN_MEM_MB=None,
                CLAUDE_CODE_BG_PERSONA=None, CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE=None,
                CLAUDE_CODE_BG_FAULT=None, CLAUDE_CODE_BG_NAME_PREFIX=None,
                CLAUDE_CODE_BG_EFFORT=None)
        self.stub.fx(append_transcript=ANSWER)
        self.client = Client(command="ignored-by-the-client", timeout=30)

    # helpers

    def ask(self, history: list[dict], **kwargs):
        kwargs.setdefault("hermes_session_id", SESSION)
        kwargs.setdefault("hermes_reasoning_effort", "low")
        return self.client.chat.completions.create(model="claude-haiku-5-5", messages=history,
                                                   **kwargs)

    def ask_in_thread(self, history: list[dict], **kwargs) -> tuple[threading.Thread, dict]:
        out: dict = {}

        def run():
            try:
                out["response"] = self.ask(history, **kwargs)
            except BaseException as exc:  # handed to the test thread
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out

    def launches(self) -> list[list[str]]:
        return [c["argv"] for c in self.stub.calls() if "--bg" in c["argv"]]

    def wait_for(self, check, what: str, seconds: float = 20) -> None:
        end = time.monotonic() + seconds
        while not check():
            if time.monotonic() > end:
                self.fail(f"timed out waiting for {what}")
            time.sleep(0.02)

    def transcript_path(self, sid: str = SID):
        return self.stub.projects / "stub" / f"{sid}.jsonl"

    def append(self, *entries: dict, sid: str = SID) -> None:
        path = self.transcript_path(sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def set_row(self, claude_id: str, **fields) -> None:
        rows = self.stub.fx()["rows"]
        for row in rows:
            if row["id"] == claude_id:
                row.update(fields)
        self.stub.fx(rows=rows)

    def text(self, response) -> str:
        return response.choices[0].message.content

    def events(self) -> list[dict]:
        path = self.stub.hermes_home / "claude-code-bg" / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # tests

    def test_first_turn_dispatches_and_returns_text(self):
        response = self.ask(msgs("hello"))
        self.assertEqual(self.text(response), answer_of("hello"))
        [argv] = self.launches()
        self.assertNotIn("--resume", argv)
        self.assertEqual(argv[-2:], ["--", "hello"])
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        conv = messages.conversation_key(SESSION, msgs("hello"))
        self.assertEqual(argv[argv.index("--name") + 1], "hb-" + conv[3:13])
        self.assertEqual(self.stub.calls()[0]["argv"][0], "agents")  # admission ran first
        [call] = [c for c in self.stub.calls() if "--bg" in c["argv"]]
        self.assertEqual(call["cwd"], str(self.cwd))
        state = store.load(conv)
        self.assertEqual((state["claude_id"], state["session_id"]), ("abc12345", SID))
        [turn] = state["turns"].values()
        self.assertEqual(turn["state"], "done")
        self.assertEqual(turn["text"], answer_of("hello"))
        ledger = json.loads((self.stub.hermes_home / "claude-watch" / "abc12345.json").read_text())
        self.assertEqual((ledger["mode"], ledger["hermes_session"]), ("chat", SESSION))
        dumped = response.model_dump()
        self.assertEqual(dumped["choices"][0]["message"]["content"], answer_of("hello"))
        self.assertEqual(dumped["model"], "claude-haiku-5-5")

    def test_second_turn_follow_up_same_session(self):
        self.ask(msgs("first"))
        response = self.ask(msgs("first", answer_of("first"), "second"))
        self.assertEqual(self.text(response), answer_of("second"))
        dispatch, resume = self.launches()
        self.assertNotIn("--resume", dispatch)
        self.assertEqual(resume[resume.index("--resume") + 1], SID)
        self.assertEqual(resume[-2:], ["--", "second"])
        stops = [c["argv"] for c in self.stub.calls() if c["argv"][:1] in (["stop"], ["rm"])]
        self.assertEqual(stops, [["stop", "abc12345"], ["rm", "abc12345"]])

    def test_retry_same_messages_sends_once(self):
        self.stub.fx(append_transcript=PROMPT_ONLY)
        history = msgs("hello")
        with self.assertRaises(TimeoutError):
            self.ask(history, timeout=0.01)
        self.append(assistant_entry("msg-1", "late hello"))
        # The retry waits on the `sent` turn instead of sending again ...
        self.assertEqual(self.text(self.ask(history)), "late hello")
        # ... and a later duplicate gets the stored reply.
        self.assertEqual(self.text(self.ask(history)), "late hello")
        self.assertEqual(len(self.launches()), 1)
        kinds = [e["kind"] for e in self.events()]
        self.assertIn("dedupe", kinds)

    def test_fault_after_send_then_retry_sends_once(self):
        set_env(self, CLAUDE_CODE_BG_FAULT="after_send")
        history = msgs("hello")
        with self.assertRaisesRegex(RuntimeError, "injected fault"):
            self.ask(history)
        self.assertEqual(self.text(self.ask(history)), answer_of("hello"))
        self.assertEqual(len(self.launches()), 1)

    def test_auxiliary_refused(self):
        with self.assertRaises(AuxiliaryRefused):
            self.ask(msgs("Write a title for this chat"), hermes_auxiliary=True)
        self.assertEqual(self.stub.calls(), [])

    def test_usage_reports_last_call(self):
        def usage(inp, read, create, out):
            return {"input_tokens": inp, "cache_read_input_tokens": read,
                    "cache_creation_input_tokens": create, "output_tokens": out}
        tool = {"type": "assistant", "isSidechain": False,
                "message": {"id": "msg-a", "role": "assistant", "stop_reason": "tool_use",
                            "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                         "input": {}}], "usage": usage(10, 1000, 200, 30)}}
        result = user_entry([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])
        self.stub.fx(append_transcript=[user_entry("{{prompt}}"), tool, result,
                                        assistant_entry("msg-b", "done", usage=usage(3, 1200, 50, 7))])
        u = self.ask(msgs("count")).usage
        self.assertEqual(u.prompt_tokens, 3 + 1200 + 50)
        self.assertEqual(u.completion_tokens, 30 + 7)
        self.assertEqual(u.total_tokens, 3 + 1200 + 50 + 37)
        self.assertEqual(u.prompt_tokens_details.cached_tokens, 1200)
        self.assertEqual(u.prompt_tokens_details.cache_write_tokens, 50)

    def test_no_tool_calls_and_stop_reason(self):
        response = self.ask(msgs("hello"))
        self.assertEqual(len(response.choices), 1)
        choice = response.choices[0]
        self.assertEqual(choice.finish_reason, "stop")
        self.assertIsNone(choice.message.tool_calls)
        self.assertEqual(choice.message.role, "assistant")
        self.assertIsNone(response.model_dump()["choices"][0]["message"]["tool_calls"])
        self.assertFalse(Client.SUPPORTS_HERMES_TOOL_CALLS)
        self.assertTrue(Client.HERMES_SKIP_TRANSPORT_WRAP)
        self.assertTrue(Client.HERMES_SKIP_ASYNC_WRAP)
        self.assertEqual(self.client.api_key, "external-process")
        self.assertEqual(self.client.base_url, "process://claude-code-bg")

    def test_cancel_raises_cancelled(self):
        self.stub.fx(append_transcript=PROMPT_ONLY)
        thread, out = self.ask_in_thread(msgs("slow question"))
        self.wait_for(lambda: self.launches(), "the dispatch")
        self.client.cancel()
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(out.get("error"), RuntimeError)
        self.assertEqual(str(out["error"]), CANCELLED)
        # The background turn keeps running: no stop was sent.
        self.assertNotIn(["stop", "abc12345"], [c["argv"][:2] for c in self.stub.calls()])

    def test_orphan_reply_delivered_late(self):
        self.stub.fx(append_transcript=PROMPT_ONLY)
        with self.assertRaises(TimeoutError):
            self.ask(msgs("first"), timeout=0.01)
        self.append(assistant_entry("msg-first", answer_of("first")))
        self.stub.fx(append_transcript=ANSWER)
        # Hermes got no reply for "first", so its history has two user messages in a row.
        history = msgs("first") + [{"role": "user", "content": "second"}]
        text = self.text(self.ask(history))
        self.assertTrue(text.startswith(LATE), text)
        self.assertEqual(text, f"{LATE}\n{answer_of('first')}\n\n{answer_of('second')}")
        # Delivered once only.
        history += [{"role": "assistant", "content": text}, {"role": "user", "content": "third"}]
        self.assertEqual(self.text(self.ask(history)), answer_of("third"))

    def test_blocked_permission_denied_with_guidance(self):
        self.stub.fx(append_transcript=PROMPT_ONLY)
        thread, out = self.ask_in_thread(msgs("push it"))
        self.wait_for(lambda: self.launches(), "the dispatch")
        self.set_row("abc12345", state="blocked", status="waiting", waitingFor="permission prompt")
        self.wait_for(lambda: any(c[-1:] == [GUIDANCE] for c in self.stub.tmux_calls()),
                      "the deny keys")
        self.set_row("abc12345", state="done", status="idle", waitingFor=None)
        self.append(assistant_entry("msg-1", "I did not push."))
        thread.join(20)
        self.assertNotIn("error", out)
        text = self.text(out["response"])
        self.assertTrue(text.startswith("I did not push."), text)
        self.assertIn("permission prompt", text)
        self.assertIn("denied", text)
        self.assertIn("deny", [e["kind"] for e in self.events()])

    def test_failed_row_raises(self):
        self.stub.fx(append_transcript=PROMPT_ONLY)
        thread, out = self.ask_in_thread(msgs("hello"))
        self.wait_for(lambda: self.launches(), "the dispatch")
        self.set_row("abc12345", state="failed", status=None)
        thread.join(20)
        self.assertIsInstance(out.get("error"), RuntimeError)
        self.assertIn("failed", str(out["error"]))

    def test_turn_without_end_turn_raises(self):
        self.stub.fx(append_transcript=[user_entry("{{prompt}}"),
                                        assistant_entry("msg-1", "cut off", "max_tokens")])
        with self.assertRaisesRegex(RuntimeError, "max_tokens"):
            self.ask(msgs("write a long essay"))

    def test_next_message_after_failed_turn(self):
        self.stub.fx(append_transcript=[user_entry("{{prompt}}"),
                                        assistant_entry("msg-1", "cut off", "max_tokens")])
        first = msgs("write a long essay")
        with self.assertRaisesRegex(RuntimeError, "max_tokens"):
            self.ask(first)
        self.stub.fx(append_transcript=ANSWER)
        history = first + [{"role": "user", "content": "shorter please"}]
        self.assertEqual(self.text(self.ask(history)), answer_of("shorter please"))
        dispatch, resume = self.launches()
        self.assertNotIn("--resume", dispatch)
        self.assertEqual(resume[resume.index("--resume") + 1], SID)
        state = store.load(messages.conversation_key(SESSION, first))
        self.assertEqual(state["session_id"], SID)
        self.assertEqual(state["turns"][messages.turn_key(first)]["state"], "abandoned")
        self.assertEqual(state["turns"][messages.turn_key(history)]["state"], "done")

    def test_busy_reply_when_not_admitted(self):
        self.meminfo.write_text(meminfo(1000))
        response = self.ask(msgs("hello"))
        self.assertEqual(self.text(response),
                         "Claude Code is busy on this host right now (0 live sessions, 1000 MB "
                         "free). Send your message again in a few minutes.")
        self.assertEqual(response.choices[0].finish_reason, "stop")
        self.assertEqual(self.launches(), [])
        conv = messages.conversation_key(SESSION, msgs("hello"))
        self.assertEqual(store.load(conv)["turns"], {})

    def test_persona_on_dispatch_only(self):
        self.ask(msgs("first"))
        self.ask(msgs("first", answer_of("first"), "second"))
        dispatch, resume = self.launches()
        self.assertEqual(dispatch[dispatch.index("--append-system-prompt") + 1], messages.PERSONA)
        self.assertNotIn("--append-system-prompt", resume)

    def test_copy_skips_repeated_history(self):
        self.ask(msgs("yes"))
        # The copy's transcript repeats the old history, then gets the new prompt.
        self.transcript_path(COPY_SID).parent.mkdir(parents=True, exist_ok=True)
        self.transcript_path(COPY_SID).write_bytes(self.transcript_path().read_bytes())
        # Until the copy writes the new prompt, a read from offset 0 sees only the old answer.
        self.stub.fx(copy_as=COPY_ID, copy_session=COPY_SID, append_transcript=[])
        thread, out = self.ask_in_thread(msgs("yes", answer_of("yes"), "yes"))
        self.wait_for(lambda: len(self.launches()) == 2, "the resume")
        time.sleep(0.3)  # give a wrong client time to return the stale answer
        self.append(user_entry("yes"), assistant_entry("msg-new", "new answer"), sid=COPY_SID)
        thread.join(20)
        self.assertNotIn("error", out)
        self.assertEqual(self.text(out["response"]), "new answer")
        state = store.load(messages.conversation_key(SESSION, msgs("yes")))
        self.assertEqual((state["claude_id"], state["session_id"]), (COPY_ID, COPY_SID))

    def test_async_caller_gets_awaitable(self):
        async def call():
            return await self.client.chat.completions.create(
                model="claude-haiku-5-5", messages=msgs("hello"), hermes_session_id=SESSION)
        self.assertEqual(self.text(asyncio.run(call())), answer_of("hello"))

    def test_events_cut_message_text(self):
        long_prompt = "x" * 300
        self.ask(msgs(long_prompt))
        raw = (self.stub.hermes_home / "claude-code-bg" / "events.jsonl").read_text()
        self.assertNotIn("x" * 91, raw)
        self.assertIn("x" * 90, raw)

    def test_deny_guidance_text(self):
        self.assertEqual(client.DENY_GUIDANCE, GUIDANCE)


if __name__ == "__main__":
    unittest.main()
