#!/bin/bash
# Install hermes-watch as a systemd user service.
#
# Idempotent: re-running overwrites the unit file but preserves enabled state.
#
# Requirements: ~/.local/bin/hermes-watch and ~/.local/share/hermes-task-sound/
#               must already exist (copy them from this repo first).

set -e

HOME_DIR="${HOME:-/home/kali}"
EXEC_PATH="$HOME_DIR/.local/bin/hermes-watch"
SOUND_DIR="$HOME_DIR/.local/share/hermes-task-sound"
LIB_PATH="$HOME_DIR/lib/hermes-watch"

if [ ! -x "$EXEC_PATH" ]; then
    echo "❌ $EXEC_PATH not found or not executable."
    echo "   Copy it first: cp hermes-watch $EXEC_PATH && chmod +x $EXEC_PATH"
    exit 1
fi

for w in start.wav success.wav error.wav; do
    [ -f "$SOUND_DIR/$w" ] || {
        echo "❌ missing $SOUND_DIR/$w"
        echo "   Copy WAVs: cp {start,success,error}.wav $SOUND_DIR/"
        exit 1
    }
done

mkdir -p "$LIB_PATH"
cp "$(dirname "$0")/hermes_watch.py" "$LIB_PATH/hermes_watch.py"

mkdir -p "$HOME_DIR/.config/systemd/user"
UNIT="$HOME_DIR/.config/systemd/user/hermes-watch.service"
cat > "$UNIT" <<EOF
[Unit]
Description=Hermes process watcher — plays audio cues when local Hermes CLI sessions go idle/busy
Documentation=https://github.com/jeremygan2021/hermes-hook-task-done-sound
After=default.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u -c "import sys; sys.path.insert(0, '$LIB_PATH'); import hermes_watch; hermes_watch.run(__import__('threading').Event())"
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hermes-watch

echo "✅ Installed and started."
echo "   Status:  systemctl --user status hermes-watch"
echo "   Logs:    journalctl --user -u hermes-watch -f"
