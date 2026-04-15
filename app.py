#!/usr/bin/env python3
"""FocusTracker — Work time tracker with idle detection, pomodoro, streaks & export."""

import csv
import io
import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["TEMPLATES_AUTO_RELOAD"] = True

# DB location — override mit FOCUSTRACKER_DATA_DIR, sonst ~/.focustracker
DATA_DIR = Path(os.environ.get("FOCUSTRACKER_DATA_DIR") or (Path.home() / ".focustracker")).expanduser()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "focus.db"

# --- Config (persisted in DB) ---
DEFAULT_CONFIG = {
    "break_threshold": 120,       # 2 min idle = auto-pause
    "idle_threshold": 180,        # 3 min idle = first notification
    "notification_cooldown": 120, # 2 min between reminders
    "daily_goal": 21600,          # 6h = 21600 sec
    "pomodoro_focus": 1500,       # 25 min
    "pomodoro_break": 300,        # 5 min
}

config = dict(DEFAULT_CONFIG)
IDLE_CHECK_INTERVAL = 10
FOCUS_MIN_SEGMENT = 600  # Arbeits-Stretches >= 10 min gelten als fokussiert

# --- State ---
tracker_state = {
    "active": False,
    "session_start": None,
    "idle_since": None,
    "last_notification": 0,
    "paused_by_idle": False,
    "on_break": False,
    "break_start": None,
    # Pomodoro
    "pomodoro_enabled": False,
    "pomodoro_phase": None,        # "focus" or "break"
    "pomodoro_phase_start": None,
    "pomodoro_count": 0,
    "pomodoro_notified": False,
    "active_project_id": None,
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_sec INTEGER DEFAULT 0,
            idle_sec INTEGER DEFAULT 0,
            note TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS idle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            duration_sec INTEGER NOT NULL,
            session_id INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS breaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_time TEXT NOT NULL,
            end_time TEXT,
            duration_sec INTEGER DEFAULT 0,
            session_id INTEGER,
            auto BOOLEAN DEFAULT 1,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            color TEXT DEFAULT '#6c5ce7',
            goal_hours INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            archived INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            title TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            period TEXT NOT NULL,
            target_sec INTEGER NOT NULL,
            project_id INTEGER,
            start_date TEXT,
            end_date TEXT,
            created_at TEXT NOT NULL,
            archived INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        )
    """)
    # Add project_id column to sessions if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "project_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id)")

    # Seed default goals if empty
    gcount = conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
    if gcount == 0:
        now = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO goals (name, period, target_sec, project_id, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("Wochenziel", "weekly", 30 * 3600, None, now),
                ("Monatsziel", "monthly", 120 * 3600, None, now),
            ]
        )

    # Load saved config
    for row in conn.execute("SELECT key, value FROM config").fetchall():
        if row[0] in config:
            config[row[0]] = int(row[1])
    conn.commit()
    conn.close()


def save_config_to_db():
    conn = sqlite3.connect(DB_PATH)
    for k, v in config.items():
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()


def get_idle_time_sec():
    try:
        result = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            if "HIDIdleTime" in line and "=" in line:
                idle_ns = int(line.split("=")[-1].strip())
                return idle_ns / 1_000_000_000
    except Exception:
        pass
    return 0.0


def set_dnd(enabled):
    """Toggle macOS Do Not Disturb via Shortcuts app.

    Aktiviert nur wenn FOCUSTRACKER_DND_SHORTCUT gesetzt ist (Name eines
    macOS-Shortcuts, der DND togglet). Sonst No-op.
    """
    shortcut = os.environ.get("FOCUSTRACKER_DND_SHORTCUT")
    if not shortcut:
        return
    try:
        subprocess.run(["shortcuts", "run", shortcut], timeout=5, capture_output=True)
    except Exception:
        pass


def send_notification(title, message, sound="Funk"):
    try:
        subprocess.run([
            "osascript", "-e",
            f'display notification "{message}" with title "{title}" sound name "{sound}"'
        ], timeout=5)
    except Exception:
        pass


def _get_active_session_id(conn):
    row = conn.execute(
        "SELECT id FROM sessions WHERE end_time IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _get_today_work_sec():
    """Calculate today's net work seconds for goal tracking."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
        (f"{today}%",)
    ).fetchone()
    total = row[0] if row else 0
    # Add current active session
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if start.strftime("%Y-%m-%d") == today:
            total += int((datetime.now() - start).total_seconds())
    conn.close()
    return total


