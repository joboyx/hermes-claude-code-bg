"""Transcript reader: completion rule, reply text and usage."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_code_bg import transcript
from tests.support import set_env

SID = "11111111-0000-4000-8000-000000000001"
CID = "abc12345"
PROMPT = "What is the capital of France?"


def user(content, **extra) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": "2026-10-21T00:00:00Z",
            "message": {"role": "user", "content": content}, **extra}


def assistant(msg_id, blocks, stop_reason, usage=None, **extra) -> dict:
    return {"type": "assistant", "isSidechain": False, "timestamp": "2026-10-21T00:00:01Z",
            "message": {"id": msg_id, "role": "assistant", "content": blocks,
                        "stop_reason": stop_reason, "usage": usage or {}}, **extra}


def text(value) -> dict:
    return {"type": "text", "text": value}


def tool_use(name="Bash") -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}}


def tool_result() -> dict:
    return user([{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}])


def queue(operation) -> dict:
    return {"type": "queue-operation", "operation": operation, "timestamp": "2026-10-21T00:00:02Z"}


def usage(inp, read, create, out) -> dict:
    return {"input_tokens": inp, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": create, "output_tokens": out}


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ccbg-transcript-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.projects = self.root / "projects"
        self.jobs = self.root / "jobs"
        self.projects.mkdir()
        self.jobs.mkdir()
        set_env(self, CLAUDE_PROJECTS=str(self.projects), CLAUDE_JOBS=str(self.jobs))
        self.path = self.projects / "-some-project" / f"{SID}.jsonl"

    def write(self, *entries, tail: str = "") -> None:
        self.path.parent.mkdir(exist_ok=True)
        with open(self.path, "a") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
            fh.write(tail)

    def read(self, offset: int = 0, prompt: str = PROMPT, skip: int = 0):
        return transcript.read_turn(SID, CID, offset, prompt, skip_matches=skip)

    def set_in_flight(self, tasks: int) -> None:
        (self.jobs / CID).mkdir(exist_ok=True)
        (self.jobs / CID / "state.json").write_text(json.dumps({"inFlight": {"tasks": tasks}}))

    def test_simple_turn(self):
        self.write(
            {"type": "system", "subtype": "init"},
            user(PROMPT),
            assistant("msg_1", [{"type": "thinking", "thinking": "hmm"}], "tool_use", usage(10, 100, 20, 5)),
            assistant("msg_1", [tool_use()], "tool_use", usage(10, 100, 20, 5)),
            tool_result(),
            assistant("msg_2", [text("Paris.")], "end_turn", usage(3, 200, 7, 9)),
            assistant("msg_2", [text("Anything else?")], "end_turn", usage(3, 200, 7, 9)),
        )
        turn = self.read()
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "Paris.\nAnything else?")
        self.assertEqual(turn.usage, {
            "input": 13, "cache_read": 300, "cache_creation": 27, "output": 14, "api_calls": 2,
            "last": {"input": 3, "cache_read": 200, "cache_creation": 7},
        })
        self.assertEqual(turn.n_end_turns, 1)
        self.assertEqual(turn.sig, self.read().sig)

    def test_two_end_turns_returns_last(self):
        self.write(
            user(PROMPT),
            assistant("msg_1", [text("Started a background check, will report back.")], "end_turn",
                      usage(1, 10, 1, 2)),
            queue("enqueue"),
            queue("dequeue"),
            user("<task-notification>background task finished</task-notification>"),
            assistant("msg_2", [text("Final answer: Paris.")], "end_turn", usage(2, 20, 2, 4)),
        )
        turn = self.read()
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "Final answer: Paris.")
        self.assertEqual(turn.n_end_turns, 2)
        self.assertEqual(turn.usage["api_calls"], 2)
        self.assertEqual(turn.usage["last"], {"input": 2, "cache_read": 20, "cache_creation": 2})

    def test_waits_while_queue_unbalanced(self):
        self.write(
            user(PROMPT),
            assistant("msg_1", [text("Interim reply.")], "end_turn"),
            queue("enqueue"),
        )
        self.assertIsNone(self.read())
        self.write(queue("remove"))
        self.assertEqual(self.read().text, "Interim reply.")

    def test_waits_while_in_flight(self):
        self.write(user(PROMPT), assistant("msg_1", [text("Paris.")], "end_turn"))
        self.set_in_flight(1)
        self.assertIsNone(self.read())
        self.set_in_flight(0)
        self.assertEqual(self.read().text, "Paris.")

    def test_ignores_sidechain_entries(self):
        self.write(
            user(PROMPT),
            assistant("msg_1", [tool_use("Agent")], "tool_use", usage(1, 1, 1, 1)),
            user("subagent prompt", isSidechain=True),
            assistant("msg_side", [text("subagent text")], "end_turn", usage(50, 50, 50, 50),
                      isSidechain=True),
            tool_result(),
            assistant("msg_2", [text("Main answer.")], "end_turn", usage(2, 2, 2, 2)),
            assistant("msg_side2", [text("late subagent text")], "end_turn", isSidechain=True),
        )
        turn = self.read()
        self.assertIsNotNone(turn)
        self.assertEqual(turn.text, "Main answer.")
        self.assertEqual(turn.usage["api_calls"], 2)
        self.assertEqual(turn.usage["input"], 3)
        self.assertEqual(turn.n_end_turns, 1)

    def test_offset_skips_earlier_turns(self):
        self.write(user(PROMPT), assistant("msg_old", [text("Old answer.")], "end_turn", usage(9, 9, 9, 9)))
        offset = transcript.size(SID)
        self.assertEqual(offset, self.path.stat().st_size)
        self.assertIsNone(self.read(offset))
        self.write(user(PROMPT))
        self.assertIsNone(self.read(offset))
        self.write(assistant("msg_new", [text("New answer.")], "end_turn", usage(1, 2, 3, 4)))
        turn = self.read(offset)
        self.assertEqual(turn.text, "New answer.")
        self.assertEqual(turn.usage["api_calls"], 1)
        self.assertEqual(turn.usage["output"], 4)

    def test_missing_transcript_returns_none(self):
        self.assertIsNone(transcript.path_of(SID))
        self.assertEqual(transcript.size(SID), 0)
        self.assertIsNone(self.read())

    def test_partial_last_line_ignored(self):
        final = json.dumps(assistant("msg_1", [text("Paris.")], "end_turn"))
        self.write(user(PROMPT), tail=final[:25])
        self.assertIsNone(self.read())
        self.write(tail=final[25:] + "\n")
        self.assertEqual(self.read().text, "Paris.")

    def test_tool_use_last_is_not_done(self):
        self.write(
            user(PROMPT),
            assistant("msg_1", [text("Let me check.")], "end_turn"),
            user("follow-up from a hook"),
            assistant("msg_2", [text("Checking."), tool_use()], "tool_use"),
        )
        self.assertIsNone(self.read())

    def test_needs_our_prompt_and_non_empty_text(self):
        self.write(user("some other message"), assistant("msg_1", [text("Reply.")], "end_turn"))
        self.assertIsNone(self.read())
        self.write(user(PROMPT, isMeta=True), assistant("msg_2", [text("Reply.")], "end_turn"))
        self.assertIsNone(self.read())
        self.write(user("prefix " + PROMPT + " suffix"), assistant("msg_3", [text("  ")], "end_turn"))
        self.assertIsNone(self.read())
        self.write(user(PROMPT), assistant("msg_4", [text("Done.")], "end_turn"))
        self.assertEqual(self.read().text, "Done.")

    def test_long_prompt_matches_first_200_chars(self):
        long_prompt = "x" * 300
        self.write(user("persona\n\n" + long_prompt[:200] + " (trimmed)"),
                   assistant("msg_1", [text("Ok.")], "end_turn"))
        self.assertEqual(self.read(prompt=long_prompt).text, "Ok.")

    def test_path_of_uses_first_sorted_match(self):
        self.write(user(PROMPT))
        later = self.projects / "z-project" / f"{SID}.jsonl"
        later.parent.mkdir()
        later.write_text("")
        self.assertEqual(transcript.path_of(SID), self.path)
        self.assertIsNone(transcript.path_of(""))

    def test_in_flight_missing_file_is_zero(self):
        self.assertEqual(transcript.in_flight(CID), 0)
        (self.jobs / CID).mkdir()
        (self.jobs / CID / "state.json").write_text(json.dumps({"state": "idle"}))
        self.assertEqual(transcript.in_flight(CID), 0)
        self.set_in_flight(2)
        self.assertEqual(transcript.in_flight(CID), 2)

    def test_skip_matches_ignores_earlier_copies_of_the_prompt(self):
        # A copied session repeats the whole history: the same short prompt can be in it already.
        self.write(user("yes"), assistant("msg_old", [text("Old answer.")], "end_turn"))
        self.assertEqual(transcript.count_prompts(SID, "yes"), 1)
        self.assertEqual(self.read(prompt="yes").text, "Old answer.")  # stale without the skip
        self.assertIsNone(self.read(prompt="yes", skip=1))
        self.write(user("yes"))
        self.assertEqual(transcript.count_prompts(SID, "yes"), 2)
        self.assertIsNone(self.read(prompt="yes", skip=1))
        self.assertIsNone(transcript.last_stop_reason(SID, 0, "yes", skip_matches=1))
        self.write(assistant("msg_new", [text("New answer.")], "end_turn"))
        self.assertEqual(self.read(prompt="yes", skip=1).text, "New answer.")
        self.assertIsNone(self.read(prompt="yes", skip=2))
        self.assertEqual(transcript.count_prompts("no-such-session", "yes"), 0)

    def test_last_stop_reason(self):
        self.assertIsNone(transcript.last_stop_reason(SID, 0, PROMPT))
        self.write(assistant("msg_0", [text("Earlier.")], "max_tokens"), user(PROMPT))
        self.assertIsNone(transcript.last_stop_reason(SID, 0, PROMPT))
        self.write(assistant("msg_1", [tool_use()], "tool_use"), tool_result(),
                   assistant("msg_side", [text("side")], "end_turn", isSidechain=True))
        self.assertEqual(transcript.last_stop_reason(SID, 0, PROMPT), "tool_use")
        self.write(assistant("msg_2", [text("Cut off")], "max_tokens"))
        self.assertEqual(transcript.last_stop_reason(SID, 0, PROMPT), "max_tokens")
        self.assertIsNone(self.read())

    def test_sig_changes_when_entries_arrive(self):
        self.write(user(PROMPT), assistant("msg_1", [text("Paris.")], "end_turn"))
        first = self.read().sig
        self.write({"type": "system", "subtype": "turn_duration"})
        self.assertNotEqual(self.read().sig, first)


if __name__ == "__main__":
    unittest.main()
