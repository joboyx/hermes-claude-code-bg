"""Store: per-conversation state files, locks, the event log and the paths under the Hermes home.

Layout under `<hermes home>/claude-code-bg/`: `conv/<conv>.json` (state, at most `KEEP_TURNS` turn
records plus every turn still `sent`), `conv/<conv>.lock`
(cross-process lock), `events.jsonl` (append-only event log) and `cwd/` (default conversation cwd).
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path

KEEP_TURNS = 20  # turn records kept per conversation (plus every turn still `sent`)
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def hermes_home() -> Path:
    """
    Return the bound Hermes home: `hermes_constants.get_hermes_home()` when Hermes is
    importable (profile-aware), else `$HERMES_HOME`, else `~/.hermes`.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return Path(os.environ.get('HERMES_HOME') or Path.home() / '.hermes')
    return Path(get_hermes_home())


def base_dir() -> Path:
    """
    Return the plugin's own directory, `<hermes home>/claude-code-bg`.
    """
    return hermes_home() / 'claude-code-bg'


def default_cwd() -> str:
    """
    Return the working directory for new conversations: `CLAUDE_CODE_BG_CWD`, else
    `<base>/cwd`.
    """
    return os.environ.get('CLAUDE_CODE_BG_CWD') or str(base_dir() / 'cwd')


def _conv_dir() -> Path:
    path = base_dir() / 'conv'
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(conv: str) -> dict:
    """
    Return the saved state of conversation `conv`, or an empty state when none is saved.
    The result always has a `turns` dict.
    """
    try:
        state = json.loads((_conv_dir() / f'{conv}.json').read_text())
    except FileNotFoundError:
        state = {}
    state.setdefault('turns', {})
    return state


def state_path(conv: str) -> Path:
    """
    Return the state file of conversation `conv`, `<base>/conv/<conv>.json` (it may not exist).
    """
    return _conv_dir() / f'{conv}.json'


def prune_turns(turns: dict) -> dict:
    """
    Return `turns` without the oldest records: the KEEP_TURNS newest by `sent_at` stay, and
    every turn still `sent` stays whatever its age (its reply may still arrive).
    """
    order = sorted(turns, key=lambda key: turns[key].get('sent_at') or 0)
    newest = set(order[-KEEP_TURNS:])
    return {key: turn for key, turn in turns.items()
            if key in newest or turn.get('state') == 'sent'}


def save(conv: str, state: dict) -> None:
    """
    Write the state of conversation `conv` atomically: write a temp file in the same
    directory, then rename it over `<conv>.json`, so a reader never sees a partial file.
    The turn records are pruned first (`prune_turns`).
    """
    if len(state.get('turns') or {}) > KEEP_TURNS:
        state['turns'] = prune_turns(state['turns'])
    directory = _conv_dir()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'{conv}.', suffix='.tmp')
    with os.fdopen(fd, 'w') as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, directory / f'{conv}.json')


def all_conversations() -> list[str]:
    """
    Return the keys of every conversation with a saved state, sorted.
    """
    directory = base_dir() / 'conv'
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob('*.json'))


def thread_lock(conv: str) -> threading.Lock:
    """
    Return the in-process lock of conversation `conv`; the same object for the same key.
    """
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(conv, threading.Lock())


class FileLock:
    """
    Cross-process lock of one conversation (`fcntl.flock` on `<base>/conv/<conv>.lock`),
    for two Hermes processes on one home. Use it as a context manager. By default entering
    blocks until the lock is free; with `blocking=False` entering raises `BlockingIOError`
    at once when another open file (this process or another one) holds the lock.
    The `.lock` file stays on disk after release; only the retention cleanup deletes it, with
    its conversation and while it holds the lock. Entering checks that the locked file is
    still the one at the path (the cleanup may have deleted it meanwhile) and locks again
    when it is not.
    """

    def __init__(self, conv: str, blocking: bool = True):
        self.path = _conv_dir() / f'{conv}.lock'
        self.blocking = blocking
        self._fh = None

    def __enter__(self) -> 'FileLock':
        while True:
            fh = open(self.path, 'w')
            try:
                fcntl.flock(fh, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self._is_current(fh):
                    break
            except BaseException:
                fh.close()
                raise
            # The cleanup deleted the file while this process waited on it: lock the new one.
            fh.close()
        self._fh = fh
        return self

    def _is_current(self, fh) -> bool:
        """True when `fh` is still the file at `self.path` (not unlinked or replaced)."""
        try:
            return os.stat(self.path).st_ino == os.fstat(fh.fileno()).st_ino
        except FileNotFoundError:
            return False

    def __exit__(self, *_exc) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None


def event(data: dict) -> None:
    """
    Append one JSON line `{"t": <unix time>, **data}` to `<base>/events.jsonl`.
    An `OSError` is ignored: the event log must never break a turn.
    """
    try:
        path = base_dir() / 'events.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a') as fh:
            fh.write(json.dumps({'t': round(time.time(), 2), **data}) + '\n')
    except OSError:
        pass
