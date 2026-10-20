#!/bin/sh
# Live smoke suite: real `claude`, claude-haiku-5-5, effort low, isolated Hermes home in .smoke/.
#
# Usage: SMOKE_CWD=<dir> scripts/smoke/run.sh [case ...]
#   SMOKE_CWD                    required: a directory Claude Code already trusts (run `claude`
#                                there once and accept the trust prompt); conversations run there
#   HERMES_AGENT_REPO            Hermes checkout (default: ~/.hermes/hermes-agent); PY overrides
#                                its venv Python
#   SMOKE_OTHER_PROVIDER_PLUGIN  optional: a second model-provider plugin dir; it is linked into
#                                the isolated home only, takes the auxiliary tasks and is chat A's
#                                provider in the /model case. Unset -> the auxiliary case is SKIP
#   SMOKE_OTHER_MODEL            model for that provider (default: haiku)
#   SMOKE_PORT, SMOKE_MAX_LIVE   see smoke.py
#
# The home is rebuilt on every run from nothing: no file of another Hermes home is copied.
# Every `smoke-` row of `claude agents` is removed at the end, also after a failure.
set -eu
: "${SMOKE_CWD:?set SMOKE_CWD to a directory Claude Code trusts}"
[ -d "$SMOKE_CWD" ] || { echo "error: SMOKE_CWD=$SMOKE_CWD is not a directory" >&2; exit 2; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
HERMES_AGENT_REPO=${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}
PY=${PY:-$HERMES_AGENT_REPO/venv/bin/python}
SMOKE_DIR="$REPO/.smoke"
HERMES_HOME="$SMOKE_DIR/hermes-home"
export SMOKE_CWD HERMES_AGENT_REPO HERMES_HOME

rm -rf "${SMOKE_DIR:?}/hermes-home" "$SMOKE_DIR/outbox.jsonl" "$SMOKE_DIR/locks"
mkdir -p "$HERMES_HOME/plugins/model-providers"
"$REPO/scripts/install.sh" "$HERMES_HOME"
ln -s "$REPO/scripts/smoke/local_platform" "$HERMES_HOME/plugins/smoke-local"
echo "linked: $HERMES_HOME/plugins/smoke-local -> $REPO/scripts/smoke/local_platform"

if [ -n "${SMOKE_OTHER_PROVIDER_PLUGIN:-}" ]; then
  OTHER_DIR=$(cd "$SMOKE_OTHER_PROVIDER_PLUGIN" && pwd)
  SMOKE_OTHER_PROVIDER=$(sed -n 's/^name:[[:space:]]*//p' "$OTHER_DIR/plugin.yaml" | head -n 1)
  ln -s "$OTHER_DIR" "$HERMES_HOME/plugins/model-providers/$(basename "$OTHER_DIR")"
  echo "linked: second provider $SMOKE_OTHER_PROVIDER from $OTHER_DIR"
  export SMOKE_OTHER_PROVIDER
fi

cleanup() {
  env -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION "$PY" "$REPO/scripts/smoke/smoke.py" cleanup || true
}
trap cleanup EXIT
env -u CLAUDECODE -u CLAUDE_CODE_CHILD_SESSION -u CLAUDE_CODE_ENTRYPOINT \
  -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN "$PY" "$REPO/scripts/smoke/smoke.py" "$@"
