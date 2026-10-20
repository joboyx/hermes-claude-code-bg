"""Admission and idle reaper: keep the number of live Claude Code processes on this host low.

Admission decides whether a turn may start or resume a Claude Code process: it counts the
live rows of `claude agents --json --all` (all kinds) and our own live chats (rows whose
`claude-watch` ledger record has `mode: "chat"`), and on Linux checks `MemAvailable`. When
the host is full it stops our own idle chats, least recently used first, before it refuses.
The reaper is one daemon thread per process that stops the process of every conversation
idle for longer than `idle_stop_seconds()`. The conversation and its session stay; the next
message resumes them. The reaper also runs the retention cleanup at most once a day: it deletes
the state and saved images of conversations idle for more than `retain_days()`.
"""
from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from claude_code_bg import driver, store

MEMINFO = '/proc/meminfo'
DEFAULT_MAX_LIVE = 3
DEFAULT_MAX_CHATS = 2
DEFAULT_MIN_MEM_MB = 1536
DEFAULT_IDLE_STOP_S = 600.0
LOW_MEM_IDLE_STOP_S = 120.0
LOW_MEM_MB = 1228
REAPER_THREAD = 'claude-code-bg-reaper'
BUSY_REPLY = ('Claude Code is busy on this host right now ({live} live sessions, {mem_mb} MB '
              'free). Send your message again in a few minutes.')

DEFAULT_RETAIN_DAYS = 30.0
CLEANUP_EVERY_S = 86400.0
CLEANUP_MARKER = 'cleanup-at'

_UNCHANGED: dict[str, tuple[int, int] | None] = {}  # state file -> stamp with nothing to reap
_REAPER: dict = {}
_REAPER_GUARD = threading.Lock()


@dataclass
class Verdict:
    """
    The admission result: `ok` is True when the turn may start or resume its process.
    `live` is the number of live Claude Code sessions on the host (all kinds) at the
    decision; `mem_mb` is `MemAvailable` in MB, or None where it is not known (macOS).
    """
    ok: bool
    live: int
    mem_mb: int | None


def mem_available_mb() -> int | None:
    """
    Return `MemAvailable` from `/proc/meminfo` in MB (kB // 1024), or None when the file
    or the line does not exist (macOS, old kernels). CLAUDE_CODE_BG_MEMINFO overrides
    the path (tests).
    """
    path = Path(os.environ.get('CLAUDE_CODE_BG_MEMINFO') or MEMINFO)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    for line in text.splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024
    return None


def idle_stop_seconds() -> float:
    """
    Return the idle time after which the reaper stops a conversation's process:
    CLAUDE_CODE_BG_IDLE_STOP_S, else 600. While `MemAvailable` is under 1228 MB the
    value is at most 120. 0 (or below) turns the reaper off.
    """
    raw = os.environ.get('CLAUDE_CODE_BG_IDLE_STOP_S')
    idle = float(raw) if raw else DEFAULT_IDLE_STOP_S
    if idle <= 0:
        return 0.0
    mem = mem_available_mb()
    if mem is not None and mem < LOW_MEM_MB:
        return min(idle, LOW_MEM_IDLE_STOP_S)
    return idle


def busy_reply(verdict: Verdict) -> str:
    """
    Return BUSY_REPLY filled from a refused verdict; an unknown memory value shows as
    `unknown`.
    """
    mem = verdict.mem_mb if verdict.mem_mb is not None else 'unknown'
    return BUSY_REPLY.format(live=verdict.live, mem_mb=mem)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _is_chat(claude_id: str) -> bool:
    """True when the ledger record of `claude_id` marks it as one of our chat sessions."""
    path = store.hermes_home() / 'claude-watch' / f'{claude_id}.json'
    try:
        record = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        # No readable record: the row belongs to someone else (board, terminal).
        return False
    return isinstance(record, dict) and record.get('mode') == 'chat'


def _working(row: dict) -> bool:
    return row.get('state') == 'working' and row.get('status') not in ('idle', None)


