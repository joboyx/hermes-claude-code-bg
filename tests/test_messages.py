"""Messages: OpenAI content -> prompt text, images -> files, conversation and turn keys, persona."""
from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
from pathlib import Path

from claude_code_bg import messages
from tests.support import set_env

PNG = b"\x89PNG\r\n\x1a\n-fake-image-bytes"
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def image_part(url: str) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


class MessagesTest(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="ccbg-msg-")
        self.addCleanup(shutil.rmtree, self.cwd, True)

    def test_text_parts_joined(self):
        content = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        self.assertEqual(messages.prompt_of(content, self.cwd), "first\n\nsecond")
        self.assertEqual(messages.text_of(content), "first\nsecond")
        self.assertEqual(messages.text_of("plain"), "plain")
        self.assertEqual(messages.text_of([image_part(DATA_URL)]), "[image]")

    def test_model_note_stripped(self):
        note = "[Note: model was just switched from a to b. Adjust.]\n\n"
        self.assertEqual(messages.prompt_of(note + "hello", self.cwd), "hello")
        parts = [{"type": "text", "text": note + "hi"}, {"type": "text", "text": "there"}]
        self.assertEqual(messages.prompt_of(parts, self.cwd), "hi\n\nthere")

    def test_data_url_image_saved_once(self):
        first = messages.save_image(DATA_URL, self.cwd)
        second = messages.save_image(DATA_URL, self.cwd)
        self.assertEqual(first, second)
        files = list((Path(self.cwd) / "tmp" / "hermes-images").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), PNG)
        self.assertEqual(files[0].suffix, ".png")
        self.assertEqual(len(files[0].stem), 12)
        self.assertEqual(
            first, f"[Attached image: {files[0]} - open it with the Read tool to see it.]")

    def test_image_only_message(self):
        prompt = messages.prompt_of([image_part(DATA_URL)], self.cwd)
        [path] = (Path(self.cwd) / "tmp" / "hermes-images").iterdir()
        self.assertEqual(
            prompt, f"[Attached image: {path} - open it with the Read tool to see it.]")

    def test_http_image_url_passed(self):
        url = "https://example.com/cat.png"
        self.assertEqual(messages.save_image(url, self.cwd), f"[Attached image URL: {url}]")
        content = [{"type": "text", "text": "look"}, image_part(url)]
        self.assertEqual(messages.prompt_of(content, self.cwd),
                         f"look\n\n[Attached image URL: {url}]")
        local = "/srv/images/cat.png"
        line = f"[Attached image: {local} - open it with the Read tool to see it.]"
        self.assertEqual(messages.save_image(local, self.cwd), line)
        self.assertEqual(messages.save_image("file://" + local, self.cwd), line)
        self.assertFalse((Path(self.cwd) / "tmp").exists())

    def test_hermes_image_hints_removed(self):
        # The shape of Hermes' `agent/image_routing.build_native_content_parts`: the caption,
        # a blank line, one hint line per image, then the pictures as data URLs.
        content = [{"type": "text", "text": "What colour is it?\n\n"
                                            "[Image attached at: /srv/inbox/red.png]\n"
                                            "[Image attached: https://example.com/b.png]"},
                   image_part(DATA_URL), image_part("https://example.com/b.png")]
        prompt = messages.prompt_of(content, self.cwd)
        [path] = (Path(self.cwd) / "tmp" / "hermes-images").iterdir()
        self.assertEqual(prompt, "What colour is it?\n\n"
                                 f"[Attached image: {path} - open it with the Read tool to see it.]"
                                 "\n\n[Attached image URL: https://example.com/b.png]")

    def test_image_hint_text_kept_without_image_parts(self):
        content = [{"type": "text", "text": "I typed [Image attached at: /x.png] myself"}]
        self.assertEqual(messages.prompt_of(content, self.cwd),
                         "I typed [Image attached at: /x.png] myself")
        line = "[Image attached at: /x.png]"
        self.assertEqual(messages.prompt_of([{"type": "text", "text": line}], self.cwd), line)

    def test_conversation_key_session_vs_hash(self):
        one = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
        two = [{"role": "user", "content": "other"}]
        session = messages.conversation_key("session-1", one)
        self.assertEqual(session, messages.conversation_key("session-1", two))
        self.assertRegex(session, r"^hs-[0-9a-f]{16}$")
        self.assertNotEqual(session, messages.conversation_key("session-2", one))
        hashed = messages.conversation_key(None, one)
        self.assertRegex(hashed, r"^hx-[0-9a-f]{16}$")
        later = one + [{"role": "assistant", "content": "yo"}, {"role": "user", "content": "more"}]
        self.assertEqual(hashed, messages.conversation_key(None, later))
        changed = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]
        self.assertNotEqual(hashed, messages.conversation_key(None, changed))
        self.assertEqual(hashed, messages.conversation_key("", one))

    def test_turn_key_changes_with_history(self):
        history = [{"role": "user", "content": "hi"}]
        key = messages.turn_key(history)
        self.assertRegex(key, r"^[0-9a-f]{16}$")
        self.assertEqual(key, messages.turn_key([{"content": "hi", "role": "user"}]))
        grown = history + [{"role": "assistant", "content": "yo"},
                           {"role": "user", "content": "hi"}]
        self.assertNotEqual(key, messages.turn_key(grown))

    def test_newest_user_raises_without_user(self):
        with self.assertRaisesRegex(ValueError, "^claude-code-bg needs a user message$"):
            messages.newest_user([{"role": "system", "content": "sys"}])
        convo = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                 {"role": "user", "content": "c"}]
        self.assertEqual(messages.newest_user(convo), (convo[2], 2))

    def test_history_block_format(self):
        earlier = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[Note: model was just switched from a to b] hi\nthere"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": [{"type": "text", "text": "look"}, image_part(DATA_URL)]},
        ]
        self.assertEqual(messages.history_block(earlier),
                         "Earlier messages in this chat (for context):\n"
                         "> User: hi\n> there\n"
                         "> Assistant: hello\n"
                         "> User: look\n> [image]")
        self.assertEqual(messages.history_block([{"role": "system", "content": "sys"}]), "")

    def test_history_block_drops_hermes_system_notes(self):
        # Hermes adds notes for its own agent (onboarding, interrupted turn) to user messages;
        # they name Hermes tools the Claude session does not have.
        onboarding = ("Remember FIG-7.\n\n[System note: This is the user's very first message ever. "
                      "OFFER to build a profile (mention /help).\n  1. Ask what they share.\n"
                      "  2. Save facts with the memory tool using target=\"user\".\n"
                      "If they decline, stop.]")
        interrupted = "[System note: Your previous turn was interrupted mid-run …]\n\nkeep going"
        block = messages.history_block([{"role": "user", "content": onboarding},
                                        {"role": "user", "content": interrupted}])
        self.assertEqual(block, "Earlier messages in this chat (for context):\n"
                                "> User: Remember FIG-7.\n> User: keep going")

    def test_history_block_keeps_newest_under_cap(self):
        earlier = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 995}
                   for i in range(30)]
        block = messages.history_block(earlier)
        self.assertLessEqual(len(block), messages.HISTORY_CAP)
        self.assertGreater(len(block), messages.HISTORY_CAP - 1100)
        lines = block.splitlines()
        self.assertEqual(lines[:2], ["Earlier messages in this chat (for context):", "> …"])
        self.assertTrue(lines[-1].startswith("> Assistant: m29 "), lines[-1][:20])
        self.assertNotIn("m0 ", block)
        one = messages.history_block([{"role": "user", "content": "y" * 20000}])
        self.assertLessEqual(len(one), messages.HISTORY_CAP)
        self.assertTrue(one.splitlines()[1].startswith("> User: …y"), one[:80])

    def test_unseen_history(self):
        sys_ = {"role": "system", "content": "sys"}
        u1, a1 = {"role": "user", "content": "one"}, {"role": "assistant", "content": "ans one"}
        u2, a2 = {"role": "user", "content": "two"}, {"role": "assistant", "content": "other ans"}
        u3 = {"role": "user", "content": "three"}
        # First turn: everything before the newest user message.
        self.assertEqual(messages.unseen_history([sys_, u1, a1, u3], None), [u1, a1])
        seen = messages.seen_marker([sys_, u1])
        # Normal follow-up: only Claude's own reply in between, nothing to carry.
        self.assertEqual(messages.unseen_history([sys_, u1, a1, u3], seen), [])
        # Another provider answered u2 in between: carry u2 and its reply.
        self.assertEqual(messages.unseen_history([sys_, u1, a1, u2, a2, u3], seen), [u2, a2])
        # The same list again (/retry): nothing.
        seen3 = messages.seen_marker([sys_, u1, a1, u3])
        self.assertEqual(messages.unseen_history([sys_, u1, a1, u3], seen3), [])
        # The list was rewritten (compression): find the seen message by its text.
        summary = {"role": "user", "content": "[summary of earlier turns]"}
        self.assertEqual(messages.unseen_history([sys_, summary, u1, u2, a2, u3], seen), [u2, a2])
        # The seen message is gone (/undo): carry nothing, Claude keeps its own history.
        self.assertEqual(messages.unseen_history([sys_, u2, u3], seen), [])

    def test_persona_file_and_off_switch(self):
        set_env(self, CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE=None, CLAUDE_CODE_BG_PERSONA=None)
        self.assertEqual(messages.persona(), messages.PERSONA)
        self.assertTrue(messages.PERSONA.startswith("You are answering a person in a Telegram chat."))
        self.assertTrue(messages.PERSONA.endswith("ask in plain text and wait for the next message."))
        set_env(self, CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE=str(Path(self.cwd) / "missing.md"))
        self.assertEqual(messages.persona(), messages.PERSONA)
        prompt_file = Path(self.cwd) / "persona.md"
        prompt_file.write_text("Custom persona.\n")
        set_env(self, CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE=str(prompt_file))
        self.assertEqual(messages.persona(), "Custom persona.")
        set_env(self, CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE=None, CLAUDE_CODE_BG_PERSONA="0")
        self.assertIsNone(messages.persona())


if __name__ == "__main__":
    unittest.main()
