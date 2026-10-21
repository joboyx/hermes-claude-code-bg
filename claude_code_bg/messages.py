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
# Hermes' image hint lines (agent/image_routing.build_native_content_parts), one per picture.
IMAGE_HINT = re.compile(r'^\[Image attached(?: at)?: [^\n]*\][ \t]*$', re.M)
HISTORY_HEADER = 'Earlier messages in this chat (for context):'
HISTORY_CAP = 12000  # characters of carried history, header included
# Hermes' notes to its own agent inside a user message (onboarding, interrupted turn ...).
SYSTEM_NOTE = re.compile(r'\[System note: .*?\][ \t]*(?:\n|$)', re.S)
CUT = '> …'
ATTACHED = re.compile(r'^\[Attached image: (.+) - open it with the Read tool to see it\.\]$', re.M)
IMAGE_EXT = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp'}

PERSONA = (
    'You are answering a person in a chat app. Hermes relays their messages to you and '
    'sends your reply back. Keep replies short and easy to read on a phone; many chat apps '
    'show bold, italics, inline code, code blocks and links, but not tables wider than a '
    'phone screen. You cannot ask follow-up questions with a picker: ask in plain text and wait for '
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


def images_dir(cwd: str) -> Path:
    """
    Return the folder the client saves image attachments in: `<cwd>/tmp/hermes-images`.
    """
    return Path(cwd) / 'tmp' / 'hermes-images'


def saved_images(prompt: str, cwd: str) -> list[str]:
    """
    Return the paths of the images in `prompt` that `save_image` wrote under
    `images_dir(cwd)` (not the files the user referenced by path), in order.
    """
    folder = str(images_dir(cwd)) + os.sep
    return [path for path in ATTACHED.findall(prompt) if path.startswith(folder)]


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
        folder = images_dir(cwd)
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
    `save_image` line, and the Hermes model-switch note removed. When the
    message has image parts, Hermes' own hint lines (`[Image attached at:
    <path>]`, `[Image attached: <url>]`) are removed from the text: the
    `save_image` line already names each picture once.
    """
    if isinstance(content, str):
        return MODEL_NOTE.sub('', content).strip()
    parts = [p for p in content or [] if isinstance(p, dict)]
    has_image = any(p.get('type') == 'image_url' for p in parts)
    out = []
    for part in parts:
        if part.get('type') == 'text':
            text = part.get('text') or ''
            if has_image:
                text = re.sub(r'\n{3,}', '\n\n', IMAGE_HINT.sub('', text)).strip()
            out.append(text)
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


def _history_text(message: dict) -> str:
    """The text of one earlier message: images as `[image]`; Hermes system and model-switch
    notes and image hints removed."""
    text = SYSTEM_NOTE.sub('', MODEL_NOTE.sub('', text_of(message.get('content')).strip()))
    return re.sub(r'\n{3,}', '\n\n', IMAGE_HINT.sub('', text)).strip()


def _newest_index(dicts: list[dict]) -> int:
    return max(i for i, m in enumerate(dicts) if m.get('role') == 'user')


def seen_marker(messages: list) -> dict:
    """
    Return what the Claude session has seen of a Hermes message list once its newest user
    message is sent: that message's index among the dict messages and a hash of its text.
    Store it after each send; `unseen_history` reads it on the next turn.
    """
    dicts = [m for m in messages if isinstance(m, dict)]
    index = _newest_index(dicts)
    return {'index': index, 'hash': _sha16(_history_text(dicts[index]))}


def unseen_history(messages: list, seen: dict | None) -> list[dict]:
    """
    Return the user and assistant messages before the newest user message that the Claude
    session has not seen. With no `seen` marker (the first turn) that is all of them: the
    chat may have run on another provider before. Else it is the messages after the seen
    user message (found at its stored index, else by its text, searching back from the
    newest), without the first assistant reply after it (Claude's own answer). When the seen
    message is no longer in the list (`/undo`, a rewrite) it returns []: the Claude session
    keeps its own history.
    """
    dicts = [m for m in messages if isinstance(m, dict)]
    newest = _newest_index(dicts)
    if seen is None:
        start = 0
    else:
        def is_seen(i: int) -> bool:
            return dicts[i].get('role') == 'user' and _sha16(_history_text(dicts[i])) == seen['hash']
        index = seen.get('index', -1)
        if 0 <= index <= newest and is_seen(index):
            start = index + 1
        else:
            found = next((i for i in range(newest - 1, -1, -1) if is_seen(i)), None)
            if found is None:
                return []
            start = found + 1
        if start < newest and dicts[start].get('role') == 'assistant':
            start += 1
    return [m for m in dicts[start:newest]
            if m.get('role') in ('user', 'assistant') and _history_text(m)]


def history_block(earlier: list[dict]) -> str:
    """
    Return the context block put before the newest message when the Claude session has not
    seen `earlier` (see `unseen_history`): HISTORY_HEADER, then each user or assistant
    message as `User: ...` / `Assistant: ...` with every line quoted (`> `). The block is
    at most HISTORY_CAP characters: the newest messages are kept, a `> …` line marks the cut,
    and a single message too long on its own keeps its end. Returns '' when there is nothing
    to show.
    """
    entries = [f"{'User' if m.get('role') == 'user' else 'Assistant'}: {_history_text(m)}"
               for m in earlier if m.get('role') in ('user', 'assistant') and _history_text(m)]
    if not entries:
        return ''

    def quote(text: str) -> str:
        return '\n'.join(f'> {line}' if line else '>' for line in text.split('\n'))

    budget = HISTORY_CAP - len(HISTORY_HEADER) - len(f'\n{CUT}\n')
    kept: list[str] = []
    used = 0
    for entry in reversed(entries):
        quoted = quote(entry)
        if used + len(quoted) + 1 > budget:
            if not kept:  # the newest message alone is too long: keep its end
                role, _, body = entry.partition(': ')
                room = budget - len(f'> {role}: …')
                tail = quote(f'{role}: …{body[max(0, len(body) - room):]}')
                while len(tail) > budget:
                    room -= len(tail) - budget
                    tail = quote(f'{role}: …{body[max(0, len(body) - room):]}')
                kept.append(tail)
            break
        kept.append(quoted)
        used += len(quoted) + 1
    lines = [HISTORY_HEADER] + ([CUT] if len(kept) < len(entries) else [])
    return '\n'.join(lines + kept[::-1])


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