def _last_active(state: dict) -> float:
    """
    Return when the conversation was last active: its last finished turn or the send of a
    turn still `sent` (a turn that failed or timed out may never finish), 0 when neither.
    A `sent` turn alone does not block a stop: only a working row does. The transcript
    keeps a late reply and the client's orphan read delivers it on the next message.
    """
    sent = [t.get('sent_at') or 0 for t in state.get('turns', {}).values()
            if t.get('state') == 'sent']
    return max([state.get('last_done_at') or 0, *sent])


def _live_rows() -> dict[str, dict]:
    return {cid: row for cid, row in driver.agents().items() if driver.pid_alive(row.get('pid'))}


def _check(live: dict[str, dict], own_id: str | None) -> Verdict:
    # A resume of a conversation whose process is live replaces that process: it adds no
    # session and about no memory (MemAvailable already excludes what that process holds).
    others = [cid for cid in live if cid != own_id]
    chats = [cid for cid in others if _is_chat(cid)]
    mem = mem_available_mb()
    mem_ok = (own_id in live or mem is None
              or mem >= _env_int('CLAUDE_CODE_BG_MIN_MEM_MB', DEFAULT_MIN_MEM_MB))
    ok = (len(others) <= _env_int('CLAUDE_CODE_BG_MAX_LIVE', DEFAULT_MAX_LIVE) - 1
          and len(chats) < _env_int('CLAUDE_CODE_BG_MAX_CHATS', DEFAULT_MAX_CHATS)
          and mem_ok)
    return Verdict(ok=ok, live=len(live), mem_mb=mem)


def _idle_chats(live: dict[str, dict], conv: str) -> list[tuple[str, str]]:
    """Return (conv, claude_id) of our other live idle chats, least recently used first."""
    found = []
    for other in store.all_conversations():
        if other == conv:
            continue
        try:
            state = store.load(other)
        except ValueError:
            continue  # a corrupt state file: not a candidate
        cid = state.get('claude_id')
        row = live.get(cid) if cid else None
        if row is None or _working(row) or not _is_chat(cid):
            continue
        found.append((_last_active(state), other, cid))
    return [(other, cid) for _, other, cid in sorted(found)]


def _stop_if_idle(conv: str, rows: dict[str, dict], idle_s: float | None, by: str) -> str | None:
    """
    Stop the process of `conv` when it is idle; return its claude id, else None.

    Takes the thread lock and the file lock without waiting: a held lock means a turn is
    in flight, so the conversation is skipped. Idle means a row that is not working and
    whose pid is alive (a `sent` turn on an idle row does not count as busy) and, when
    `idle_s` is set, a last activity (`_last_active`) more than `idle_s` seconds ago that
    was not reaped yet.
    """
    lock = store.thread_lock(conv)
    if not lock.acquire(blocking=False):
        return None
    try:
        with contextlib.ExitStack() as stack:
            try:
                stack.enter_context(store.FileLock(conv, blocking=False))
            except BlockingIOError:
                return None  # another process runs a turn of this conversation
            state = store.load(conv)
            cid = state.get('claude_id')
            if not cid:
                return None
            now = time.time()
            done_at = _last_active(state)
            if idle_s is not None and (not done_at or now - done_at < idle_s
                                       or (state.get('reaped_at') or 0) >= done_at):
                return None
            row = rows.get(cid)
            if row is None or _working(row) or not driver.pid_alive(row.get('pid')):
                return None
            driver.stop(cid)
            state['reaped_at'] = time.time()
            store.save(conv, state)
            store.event({'kind': 'reaped', 'by': by, 'conv': conv, 'claude_id': cid,
                         'pid': row.get('pid'),
                         'idle_s': round(now - done_at, 1) if done_at else None})
            return cid
    finally:
        lock.release()