def idle_monitor():
    """Background thread: idle detection, auto-breaks, pomodoro, escalating reminders."""
    goal_notified = False

    while True:
        time.sleep(IDLE_CHECK_INTERVAL)

        # --- Daily goal check ---
        if tracker_state["active"] and not goal_notified:
            work = _get_today_work_sec()
            if work >= config["daily_goal"]:
                hours = config["daily_goal"] // 3600
                send_notification(
                    "Tagesziel erreicht!",
                    f"Du hast heute {hours}h produktiv gearbeitet. Stark!",
                    "Glass"
                )
                goal_notified = True

        # Reset goal notification at midnight
        if datetime.now().hour == 0 and datetime.now().minute == 0:
            goal_notified = False

        if not tracker_state["active"]:
            continue

        idle_sec = get_idle_time_sec()
        now = time.time()

        # --- Pomodoro phase transitions ---
        if tracker_state["pomodoro_enabled"] and tracker_state["pomodoro_phase_start"]:
            phase_start = datetime.fromisoformat(tracker_state["pomodoro_phase_start"])
            phase_elapsed = int((datetime.now() - phase_start).total_seconds())

            if tracker_state["pomodoro_phase"] == "focus":
                if phase_elapsed >= config["pomodoro_focus"] and not tracker_state["pomodoro_notified"]:
                    tracker_state["pomodoro_count"] += 1
                    tracker_state["pomodoro_notified"] = True
                    pomo_break = config["pomodoro_break"] // 60
                    send_notification(
                        f"Fokus-Block #{tracker_state['pomodoro_count']} fertig!",
                        f"Zeit fuer {pomo_break} Min Pause. Steh auf, beweg dich!",
                        "Hero"
                    )
                    # Auto-transition to break phase
                    tracker_state["pomodoro_phase"] = "break"
                    tracker_state["pomodoro_phase_start"] = datetime.now().isoformat()
                    tracker_state["pomodoro_notified"] = False

            elif tracker_state["pomodoro_phase"] == "break":
                if phase_elapsed >= config["pomodoro_break"] and not tracker_state["pomodoro_notified"]:
                    tracker_state["pomodoro_notified"] = True
                    send_notification(
                        "Pause vorbei!",
                        "Naechster Fokus-Block startet. Los geht's!",
                        "Ping"
                    )
                    tracker_state["pomodoro_phase"] = "focus"
                    tracker_state["pomodoro_phase_start"] = datetime.now().isoformat()
                    tracker_state["pomodoro_notified"] = False

        # --- Auto-Break: trigger at break_threshold ---
        if idle_sec >= config["break_threshold"] and not tracker_state["on_break"]:
            tracker_state["on_break"] = True
            tracker_state["paused_by_idle"] = True
            break_start = datetime.now() - timedelta(seconds=idle_sec)
            tracker_state["break_start"] = break_start.isoformat()
            tracker_state["idle_since"] = break_start.isoformat()

            conn = sqlite3.connect(DB_PATH)
            session_id = _get_active_session_id(conn)
            conn.execute(
                "INSERT INTO breaks (start_time, session_id, auto) VALUES (?, ?, 1)",
                (break_start.isoformat(), session_id)
            )
            conn.commit()
            conn.close()

            send_notification(
                "Pause gestartet",
                "Du bist seit 2 Min inaktiv — Timer ist pausiert."
            )
            tracker_state["last_notification"] = now

        # --- Escalating reminders during break ---
        if tracker_state["on_break"] and tracker_state["break_start"]:
            break_start = datetime.fromisoformat(tracker_state["break_start"])
            break_dur = int((datetime.now() - break_start).total_seconds())

            if now - tracker_state["last_notification"] > config["notification_cooldown"]:
                minutes = break_dur // 60

                if break_dur >= 600:  # 10+ min
                    send_notification(
                        "Ist alles okay?",
                        f"Du bist seit {minutes} Min weg. Das war keine kurze Pause mehr.",
                        "Sosumi"
                    )
                elif break_dur >= 300:  # 5+ min — aggressive
                    send_notification(
                        "Du scrollst schon wieder...",
                        f"Seit {minutes} Min inaktiv. Handy weg, zurueck an die Arbeit!",
                        "Basso"
                    )
                elif idle_sec >= config["idle_threshold"]:
                    send_notification(
                        "Hey, bist du noch da?",
                        f"Pause laeuft seit {minutes} Min — zurueck an die Arbeit!"
                    )
                else:
                    continue  # don't update last_notification
                tracker_state["last_notification"] = now

        # --- User came back ---
        if idle_sec < 30 and tracker_state["on_break"]:
            break_start = datetime.fromisoformat(tracker_state["break_start"])
            break_duration = int((datetime.now() - break_start).total_seconds())

            conn = sqlite3.connect(DB_PATH)
            session_id = _get_active_session_id(conn)

            conn.execute(
                "UPDATE breaks SET end_time = ?, duration_sec = ? WHERE end_time IS NULL AND session_id = ?",
                (datetime.now().isoformat(), break_duration, session_id)
            )
            conn.execute(
                "INSERT INTO idle_events (timestamp, duration_sec, session_id) VALUES (?, ?, ?)",
                (tracker_state["break_start"], break_duration, session_id)
            )
            if session_id:
                conn.execute(
                    "UPDATE sessions SET idle_sec = idle_sec + ? WHERE id = ?",
                    (break_duration, session_id)
                )
            conn.commit()
            conn.close()

            tracker_state["on_break"] = False
            tracker_state["paused_by_idle"] = False
            tracker_state["break_start"] = None
            tracker_state["idle_since"] = None

            send_notification(
                "Willkommen zurueck!",
                f"Pause war {break_duration // 60} Min {break_duration % 60} Sek. Weiter geht's!"
            )


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_session():
    if tracker_state["active"]:
        return jsonify({"error": "Already tracking"}), 400

    now = datetime.now()
    project_id = None
    if request.json and request.json.get("project_id") is not None:
        try:
            project_id = int(request.json["project_id"])
        except (TypeError, ValueError):
            project_id = None

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO sessions (start_time, project_id) VALUES (?, ?)",
        (now.isoformat(), project_id)
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()

    tracker_state["active"] = True
    tracker_state["session_start"] = now.isoformat()
    tracker_state["paused_by_idle"] = False
    tracker_state["idle_since"] = None
    tracker_state["on_break"] = False
    tracker_state["break_start"] = None
    tracker_state["active_project_id"] = project_id

    # Enable DND
    set_dnd(True)

    # Start pomodoro if enabled
    if tracker_state["pomodoro_enabled"]:
        tracker_state["pomodoro_phase"] = "focus"
        tracker_state["pomodoro_phase_start"] = now.isoformat()
        tracker_state["pomodoro_notified"] = False

    return jsonify({"session_id": session_id, "start_time": now.isoformat()})


@app.route("/api/stop", methods=["POST"])
def stop_session():
    if not tracker_state["active"]:
        return jsonify({"error": "Not tracking"}), 400

    now = datetime.now()
    start = datetime.fromisoformat(tracker_state["session_start"])
    duration = int((now - start).total_seconds())
    note = request.json.get("note", "") if request.json else ""

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE sessions SET end_time = ?, duration_sec = ?, note = ? WHERE end_time IS NULL",
        (now.isoformat(), duration, note)
    )
    # Close any open breaks
    conn.execute(
        "UPDATE breaks SET end_time = ?, duration_sec = 0 WHERE end_time IS NULL",
        (now.isoformat(),)
    )
    conn.commit()
    conn.close()

    tracker_state["active"] = False
    tracker_state["session_start"] = None
    tracker_state["paused_by_idle"] = False
    tracker_state["on_break"] = False
    tracker_state["pomodoro_phase"] = None
    tracker_state["pomodoro_phase_start"] = None
    tracker_state["active_project_id"] = None

    # Disable DND
    set_dnd(False)

    return jsonify({"duration_sec": duration})


@app.route("/api/pause", methods=["POST"])
def toggle_pause():
    """Manual pause/resume — user-initiated, independent of auto-break."""
    if not tracker_state["active"]:
        return jsonify({"error": "Not tracking"}), 400

    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    session_id = _get_active_session_id(conn)

    if tracker_state["on_break"]:
        # Resume: close current break, add duration to idle_sec
        if tracker_state["break_start"]:
            bs = datetime.fromisoformat(tracker_state["break_start"])
            dur = int((now - bs).total_seconds())
            conn.execute(
                "UPDATE breaks SET end_time = ?, duration_sec = ? WHERE end_time IS NULL AND session_id = ?",
                (now.isoformat(), dur, session_id)
            )
            if session_id:
                conn.execute(
                    "UPDATE sessions SET idle_sec = idle_sec + ? WHERE id = ?",
                    (dur, session_id)
                )
            conn.commit()
        tracker_state["on_break"] = False
        tracker_state["paused_by_idle"] = False
        tracker_state["break_start"] = None
        tracker_state["idle_since"] = None
        conn.close()
        return jsonify({"paused": False})
    else:
        # Start manual pause
        conn.execute(
            "INSERT INTO breaks (start_time, session_id, auto) VALUES (?, ?, 0)",
            (now.isoformat(), session_id)
        )
        conn.commit()
        tracker_state["on_break"] = True
        tracker_state["paused_by_idle"] = False
        tracker_state["break_start"] = now.isoformat()
        tracker_state["idle_since"] = now.isoformat()
        conn.close()
        return jsonify({"paused": True})


