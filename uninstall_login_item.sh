#!/bin/bash
# uninstall_login_item.sh — Removes FocusTracker Login Item.
# Idempotent: safe to run multiple times.

set -e

PLIST_PATH="$HOME/Library/LaunchAgents/com.focustracker.menubar.plist"

if [ ! -f "$PLIST_PATH" ]; then
  echo "Plist not found at $PLIST_PATH — nothing to remove."
  exit 0
fi

# Stop if running
launchctl stop com.focustracker.menubar 2>/dev/null || true

# Unload
launchctl unload -w "$PLIST_PATH" 2>/dev/null || true

# Remove plist
rm -f "$PLIST_PATH"

echo "Auto-Start deaktiviert. Plist entfernt: $PLIST_PATH"
