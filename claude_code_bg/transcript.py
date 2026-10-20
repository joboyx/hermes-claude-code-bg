"""Transcript reader: decides when a Claude Code turn is finished, and its reply and usage.

A turn is read from the session transcript `<projects>/*/<session_id>.jsonl`, starting at the
byte offset taken before the send, using only complete lines. The job state
`<jobs>/<claude_id>/state.json` tells whether background tasks still run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PROMPT_MATCH_CHARS = 200


def projects_dir() -> Path:
    """
    Return the Claude Code projects directory: `$CLAUDE_PROJECTS`, else `~/.claude/projects`.
    """
    return Path(os.environ.get('CLAUDE_PROJECTS') or Path.home() / '.claude' / 'projects')


def jobs_dir() -> Path:
    """
    Return the Claude Code jobs directory: `$CLAUDE_JOBS`, else `~/.claude/jobs`.
    """
    return Path(os.environ.get('CLAUDE_JOBS') or Path.home() / '.claude' / 'jobs')


def path_of(session_id: str) -> Path | None:
    """
    Return the transcript of `session_id` (first sorted match of `<projects>/*/<sid>.jsonl`),
    or None when there is none yet.
    """
    if not session_id:
        return None
    return next(iter(sorted(projects_dir().glob(f'*/{session_id}.jsonl'))), None)


def size(session_id: str) -> int:
    """
    Return the transcript size in bytes (0 when it does not exist). Take it before a send:
    it is the offset that `read_turn` starts from.
    """
    path = path_of(session_id)
    return path.stat().st_size if path else 0


def in_flight(claude_id: str) -> int:
    """
    Return the number of background tasks still running for the job `claude_id`
    (`inFlight.tasks` in `<jobs>/<id>/state.json`); 0 when the file is missing or unreadable.
    """
    try:
        state = json.loads((jobs_dir() / claude_id / 'state.json').read_text())
    except (OSError, ValueError):
        # No readable job state: treat it as no background task (the turn still needs end_turn).
        return 0
    return int((state.get('inFlight') or {}).get('tasks') or 0)


@dataclass
class Turn:
    """
    A finished turn: the reply `text`, the `usage` sums (`input`, `cache_read`,
    `cache_creation`, `output`, `api_calls`, plus `last` = the last API call's three input
    buckets), the number of distinct end_turn messages `n_end_turns`, and `sig`, which
    changes whenever the transcript grows or the last message changes. Accept a turn only
    when two consecutive polls return the same `sig`. `blocker` is None for a finished
    turn; `inspect_turn` also returns a turn that ended with an end_turn but is held back:
    `in_flight` (background tasks still run) or `empty` (the end_turn has no text).
    """
    text: str
    usage: dict
    n_end_turns: int
    sig: tuple
    blocker: str | None = None


def read_turn(session_id: str, claude_id: str, offset: int, prompt: str,
              skip_matches: int = 0) -> Turn | None:
    """
    Return the finished turn after byte `offset`, or None while it still runs.

    Complete means: our user entry is found (not meta, string content that contains the first
    200 characters of `prompt`; the first `skip_matches` such entries are ignored, for a
    copied session whose transcript repeats the history); among main-chain entries after it,
    the last user/assistant entry is an assistant end_turn with non-empty text; every queue
    enqueue was dequeued or removed; and the job has no in-flight background task. One user
    message can produce several end_turns (an async subagent or background command finishes
    after the first); the reply is the text of the last one.
    """
    turn = inspect_turn(session_id, claude_id, offset, prompt, skip_matches)
    return turn if turn is not None and turn.blocker is None else None


def inspect_turn(session_id: str, claude_id: str, offset: int, prompt: str,
                 skip_matches: int = 0) -> Turn | None:
    """
    Like `read_turn`, but also return a turn that only one rule holds back, with `blocker`
    set: `in_flight` when the end_turn has text but the job still has in-flight background
    tasks (a dev server started in the background never ends), `empty` when the end_turn
    has no text and nothing else runs. Returns None while the turn still runs for any other
    reason (no end_turn yet, an unbalanced queue, an empty end_turn with in-flight tasks).
    """
    entries = _entries(session_id, offset)
    start = _our_start(entries, prompt, skip_matches)
    if start is None:
        return None
    main = [e for e in entries[start:] if not e.get('isSidechain')]
    convo = [e for e in main if e.get('type') in ('user', 'assistant')]
    last = convo[-1] if convo else None
    if last is None or last.get('type') != 'assistant' or _msg(last).get('stop_reason') != 'end_turn':
        return None
    ops = [e.get('operation') for e in main if e.get('type') == 'queue-operation']
    if ops.count('enqueue') > ops.count('dequeue') + ops.count('remove'):
        return None
    last_id = _msg(last).get('id')
    texts = [block.get('text', '') for e in convo if e.get('type') == 'assistant' and _msg(e).get('id') == last_id
             for block in _msg(e).get('content') or []
             if isinstance(block, dict) and block.get('type') == 'text']
    has_text = any(t.strip() for t in texts)
    busy = in_flight(claude_id) > 0
    if busy and not has_text:
        return None
    blocker = 'in_flight' if busy else (None if has_text else 'empty')
    assistants = [e for e in convo if e.get('type') == 'assistant']
    n_end = len({_msg(e).get('id') for e in assistants if _msg(e).get('stop_reason') == 'end_turn'})
    return Turn(text='\n'.join(texts).strip(), usage=_usage(assistants), n_end_turns=n_end,
                sig=(len(entries), last_id), blocker=blocker)


def count_prompts(session_id: str, prompt: str) -> int:
    """
    Return the number of user entries in the whole transcript that match `prompt` (the same
    rule as `read_turn`); 0 when there is no transcript. Before reading a copied session, count
    them in the old transcript and pass the count as `skip_matches`.
    """
    return sum(1 for e in _entries(session_id, 0) if _is_our_prompt(e, prompt))


def last_stop_reason(session_id: str, offset: int, prompt: str,
                     skip_matches: int = 0) -> str | None:
    """
    Return the `stop_reason` of the last main-chain assistant entry after our user entry
    (found as in `read_turn`), or None when there is no such entry yet. Together with an
    agents row that is no longer working, a value other than `end_turn` (`max_tokens`, an
    API-error message) means the turn ended without a reply.
    """
    entries = _entries(session_id, offset)
    start = _our_start(entries, prompt, skip_matches)
    if start is None:
        return None
    assistants = [e for e in entries[start:]
                  if e.get('type') == 'assistant' and not e.get('isSidechain')]
    return _msg(assistants[-1]).get('stop_reason') if assistants else None


def _entries(session_id: str, offset: int) -> list[dict]:
    path = path_of(session_id)
    if path is None:
        return []
    with open(path, 'rb') as fh:
        fh.seek(offset)
        data = fh.read()
    return _complete_entries(data)


def _our_start(entries: list[dict], prompt: str, skip_matches: int) -> int | None:
    """Return the index of our user entry after `skip_matches` earlier matches, else None."""
    matches = (i for i, e in enumerate(entries) if _is_our_prompt(e, prompt))
    for _ in range(skip_matches):
        if next(matches, None) is None:
            return None
    return next(matches, None)


def _complete_entries(data: bytes) -> list[dict]:
    # Only lines that end in a newline: the writer may be mid-line. A complete line that is
    # not JSON is skipped so one bad line cannot block the turn.
    entries = []
    for line in data[:data.rfind(b'\n') + 1].splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _msg(entry: dict) -> dict:
    return entry.get('message') or {}


def _is_our_prompt(entry: dict, prompt: str) -> bool:
    content = _msg(entry).get('content')
    return (entry.get('type') == 'user' and not entry.get('isMeta') and isinstance(content, str)
            and prompt[:PROMPT_MATCH_CHARS] in content)


def _usage(assistants: list[dict]) -> dict:
    # One API message can span several lines (one per content block) with the same id and
    # the same usage: count each message id once.
    usage = {'input': 0, 'cache_read': 0, 'cache_creation': 0, 'output': 0, 'api_calls': 0,
             'last': {'input': 0, 'cache_read': 0, 'cache_creation': 0}}
    seen = set()
    for entry in assistants:
        msg = _msg(entry)
        if msg.get('id') in seen:
            continue
        seen.add(msg.get('id'))
        raw = msg.get('usage') or {}
        call = {'input': raw.get('input_tokens', 0), 'cache_read': raw.get('cache_read_input_tokens', 0),
                'cache_creation': raw.get('cache_creation_input_tokens', 0)}
        for key, value in call.items():
            usage[key] += value
        usage['output'] += raw.get('output_tokens', 0)
        usage['api_calls'] += 1
        # The last call's context is what Hermes' context compressor should see.
        usage['last'] = call
    return usage
