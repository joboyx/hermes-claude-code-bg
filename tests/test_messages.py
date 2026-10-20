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
