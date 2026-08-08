#!/bin/bash
set -e

HOOK_NAME="task_done_sound"
PROFILE="${1:-default}"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"

if [ "$PROFILE" != "default" ]; then
  TARGET="$HERMES_HOME/profiles/$PROFILE/hooks/$HOOK_NAME"
else
  TARGET="$HERMES_HOME/hooks/$HOOK_NAME"
fi

mkdir -p "$TARGET"

REPO_BASE="https://raw.githubusercontent.com/${REPO:-YOUR-FORK}/hermes-hook-task-done/main"

echo "Installing task-done-sound hook to $TARGET ..."

curl -fsSL "$REPO_BASE/HOOK.yaml"      -o "$TARGET/HOOK.yaml"
curl -fsSL "$REPO_BASE/handler.py"     -o "$TARGET/handler.py"
curl -fsSL "$REPO_BASE/defaults.json"  -o "$TARGET/defaults.json"
curl -fsSL "$REPO_BASE/start.wav"      -o "$TARGET/start.wav"
curl -fsSL "$REPO_BASE/success.wav"    -o "$TARGET/success.wav"
curl -fsSL "$REPO_BASE/error.wav"      -o "$TARGET/error.wav"

echo "✅ Hook installed to $TARGET"
echo ""
echo "   Profile: $PROFILE"
echo "   Events:  agent:start, agent:end"
echo ""
echo "⚠️  You must restart the Hermes gateway for the hook to take effect."
echo ""
echo "   hermes gateway restart      # from shell"
echo "   /restart                    # from any chat session"