@app.route("/api/status")
def status():
    elapsed = 0
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        elapsed = int((datetime.now() - start).total_seconds())

    idle_sec = get_idle_time_sec()

    break_sec = 0
    if tracker_state["on_break"] and tracker_state["break_start"]:
        bs = datetime.fromisoformat(tracker_state["break_start"])
        break_sec = int((datetime.now() - bs).total_seconds())

    # Pomodoro phase info
    pomo_remaining = 0
    pomo_phase_total = 0
    if tracker_state["pomodoro_enabled"] and tracker_state["pomodoro_phase_start"]:
        phase_start = datetime.fromisoformat(tracker_state["pomodoro_phase_start"])
        phase_elapsed = int((datetime.now() - phase_start).total_seconds())
        if tracker_state["pomodoro_phase"] == "focus":
            pomo_phase_total = config["pomodoro_focus"]
        else:
            pomo_phase_total = config["pomodoro_break"]
        pomo_remaining = max(0, pomo_phase_total - phase_elapsed)

    return jsonify({
        "active": tracker_state["active"],
        "session_start": tracker_state["session_start"],
        "elapsed_sec": elapsed,
        "idle_sec": round(idle_sec, 1),
        "paused_by_idle": tracker_state["paused_by_idle"],
        "on_break": tracker_state["on_break"],
        "break_sec": break_sec,
        "break_threshold": config["break_threshold"],
        "pomodoro_enabled": tracker_state["pomodoro_enabled"],
        "pomodoro_phase": tracker_state["pomodoro_phase"],
        "pomodoro_remaining": pomo_remaining,
        "pomodoro_phase_total": pomo_phase_total,
        "pomodoro_count": tracker_state["pomodoro_count"],
        "active_project_id": tracker_state["active_project_id"],
    })


def _classify_segments(session_start, session_end, session_breaks):
    """Split work into segments around breaks; classify each by length.

    Segment >= FOCUS_MIN_SEGMENT = focused, sonst scattered.
    Liefert (focused_sec, scattered_sec).
    """
    if session_end <= session_start:
        return 0, 0
    breaks = sorted(
        [(bs, be) for bs, be in session_breaks if be is not None],
        key=lambda x: x[0],
    )
    focused = 0
    scattered = 0
    cursor = session_start
    for b_start, b_end in breaks:
        bs = max(b_start, session_start)
        be = min(b_end, session_end)
        if bs >= session_end:
            break
        seg = (bs - cursor).total_seconds()
        if seg > 0:
            if seg >= FOCUS_MIN_SEGMENT:
                focused += int(seg)
            else:
                scattered += int(seg)
        if be > cursor:
            cursor = be
    seg = (session_end - cursor).total_seconds()
    if seg > 0:
        if seg >= FOCUS_MIN_SEGMENT:
            focused += int(seg)
        else:
            scattered += int(seg)
    return focused, scattered


