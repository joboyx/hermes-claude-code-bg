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
DEFAULT_FX = {
    "rows": [],
    "bg_id": "abc12345",
    "bg_session": "abc12345-0000-4000-8000-000000000001",
}


class StubEnv:
    """
    Temp directory with a stub `claude` and the dirs the plugin reads.

    Layout under `root`: `bin/claude` (runs `tests/stub_claude.py` with this
    interpreter), `hermes/` (HERMES_HOME), `projects/` (CLAUDE_PROJECTS),
    `jobs/` (CLAUDE_JOBS), `fx.json` (fixture) and `calls.jsonl` (call log).
    Use it as a context manager; exit kills every child the stub started and
    removes the directory.
    """

    def __enter__(self) -> "StubEnv":
        self.root = Path(tempfile.mkdtemp(prefix="ccbg-test-")).resolve()
        self.bin = self.root / "bin"
        self.command = str(self.bin / "claude")
        self.hermes_home = self.root / "hermes"
        self.projects = self.root / "projects"
        self.jobs = self.root / "jobs"
        self.fx_path = self.root / "fx.json"
        self.log_path = self.root / "calls.jsonl"
        for path in (self.bin, self.hermes_home, self.projects, self.jobs):
            path.mkdir()
        wrapper = Path(self.command)
        wrapper.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                           f"{shlex.quote(str(STUB))} \"$@\"\n")
        wrapper.chmod(0o755)
        self.fx_path.write_text(json.dumps(DEFAULT_FX))
        self.log_path.touch()
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
        Return the logged stub calls in order: {"argv", "cwd", "env_keys"}.
        """
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line]

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
