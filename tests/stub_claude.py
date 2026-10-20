"""Stub `claude` CLI for the unit tests, driven by one fixture JSON file.

`tests/support.py` (`StubEnv`) writes `bin/claude`, which runs this file with
the interpreter that runs the tests. Environment:

  STUB_FX            path of the fixture JSON (read and rewritten on every call)
  STUB_LOG           path of the call log (one JSON line per call:
                     {"argv": [...], "cwd": str, "env_keys": [sorted names]};
                     `rm <id>` and `--resume <sid>` calls also carry
                     "prev_pid_alive": whether the pid of the last launch of
                     that id (<sid>[:8] for a resume) was still alive, or
                     null when the stub never launched that id)
  CLAUDE_PROJECTS    transcripts go to <CLAUDE_PROJECTS>/stub/<sessionId>.jsonl
  CLAUDE_JOBS        job state goes to <CLAUDE_JOBS>/<id>/state.json

Fixture keys:

  rows                 list of `claude agents --json --all` rows; the stub adds,
                       changes and removes rows as calls arrive
  bg_id                8-char id printed for a new `--bg` dispatch
  bg_session           sessionId of that new dispatch
  copy_as              on `--resume`: print the copy note and use this id ...
  copy_session         ... with this sessionId
  exit_after           after `stop <id>`: the row keeps its status for N more
                       `agents` calls, then status becomes null (absent or 0:
                       null at once)
  pid_alive_after_stop seconds the row's pid stays alive after `stop <id>`
                       (absent or 0: `stop` returns after the pid is gone)
  append_transcript    list of JSONL entries appended to the transcript on each
                       `--bg` launch; "{{prompt}}" in any string becomes the prompt
  fail_resume          on `--resume`: print this text to stderr and exit 1
  fail_agents          on `agents`: print this text to stderr and exit 1
  fail_dispatch        on a `--bg` launch without `--resume`: print this text
                       to stderr and exit 1
  jobs_state           JSON written to <CLAUDE_JOBS>/<id>/state.json on each
                       `--bg` launch
  usage_body           on `-p`: the inner `response` object of the control
                       response (absent: null)
  usage_subtype        on `-p`: the control response subtype (default success)
  usage_raw            on `-p`: print this text instead of a control response
  usage_delay          on `-p`: seconds to wait before answering

Internal keys (written by the stub, read by `StubEnv`): `_pids` (every child
pid started, for cleanup), `_exiting` ({id: agents calls left}; cleared for
an id on `rm` and on a new launch of that id), `_pid_of` ({id: pid of its
last launch}) and `_requests` (every stdin line a `-p` call read, parsed).

Commands:

  agents ...                   print `rows` as JSON
  [--resume <sid>] --bg ...    print `backgrounded · <id> · <name>` (after the
                               copy note, if any); add or replace the row with
                               state `done`, status `idle` and the pid of a new
                               detached `sleep`; append the transcript entries.
                               A resume without `copy_as` uses <sid>[:8] as id.
  stop <id>                    row state `stopped`; status and pid per
                               `exit_after` / `pid_alive_after_stop`
  rm <id>                      remove the row
  -p ...                       read one control request from stdin and answer
                               it (stream-json) per the `usage_*` keys
  anything else                exit 1
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CHILD_SECONDS = "600"  # upper bound on a leaked child's life


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill_now(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    end = time.monotonic() + 2
    while pid_alive(pid) and time.monotonic() < end:  # wait for init to reap it
        time.sleep(0.01)


def detached(argv: list[str]) -> int:
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    return proc.pid


def arg_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else ""


def append_transcript(fx: dict, session_id: str, prompt: str) -> None:
    entries = fx.get("append_transcript") or []
    if not entries:
        return
    path = Path(os.environ["CLAUDE_PROJECTS"]) / "stub" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = json.dumps(prompt)[1:-1]
    with path.open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry).replace("{{prompt}}", quoted) + "\n")


def launch(fx: dict, argv: list[str]) -> int:
    name = arg_after(argv, "--name")
    prompt = argv[argv.index("--") + 1] if "--" in argv[:-1] else ""
    resume = arg_after(argv, "--resume")
    if resume:
        if fx.get("fail_resume"):
            print(fx["fail_resume"], file=sys.stderr)
            return 1
        cid, sid = resume[:8], resume
        if fx.get("copy_as"):
            print(f"note: {cid} is still running; started a copy as {fx['copy_as']}")
            cid, sid = fx["copy_as"], fx["copy_session"]
    else:
        if fx.get("fail_dispatch"):
            print(fx["fail_dispatch"], file=sys.stderr)
            return 1
        cid, sid = fx["bg_id"], fx["bg_session"]
    pid = detached(["sleep", CHILD_SECONDS])
    fx.setdefault("_pids", []).append(pid)
    fx.setdefault("_pid_of", {})[cid] = pid
    fx.setdefault("_exiting", {}).pop(cid, None)
    fx["rows"] = [r for r in fx["rows"] if r.get("id") != cid] + [{
        "id": cid, "sessionId": sid, "name": name, "kind": "background",
        "state": "done", "status": "idle", "pid": pid, "cwd": os.getcwd()}]
    append_transcript(fx, sid, prompt)
    if "jobs_state" in fx:
        state = Path(os.environ["CLAUDE_JOBS"]) / cid / "state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps(fx["jobs_state"]))
    print(f"backgrounded · {cid} · {name}")
    print(f"  claude attach {cid}    open in this terminal")
    return 0


def agents(fx: dict) -> int:
    if fx.get("fail_agents"):
        print(fx["fail_agents"], file=sys.stderr)
        return 1
    exiting = fx.setdefault("_exiting", {})
    for cid, left in list(exiting.items()):
        if left > 0:
            exiting[cid] = left - 1
            continue
        del exiting[cid]
        for row in fx["rows"]:
            if row.get("id") == cid:
                row["status"] = None
    print(json.dumps(fx["rows"]))
    return 0


def stop(fx: dict, cid: str) -> int:
    for row in fx["rows"]:
        if row.get("id") != cid:
            continue
        row["state"] = "stopped"
        if fx.get("exit_after"):
            fx.setdefault("_exiting", {})[cid] = fx["exit_after"]
        else:
            row["status"] = None
        pid = row.get("pid")
        if type(pid) is int and pid > 0:
            delay = fx.get("pid_alive_after_stop") or 0
            if delay:
                detached(["/bin/sh", "-c", f"sleep {float(delay)}; kill -9 {pid}"])
            else:
                kill_now(pid)
    print(f"stopped {cid}")
    return 0


def rm(fx: dict, cid: str) -> int:
    fx["rows"] = [r for r in fx["rows"] if r.get("id") != cid]
    fx.setdefault("_exiting", {}).pop(cid, None)
    print(f"removed {cid}")
    return 0


def print_mode(fx: dict) -> int:
    line = sys.stdin.readline()
    request = json.loads(line) if line.strip() else {}
    fx.setdefault("_requests", []).append(request)
    if fx.get("usage_delay"):
        time.sleep(float(fx["usage_delay"]))
    print(json.dumps({"type": "system", "subtype": "init"}))
    if "usage_raw" in fx:
        print(fx["usage_raw"])
        return 0
    print(json.dumps({"type": "control_response", "response": {
        "subtype": fx.get("usage_subtype") or "success",
        "request_id": request.get("request_id"), "response": fx.get("usage_body")}}))
    return 0


def run(fx: dict, argv: list[str]) -> int:
    fx.setdefault("rows", [])
    if argv[:1] == ["agents"]:
        return agents(fx)
    if argv[:1] == ["stop"] and len(argv) > 1:
        return stop(fx, argv[1])
    if argv[:1] == ["rm"] and len(argv) > 1:
        return rm(fx, argv[1])
    if "--bg" in argv:
        return launch(fx, argv)
    if "-p" in argv:
        return print_mode(fx)
    print(f"stub claude: unsupported call {argv}", file=sys.stderr)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    fx_path = Path(os.environ["STUB_FX"])
    with open(f"{fx_path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        fx = json.loads(fx_path.read_text())
        record = {"argv": argv, "cwd": os.getcwd(), "env_keys": sorted(os.environ)}
        target = (argv[1] if argv[:1] == ["rm"] and len(argv) > 1
                  else arg_after(argv, "--resume")[:8])
        if target:
            pid = (fx.get("_pid_of") or {}).get(target)
            record["prev_pid_alive"] = pid_alive(pid) if pid else None
        with open(os.environ["STUB_LOG"], "a") as log:
            log.write(json.dumps(record) + "\n")
        code = run(fx, argv)
        tmp = fx_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(fx))
        tmp.replace(fx_path)
    return code


if __name__ == "__main__":
    sys.exit(main())