@app.route("/api/today")
def today_stats():
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    sessions = conn.execute(
        "SELECT id, start_time, end_time, duration_sec, idle_sec, note, project_id FROM sessions WHERE start_time LIKE ?",
        (f"{today}%",)
    ).fetchall()

    total_work = sum(s[3] - s[4] for s in sessions if s[3])
    total_idle = sum(s[4] for s in sessions if s[4])
    total_raw = sum(s[3] for s in sessions if s[3])

    breaks = conn.execute(
        "SELECT id, start_time, end_time, duration_sec, session_id, auto FROM breaks WHERE start_time LIKE ? ORDER BY start_time DESC",
        (f"{today}%",)
    ).fetchall()

    # Per-session: split into work segments around breaks, classify each segment
    breaks_by_session = {}
    for b in breaks:
        sid = b[4]
        if sid is None:
            continue
        b_start = datetime.fromisoformat(b[1])
        b_end = datetime.fromisoformat(b[2]) if b[2] else None
        breaks_by_session.setdefault(sid, []).append((b_start, b_end))

    focused_work = 0
    scattered_work = 0
    for s in sessions:
        if not s[2]:
            continue  # active session — handled below
        s_start = datetime.fromisoformat(s[1])
        s_end = datetime.fromisoformat(s[2])
        f, sc = _classify_segments(s_start, s_end, breaks_by_session.get(s[0], []))
        focused_work += f
        scattered_work += sc

    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if start.strftime("%Y-%m-%d") == today:
            current = int((datetime.now() - start).total_seconds())
            active_id = _get_active_session_id(conn)
            active_idle = 0
            if active_id:
                row = conn.execute("SELECT COALESCE(idle_sec, 0) FROM sessions WHERE id = ?", (active_id,)).fetchone()
                active_idle = row[0] if row else 0
            active_net = max(0, current - active_idle)
            total_raw += current
            total_work += active_net
            total_idle += active_idle
            # Classify active session by segments
            f, sc = _classify_segments(start, datetime.now(), breaks_by_session.get(active_id, []))
            focused_work += f
            scattered_work += sc

    total_break = sum(b[3] for b in breaks if b[3])
    total_auto_break = sum(b[3] for b in breaks if b[3] and b[5] == 1)
    total_manual_break = sum(b[3] for b in breaks if b[3] and b[5] == 0)

    # Longest focus stretch today (longest session net duration)
    longest_row = conn.execute(
        "SELECT MAX(duration_sec - idle_sec) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
        (f"{today}%",)
    ).fetchone()
    longest_focus = (longest_row[0] or 0) if longest_row else 0
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if start.strftime("%Y-%m-%d") == today:
            active_id = _get_active_session_id(conn)
            active_idle = 0
            if active_id:
                row = conn.execute("SELECT COALESCE(idle_sec,0) FROM sessions WHERE id = ?", (active_id,)).fetchone()
                active_idle = row[0] if row else 0
            active_net = int((datetime.now() - start).total_seconds()) - active_idle
            if active_net > longest_focus:
                longest_focus = active_net

    # Focus score — only penalize AUTO long breaks (>3 min, idle-triggered = distraction)
    # Manual breaks (pause button = real break like eating) don't hurt the score.
    long_breaks = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0) FROM breaks WHERE start_time LIKE ? AND duration_sec > 180 AND auto = 1",
        (f"{today}%",)
    ).fetchone()[0]

    # Short auto breaks (<=3 min, toilet/scroll — not penalized but shown)
    short_breaks = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0), COUNT(*) FROM breaks WHERE start_time LIKE ? AND duration_sec <= 180 AND duration_sec > 0 AND auto = 1",
        (f"{today}%",)
    ).fetchone()
    short_break_sec = short_breaks[0]
    short_break_count = short_breaks[1]

    long_break_count = conn.execute(
        "SELECT COUNT(*) FROM breaks WHERE start_time LIKE ? AND duration_sec > 180 AND auto = 1",
        (f"{today}%",)
    ).fetchone()[0]

    # Manual breaks (pause button) — shown separately, not penalized
    manual_breaks = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0), COUNT(*) FROM breaks WHERE start_time LIKE ? AND duration_sec > 0 AND auto = 0",
        (f"{today}%",)
    ).fetchone()
    manual_break_sec = manual_breaks[0]
    manual_break_count = manual_breaks[1]

    conn.close()
    focus_denom = total_work + long_breaks
    focus_score = round((total_work / focus_denom * 100) if focus_denom > 0 else 100)

    # Goal progress
    goal_pct = min(100, round(total_work / config["daily_goal"] * 100)) if config["daily_goal"] > 0 else 0

    # Hourly breakdown for timeline
    hours = []
    now_hour = datetime.now().hour
    for h in range(24):
        h_start = f"{today}T{h:02d}:00:00"
        h_end = f"{today}T{h:02d}:59:59"
        # Work seconds in this hour
        h_work = 0
        h_break = 0
        for s in sessions:
            s_start = datetime.fromisoformat(s[1])
            s_end = datetime.fromisoformat(s[2]) if s[2] else datetime.now()
            # Overlap of session with this hour
            hour_begin = datetime.fromisoformat(h_start)
            hour_finish = hour_begin + timedelta(hours=1)
            overlap_start = max(s_start, hour_begin)
            overlap_end = min(s_end, hour_finish)
            if overlap_start < overlap_end:
                h_work += (overlap_end - overlap_start).total_seconds()
        # Breaks in this hour
        for b in breaks:
            if not b[1]:
                continue
            b_start = datetime.fromisoformat(b[1])
            b_end = datetime.fromisoformat(b[2]) if b[2] else datetime.now()
            hour_begin = datetime.fromisoformat(h_start)
            hour_finish = hour_begin + timedelta(hours=1)
            overlap_start = max(b_start, hour_begin)
            overlap_end = min(b_end, hour_finish)
            if overlap_start < overlap_end:
                h_break += (overlap_end - overlap_start).total_seconds()
        h_work = max(0, h_work - h_break)
        hours.append({"hour": h, "work": int(h_work), "pause": int(h_break)})

    return jsonify({
        "date": today,
        "total_work_sec": total_work,
        "total_idle_sec": total_idle,
        "total_break_sec": total_break,
        "total_auto_break_sec": total_auto_break,
        "total_manual_break_sec": total_manual_break,
        "focused_work_sec": focused_work,
        "scattered_work_sec": scattered_work,
        "longest_focus_sec": longest_focus,
        "hours": hours,
        "total_raw_sec": total_raw,
        "session_count": len(sessions),
        "focus_score": focus_score,
        "focus_breakdown": {
            "work_sec": total_work,
            "long_break_sec": long_breaks,
            "long_break_count": long_break_count,
            "short_break_sec": short_break_sec,
            "short_break_count": short_break_count,
            "manual_break_sec": manual_break_sec,
            "manual_break_count": manual_break_count,
            "formula": "Arbeit / (Arbeit + Auto-Ablenkungen >3min). Manuelle Pausen zaehlen nicht.",
        },
        "daily_goal": config["daily_goal"],
        "goal_pct": goal_pct,
        "sessions": [
            {"id": s[0], "start": s[1], "end": s[2], "duration_sec": s[3], "idle_sec": s[4], "note": s[5], "project_id": s[6]}
            for s in sessions
        ],
        "breaks": [
            {"id": b[0], "start": b[1], "end": b[2], "duration_sec": b[3], "session_id": b[4]}
            for b in breaks
        ],
    })


@app.route("/api/week")
def week_stats():
    conn = sqlite3.connect(DB_PATH)
    days = []
    for i in range(6, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0), COALESCE(SUM(idle_sec), 0), COUNT(*) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{day}%",)
        ).fetchone()
        days.append({
            "date": day,
            "label": (datetime.now() - timedelta(days=i)).strftime("%a"),
            "work_sec": row[0],
            "idle_sec": row[1],
            "sessions": row[2],
        })
    # Last week totals for comparison
    this_week_work = sum(d["work_sec"] for d in days)
    last_week_work = 0
    for i in range(13, 6, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{day}%",)
        ).fetchone()
        last_week_work += row[0]

    conn.close()

    diff = this_week_work - last_week_work
    diff_pct = round(diff / max(last_week_work, 1) * 100) if last_week_work > 0 else 0

    return jsonify({
        "days": days,
        "this_week_sec": this_week_work,
        "last_week_sec": last_week_work,
        "diff_sec": diff,
        "diff_pct": diff_pct,
    })


