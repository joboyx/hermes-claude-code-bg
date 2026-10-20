"""Store: per-conversation state files, locks, the event log and the paths under the Hermes home.

Layout under `<hermes home>/claude-code-bg/`: `conv/<conv>.json` (state), `conv/<conv>.lock`
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


def save(conv: str, state: dict) -> None:
    """
    Write the state of conversation `conv` atomically: write a temp file in the same
    directory, then rename it over `<conv>.json`, so a reader never sees a partial file.
    """
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
    The `.lock` file stays on disk after release.
    """

    def __init__(self, conv: str, blocking: bool = True):
        self.path = _conv_dir() / f'{conv}.lock'
        self.blocking = blocking
        self._fh = None

    def __enter__(self) -> 'FileLock':
        fh = open(self.path, 'w')
        try:
            fcntl.flock(fh, fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            fh.close()
            raise
        self._fh = fh
        return self

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