def admit(conv: str) -> Verdict:
    """
    Decide whether conversation `conv` may start or resume its Claude Code process.

    Admitted when the other live sessions (all kinds) are at most
    CLAUDE_CODE_BG_MAX_LIVE (default 3) - 1, our other live chats are fewer than
    CLAUDE_CODE_BG_MAX_CHATS (default 2) and, where `MemAvailable` is known,
    it is at least CLAUDE_CODE_BG_MIN_MEM_MB (default 1536). The process of `conv`
    itself, when live, does not count and skips the memory check: the resume replaces
    it. When not admitted, our
    other idle chats are stopped one at a time, least recently used first, and the
    check runs again. A refusal is logged as an `admission` event.
    """
    own_id = store.load(conv).get('claude_id')
    tried: set[str] = set()
    while True:
        live = _live_rows()
        verdict = _check(live, own_id)
        if verdict.ok:
            return verdict
        candidates = [(c, cid) for c, cid in _idle_chats(live, conv) if cid not in tried]
        if not candidates:
            store.event({'kind': 'admission', 'conv': conv, 'ok': False,
                         'live': verdict.live, 'mem_mb': verdict.mem_mb})
            return verdict
        other, cid = candidates[0]
        tried.add(cid)
        if _stop_if_idle(other, live, None, 'admission'):
            driver.wait_exit(cid, live[cid].get('pid'))


def reap_once() -> list[str]:
    """
    Stop the process of every conversation last active (last finished turn, or the send
    of a turn still `sent`) more than `idle_stop_seconds()` ago; return the stopped
    claude ids. Skips a conversation with a held thread or file lock, a working row, no
    row or a dead pid, and one already reaped since its last activity. A `sent` turn on
    an idle row is reaped: its late reply stays in the transcript. Records `reaped_at` in the state.
    A state file found with nothing to do until it changes (no session, or reaped since its
    last activity) is not read again while its inode and mtime stay the same.
    An error in one conversation (a corrupt state file) is logged as a `reaper_error`
    event and the others are still reaped. Ends with `maybe_cleanup()`.
    """
    idle_s = idle_stop_seconds()
    if idle_s <= 0:
        return []
    stopped: list[str] = []
    rows: dict[str, dict] | None = None
    now = time.time()
    for conv in store.all_conversations():
        path = store.state_path(conv)
        stamp = _stamp(path)
        if stamp is not None and _UNCHANGED.get(str(path)) == stamp:
            continue
        try:
            peek = store.load(conv)
            done_at = _last_active(peek)
            if (not peek.get('claude_id') or not done_at
                    or (peek.get('reaped_at') or 0) >= done_at):
                _UNCHANGED[str(path)] = stamp  # nothing to do here until the file changes
                continue
            if now - done_at < idle_s:
                continue
            rows = rows if rows is not None else driver.agents()
            cid = _stop_if_idle(conv, rows, idle_s, 'reaper')
            if cid:
                stopped.append(cid)
                _UNCHANGED[str(path)] = _stamp(path)
        except Exception as exc:  # one bad conversation must not stop the others
            store.event({'kind': 'reaper_error', 'conv': conv, 'error': repr(exc)[:300]})
    maybe_cleanup()
    return stopped


