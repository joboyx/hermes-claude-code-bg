"""Child process for the FileLock test: take the lock, record when.

Usage: python -m tests.lock_child <conv> <out-file>
Writes `started <time>` before it asks for the lock and `acquired <time>`
once it holds it. HERMES_HOME comes from the environment.
"""
from __future__ import annotations

import sys
import time

from claude_code_bg import store


def main(conv: str, out: str) -> None:
    with open(out, "a") as fh:
        fh.write(f"started {time.time()}\n")
    with store.FileLock(conv):
        with open(out, "a") as fh:
            fh.write(f"acquired {time.time()}\n")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