@app.route("/api/history")
def history():
    """Long-term stats: monthly breakdown + all-time totals."""
    conn = sqlite3.connect(DB_PATH)

    # All-time totals
    totals = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(duration_sec), 0), COALESCE(SUM(idle_sec), 0), MIN(DATE(start_time)), MAX(DATE(start_time)) FROM sessions WHERE end_time IS NOT NULL"
    ).fetchone()

    total_sessions = totals[0]
    total_raw = totals[1]
    total_idle = totals[2]
    total_work = total_raw - total_idle
    first_day = totals[3]
    last_day = totals[4]

    # Days with any session
    active_days = conn.execute(
        "SELECT COUNT(DISTINCT DATE(start_time)) FROM sessions WHERE end_time IS NOT NULL"
    ).fetchone()[0]

    # Days that met goal
    goal_days = conn.execute(
        "SELECT COUNT(*) FROM (SELECT DATE(start_time) as d, SUM(duration_sec - idle_sec) as w FROM sessions WHERE end_time IS NOT NULL GROUP BY d HAVING w >= ?)",
        (config["daily_goal"],)
    ).fetchone()[0]

    # Monthly breakdown (last 12 months)
    months = []
    for i in range(11, -1, -1):
        d = datetime.now() - timedelta(days=i * 30)
        ym = d.strftime("%Y-%m")
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0), COALESCE(SUM(idle_sec), 0), COUNT(*), COUNT(DISTINCT DATE(start_time)) FROM sessions WHERE strftime('%Y-%m', start_time) = ? AND end_time IS NOT NULL",
            (ym,)
        ).fetchone()
        if row[2] > 0:
            months.append({
                "month": ym,
                "label": d.strftime("%b %Y"),
                "work_sec": row[0],
                "idle_sec": row[1],
                "sessions": row[2],
                "active_days": row[3],
                "avg_per_day": round(row[0] / max(row[3], 1)),
            })

    # Daily breakdown for current month (for heatmap)
    cm = datetime.now().strftime("%Y-%m")
    daily = conn.execute(
        "SELECT DATE(start_time) as d, SUM(duration_sec - idle_sec), COUNT(*) FROM sessions WHERE strftime('%Y-%m', start_time) = ? AND end_time IS NOT NULL GROUP BY d ORDER BY d",
        (cm,)
    ).fetchall()

    conn.close()

    return jsonify({
        "all_time": {
            "total_work_sec": total_work,
            "total_idle_sec": total_idle,
            "total_sessions": total_sessions,
            "active_days": active_days,
            "goal_days": goal_days,
            "first_day": first_day,
            "last_day": last_day,
            "avg_per_day": round(total_work / max(active_days, 1)),
        },
        "months": months,
        "current_month_daily": [
            {"date": d[0], "work_sec": d[1], "sessions": d[2]}
            for d in daily
        ],
    })


@app.route("/api/streak")
def streak():
    """Calculate consecutive days with daily_goal met."""
    conn = sqlite3.connect(DB_PATH)
    streak_count = 0
    day = datetime.now() - timedelta(days=1)  # start from yesterday

    while True:
        ds = day.strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{ds}%",)
        ).fetchone()
        if row and row[0] >= config["daily_goal"]:
            streak_count += 1
            day -= timedelta(days=1)
        else:
            break

    # Check if today also meets goal
    today_work = _get_today_work_sec()
    today_met = today_work >= config["daily_goal"]
    if today_met:
        streak_count += 1

    conn.close()
    return jsonify({
        "streak": streak_count,
        "today_met": today_met,
        "goal_hours": config["daily_goal"] // 3600,
    })


@app.route("/api/pomodoro", methods=["POST"])
def toggle_pomodoro():
    tracker_state["pomodoro_enabled"] = not tracker_state["pomodoro_enabled"]

    if tracker_state["pomodoro_enabled"]:
        tracker_state["pomodoro_phase"] = "focus" if tracker_state["active"] else None
        tracker_state["pomodoro_phase_start"] = datetime.now().isoformat() if tracker_state["active"] else None
        tracker_state["pomodoro_count"] = 0
        tracker_state["pomodoro_notified"] = False
    else:
        tracker_state["pomodoro_phase"] = None
        tracker_state["pomodoro_phase_start"] = None

    return jsonify({"pomodoro_enabled": tracker_state["pomodoro_enabled"]})


# --- Gamification ---

ACHIEVEMENTS = [
    {"id": "first_hour", "name": "Erster Schritt", "desc": "1 Stunde an einem Tag gearbeitet", "icon": "🌱", "threshold_sec": 3600},
    {"id": "four_hours", "name": "Halbzeit", "desc": "4 Stunden an einem Tag", "icon": "⚡", "threshold_sec": 14400},
    {"id": "six_hours", "name": "Tagesziel", "desc": "6 Stunden an einem Tag", "icon": "🏆", "threshold_sec": 21600},
    {"id": "eight_hours", "name": "Marathonlaeufer", "desc": "8 Stunden an einem Tag", "icon": "💎", "threshold_sec": 28800},
    {"id": "ten_hours", "name": "Maschine", "desc": "10 Stunden an einem Tag", "icon": "🤖", "threshold_sec": 36000},
    {"id": "streak_3", "name": "Drei-Tage-Feuer", "desc": "3 Tage Streak", "icon": "🔥", "type": "streak", "threshold": 3},
    {"id": "streak_7", "name": "Wochenkrieger", "desc": "7 Tage Streak", "icon": "⚔️", "type": "streak", "threshold": 7},
    {"id": "streak_14", "name": "Unstoppable", "desc": "14 Tage Streak", "icon": "🚀", "type": "streak", "threshold": 14},
    {"id": "streak_30", "name": "Legende", "desc": "30 Tage Streak", "icon": "👑", "type": "streak", "threshold": 30},
    {"id": "pomo_5", "name": "Fokus-Anfaenger", "desc": "5 Pomodoros an einem Tag", "icon": "🍅", "type": "pomo", "threshold": 5},
    {"id": "pomo_10", "name": "Fokus-Meister", "desc": "10 Pomodoros an einem Tag", "icon": "🍅", "type": "pomo", "threshold": 10},
    {"id": "focus_95", "name": "Laser-Fokus", "desc": "95%+ Focus Score an einem Tag (min 4h)", "icon": "🎯", "type": "focus", "threshold": 95},
    {"id": "early_bird", "name": "Fruehaufsteher", "desc": "Session vor 07:00 gestartet", "icon": "🌅", "type": "time", "threshold": 7},
    {"id": "night_owl", "name": "Nachteule", "desc": "Session nach 23:00 noch aktiv", "icon": "🦉", "type": "time", "threshold": 23},
]

XP_PER_HOUR = 100       # 100 XP per productive hour
XP_PER_POMO = 25        # 25 XP per completed pomodoro
XP_PER_STREAK_DAY = 50  # 50 XP bonus per streak day
LEVELS = [0, 100, 300, 600, 1000, 1500, 2200, 3000, 4000, 5500, 7500, 10000, 13000, 17000, 22000, 28000, 35000, 43000, 52000, 65000]


def _calc_level(xp):
    for i in range(len(LEVELS) - 1, -1, -1):
        if xp >= LEVELS[i]:
            next_lvl = LEVELS[i + 1] if i + 1 < len(LEVELS) else LEVELS[i] + 15000
            return {
                "level": i + 1,
                "current_xp": xp - LEVELS[i],
                "next_level_xp": next_lvl - LEVELS[i],
                "total_xp": xp,
            }
    return {"level": 1, "current_xp": 0, "next_level_xp": 100, "total_xp": 0}


def _get_streak_count():
    conn = sqlite3.connect(DB_PATH)
    streak_count = 0
    day = datetime.now() - timedelta(days=1)
    while True:
        ds = day.strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{ds}%",)
        ).fetchone()
        if row and row[0] >= config["daily_goal"]:
            streak_count += 1
            day -= timedelta(days=1)
        else:
            break
    today_work = _get_today_work_sec()
    if today_work >= config["daily_goal"]:
        streak_count += 1
    conn.close()
    return streak_count


