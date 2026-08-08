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

REPO_BASE="https://raw.githubusercontent.com/${REPO:-jeremygan2021}/hermes-hook-task-done-sound/main"

echo "Installing task-done-sound hook to $TARGET ..."
echo "  (REPO_BASE=$REPO_BASE)"

# Fetch each file. If the GitHub fetch fails AND we're being run from inside
# a checkout of the repo, fall back to the local copy so offline installs work.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fetch() {
    local name="$1"
    if curl -fsSL --max-time 10 "$REPO_BASE/$name" -o "$TARGET/$name" 2>/dev/null; then
        return 0
    fi
    if [ -f "$SCRIPT_DIR/$name" ]; then
        echo "  ⚠️  GitHub fetch failed for $name — using local copy"
        cp "$SCRIPT_DIR/$name" "$TARGET/$name"
        return 0
    fi
    echo "  ❌ Could not obtain $name (no network, no local copy)"
    return 1
}

fetch HOOK.yaml
fetch handler.py
fetch defaults.json
fetch start.wav
fetch success.wav
fetch error.wav

echo "✅ Hook installed to $TARGET"
echo ""
echo "   Profile: $PROFILE"
echo "   Events:  agent:start, agent:end"
echo ""
echo "⚠️  You must restart the Hermes gateway for the hook to take effect."
echo ""
echo "   hermes gateway restart      # from shell"
echo "   /restart                    # from any chat session"
