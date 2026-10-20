#!/bin/sh
# Link the plugin into a Hermes home: <home>/plugins/claude-code-bg -> <repo>/claude_code_bg.
# Usage: scripts/install.sh [HERMES_HOME]   (default: $HERMES_HOME, else ~/.hermes)
# Idempotent: an existing link to this checkout is kept, a link elsewhere is replaced,
# and a real directory at that path is never touched.
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
TARGET_HOME=${1:-${HERMES_HOME:-$HOME/.hermes}}
SRC="$REPO/claude_code_bg"
DEST="$TARGET_HOME/plugins/claude-code-bg"

mkdir -p "$TARGET_HOME/plugins"
if [ -L "$DEST" ]; then
  OLD=$(readlink "$DEST")
  if [ "$OLD" = "$SRC" ]; then
    echo "already linked: $DEST -> $SRC"
    exit 0
  fi
  rm "$DEST"
  echo "removed old link: $DEST -> $OLD"
elif [ -e "$DEST" ]; then
  echo "error: $DEST exists and is not a link; move it away first" >&2
  exit 1
fi
ln -s "$SRC" "$DEST"
echo "linked: $DEST -> $SRC"
echo "next: restart Hermes, then select the provider (see docs/configuration.md)"