@app.route("/api/gamification")
def gamification():
    conn = sqlite3.connect(DB_PATH)

    # Total work hours all time
    row = conn.execute(
        "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE end_time IS NOT NULL"
    ).fetchone()
    total_work_sec = row[0] if row else 0
    # Add current session
    total_work_sec_with_current = total_work_sec
    if tracker_state["active"] and tracker_state["session_start"]:
        total_work_sec_with_current += int((datetime.now() - datetime.fromisoformat(tracker_state["session_start"])).total_seconds())

    # Today's work for achievements
    today_work = _get_today_work_sec()
    today = datetime.now().strftime("%Y-%m-%d")
    today_focus_total = today_work
    today_breaks = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0) FROM breaks WHERE start_time LIKE ?",
        (f"{today}%",)
    ).fetchone()
    today_break_sec = today_breaks[0] if today_breaks else 0
    today_focus_denom = today_work + today_break_sec
    today_focus_score = round((today_work / today_focus_denom * 100) if today_focus_denom > 0 else 100)

    # Max daily work ever (for hour-based achievements)
    max_daily = conn.execute(
        "SELECT DATE(start_time), SUM(duration_sec - idle_sec) as w FROM sessions WHERE end_time IS NOT NULL GROUP BY DATE(start_time) ORDER BY w DESC LIMIT 1"
    ).fetchone()
    max_daily_sec = max(max_daily[1] if max_daily else 0, today_work)

    # Session times for time-based achievements
    earliest = conn.execute("SELECT MIN(TIME(start_time)) FROM sessions").fetchone()
    latest = conn.execute("SELECT MAX(TIME(end_time)) FROM sessions WHERE end_time IS NOT NULL").fetchone()

    conn.close()

    # Streak
    streak_count = _get_streak_count()

    # Pomodoro count today
    pomo_count = tracker_state.get("pomodoro_count", 0)

    # Calculate XP
    xp = int(total_work_sec_with_current / 3600 * XP_PER_HOUR)
    xp += streak_count * XP_PER_STREAK_DAY
    xp += pomo_count * XP_PER_POMO

    level_info = _calc_level(xp)

    # Check achievements
    unlocked = []
    for a in ACHIEVEMENTS:
        achieved = False
        if a.get("type") == "streak":
            achieved = streak_count >= a["threshold"]
        elif a.get("type") == "pomo":
            achieved = pomo_count >= a["threshold"]
        elif a.get("type") == "focus":
            achieved = today_focus_score >= a["threshold"] and today_work >= 14400
        elif a.get("type") == "time":
            if a["id"] == "early_bird" and earliest and earliest[0]:
                achieved = earliest[0] < "07:00"
            elif a["id"] == "night_owl" and latest and latest[0]:
                achieved = latest[0] >= "23:00"
        elif "threshold_sec" in a:
            achieved = max_daily_sec >= a["threshold_sec"]

        unlocked.append({
            "id": a["id"], "name": a["name"], "desc": a["desc"],
            "icon": a["icon"], "unlocked": achieved
        })

    # Day rating
    if today_work < 1800:
        rating = {"grade": "--", "label": "Noch nicht genug Daten"}
    elif today_focus_score >= 95 and today_work >= config["daily_goal"]:
        rating = {"grade": "S", "label": "Perfekter Tag"}
    elif today_work >= config["daily_goal"] and today_focus_score >= 85:
        rating = {"grade": "A", "label": "Ausgezeichnet"}
    elif today_work >= config["daily_goal"] * 0.75:
        rating = {"grade": "B", "label": "Gut"}
    elif today_work >= config["daily_goal"] * 0.5:
        rating = {"grade": "C", "label": "Okay"}
    else:
        rating = {"grade": "D", "label": "Mehr drin"}

    return jsonify({
        **level_info,
        "xp_breakdown": {
            "work_hours": int(total_work_sec_with_current / 3600 * XP_PER_HOUR),
            "streak": streak_count * XP_PER_STREAK_DAY,
            "pomodoros": pomo_count * XP_PER_POMO,
        },
        "achievements": unlocked,
        "unlocked_count": sum(1 for a in unlocked if a["unlocked"]),
        "total_achievements": len(unlocked),
        "rating": rating,
    })


# --- Projects / Todos / Goals ---

def _project_work_sec(conn, project_id=None, since=None, until=None):
    """Sum work sec (duration - idle) for sessions, including active session."""
    q = "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE end_time IS NOT NULL"
    params = []
    if project_id is not None:
        q += " AND project_id = ?"
        params.append(project_id)
    if since is not None:
        q += " AND start_time >= ?"
        params.append(since)
    if until is not None:
        q += " AND start_time < ?"
        params.append(until)
    total = conn.execute(q, params).fetchone()[0] or 0

    # Include active session if it matches project filter + period
    if tracker_state["active"] and tracker_state["session_start"]:
        active_pid = tracker_state["active_project_id"]
        if project_id is None or project_id == active_pid:
            start = datetime.fromisoformat(tracker_state["session_start"])
            if (since is None or start.isoformat() >= since) and \
               (until is None or start.isoformat() < until):
                elapsed = int((datetime.now() - start).total_seconds())
                # Subtract idle already booked
                active_id = _get_active_session_id(conn)
                active_idle = 0
                if active_id:
                    row = conn.execute("SELECT COALESCE(idle_sec, 0) FROM sessions WHERE id = ?", (active_id,)).fetchone()
                    active_idle = row[0] if row else 0
                total += max(0, elapsed - active_idle)
    return int(total)


@app.route("/api/projects/breakdown")
def projects_breakdown():
    """Work seconds per project over day/week/month/all time."""
    conn = sqlite3.connect(DB_PATH)
    projects = conn.execute(
        "SELECT id, name, color FROM projects WHERE archived = 0 ORDER BY name"
    ).fetchall()

    periods = {
        "day": _goal_window("daily"),
        "week": _goal_window("weekly"),
        "month": _goal_window("monthly"),
    }
    result = {"projects": [], "totals": {"day": 0, "week": 0, "month": 0, "all": 0}}
    # also include unassigned (project_id IS NULL)
    groups = [(p[0], p[1], p[2]) for p in projects] + [(None, "(ohne Projekt)", "#5a5a72")]
    for pid, name, color in groups:
        entry = {"id": pid, "name": name, "color": color}
        for label, (since, until) in periods.items():
            sec = _project_work_sec(conn, project_id=pid, since=since, until=until) if pid is not None else _unassigned_work_sec(conn, since, until)
            entry[label] = sec
            result["totals"][label] += sec
        # All-time
        if pid is not None:
            entry["all"] = _project_work_sec(conn, project_id=pid)
        else:
            entry["all"] = _unassigned_work_sec(conn)
        result["totals"]["all"] += entry["all"]
        result["projects"].append(entry)
    conn.close()
    return jsonify(result)