def _stamp(path: Path) -> tuple[int, int] | None:
    """The inode and mtime of a state file (a save replaces the inode), None when it is gone."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_mtime_ns


def retain_days() -> float:
    """
    Return how many idle days a conversation's state and saved images are kept:
    CLAUDE_CODE_BG_RETAIN_DAYS, else 30. 0 (or below) turns the cleanup off.
    """
    raw = os.environ.get('CLAUDE_CODE_BG_RETAIN_DAYS')
    return max(0.0, float(raw)) if raw else DEFAULT_RETAIN_DAYS


def maybe_cleanup() -> list[str]:
    """
    Run `cleanup_once()` when it has not run in the last 24 hours (the mtime of
    `<base>/cleanup-at`, shared by every process on the home); return the deleted
    conversation keys, else [].
    """
    marker = store.base_dir() / CLEANUP_MARKER
    try:
        last = marker.stat().st_mtime
    except FileNotFoundError:
        last = 0.0
    if time.time() - last < CLEANUP_EVERY_S:
        return []
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return cleanup_once()


def cleanup_once() -> list[str]:
    """
    Delete every conversation idle for more than `retain_days()` (last activity as in the
    reaper; the state file's mtime when it has none): its state file, then its `.lock` file
    (only while this call holds that lock, taken without waiting), and the images it saved
    under `<cwd>/tmp/hermes-images/` that no kept conversation lists. Skips a conversation
    whose lock is held or whose process is still alive, and a corrupt state file. Never
    touches the Claude Code transcripts, rows or ledger records. Returns the deleted keys.
    """
    days = retain_days()
    if days <= 0:
        return []
    cutoff = time.time() - days * 86400
    states = {}
    for conv in store.all_conversations():
        try:
            states[conv] = store.load(conv)
        except ValueError:
            continue  # a corrupt state file: the reaper logs it
    if not any(_idle_since(c, s) < cutoff for c, s in states.items()):
        return []
    live = _live_rows()
    deleted: dict[str, list[str]] = {}
    for conv in sorted(c for c, s in states.items() if _idle_since(c, s) < cutoff):
        images = _delete_if_old(conv, live, cutoff)
        if images is not None:
            deleted[conv] = images
    kept = {p for c, s in states.items() if c not in deleted for p in s.get('images') or []}
    for conv, images in deleted.items():
        gone = [p for p in images if p not in kept and _is_saved_image(p)]
        for path in gone:
            Path(path).unlink(missing_ok=True)
        store.event({'kind': 'cleanup', 'conv': conv, 'images': len(gone)})
    return list(deleted)


def _idle_since(conv: str, state: dict) -> float:
    return _last_active(state) or (_stamp_time(store.state_path(conv)))


def _stamp_time(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return time.time()


def _is_saved_image(path: str) -> bool:
    """Only files the client saved (`<cwd>/tmp/hermes-images/<file>`) are ever deleted."""
    parent = Path(path).parent
    return parent.name == 'hermes-images' and parent.parent.name == 'tmp'


def _delete_if_old(conv: str, live: dict[str, dict], cutoff: float) -> list[str] | None:
    """Delete the state and lock files of `conv` under its locks; return its images, else None."""
    lock = store.thread_lock(conv)
    if not lock.acquire(blocking=False):
        return None
    try:
        with contextlib.ExitStack() as stack:
            try:
                file_lock = stack.enter_context(store.FileLock(conv, blocking=False))
            except BlockingIOError:
                return None  # a turn of this conversation runs in another process
            state = store.load(conv)
            if _idle_since(conv, state) >= cutoff or state.get('claude_id') in live:
                return None
            store.state_path(conv).unlink(missing_ok=True)
            file_lock.path.unlink(missing_ok=True)
            _UNCHANGED.pop(str(store.state_path(conv)), None)
            return list(state.get('images') or [])
    finally:
        lock.release()


def _reap_tick() -> None:
    """Run one reaper pass; log any error as a `reaper_error` event instead of raising."""
    try:
        reap_once()
    except Exception as exc:  # the reaper thread must never die
        store.event({'kind': 'reaper_error', 'error': repr(exc)[:300]})


def _reap_loop() -> None:
    while True:
        try:
            idle_s = idle_stop_seconds()
        except ValueError:
            idle_s = DEFAULT_IDLE_STOP_S  # a bad env value: _reap_tick logs it
        time.sleep(min(60.0, max(5.0, idle_s / 4)))
        _reap_tick()


def start_reaper() -> None:
    """
    Start the idle reaper: one daemon thread per process (later calls do nothing).
    Every min(60, max(5, idle / 4)) s it runs `reap_once`; an error is logged as a
    `reaper_error` event and the loop goes on. Does not start when
    `idle_stop_seconds()` is 0 (CLAUDE_CODE_BG_IDLE_STOP_S=0).
    """
    with _REAPER_GUARD:
        if _REAPER.get('thread') or idle_stop_seconds() <= 0:
            return
        thread = threading.Thread(target=_reap_loop, name=REAPER_THREAD, daemon=True)
        _REAPER['thread'] = thread
        thread.start()
