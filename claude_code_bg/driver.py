"""Driver: the only module that runs the `claude` CLI.

It starts a chat session in the background (`claude --bg`), sends a follow-up
(stop, wait for the process to exit, rm, `--resume`), denies a permission prompt
through a short tmux attach, and writes the chat ledger record under
`<hermes home>/claude-watch/` so hub tooling can tell chat sessions apart.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from claude_code_bg import catalog, store

SETTINGS = {
    'autoMemoryEnabled': False,
    'remoteControlAtStartup': False,
    'skipDangerousModePermissionPrompt': True,
    'autoCompactWindow': 200000,
}
STRIPPED_ENV = ('CLAUDECODE', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_ENTRYPOINT',
                'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN')
CALL_TIMEOUT_S = 60
ROW_TRIES = 60  # x 0.5 s: wait about 30 s for a new row's sessionId

ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
BG_LINE = re.compile(r'backgrounded\s+·\s+([A-Za-z0-9]+)')
COPY_NOTE = re.compile(r'started a copy as\s+([A-Za-z0-9]+)')
UNTRUSTED = 'Workspace not trusted'
PROCEED = 'Do you want to proceed?'
OPTION = re.compile(r'^\s*(?:❯\s*)?(\d+)\.\s+(.*?)\s*$')


class DriverError(RuntimeError):
    """
    A `claude` CLI call failed or printed something the driver cannot use
    (no session id, a failed row, an untrusted workspace, a failed resume).
    """


@dataclass
class Session:
    """
    One Claude Code background session: the short agents-row id and the
    conversation (transcript) session id. `copied` is True when a follow-up
    started a copy under a new id instead of resuming the same row.
    """
    claude_id: str
    session_id: str
    copied: bool = False


def nap(seconds: float) -> None:
    """
    Sleep `seconds` times CLAUDE_CODE_BG_SLEEP_SCALE (float, default 1).
    A scale of 0 or below does not sleep, so tests run without waits.
    """
    scale = float(os.environ.get('CLAUDE_CODE_BG_SLEEP_SCALE') or 1)
    if scale > 0:
        time.sleep(seconds * scale)


def command() -> str:
    """
    Return the Claude Code command: CLAUDE_CODE_BG_COMMAND, else `claude`.
    """
    return os.environ.get('CLAUDE_CODE_BG_COMMAND') or 'claude'


def clean_env() -> dict:
    """
    Return the process environment without the variables that would make
    Claude Code act as a child session or use an API key instead of the
    host login (STRIPPED_ENV).
    """
    return {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}


def launch_settings() -> dict:
    """
    Return the `--settings` object for every launch: SETTINGS merged with
    the JSON object in CLAUDE_CODE_BG_SETTINGS (its keys win). Raises
    DriverError when that variable is not a JSON object.
    """
    raw = os.environ.get('CLAUDE_CODE_BG_SETTINGS')
    if not raw:
        return dict(SETTINGS)
    try:
        extra = json.loads(raw)
    except ValueError as exc:
        raise DriverError(f'CLAUDE_CODE_BG_SETTINGS is not JSON: {exc}') from exc
    if not isinstance(extra, dict):
        raise DriverError('CLAUDE_CODE_BG_SETTINGS must be a JSON object')
    return {**SETTINGS, **extra}


def _run(argv: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, cwd=cwd, env=clean_env(), check=False, capture_output=True,
                              text=True, timeout=CALL_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DriverError(f'cannot run {argv[0]}: {exc}') from exc


def _claude(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    return _run([command(), *args], cwd=cwd)


def _output(proc: subprocess.CompletedProcess) -> str:
    return ANSI.sub('', (proc.stdout or '') + (proc.stderr or '')).strip()


def agents() -> dict[str, dict]:
    """
    Return the `claude agents --json --all` rows by short id. A failed call
    or output without a readable JSON list gives {}: use it where an empty
    answer is safe (it only skips work). A caller that deletes or frees
    something because a row is absent uses `agents_strict`.
    """
    try:
        return agents_strict()
    except _ListUnreadable:
        return {}


class _ListUnreadable(DriverError):
    """`claude agents` exited non-zero or printed no readable JSON list."""


def agents_strict() -> dict[str, dict]:
    """
    Return the `claude agents --json --all` rows by short id, like `agents`,
    but raise DriverError when the call fails or its output has no readable
    JSON list, so a failure is never read as an empty host.
    """
    proc = _claude('agents', '--json', '--all')
    raw = proc.stdout or ''
    start = raw.find('[')
    if proc.returncode != 0 or start < 0:
        raise _ListUnreadable(f'claude agents failed (exit {proc.returncode}): '
                              f'{_output(proc)[:300]}')
    try:
        rows = json.loads(raw[start:])
    except ValueError as exc:
        raise _ListUnreadable(f'claude agents printed no JSON list: {exc}') from exc
    if not isinstance(rows, list):
        raise _ListUnreadable('claude agents printed no JSON list')
    return {r['id']: r for r in rows if isinstance(r, dict) and r.get('id')}


def pid_alive(pid) -> bool:
    """
    Return True while process `pid` exists. Only a positive int (not a bool)
    counts as a pid; anything else gives False. A process of another user
    (PermissionError) counts as alive.
    """
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _row_pid(row: dict | None) -> int | None:
    pid = (row or {}).get('pid')
    return pid if type(pid) is int and pid > 0 else None


def _launch_flags(name: str, model: str, effort: str) -> list[str]:
    # --disallowedTools takes several values, so another flag follows it and `--` ends the options.
    return ['--bg', '--name', name, '--model', catalog.resolve(model),
            '--effort', catalog.effort_of(effort), '--permission-mode', 'bypassPermissions',
            '--disallowedTools', 'AskUserQuestion',
            '--settings', json.dumps(launch_settings())]


def _check_launch(proc: subprocess.CompletedProcess, what: str, cwd: str) -> str:
    """Return the short id the launch printed (the copy's id after a copy note)."""
    text = _output(proc)
    if UNTRUSTED in text:
        raise DriverError(f'workspace {cwd} is not trusted by Claude Code: run `claude` there '
                          'once and accept the trust prompt')
    if proc.returncode != 0:
        raise DriverError(f'{what} failed (exit {proc.returncode}): {text[:300]}')
    match = COPY_NOTE.search(text) or BG_LINE.search(text)
    if not match:
        raise DriverError(f'no session id in {what} output: {text[:300]}')
    return match.group(1)


def _wait_row(claude_id: str) -> dict | None:
    for i in range(ROW_TRIES):
        row = agents().get(claude_id)
        if row and (row.get('sessionId') or row.get('state') == 'failed'):
            return row
        if i < ROW_TRIES - 1:
            nap(0.5)
    return None


def _started_session(claude_id: str) -> str:
    """Return the new row's sessionId; DriverError when the row failed or never got one."""
    row = _wait_row(claude_id)
    if row is None:
        raise DriverError(f'{claude_id} has no sessionId in claude agents after '
                          f'{ROW_TRIES // 2} s')
    if row.get('state') == 'failed':
        raise DriverError(f'{claude_id} failed at start')
    return row['sessionId']


def dispatch(prompt: str, *, name: str, cwd: str, model: str, effort: str,
             persona: str | None, hermes_session: str | None) -> Session:
    """
    Start a new background session in `cwd` with the launch flags, plus
    `--append-system-prompt <persona>` when a persona is set, and `prompt`
    after `--`. Waits up to about 30 s for the row's sessionId and writes
    the chat ledger record (state `abandoned` when the start fails).
    Raises DriverError on an untrusted workspace, a failed launch, no id in
    the output, a failed row or no sessionId.
    """
    flags = _launch_flags(name, model, effort)
    if persona:
        flags += ['--append-system-prompt', persona]
    proc = _claude(*flags, '--', prompt, cwd=cwd)
    claude_id = _check_launch(proc, 'claude --bg', cwd)
    # Record the row before polling, so it stays ours whatever happens next.
    write_ledger(Session(claude_id, ''), name=name, cwd=cwd, hermes_session=hermes_session)
    try:
        session_id = _started_session(claude_id)
    except DriverError:
        write_ledger(Session(claude_id, ''), name=name, cwd=cwd, hermes_session=hermes_session,
                     state='abandoned')
        raise
    s = Session(claude_id, session_id)
    write_ledger(s, name=name, cwd=cwd, hermes_session=hermes_session)
    return s


def follow_up(s: Session, prompt: str, *, name: str, cwd: str, model: str,
              effort: str) -> Session:
    """
    Send `prompt` to the same conversation: when the row still exists,
    `stop` it, wait until it has exited (wait_exit) and `rm` it; then
    `--resume <session_id>` with the launch flags (no persona: the
    system-prompt snapshot keeps it). When Claude Code prints a copy note,
    return the copy's Session (copied=True), write its ledger record and
    mark the old record `abandoned`. Raises DriverError on a non-zero exit,
    an untrusted workspace, a failed `rm` or a copy without a sessionId.
    """
    retire(s)
    proc = _claude('--resume', s.session_id, *_launch_flags(name, model, effort), '--', prompt,
                   cwd=cwd)
    new_id = _check_launch(proc, 'claude --resume', cwd)
    previous = _read_ledger(s.claude_id)
    hermes_session = previous.get('hermes_session')
    if new_id == s.claude_id:
        write_ledger(s, name=name, cwd=cwd, hermes_session=hermes_session)
        return Session(s.claude_id, s.session_id)
    copy = Session(new_id, _started_session(new_id), copied=True)
    write_ledger(copy, name=name, cwd=cwd, hermes_session=hermes_session)
    write_ledger(s, name=name, cwd=cwd, hermes_session=hermes_session, state='abandoned')
    return copy


def retire(s: Session) -> None:
    """
    When the row of `s` still exists, `stop` it, wait until it has exited
    (wait_exit) and `rm` it. Raises DriverError when `rm` fails or the agents
    list cannot be read: the caller abandons the session when no row is left,
    so a failed list must not read as an absent row.
    """
    row = agents_strict().get(s.claude_id)
    if row is None:
        return
    stop(s.claude_id)
    wait_exit(s.claude_id, _row_pid(row))
    removed = _claude('rm', s.claude_id)
    if removed.returncode != 0:
        raise DriverError(f'claude rm {s.claude_id} failed: {_output(removed)[:300]}')


def wait_exit(claude_id: str, pid: int | None, tries: int = 60, interval: float = 0.5) -> None:
    """
    Poll the agents list up to `tries` times, `interval` s apart, until the
    row for `claude_id` shows status null or is gone AND the session process
    has exited. The pid is `pid` (from the row before stop) or the first
    polled row that carries one; with no pid only the row decides. Returns
    after the bound even when the process still runs.
    """
    for i in range(tries):
        row = agents().get(claude_id)
        pid = pid or _row_pid(row)
        # The row can show status null before the process is gone; a resume
        # in that window starts a copy.
        if (row is None or row.get('status') is None) and not pid_alive(pid):
            return
        if i < tries - 1:
            nap(interval)


def stop(claude_id: str) -> None:
    """
    Run `claude stop <id>`. The result is not checked: a row that is not
    running is fine.
    """
    _claude('stop', claude_id)


def _tmux_bin() -> str | None:
    return shutil.which(os.environ.get('CLAUDE_CODE_BG_TMUX') or 'tmux')


def _no_option(screen: str) -> int | None:
    """Return the number of the first `No` option under the last PROCEED line."""
    lines = screen.splitlines()
    marks = [i for i, line in enumerate(lines) if PROCEED in line]
    if not marks:
        return None
    for line in lines[marks[-1] + 1:]:
        match = OPTION.match(line)
        if match and match.group(2).startswith('No'):
            return int(match.group(1))
    return None


def deny(claude_id: str, guidance: str) -> bool:
    """
    Deny the permission prompt the session waits on, with `guidance` as the
    reason: attach in a short tmux session `ccbg-<id>`, wait for the prompt
    on the screen, move to the `No` option (`Down`), `Tab`, type the
    guidance, `Enter`, then detach (`C-z`) and kill that tmux session.
    Returns True when the keys were sent. When tmux is not found, the tmux
    session cannot start, or no prompt shows up, it stops the session
    instead and returns False.
    """
    tmux = _tmux_bin()
    if tmux is None:
        stop(claude_id)
        return False
    name = f'ccbg-{claude_id}'
    started = _run([tmux, 'new-session', '-d', '-s', name, '-x', '140', '-y', '45',
                    f'{shlex.quote(command())} attach {claude_id}'])
    if started.returncode != 0:
        stop(claude_id)
        return False
    target = f'={name}:'
    try:
        no = None
        for i in range(20):
            nap(0.5)
            no = _no_option(_run([tmux, 'capture-pane', '-t', target, '-p']).stdout or '')
            if no:
                break
        if not no:
            stop(claude_id)
            return False
        for key in [['Down']] * (no - 1) + [['Tab'], ['-l', guidance], ['Enter']]:
            _run([tmux, 'send-keys', '-t', target, *key])
        nap(3)
        return True
    finally:
        _run([tmux, 'send-keys', '-t', target, 'C-z'])
        _run([tmux, 'kill-session', '-t', f'={name}'])


def _ledger_dir() -> Path:
    return store.hermes_home() / 'claude-watch'


def _read_ledger(claude_id: str) -> dict:
    try:
        return json.loads((_ledger_dir() / f'{claude_id}.json').read_text())
    except FileNotFoundError:
        return {}


def write_ledger(s: Session, *, name: str, cwd: str, hermes_session: str | None,
                 state: str = 'running') -> None:
    """
    Write the chat ledger record `<hermes home>/claude-watch/<claude_id>.json`
    atomically: `mode: "chat"`, `claude_id`, `session_id`, `name`, `cwd`,
    `state` (`running` or `abandoned`), `started` (kept from an earlier
    record of the same id), `purpose` and `hermes_session`.
    """
    directory = _ledger_dir()
    directory.mkdir(parents=True, exist_ok=True)
    started = _read_ledger(s.claude_id).get('started') or \
        datetime.now(timezone.utc).isoformat(timespec='seconds')
    record = {
        'mode': 'chat',
        'claude_id': s.claude_id,
        'session_id': s.session_id or None,
        'name': name,
        'cwd': cwd,
        'state': state,
        'started': started,
        'purpose': f'Hermes chat {hermes_session or name}',
        'hermes_session': hermes_session,
    }
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'{s.claude_id}.', suffix='.tmp')
    with os.fdopen(fd, 'w') as fh:
        json.dump(record, fh, indent=2)
    os.replace(tmp, directory / f'{s.claude_id}.json')