def _unassigned_work_sec(conn, since=None, until=None):
    q = "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE end_time IS NOT NULL AND project_id IS NULL"
    params = []
    if since is not None:
        q += " AND start_time >= ?"
        params.append(since)
    if until is not None:
        q += " AND start_time < ?"
        params.append(until)
    return int(conn.execute(q, params).fetchone()[0] or 0)


@app.route("/api/sessions/<int:sid>", methods=["PATCH"])
def session_detail(sid):
    data = request.json or {}
    conn = sqlite3.connect(DB_PATH)
    fields, params = [], []
    if "project_id" in data:
        pv = data["project_id"]
        fields.append("project_id = ?")
        params.append(int(pv) if pv not in (None, "", 0) else None)
    if "note" in data:
        fields.append("note = ?")
        params.append(data["note"] or "")
    if fields:
        params.append(sid)
        conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/sessions")
def sessions_list():
    """All sessions for history / bulk edit."""
    limit = int(request.args.get("limit", 200))
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, start_time, end_time, duration_sec, idle_sec, note, project_id FROM sessions WHERE end_time IS NOT NULL ORDER BY start_time DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "start": r[1], "end": r[2], "duration_sec": r[3], "idle_sec": r[4], "note": r[5], "project_id": r[6]}
        for r in rows
    ])


@app.route("/api/projects", methods=["GET", "POST"])
def projects_route():
    conn = sqlite3.connect(DB_PATH)
    if request.method == "POST":
        data = request.json or {}
        name = (data.get("name") or "").strip()
        if not name:
            conn.close()
            return jsonify({"error": "name required"}), 400
        color = data.get("color") or "#6c5ce7"
        goal_hours = int(data.get("goal_hours") or 0)
        try:
            cur = conn.execute(
                "INSERT INTO projects (name, color, goal_hours, created_at) VALUES (?, ?, ?, ?)",
                (name, color, goal_hours, datetime.now().isoformat())
            )
            conn.commit()
            pid = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"error": "name exists"}), 409
        conn.close()
        return jsonify({"id": pid, "name": name, "color": color, "goal_hours": goal_hours})

    rows = conn.execute(
        "SELECT id, name, color, goal_hours, archived FROM projects WHERE archived = 0 ORDER BY name"
    ).fetchall()
    result = []
    for r in rows:
        total_sec = _project_work_sec(conn, project_id=r[0])
        goal_sec = r[3] * 3600
        pct = min(100, round(total_sec / goal_sec * 100)) if goal_sec > 0 else 0
        open_todos = conn.execute(
            "SELECT COUNT(*) FROM todos WHERE project_id = ? AND done = 0", (r[0],)
        ).fetchone()[0]
        result.append({
            "id": r[0], "name": r[1], "color": r[2], "goal_hours": r[3],
            "total_sec": total_sec, "goal_pct": pct, "open_todos": open_todos,
        })
    conn.close()
    return jsonify(result)


