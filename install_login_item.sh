#!/bin/bash
# install_login_item.sh — Registers FocusTracker as a macOS Login Item via LaunchAgent.
# Idempotent: safe to run multiple times.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.focustracker.menubar.plist"
START_SH="$SCRIPT_DIR/start.sh"
LOG_DIR="$HOME/.focustracker/logs"

# Ensure directories exist
mkdir -p "$PLIST_DIR"
mkdir -p "$LOG_DIR"

# Make start.sh executable
chmod +x "$START_SH"

# Write the plist
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.focustracker.menubar</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${START_SH}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <false/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchagent.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchagent-error.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
EOF

echo "Plist written to: $PLIST_PATH"

# Unload first (ignore error if not loaded)
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# Load and enable
launchctl load -w "$PLIST_PATH"

echo ""
echo "Auto-Start aktiviert."
echo "Test mit Logout/Login oder:"
echo "  launchctl start com.focustracker.menubar"
echo ""
echo "Logs: $LOG_DIR/"
