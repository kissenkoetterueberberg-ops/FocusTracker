#!/bin/bash
# FocusTracker Wrapper — startet Flask (Hintergrund) + Menubar (Vordergrund).
# Beim Beenden der Menubar wird Flask automatisch mitgestoppt (trap).

set -e

export FOCUSTRACKER_DATA_DIR="${FOCUSTRACKER_DATA_DIR:-/Users/Shared/FocusTracker}"
export FOCUSTRACKER_PORT="${FOCUSTRACKER_PORT:-5050}"
export FOCUSTRACKER_DND_SHORTCUT="${FOCUSTRACKER_DND_SHORTCUT:-Nicht Stoeren}"
# Activation-Pack Env-Vars (Defaults, überschreibbar):
export FOCUSTRACKER_HOTKEY="${FOCUSTRACKER_HOTKEY:-<alt>+<cmd>+f}"
export FOCUSTRACKER_NUDGE_MORNING="${FOCUSTRACKER_NUDGE_MORNING:-09:00}"
export FOCUSTRACKER_NUDGE_EVENING="${FOCUSTRACKER_NUDGE_EVENING:-20:00}"
export FOCUSTRACKER_NUDGE_WEEKDAYS_ONLY="${FOCUSTRACKER_NUDGE_WEEKDAYS_ONLY:-1}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$HOME/.focustracker-venv"
LOG_DIR="$HOME/.focustracker/logs"
mkdir -p "$LOG_DIR"

# Setup venv if missing
if [ ! -d "$VENV" ]; then
  echo "Setting up venv at $VENV ..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

# Check if Flask is already running on the configured port
_flask_running() {
  lsof -iTCP:"$FOCUSTRACKER_PORT" -sTCP:LISTEN -t >/dev/null 2>&1
}

FLASK_PID=""

# Only start Flask if not already running
if _flask_running; then
  echo "Flask already running on port $FOCUSTRACKER_PORT — skipping backend start"
else
  echo "Starting Flask backend on port $FOCUSTRACKER_PORT ..."
  "$VENV/bin/python" app.py >> "$LOG_DIR/app.log" 2>&1 &
  FLASK_PID=$!
  echo "Flask PID: $FLASK_PID"

  # Wait up to 5s for backend to be ready
  for i in 1 2 3 4 5; do
    sleep 1
    if _flask_running; then
      echo "Backend ready."
      break
    fi
    if [ "$i" -eq 5 ]; then
      echo "Warning: backend may not be ready yet — starting menubar anyway"
    fi
  done
fi

# Cleanup: kill Flask when menubar exits (only if we started it)
_cleanup() {
  if [ -n "$FLASK_PID" ]; then
    echo "Stopping Flask (PID $FLASK_PID)..."
    kill "$FLASK_PID" 2>/dev/null || true
  fi
}
trap _cleanup EXIT INT TERM

echo "Starting Menubar..."
"$VENV/bin/python" menubar.py >> "$LOG_DIR/menubar.log" 2>&1
