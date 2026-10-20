"""Test helpers: a temp home with the stub `claude` and its fixture."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Iterator

STUB = Path(__file__).resolve().parent / "stub_claude.py"
FAKE_TMUX = Path(__file__).resolve().parent / "fake_tmux.py"
DEFAULT_FX = {
    "rows": [],
    "bg_id": "abc12345",
    "bg_session": "abc12345-0000-4000-8000-000000000001",
}


def set_env(test, **values: str | None) -> None:
    """
    Set (str) or unset (None) environment variables for one test and
    restore the previous values when the test ends (via addCleanup).
    """
    for name, value in values.items():
        test.addCleanup(_restore_env, name, os.environ.get(name))
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _restore_env(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


class StubEnv:
    """
    Temp directory with a stub `claude` and the dirs the plugin reads.

    Layout under `root`: `bin/claude` (runs `tests/stub_claude.py` with this
    interpreter), `hermes/` (HERMES_HOME), `projects/` (CLAUDE_PROJECTS),
    `jobs/` (CLAUDE_JOBS), `fx.json` (fixture) and `calls.jsonl` (call log),
    plus `bin/tmux` (runs `tests/fake_tmux.py`; CLAUDE_CODE_BG_TMUX),
    `tmux_calls.jsonl` (its call log) and `tmux_screen.txt` (the screen it
    prints, when a test writes that file).
    Use it as a context manager; exit kills every child the stub started and
    removes the directory.
    """

    def __enter__(self) -> "StubEnv":
        self.root = Path(tempfile.mkdtemp(prefix="ccbg-test-")).resolve()
        self.bin = self.root / "bin"
        self.command = str(self.bin / "claude")
        self.tmux = str(self.bin / "tmux")
        self.hermes_home = self.root / "hermes"
        self.projects = self.root / "projects"
        self.jobs = self.root / "jobs"
        self.fx_path = self.root / "fx.json"
        self.log_path = self.root / "calls.jsonl"
        self.tmux_log_path = self.root / "tmux_calls.jsonl"
        self.tmux_screen = self.root / "tmux_screen.txt"
        for path in (self.bin, self.hermes_home, self.projects, self.jobs):
            path.mkdir()
        for wrapper, script in ((Path(self.command), STUB), (Path(self.tmux), FAKE_TMUX)):
            wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                               f"{shlex.quote(str(script))} \"$@\"\n")
            wrapper.chmod(0o755)
        self.fx_path.write_text(json.dumps(DEFAULT_FX))
        self.log_path.touch()
        self.tmux_log_path.touch()
        return self

    def __exit__(self, *exc) -> None:
        if not self.root.exists():
            return
        for pid in self.fx().get("_pids", []):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(self.root, ignore_errors=True)

    def env(self) -> dict[str, str]:
        """
        Return os.environ plus the variables that point the plugin and the
        stub at this directory.
        """
        env = dict(os.environ)
        env.update({
            "HERMES_HOME": str(self.hermes_home),
            "CLAUDE_PROJECTS": str(self.projects),
            "CLAUDE_JOBS": str(self.jobs),
            "CLAUDE_CODE_BG_COMMAND": self.command,
            "STUB_FX": str(self.fx_path),
            "STUB_LOG": str(self.log_path),
            "CLAUDE_CODE_BG_TMUX": self.tmux,
            "FAKE_TMUX_LOG": str(self.tmux_log_path),
            "FAKE_TMUX_SCREEN": str(self.tmux_screen),
        })
        return env

    def fx(self, **update) -> dict:
        """
        Merge `update` into the fixture (top-level keys) and return the
        whole fixture. With no arguments it only reads it.
        """
        with open(f"{self.fx_path}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fx = json.loads(self.fx_path.read_text())
            if update:
                fx.update(update)
                self.fx_path.write_text(json.dumps(fx))
        return fx

    def calls(self) -> list[dict]:
        """
        Return the logged stub calls in order: {"argv", "cwd", "env_keys"},
        plus "prev_pid_alive" on `rm` and `--resume` calls.
        """
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line]

    def tmux_calls(self) -> list[list[str]]:
        """
        Return the argv of every logged fake `tmux` call, in order.
        """
        lines = self.tmux_log_path.read_text().splitlines()
        return [json.loads(line)["argv"] for line in lines if line]

    @contextlib.contextmanager
    def apply_env(self) -> Iterator[None]:
        """
        Set `env()` on os.environ for in-process tests; restore it on exit.
        """
        saved = dict(os.environ)
        os.environ.update(self.env())
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(saved)