@app.route("/api/projects/<int:pid>", methods=["PATCH", "DELETE"])
def project_detail(pid):
    conn = sqlite3.connect(DB_PATH)
    if request.method == "DELETE":
        conn.execute("UPDATE projects SET archived = 1 WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    data = request.json or {}
    fields, params = [], []
    for k in ("name", "color"):
        if k in data:
            fields.append(f"{k} = ?")
            params.append(data[k])
    if "goal_hours" in data:
        fields.append("goal_hours = ?")
        params.append(int(data["goal_hours"] or 0))
    if "archived" in data:
        fields.append("archived = ?")
        params.append(1 if data["archived"] else 0)
    if fields:
        params.append(pid)
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/todos", methods=["GET", "POST"])
def todos_route():
    conn = sqlite3.connect(DB_PATH)
    if request.method == "POST":
        data = request.json or {}
        title = (data.get("title") or "").strip()
        if not title:
            conn.close()
            return jsonify({"error": "title required"}), 400
        project_id = data.get("project_id")
        if project_id == "" or project_id == 0:
            project_id = None
        elif project_id is not None:
            project_id = int(project_id)
        priority = int(data.get("priority") or 0)
        cur = conn.execute(
            "INSERT INTO todos (title, project_id, priority, created_at) VALUES (?, ?, ?, ?)",
            (title, project_id, priority, datetime.now().isoformat())
        )
        conn.commit()
        tid = cur.lastrowid
        conn.close()
        return jsonify({"id": tid})

    pid_param = request.args.get("project_id")
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    if pid_param and pid_param != "all":
        rows = conn.execute(
            """SELECT id, project_id, title, done, priority, created_at, completed_at
               FROM todos
               WHERE project_id = ? AND (done = 0 OR completed_at >= ?)
               ORDER BY done, priority DESC, created_at DESC""",
            (int(pid_param), cutoff)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, project_id, title, done, priority, created_at, completed_at
               FROM todos
               WHERE done = 0 OR completed_at >= ?
               ORDER BY done, priority DESC, created_at DESC""",
            (cutoff,)
        ).fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "project_id": r[1], "title": r[2], "done": bool(r[3]),
         "priority": r[4], "created_at": r[5], "completed_at": r[6]}
        for r in rows
    ])


@app.route("/api/todos/<int:tid>", methods=["PATCH", "DELETE"])
def todo_detail(tid):
    conn = sqlite3.connect(DB_PATH)
    if request.method == "DELETE":
        conn.execute("DELETE FROM todos WHERE id = ?", (tid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    data = request.json or {}
    fields, params = [], []
    if "title" in data:
        fields.append("title = ?")
        params.append(data["title"])
    if "priority" in data:
        fields.append("priority = ?")
        params.append(int(data["priority"]))
    if "project_id" in data:
        fields.append("project_id = ?")
        pv = data["project_id"]
        params.append(int(pv) if pv not in (None, "", 0) else None)
    if "done" in data:
        fields.append("done = ?")
        params.append(1 if data["done"] else 0)
        fields.append("completed_at = ?")
        params.append(datetime.now().isoformat() if data["done"] else None)
    if fields:
        params.append(tid)
        conn.execute(f"UPDATE todos SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


def _goal_window(period, start_date=None, end_date=None):
    """Return (since_iso, until_iso) for a goal period."""
    now = datetime.now()
    if period == "daily":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until = since + timedelta(days=1)
    elif period == "weekly":
        since = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        until = since + timedelta(days=7)
    elif period == "monthly":
        since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if since.month == 12:
            until = since.replace(year=since.year + 1, month=1)
        else:
            until = since.replace(month=since.month + 1)
    elif period == "custom":
        since = datetime.fromisoformat(start_date) if start_date else now
        until = datetime.fromisoformat(end_date) + timedelta(days=1) if end_date else now + timedelta(days=365)
    else:
        since = now
        until = now
    return since.isoformat(), until.isoformat()


@app.route("/api/goals", methods=["GET", "POST"])
def goals_route():
    conn = sqlite3.connect(DB_PATH)
    if request.method == "POST":
        data = request.json or {}
        name = (data.get("name") or "").strip()
        period = data.get("period") or "weekly"
        target_sec = int(data.get("target_sec") or 0)
        if not name or target_sec <= 0 or period not in ("daily", "weekly", "monthly", "custom"):
            conn.close()
            return jsonify({"error": "invalid goal"}), 400
        pid = data.get("project_id")
        pid = int(pid) if pid not in (None, "", 0) else None
        start_date = data.get("start_date") if period == "custom" else None
        end_date = data.get("end_date") if period == "custom" else None
        cur = conn.execute(
            "INSERT INTO goals (name, period, target_sec, project_id, start_date, end_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, period, target_sec, pid, start_date, end_date, datetime.now().isoformat())
        )
        conn.commit()
        gid = cur.lastrowid
        conn.close()
        return jsonify({"id": gid})

    rows = conn.execute(
        """SELECT g.id, g.name, g.period, g.target_sec, g.project_id, g.start_date, g.end_date, p.name, p.color
           FROM goals g LEFT JOIN projects p ON p.id = g.project_id
           WHERE g.archived = 0
           ORDER BY CASE g.period WHEN 'daily' THEN 1 WHEN 'weekly' THEN 2 WHEN 'monthly' THEN 3 ELSE 4 END, g.created_at"""
    ).fetchall()
    result = []
    for r in rows:
        since, until = _goal_window(r[2], r[5], r[6])
        progress = _project_work_sec(conn, project_id=r[4], since=since, until=until)
        pct = min(100, round(progress / r[3] * 100)) if r[3] > 0 else 0
        result.append({
            "id": r[0], "name": r[1], "period": r[2], "target_sec": r[3],
            "project_id": r[4], "start_date": r[5], "end_date": r[6],
            "project_name": r[7], "project_color": r[8],
            "progress_sec": progress, "pct": pct,
            "window_start": since, "window_end": until,
        })
    conn.close()
    return jsonify(result)


@app.route("/api/goals/<int:gid>", methods=["PATCH", "DELETE"])
def goal_detail(gid):
    conn = sqlite3.connect(DB_PATH)
    if request.method == "DELETE":
        conn.execute("UPDATE goals SET archived = 1 WHERE id = ?", (gid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    data = request.json or {}
    fields, params = [], []
    for k in ("name", "period", "start_date", "end_date"):
        if k in data:
            fields.append(f"{k} = ?")
            params.append(data[k])
    if "target_sec" in data:
        fields.append("target_sec = ?")
        params.append(int(data["target_sec"]))
    if "project_id" in data:
        fields.append("project_id = ?")
        pv = data["project_id"]
        params.append(int(pv) if pv not in (None, "", 0) else None)
    if fields:
        params.append(gid)
        conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET", "POST"])
def config_route():
    if request.method == "POST":
        data = request.json
        for key in DEFAULT_CONFIG:
            if key in data:
                config[key] = int(data[key])
        save_config_to_db()
        return jsonify({"ok": True})
    return jsonify(config)


@app.route("/api/export")
def export():
    fmt = request.args.get("format", "csv")
    days = int(request.args.get("days", 30))
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    sessions = conn.execute(
        "SELECT start_time, end_time, duration_sec, idle_sec, note FROM sessions WHERE start_time >= ? AND end_time IS NOT NULL ORDER BY start_time",
        (since,)
    ).fetchall()
    conn.close()

    if fmt == "json":
        data = [
            {"start": s[0], "end": s[1], "duration_sec": s[2], "idle_sec": s[3],
             "work_sec": s[2] - s[3], "note": s[4]}
            for s in sessions
        ]
        return jsonify(data)

    # CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Datum", "Start", "Ende", "Dauer (Min)", "Pausen (Min)", "Arbeit (Min)", "Notiz"])
    for s in sessions:
        start = datetime.fromisoformat(s[0])
        writer.writerow([
            start.strftime("%Y-%m-%d"),
            start.strftime("%H:%M"),
            datetime.fromisoformat(s[1]).strftime("%H:%M") if s[1] else "",
            round(s[2] / 60, 1),
            round(s[3] / 60, 1),
            round((s[2] - s[3]) / 60, 1),
            s[4] or ""
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=focustracker-{days}d.csv"}
    )


def backup_db():
    """Create a timestamped backup of the DB on startup."""
    if DB_PATH.exists():
        import shutil
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        dest = backup_dir / f"focus_{stamp}.db"
        shutil.copy2(DB_PATH, dest)
        # Keep only last 10 backups
        backups = sorted(backup_dir.glob("focus_*.db"))
        for old in backups[:-10]:
            old.unlink()
        print(f"  Backup: {dest}")


def recover_session():
    """Resume or close any session that was running when the server died."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, start_time FROM sessions WHERE end_time IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return

    session_id, start_time = row
    start = datetime.fromisoformat(start_time)
    age = (datetime.now() - start).total_seconds()

    same_day = start.strftime("%Y-%m-%d") == datetime.now().strftime("%Y-%m-%d")

    if same_day:
        # Same day — resume
        tracker_state["active"] = True
        tracker_state["session_start"] = start_time
        print(f"  Recovered session #{session_id} (started {start.strftime('%H:%M')}, {int(age//60)} min ago)")
    else:
        # Different day — close at midnight of the start day
        midnight = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        duration = int((midnight - start).total_seconds())
        conn.execute(
            "UPDATE sessions SET end_time = ?, duration_sec = ?, note = ? WHERE id = ?",
            (midnight.isoformat(), duration, "[auto-closed: Tageswechsel]", session_id)
        )
        conn.commit()
        print(f"  Auto-closed session #{session_id} from {start.strftime('%d.%m')} at midnight ({duration//3600}h {(duration%3600)//60}m)")

    conn.close()


if __name__ == "__main__":
    backup_db()
    init_db()
    recover_session()
    monitor = threading.Thread(target=idle_monitor, daemon=True)
    monitor.start()
    port = int(os.environ.get("FOCUSTRACKER_PORT", "5050"))
    print(f"\n  FocusTracker running at http://localhost:{port}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
