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

if [ ! -d "$TARGET" ]; then
  echo "❌ Hook '$HOOK_NAME' not found at $TARGET"
  exit 1
fi

rm -rf "$TARGET"
echo "✅ Removed $TARGET"
echo ""
echo "⚠️  The hook has been removed from disk, but the gateway may still hold"
echo "   the old handler in memory until it is restarted."
echo ""
echo "   hermes gateway restart      # from shell"
echo "   /restart                    # from any chat session"
