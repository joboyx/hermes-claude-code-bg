"""OpenAI chat messages -> Claude Code prompt text, image files, conversation and turn keys, persona."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path

# Hermes prefixes the next user message with this note after `/model`.
MODEL_NOTE = re.compile(r'^\[Note: model was just switched[^\]]*\]\s*')
IMAGE_EXT = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}

PERSONA = (
    'You are answering a person in a Telegram chat. Hermes relays their messages to you and '
    'sends your reply back. Keep replies short and easy to read on a phone; Telegram shows '
    'bold, italics, inline code, code blocks and links, but not tables wider than a phone '
    'screen. You cannot ask follow-up questions with a picker: ask in plain text and wait for '
    'the next message.'
)


def text_of(content) -> str:
    """
    Return the plain text of an OpenAI message content (a string or a list
    of parts). Image parts become `[image]`. Use it for keys and logs, not
    for the prompt.
    """
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'text':
            out.append(part.get('text') or '')
        elif part.get('type') == 'image_url':
            out.append('[image]')
    return '\n'.join(out)


def _attached(path) -> str:
    return f'[Attached image: {path} - open it with the Read tool to see it.]'


def save_image(url: str, cwd: str) -> str:
    """
    Return the prompt line for one image URL. A data URL is written once to
    `<cwd>/tmp/hermes-images/<sha256[:12]>.<ext>`; a file path or `file://`
    URL is referenced as is; any other URL (http/https) is passed as text.
    """
    if url.startswith('data:'):
        head, _, data = url.partition(',')
        mime = head[5:].split(';')[0] or 'image/png'
        raw = base64.b64decode(data) if ';base64' in head else data.encode()
        folder = Path(cwd) / 'tmp' / 'hermes-images'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{hashlib.sha256(raw).hexdigest()[:12]}.{IMAGE_EXT.get(mime, 'png')}"
        if not path.exists():
            path.write_bytes(raw)
        return _attached(path)
    if url.startswith('file://'):
        return _attached(url[len('file://'):])
    if url.startswith('/'):
        return _attached(url)
    return f'[Attached image URL: {url}]'


def prompt_of(content, cwd: str) -> str:
    """
    Return the prompt Claude Code gets for one user message: text parts in
    order (joined by a blank line), each image part replaced by its
    `save_image` line, and the Hermes model-switch note removed.
    """
    if isinstance(content, str):
        return MODEL_NOTE.sub('', content).strip()
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'text':
            out.append(part.get('text') or '')
        elif part.get('type') == 'image_url':
            image = part.get('image_url')
            url = image.get('url', '') if isinstance(image, dict) else str(image or '')
            if url:
                out.append(save_image(url, cwd))
    return MODEL_NOTE.sub('', '\n\n'.join(x for x in out if x)).strip()


def newest_user(messages: list) -> tuple[dict, int]:
    """
    Return the newest user message and the number of user messages.
    Raise ValueError when the list has no user message.
    """
    users = [m for m in messages if isinstance(m, dict) and m.get('role') == 'user']
    if not users:
        raise ValueError('claude-code-bg needs a user message')
    return users[-1], len(users)


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def conversation_key(session_id: str | None, messages: list) -> str:
    """
    Return the conversation key: `hs-<sha16>` of the Hermes session id when
    there is one, else `hx-<sha16>` of the system prompt and the first user
    message (stable while the history only grows).
    """
    if session_id:
        return 'hs-' + _sha16(session_id)
    dicts = [m for m in messages if isinstance(m, dict)]
    system = next((text_of(m.get('content')) for m in dicts if m.get('role') == 'system'), '')
    first = next((text_of(m.get('content')) for m in dicts if m.get('role') == 'user'), '')
    return 'hx-' + _sha16(json.dumps([system, first]))


def turn_key(messages: list) -> str:
    """
    Return the identity of one Hermes turn: the first 16 hex of the sha256
    of the whole message list. A retry sends the same list (same key); a new
    turn or a compaction changes it.
    """
    return _sha16(json.dumps(messages, sort_keys=True, default=str))


def persona() -> str | None:
    """
    Return the first-turn persona for `--append-system-prompt`: the content
    of CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE when that file exists, None when
    CLAUDE_CODE_BG_PERSONA=0, else PERSONA.
    """
    path = os.environ.get('CLAUDE_CODE_BG_SYSTEM_PROMPT_FILE')
    if path and Path(path).is_file():
        return Path(path).read_text().strip() or None
    if os.environ.get('CLAUDE_CODE_BG_PERSONA') == '0':
        return None
    return PERSONA
