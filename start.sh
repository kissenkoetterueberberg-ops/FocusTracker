#!/bin/bash
# FocusTracker Wrapper — startet die App aus dem Shared-Code-Ordner.
# Pro User wird eine eigene venv unter ~/.focustracker-venv angelegt.
# DB liegt geteilt unter /Users/Shared/FocusTracker.

set -e

export FOCUSTRACKER_DATA_DIR="${FOCUSTRACKER_DATA_DIR:-/Users/Shared/FocusTracker}"
# Optional DND — wenn du keinen Shortcut hast, einfach die naechste Zeile auskommentieren:
export FOCUSTRACKER_DND_SHORTCUT="${FOCUSTRACKER_DND_SHORTCUT:-Nicht Stoeren}"

cd "$(dirname "$0")"

VENV="$HOME/.focustracker-venv"
if [ ! -d "$VENV" ]; then
  echo "Setting up venv at $VENV ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

exec "$VENV/bin/python" app.py
