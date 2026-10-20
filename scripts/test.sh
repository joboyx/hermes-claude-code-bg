#!/bin/sh
# Run the unit tests. PY overrides the interpreter (default: the Hermes venv Python).
set -eu
PY="${PY:-${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}/venv/bin/python}"
cd "$(dirname "$0")/.."
exec "$PY" -m unittest discover -s tests -t . "$@"
