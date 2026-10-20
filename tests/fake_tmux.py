"""Fake `tmux` for the unit tests: logs every call and prints a fixed screen.

`tests/support.py` (`StubEnv`) writes `bin/tmux`, which runs this file with
the interpreter that runs the tests. Environment:

  FAKE_TMUX_LOG     path of the call log (one JSON line per call: {"argv": [...]})
  FAKE_TMUX_SCREEN  path of the screen `capture-pane` prints; when the file
                    does not exist it prints DEFAULT_SCREEN (a permission prompt)

Every call exits 0 and prints nothing, except `capture-pane`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_SCREEN = """\
 Bash command

   git push origin main
   Push the branch

 Do you want to proceed?
 ❯ 1. Yes
   2. No, and tell Claude what to do differently (esc)
"""


def main() -> int:
    argv = sys.argv[1:]
    with open(os.environ["FAKE_TMUX_LOG"], "a") as log:
        log.write(json.dumps({"argv": argv}) + "\n")
    if argv[:1] == ["capture-pane"]:
        screen = Path(os.environ.get("FAKE_TMUX_SCREEN") or "/nonexistent")
        print(screen.read_text() if screen.is_file() else DEFAULT_SCREEN, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
