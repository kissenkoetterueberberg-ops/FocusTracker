#!/usr/bin/env python3
"""FocusTracker — Work time tracker with idle detection, pomodoro, streaks & export."""

import csv
import io
import os
import shlex
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

try:
    from ai_planner import generate_day_plan as _generate_day_plan
    _PLANNER_AVAILABLE = True
except ImportError:
    _PLANNER_AVAILABLE = False
    def _generate_day_plan(*a, **kw):
        return {"error": "ai_planner module not found", "blocks": []}

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
    # --- Activity-Detection ---
    "distraction_grace_sec": 120,  # distraction app >2min → auto-pause (kurze Checks toleriert)
    "productive_grace_sec": 60,    # productive app >60s (tracker off) → start-reminder
    "start_reminder_cooldown": 900, # 15 min between start-reminders
    "auto_stop_idle_day": 2700,    # 45 min idle tagsüber → auto-stop
    "auto_stop_idle_night": 900,   # 15 min idle 22–06 Uhr → auto-stop
    "night_start_hour": 22,
    "night_end_hour": 6,
    "auto_close_tabs": True,
}

# List-type config: stored as comma-separated TEXT
DEFAULT_LIST_CONFIG = {
    "blocklist_domains": "youtube.com,instagram.com,tiktok.com,reddit.com,x.com,twitter.com,facebook.com,9gag.com",
    "blocklist_apps": "WhatsApp,Messages,Telegram,Discord,Signal",
    "productive_domains": "localhost,127.0.0.1,shopify.com,myshopify.com,gethappyhours.de,vercel.com,github.com,gitlab.com,claude.ai,anthropic.com,chatgpt.com,openai.com,perplexity.ai,notion.so,figma.com,stripe.com,cursor.com,cursor.sh,clarity.microsoft.com,klaviyo.com,mail.google.com,docs.google.com,drive.google.com,calendar.google.com,sheets.google.com,business.facebook.com,ads.facebook.com,adsmanager.facebook.com,analytics.google.com,search.google.com,n8n.io,linear.app,slack.com,developer.mozilla.org,react.dev,nextjs.org,tailwindcss.com,docker.com,supabase.com,postgresql.org,sqlite.org",
    "productive_apps": "Code,Cursor,Visual Studio Code,iTerm2,Terminal,Warp,Xcode,Ghostty,Claude,ChatGPT,Docker,Docker Desktop,Postico,TablePlus,Postman,Insomnia,Slack,Linear,Notion,Figma",
}

config = dict(DEFAULT_CONFIG)
list_config = dict(DEFAULT_LIST_CONFIG)
IDLE_CHECK_INTERVAL = 5  # war 10, jetzt schneller für Activity-Detection
FOCUS_MIN_SEGMENT = 600  # Arbeits-Stretches >= 10 min gelten als fokussiert
BREAK_GLUE_SEC = 90      # Breaks <= 90s werden ignoriert (Segmente davor/danach verschmelzen)

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
    "source": "live",              # "live" | "offline" — offline skips idle/activity checks
    # Activity-Detection
    "current_app": None,
    "current_url": None,
    "current_classification": "neutral",  # productive | distraction | neutral
    "classification_since": None,          # when current classification started
    "last_productive_ts": None,            # last time we saw productive activity
    "last_start_reminder": 0,              # unix ts — cooldown for start-reminder
    "paused_by_distraction": False,        # current pause is because of distraction
}

# Lock protecting all reads/writes of tracker_state across Flask threads + idle_monitor
_state_lock = threading.Lock()


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
            color TEXT DEFAULT '#14b8a6',
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
        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type TEXT NOT NULL,
            period_key TEXT NOT NULL,
            target_sec INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(period_type, period_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            app TEXT,
            domain TEXT,
            classification TEXT,
            duration_sec INTEGER NOT NULL,
            session_id INTEGER,
            in_break INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_log(ts)")
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
    # --- AI Planner tables (additive migration) ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS day_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            raw_input TEXT,
            plan_json TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_day_plans_date ON day_plans(date)")

    # Add new columns to todos if missing
    todo_cols = [r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()]
    if "planned_start_time" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN planned_start_time TEXT")
    if "planned_duration_min" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN planned_duration_min INTEGER")
    if "day_plan_id" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN day_plan_id INTEGER REFERENCES day_plans(id)")
    if "carried_from_date" not in todo_cols:
        conn.execute("ALTER TABLE todos ADD COLUMN carried_from_date TEXT")

    # Add project_id column to sessions if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
    if "project_id" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN project_id INTEGER REFERENCES projects(id)")
    if "source" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'live'")
        conn.execute("UPDATE sessions SET source = 'live' WHERE source IS NULL")

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
        k, v = row[0], row[1]
        if k in config:
            try:
                config[k] = int(v)
            except (TypeError, ValueError):
                pass
        elif k in list_config:
            list_config[k] = v
    conn.commit()
    conn.close()


def save_config_to_db():
    conn = sqlite3.connect(DB_PATH)
    for k, v in config.items():
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, str(v)))
    for k, v in list_config.items():
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def _parse_list(s):
    return [x.strip().lower() for x in (s or "").split(",") if x.strip()]


def _extract_domain(url):
    """Return the registrable host from a URL (strips scheme, path, www.)."""
    if not url:
        return None
    u = url.strip().lower()
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u or None


def log_activity(app_name, url, classification, duration_sec, session_id, in_break):
    """Persist an activity sample. Coalesce with the previous row if same app/domain
    to keep the table small (one row per continuous context)."""
    if duration_sec <= 0:
        return
    domain = _extract_domain(url)
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute(
            "SELECT id, ts, duration_sec, app, domain, classification, session_id, in_break "
            "FROM activity_log ORDER BY id DESC LIMIT 1"
        )
        prev = cur.fetchone()
        now_iso = datetime.now().isoformat()
        in_break_i = 1 if in_break else 0
        if prev:
            prev_id, prev_ts, prev_dur, prev_app, prev_dom, prev_cls, prev_sid, prev_ib = prev
            same = (
                (prev_app or None) == (app_name or None)
                and (prev_dom or None) == (domain or None)
                and prev_cls == classification
                and (prev_sid or 0) == (session_id or 0)
                and (prev_ib or 0) == in_break_i
            )
            if same:
                # Only coalesce if the previous row is recent (<= 2x sample interval)
                try:
                    gap = (datetime.now() - datetime.fromisoformat(prev_ts)).total_seconds()
                except Exception:
                    gap = 9999
                if gap <= duration_sec + IDLE_CHECK_INTERVAL * 2:
                    conn.execute(
                        "UPDATE activity_log SET duration_sec = duration_sec + ?, ts = ? WHERE id = ?",
                        (duration_sec, now_iso, prev_id)
                    )
                    conn.commit()
                    conn.close()
                    return
        conn.execute(
            "INSERT INTO activity_log (ts, app, domain, classification, duration_sec, session_id, in_break) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (now_iso, app_name, domain, classification, duration_sec, session_id, in_break_i)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


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


_BROWSER_URL_SCRIPTS = {
    "Google Chrome": 'tell application "Google Chrome" to if it is running then return URL of active tab of front window',
    "Chromium": 'tell application "Chromium" to if it is running then return URL of active tab of front window',
    "Brave Browser": 'tell application "Brave Browser" to if it is running then return URL of active tab of front window',
    "Microsoft Edge": 'tell application "Microsoft Edge" to if it is running then return URL of active tab of front window',
    "Arc": 'tell application "Arc" to if it is running then return URL of active tab of front window',
    "Dia": 'tell application "Dia" to if it is running then return URL of active tab of front window',
    "Safari": 'tell application "Safari" to if it is running then return URL of front document',
    "Safari Technology Preview": 'tell application "Safari Technology Preview" to if it is running then return URL of front document',
}


def _run_osascript(script, timeout=2):
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        out = (res.stdout or "").strip()
        return out if res.returncode == 0 and out else None
    except Exception:
        return None


def get_active_context():
    """Return (app_name, url_or_None). URL only if frontmost is a supported browser."""
    app_name = _run_osascript(
        'tell application "System Events" to get name of (first process whose frontmost is true)'
    )
    url = None
    if app_name in _BROWSER_URL_SCRIPTS:
        url = _run_osascript(_BROWSER_URL_SCRIPTS[app_name])
    return app_name, url


def _domain_matches(url, domain):
    if not url or not domain:
        return False
    url_l = url.lower()
    return domain in url_l  # substring match is fine — domain already includes tld


def classify_activity(app_name, url):
    """Return 'productive' | 'distraction' | 'neutral'."""
    if not app_name:
        return "neutral"
    app_l = app_name.lower()
    block_apps = _parse_list(list_config.get("blocklist_apps", ""))
    prod_apps = _parse_list(list_config.get("productive_apps", ""))
    block_doms = _parse_list(list_config.get("blocklist_domains", ""))
    prod_doms = _parse_list(list_config.get("productive_domains", ""))

    # URL signals win over app signals (browser can be either)
    if url:
        for d in block_doms:
            if _domain_matches(url, d):
                return "distraction"
        for d in prod_doms:
            if _domain_matches(url, d):
                return "productive"
        return "neutral"  # unknown URL — don't guess

    if app_l in block_apps or any(a.lower() == app_l for a in block_apps):
        return "distraction"
    if app_l in prod_apps or any(a.lower() == app_l for a in prod_apps):
        return "productive"
    return "neutral"


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
        # Escape backslashes and double-quotes to prevent osascript injection
        def _esc(s):
            return str(s).replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{_esc(message)}" with title "{_esc(title)}" sound name "{_esc(sound)}"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        pass


def close_blocklist_tabs():
    """Close all browser tabs matching blocklist domains via AppleScript."""
    block_doms = _parse_list(list_config.get("blocklist_domains", ""))
    if not block_doms:
        return
    cond = " or ".join(f'(URL of t) contains "{d}"' for d in block_doms)
    chrome_like = ["Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge", "Arc", "Dia"]
    for browser in chrome_like:
        script = f'''
tell application "System Events"
    if exists (process "{browser}") then
        tell application "{browser}"
            repeat with w in windows
                set tabsToClose to {{}}
                repeat with t in tabs of w
                    if {cond} then set end of tabsToClose to t
                end repeat
                repeat with t in tabsToClose
                    close t
                end repeat
            end repeat
        end tell
    end if
end tell'''
        _run_osascript(script, timeout=5)
    safari_cond = " or ".join(f'(URL of t) contains "{d}"' for d in block_doms)
    _run_osascript(f'''
tell application "System Events"
    if exists (process "Safari") then
        tell application "Safari"
            repeat with w in windows
                set tabsToClose to {{}}
                repeat with t in tabs of w
                    if {safari_cond} then set end of tabsToClose to t
                end repeat
                repeat with t in tabsToClose
                    close t
                end repeat
            end repeat
        end tell
    end if
end tell''', timeout=5)


def _get_active_session_id(conn):
    row = conn.execute(
        "SELECT id FROM sessions WHERE end_time IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _get_today_work_sec(live_only: bool = False):
    """Calculate today's net work seconds (excluding idle/breaks) for goal tracking.

    live_only=True excludes offline/manual sessions — use for focus-score math.
    Default (False) includes all sources — use for daily-goal progress.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    if live_only:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions "
            "WHERE start_time LIKE ? AND end_time IS NOT NULL AND COALESCE(source,'live')='live'",
            (f"{today}%",)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{today}%",)
        ).fetchone()
    total = row[0] if row else 0
    # Add current active session net of already-booked idle
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if start.strftime("%Y-%m-%d") == today:
            elapsed = int((datetime.now() - start).total_seconds())
            active_id = _get_active_session_id(conn)
            active_idle = 0
            active_source = "live"
            if active_id:
                r = conn.execute(
                    "SELECT COALESCE(idle_sec, 0), COALESCE(source,'live') FROM sessions WHERE id = ?",
                    (active_id,)
                ).fetchone()
                if r:
                    active_idle = r[0]
                    active_source = r[1]
            if not live_only or active_source == "live":
                total += max(0, elapsed - active_idle)
    conn.close()
    return total


def _is_night_hour(h):
    ns, ne = config["night_start_hour"], config["night_end_hour"]
    # Night wraps midnight (e.g. 22–06): ns > ne
    if ns > ne:
        return h >= ns or h < ne
    # Night within same day (unusual config)
    return ns <= h < ne


def _auto_stop_session(reason, last_activity_dt):
    """Close the active session using last_activity_dt as end_time (not now)."""
    conn = sqlite3.connect(DB_PATH)
    session_id = _get_active_session_id(conn)
    if not session_id:
        conn.close()
        return
    start = datetime.fromisoformat(tracker_state["session_start"])
    end = max(start, last_activity_dt)
    duration = int((end - start).total_seconds())
    note = f"[auto-stop: {reason}]"
    conn.execute(
        "UPDATE sessions SET end_time = ?, duration_sec = ?, note = COALESCE(NULLIF(note,''), ?) WHERE id = ?",
        (end.isoformat(), duration, note, session_id)
    )
    # Close any open break at last_activity too (or at now if break started after)
    conn.execute(
        "UPDATE breaks SET end_time = ?, duration_sec = MAX(0, CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER)) WHERE end_time IS NULL AND session_id = ?",
        (end.isoformat(), end.isoformat(), session_id)
    )
    conn.commit()
    conn.close()
    tracker_state["active"] = False
    tracker_state["session_start"] = None
    tracker_state["paused_by_idle"] = False
    tracker_state["paused_by_distraction"] = False
    tracker_state["on_break"] = False
    tracker_state["break_start"] = None
    tracker_state["pomodoro_phase"] = None
    tracker_state["pomodoro_phase_start"] = None
    tracker_state["active_project_id"] = None
    set_dnd(False)
    send_notification(
        "Session automatisch beendet",
        f"{reason}. Ende: {end.strftime('%H:%M')} ({duration // 3600}h {(duration % 3600) // 60}m).",
        "Submarine"
    )


def idle_monitor():
    """Background thread: idle/activity detection, auto-breaks, pomodoro, reminders, auto-stop.

    Expensive I/O (idle check, osascript, notifications) runs outside _state_lock.
    All tracker_state reads/writes are protected by _state_lock to avoid races
    between this thread and Flask request handlers.
    """
    goal_notified = False

    while True:
        time.sleep(IDLE_CHECK_INTERVAL)

        # --- Collect external data outside the lock (no state mutation) ---
        idle_sec = get_idle_time_sec()
        now = time.time()
        now_dt = datetime.now()
        app_name, url = get_active_context()

        # Derive classification purely from external data
        classification = classify_activity(app_name, url)
        if idle_sec >= 60:
            classification = "neutral"

        # --- Update context fields + snapshot mutable state ---
        with _state_lock:
            prev_cls = tracker_state["current_classification"]
            tracker_state["current_app"] = app_name
            tracker_state["current_url"] = url
            if classification != prev_cls:
                tracker_state["current_classification"] = classification
                tracker_state["classification_since"] = now_dt.isoformat()
            if classification == "productive":
                tracker_state["last_productive_ts"] = now_dt.isoformat()

            cls_since_str = tracker_state["classification_since"]
            is_active = tracker_state["active"]
            on_break_now = tracker_state["on_break"]
            current_source = tracker_state["source"]

        # Compute classification duration (no lock needed — only uses local copy)
        classification_dur = 0
        if cls_since_str:
            try:
                classification_dur = int(
                    (now_dt - datetime.fromisoformat(cls_since_str)).total_seconds()
                )
            except Exception:
                classification_dur = 0

        # --- Daily goal check (under lock for state read/write) ---
        with _state_lock:
            if is_active and not goal_notified:
                work = _get_today_work_sec()
                if work >= config["daily_goal"]:
                    hours = config["daily_goal"] // 3600
                    send_notification(
                        "Tagesziel erreicht!",
                        f"Du hast heute {hours}h produktiv gearbeitet. Stark!",
                        "Glass"
                    )
                    goal_notified = True
            if now_dt.hour == 0 and now_dt.minute == 0:
                goal_notified = False

        # --- Offline sessions: skip all computer-based checks ---
        if is_active and current_source == "offline":
            continue

        # --- Persist activity sample (only while session active, for stats) ---
        if is_active and app_name:
            try:
                conn_l = sqlite3.connect(DB_PATH)
                sid = _get_active_session_id(conn_l)
                conn_l.close()
                with _state_lock:
                    in_brk = tracker_state["on_break"]
                log_activity(app_name, url, classification, IDLE_CHECK_INTERVAL, sid, in_brk)
            except Exception:
                pass

        # --- Start-Reminder when tracker is off ---
        if not is_active:
            with _state_lock:
                last_remind = tracker_state["last_start_reminder"]
            if (classification == "productive"
                    and classification_dur >= config["productive_grace_sec"]
                    and now - last_remind >= config["start_reminder_cooldown"]):
                app_hint = app_name or "Arbeits-App"
                send_notification(
                    "Tracker ist aus",
                    f"Du arbeitest in {app_hint} — Session starten?",
                    "Ping"
                )
                with _state_lock:
                    tracker_state["last_start_reminder"] = now
            continue

        # --- Auto-Stop: session crossed midnight (HIDIdleTime resets on sleep, so idle alone is unreliable) ---
        with _state_lock:
            _s_start = tracker_state["session_start"]
        if _s_start:
            try:
                _s_start_dt = datetime.fromisoformat(_s_start)
                if _s_start_dt.date() < now_dt.date():
                    _midnight = datetime.combine(now_dt.date(), datetime.min.time())
                    with _state_lock:
                        if tracker_state["active"]:
                            _auto_stop_session("Mitternacht ueberschritten", _midnight)
                    continue
            except Exception:
                pass

        # --- Auto-Stop: idle too long (adaptive day/night threshold) ---
        night = _is_night_hour(now_dt.hour)
        stop_threshold = config["auto_stop_idle_night"] if night else config["auto_stop_idle_day"]
        if idle_sec >= stop_threshold:
            last_activity = now_dt - timedelta(seconds=idle_sec)
            reason = f"{int(stop_threshold // 60)} Min inaktiv" + (" (Nachts)" if night else "")
            with _state_lock:
                if tracker_state["active"]:  # re-check under lock
                    _auto_stop_session(reason, last_activity)
            continue

        # --- Pomodoro phase transitions ---
        with _state_lock:
            if tracker_state["pomodoro_enabled"] and tracker_state["pomodoro_phase_start"]:
                try:
                    phase_start_dt = datetime.fromisoformat(tracker_state["pomodoro_phase_start"])
                except Exception:
                    phase_start_dt = now_dt
                phase_elapsed = int((now_dt - phase_start_dt).total_seconds())

                if tracker_state["pomodoro_phase"] == "focus":
                    if phase_elapsed >= config["pomodoro_focus"] and not tracker_state["pomodoro_notified"]:
                        tracker_state["pomodoro_count"] += 1
                        tracker_state["pomodoro_notified"] = True
                        pomo_count = tracker_state["pomodoro_count"]
                        pomo_break_min = config["pomodoro_break"] // 60
                        send_notification(
                            f"Fokus-Block #{pomo_count} fertig!",
                            f"Zeit fuer {pomo_break_min} Min Pause. Steh auf, beweg dich!",
                            "Hero"
                        )
                        tracker_state["pomodoro_phase"] = "break"
                        tracker_state["pomodoro_phase_start"] = now_dt.isoformat()
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
                        tracker_state["pomodoro_phase_start"] = now_dt.isoformat()
                        tracker_state["pomodoro_notified"] = False

        # --- Auto-Break: trigger at break_threshold ---
        with _state_lock:
            if idle_sec >= config["break_threshold"] and not tracker_state["on_break"]:
                tracker_state["on_break"] = True
                tracker_state["paused_by_idle"] = True
                tracker_state["paused_by_distraction"] = False
                break_start_dt = now_dt - timedelta(seconds=idle_sec)
                tracker_state["break_start"] = break_start_dt.isoformat()
                tracker_state["idle_since"] = break_start_dt.isoformat()

                conn = sqlite3.connect(DB_PATH)
                session_id = _get_active_session_id(conn)
                conn.execute(
                    "INSERT INTO breaks (start_time, session_id, auto) VALUES (?, ?, 1)",
                    (break_start_dt.isoformat(), session_id)
                )
                conn.commit()
                conn.close()

                send_notification("Pause gestartet", "Du bist seit 2 Min inaktiv — Timer ist pausiert.")
                tracker_state["last_notification"] = now

        # --- Distraction-Break: tracker läuft, User aber in Blocklist-App ---
        _should_close_tabs = False
        with _state_lock:
            if (classification == "distraction"
                    and classification_dur >= config["distraction_grace_sec"]
                    and not tracker_state["on_break"]):
                tracker_state["on_break"] = True
                tracker_state["paused_by_distraction"] = True
                tracker_state["paused_by_idle"] = False
                try:
                    break_start_dt = datetime.fromisoformat(tracker_state["classification_since"])
                except Exception:
                    break_start_dt = now_dt
                tracker_state["break_start"] = break_start_dt.isoformat()
                tracker_state["idle_since"] = break_start_dt.isoformat()

                conn = sqlite3.connect(DB_PATH)
                session_id = _get_active_session_id(conn)
                conn.execute(
                    "INSERT INTO breaks (start_time, session_id, auto) VALUES (?, ?, 1)",
                    (break_start_dt.isoformat(), session_id)
                )
                conn.commit()
                conn.close()

                what = app_name or "Ablenkung"
                send_notification(
                    "Ablenkung erkannt",
                    f"Timer pausiert — du bist in {what}. Zurueck zur Arbeit!",
                    "Basso"
                )
                tracker_state["last_notification"] = now
                _should_close_tabs = config.get("auto_close_tabs", True)

        if _should_close_tabs:
            close_blocklist_tabs()
            _should_close_tabs = False

        # --- Escalating reminders during break ---
        notify_args = None
        with _state_lock:
            if tracker_state["on_break"] and tracker_state["break_start"]:
                try:
                    brk_start_dt = datetime.fromisoformat(tracker_state["break_start"])
                except Exception:
                    brk_start_dt = now_dt
                break_dur = int((now_dt - brk_start_dt).total_seconds())

                if now - tracker_state["last_notification"] > config["notification_cooldown"]:
                    minutes = break_dur // 60
                    if break_dur >= 600:
                        notify_args = ("Ist alles okay?", f"Du bist seit {minutes} Min weg. Das war keine kurze Pause mehr.", "Sosumi")
                    elif break_dur >= 300:
                        notify_args = ("Du scrollst schon wieder...", f"Seit {minutes} Min inaktiv. Handy weg, zurueck an die Arbeit!", "Basso")
                    elif idle_sec >= config["idle_threshold"]:
                        notify_args = ("Hey, bist du noch da?", f"Pause laeuft seit {minutes} Min — zurueck an die Arbeit!", None)
                    if notify_args:
                        tracker_state["last_notification"] = now

        if notify_args:
            title, msg, sound = notify_args
            if sound:
                send_notification(title, msg, sound)
            else:
                send_notification(title, msg)

        # --- User came back ---
        # Idle-break ends on keyboard/mouse activity.
        # Distraction-break ends when classification is no longer 'distraction' AND user is active.
        came_back = False
        break_start_str_cb = None
        with _state_lock:
            if tracker_state["on_break"]:
                if tracker_state["paused_by_distraction"]:
                    came_back = classification != "distraction" and idle_sec < 30
                else:
                    came_back = idle_sec < 30
                if came_back:
                    break_start_str_cb = tracker_state["break_start"]

        if came_back and break_start_str_cb:
            try:
                brk_dt = datetime.fromisoformat(break_start_str_cb)
            except Exception:
                brk_dt = now_dt
            break_duration = int((now_dt - brk_dt).total_seconds())

            conn = sqlite3.connect(DB_PATH)
            session_id = _get_active_session_id(conn)
            conn.execute(
                "UPDATE breaks SET end_time = ?, duration_sec = ? WHERE end_time IS NULL AND session_id = ?",
                (now_dt.isoformat(), break_duration, session_id)
            )
            conn.execute(
                "INSERT INTO idle_events (timestamp, duration_sec, session_id) VALUES (?, ?, ?)",
                (break_start_str_cb, break_duration, session_id)
            )
            if session_id:
                conn.execute(
                    "UPDATE sessions SET idle_sec = idle_sec + ? WHERE id = ?",
                    (break_duration, session_id)
                )
            conn.commit()
            conn.close()

            with _state_lock:
                tracker_state["on_break"] = False
                tracker_state["paused_by_idle"] = False
                tracker_state["paused_by_distraction"] = False
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
    with _state_lock:
        if tracker_state["active"]:
            return jsonify({"error": "Already tracking"}), 400

    now = datetime.now()
    project_id = None
    source = "live"
    note = ""
    body = request.json or {}
    if body.get("project_id") is not None:
        try:
            project_id = int(body["project_id"])
        except (TypeError, ValueError):
            project_id = None
    if body.get("source") in ("live", "offline"):
        source = body["source"]
    if isinstance(body.get("note"), str):
        note = body["note"].strip()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO sessions (start_time, project_id, source, note) VALUES (?, ?, ?, ?)",
        (now.isoformat(), project_id, source, note)
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()

    with _state_lock:
        tracker_state["active"] = True
        tracker_state["session_start"] = now.isoformat()
        tracker_state["source"] = source
        tracker_state["paused_by_idle"] = False
        tracker_state["paused_by_distraction"] = False
        tracker_state["idle_since"] = None
        tracker_state["on_break"] = False
        tracker_state["break_start"] = None
        tracker_state["active_project_id"] = project_id

        # Pomodoro only for live sessions
        if source == "live" and tracker_state["pomodoro_enabled"]:
            tracker_state["pomodoro_phase"] = "focus"
            tracker_state["pomodoro_phase_start"] = now.isoformat()
            tracker_state["pomodoro_notified"] = False
        else:
            tracker_state["pomodoro_phase"] = None
            tracker_state["pomodoro_phase_start"] = None

    # DND only for live sessions (offline = you're away, computer can ring)
    if source == "live":
        set_dnd(True)

    return jsonify({"session_id": session_id, "start_time": now.isoformat(), "source": source})


@app.route("/api/stop", methods=["POST"])
def stop_session():
    with _state_lock:
        if not tracker_state["active"]:
            return jsonify({"error": "Not tracking"}), 400
        now = datetime.now()
        start = datetime.fromisoformat(tracker_state["session_start"])
        duration = int((now - start).total_seconds())

    note = request.json.get("note", "") if request.json else ""

    conn = sqlite3.connect(DB_PATH)
    active_sid = _get_active_session_id(conn)
    conn.execute(
        "UPDATE sessions SET end_time = ?, duration_sec = ?, note = ? WHERE id = ? AND end_time IS NULL",
        (now.isoformat(), duration, note, active_sid)
    )
    # Close any open breaks for this session — compute actual duration from start_time
    conn.execute(
        "UPDATE breaks SET end_time = ?, "
        "duration_sec = MAX(0, CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER)) "
        "WHERE end_time IS NULL AND session_id = ?",
        (now.isoformat(), now.isoformat(), active_sid)
    )
    conn.commit()
    conn.close()

    with _state_lock:
        tracker_state["active"] = False
        tracker_state["session_start"] = None
        tracker_state["source"] = "live"
        tracker_state["paused_by_idle"] = False
        tracker_state["paused_by_distraction"] = False
        tracker_state["on_break"] = False
        tracker_state["pomodoro_phase"] = None
        tracker_state["pomodoro_phase_start"] = None
        tracker_state["active_project_id"] = None
        # Prevent start-reminder from firing immediately after manual stop
        tracker_state["last_start_reminder"] = time.time()

    # Disable DND (outside lock — slow I/O)
    set_dnd(False)

    return jsonify({"duration_sec": duration})


@app.route("/api/sessions/manual", methods=["POST"])
def sessions_manual():
    """Create a retroactive session entry (offline / forgotten tracking).
    Does NOT touch tracker_state — independent of live tracking.
    These sessions do NOT count toward the focus score.
    """
    data = request.json or {}
    start_raw = (data.get("start_time") or "").strip()
    end_raw = (data.get("end_time") or "").strip()
    if not start_raw or not end_raw:
        return jsonify({"error": "start_time und end_time erforderlich (ISO oder YYYY-MM-DDTHH:MM)"}), 400
    try:
        start_dt = datetime.fromisoformat(start_raw)
        end_dt = datetime.fromisoformat(end_raw)
    except ValueError:
        return jsonify({"error": "Ungueltiges Datumsformat"}), 400
    if end_dt <= start_dt:
        return jsonify({"error": "end_time muss nach start_time liegen"}), 400
    duration = int((end_dt - start_dt).total_seconds())
    if duration > 24 * 3600:
        return jsonify({"error": "Session darf maximal 24h dauern"}), 400

    project_id = None
    if data.get("project_id") is not None:
        try:
            project_id = int(data["project_id"])
        except (TypeError, ValueError):
            project_id = None
    note = (data.get("note") or "").strip()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute(
        "INSERT INTO sessions (start_time, end_time, duration_sec, idle_sec, note, project_id, source) "
        "VALUES (?, ?, ?, 0, ?, ?, 'manual')",
        (start_dt.isoformat(), end_dt.isoformat(), duration, note, project_id)
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()
    return jsonify({
        "session_id": session_id,
        "duration_sec": duration,
        "source": "manual",
    })


@app.route("/api/pause", methods=["POST"])
def toggle_pause():
    """Manual pause/resume — user-initiated, independent of auto-break."""
    with _state_lock:
        if not tracker_state["active"]:
            return jsonify({"error": "Not tracking"}), 400
        currently_on_break = tracker_state["on_break"]
        break_start_str = tracker_state["break_start"]

    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    session_id = _get_active_session_id(conn)

    if currently_on_break:
        # Resume: close current break, add duration to idle_sec
        if break_start_str:
            bs = datetime.fromisoformat(break_start_str)
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
        conn.close()
        with _state_lock:
            tracker_state["on_break"] = False
            tracker_state["paused_by_idle"] = False
            tracker_state["paused_by_distraction"] = False
            tracker_state["break_start"] = None
            tracker_state["idle_since"] = None
        return jsonify({"paused": False})
    else:
        # Start manual pause
        conn.execute(
            "INSERT INTO breaks (start_time, session_id, auto) VALUES (?, ?, 0)",
            (now.isoformat(), session_id)
        )
        conn.commit()
        conn.close()
        with _state_lock:
            tracker_state["on_break"] = True
            tracker_state["paused_by_idle"] = False
            tracker_state["paused_by_distraction"] = False
            tracker_state["break_start"] = now.isoformat()
            tracker_state["idle_since"] = now.isoformat()
        return jsonify({"paused": True})


@app.route("/api/status")
def status():
    now_dt = datetime.now()
    idle_sec = get_idle_time_sec()  # slow I/O, outside lock

    with _state_lock:
        active = tracker_state["active"]
        session_start_str = tracker_state["session_start"]
        on_break = tracker_state["on_break"]
        break_start_str = tracker_state["break_start"]
        pomo_enabled = tracker_state["pomodoro_enabled"]
        pomo_phase = tracker_state["pomodoro_phase"]
        pomo_phase_start_str = tracker_state["pomodoro_phase_start"]
        pomo_count = tracker_state["pomodoro_count"]
        active_project_id = tracker_state["active_project_id"]
        current_app = tracker_state["current_app"]
        current_url = tracker_state["current_url"]
        current_cls = tracker_state["current_classification"]
        paused_by_idle = tracker_state["paused_by_idle"]
        paused_by_distraction = tracker_state["paused_by_distraction"]
        source = tracker_state["source"]

    elapsed = 0
    if active and session_start_str:
        start = datetime.fromisoformat(session_start_str)
        elapsed = int((now_dt - start).total_seconds())

    break_sec = 0
    if on_break and break_start_str:
        bs = datetime.fromisoformat(break_start_str)
        break_sec = int((now_dt - bs).total_seconds())

    # Pomodoro phase info
    pomo_remaining = 0
    pomo_phase_total = 0
    if pomo_enabled and pomo_phase_start_str:
        phase_start = datetime.fromisoformat(pomo_phase_start_str)
        phase_elapsed = int((now_dt - phase_start).total_seconds())
        pomo_phase_total = config["pomodoro_focus"] if pomo_phase == "focus" else config["pomodoro_break"]
        pomo_remaining = max(0, pomo_phase_total - phase_elapsed)

    return jsonify({
        "active": active,
        "session_start": session_start_str,
        "elapsed_sec": elapsed,
        "idle_sec": round(idle_sec, 1),
        "paused_by_idle": paused_by_idle,
        "paused_by_distraction": paused_by_distraction,
        "on_break": on_break,
        "break_sec": break_sec,
        "break_threshold": config["break_threshold"],
        "pomodoro_enabled": pomo_enabled,
        "pomodoro_phase": pomo_phase,
        "pomodoro_remaining": pomo_remaining,
        "pomodoro_phase_total": pomo_phase_total,
        "pomodoro_count": pomo_count,
        "active_project_id": active_project_id,
        "current_app": current_app,
        "current_url": current_url,
        "current_classification": current_cls,
        "source": source,
    })


def _classify_segments(session_start, session_end, session_breaks):
    """Split work into segments around breaks; classify each by length.

    Segment >= FOCUS_MIN_SEGMENT = focused, sonst scattered.
    Liefert (focused_sec, scattered_sec).
    """
    if session_end <= session_start:
        return 0, 0
    breaks = sorted(
        [(bs, be) for bs, be in session_breaks
         if be is not None and (be - bs).total_seconds() > BREAK_GLUE_SEC],
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
        "SELECT id, start_time, end_time, duration_sec, idle_sec, note, project_id, "
        "COALESCE(source, 'live') AS source FROM sessions WHERE start_time LIKE ?",
        (f"{today}%",)
    ).fetchall()

    def _is_live(s):
        return (s[7] if len(s) > 7 else "live") == "live"

    live_sessions = [s for s in sessions if _is_live(s)]
    offline_sessions = [s for s in sessions if not _is_live(s)]

    live_work = sum(s[3] - s[4] for s in live_sessions if s[3])
    offline_work = sum(s[3] - s[4] for s in offline_sessions if s[3])
    total_work = live_work + offline_work
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
        if not _is_live(s):
            continue  # offline/manual sessions bypass focus classification
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
            active_source = "live"
            if active_id:
                row = conn.execute(
                    "SELECT COALESCE(idle_sec, 0), COALESCE(source, 'live') FROM sessions WHERE id = ?",
                    (active_id,)
                ).fetchone()
                if row:
                    active_idle = row[0]
                    active_source = row[1]
            active_net = max(0, current - active_idle)
            total_raw += current
            total_work += active_net
            total_idle += active_idle
            if active_source == "live":
                live_work += active_net
                # Classify active session by segments
                f, sc = _classify_segments(start, datetime.now(), breaks_by_session.get(active_id, []))
                focused_work += f
                scattered_work += sc
            else:
                offline_work += active_net

    total_break = sum(b[3] for b in breaks if b[3])
    total_auto_break = sum(b[3] for b in breaks if b[3] and b[5] == 1)
    total_manual_break = sum(b[3] for b in breaks if b[3] and b[5] == 0)

    # Longest focus stretch today (longest live session net duration)
    longest_row = conn.execute(
        "SELECT MAX(duration_sec - idle_sec) FROM sessions "
        "WHERE start_time LIKE ? AND end_time IS NOT NULL AND COALESCE(source,'live')='live'",
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
    focus_denom = live_work + long_breaks
    focus_score = round((live_work / focus_denom * 100) if focus_denom > 0 else 100)

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
        "live_work_sec": live_work,
        "offline_work_sec": offline_work,
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
            "work_sec": live_work,
            "offline_work_sec": offline_work,
            "long_break_sec": long_breaks,
            "long_break_count": long_break_count,
            "short_break_sec": short_break_sec,
            "short_break_count": short_break_count,
            "manual_break_sec": manual_break_sec,
            "manual_break_count": manual_break_count,
            "formula": "Live-Arbeit / (Live-Arbeit + Auto-Ablenkungen >3min). Offline/Nachtrag, manuelle Pausen zaehlen nicht.",
        },
        "daily_goal": config["daily_goal"],
        "goal_pct": goal_pct,
        "sessions": [
            {"id": s[0], "start": s[1], "end": s[2], "duration_sec": s[3], "idle_sec": s[4], "note": s[5], "project_id": s[6], "source": s[7] if len(s) > 7 else "live"}
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


def _iso_week_bounds(offset_weeks=0):
    """Return (monday_date, sunday_date) for the week that is offset_weeks from now.
    offset_weeks=0 = current week, -1 = last week, 1 = next week."""
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    monday = monday + timedelta(weeks=offset_weeks)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _day_breakdown(conn, day_str):
    """Return dict with focused_sec, scattered_sec, idle_sec, manual_break_sec, sessions,
    focus_score for one day."""
    sessions = conn.execute(
        "SELECT id, start_time, end_time, duration_sec, idle_sec, note, project_id, "
        "COALESCE(source,'live') AS source "
        "FROM sessions WHERE start_time LIKE ? AND end_time IS NOT NULL",
        (f"{day_str}%",)
    ).fetchall()
    breaks = conn.execute(
        "SELECT id, start_time, end_time, duration_sec, session_id, auto "
        "FROM breaks WHERE start_time LIKE ?",
        (f"{day_str}%",)
    ).fetchall()

    breaks_by_session = {}
    for b in breaks:
        sid = b[4]
        if sid is None:
            continue
        b_start = datetime.fromisoformat(b[1])
        b_end = datetime.fromisoformat(b[2]) if b[2] else None
        breaks_by_session.setdefault(sid, []).append((b_start, b_end))

    focused = scattered = 0
    for s in sessions:
        if s[7] != "live":
            continue
        s_start = datetime.fromisoformat(s[1])
        s_end = datetime.fromisoformat(s[2])
        f, sc = _classify_segments(s_start, s_end, breaks_by_session.get(s[0], []))
        focused += f
        scattered += sc

    live_work = sum((s[3] or 0) - (s[4] or 0) for s in sessions if s[7] == "live")
    offline_work = sum((s[3] or 0) - (s[4] or 0) for s in sessions if s[7] != "live")
    total_work = live_work + offline_work
    total_idle = sum(s[4] or 0 for s in sessions)
    auto_break = sum(b[3] or 0 for b in breaks if b[5] == 1)
    manual_break = sum(b[3] or 0 for b in breaks if b[5] == 0)

    long_auto = sum(b[3] or 0 for b in breaks if b[5] == 1 and (b[3] or 0) > 180)
    denom = live_work + long_auto
    focus_score = round((live_work / denom * 100) if denom > 0 else (100 if live_work > 0 else 0))

    return {
        "date": day_str,
        "focused_sec": focused,
        "scattered_sec": scattered,
        "work_sec": total_work,
        "live_work_sec": live_work,
        "offline_work_sec": offline_work,
        "idle_sec": total_idle,
        "auto_break_sec": auto_break,
        "manual_break_sec": manual_break,
        "session_count": len(sessions),
        "focus_score": focus_score,
    }


@app.route("/api/stats/week")
def stats_week():
    """Week overview with navigation via ?offset=0 (current), -1 (last), etc."""
    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    monday, sunday = _iso_week_bounds(offset)
    conn = sqlite3.connect(DB_PATH)
    days = []
    total_work = 0
    total_focused = 0
    for i in range(7):
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        bd = _day_breakdown(conn, ds)
        bd["label"] = d.strftime("%a")
        bd["weekday"] = i
        days.append(bd)
        total_work += bd["work_sec"]
        total_focused += bd["focused_sec"]

    # Previous week total for comparison
    prev_monday = monday - timedelta(weeks=1)
    prev_sunday = sunday - timedelta(weeks=1)
    prev_work = 0
    for i in range(7):
        d = prev_monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        r = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions "
            "WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{ds}%",)
        ).fetchone()
        prev_work += r[0] or 0

    # Best day + avg focus score (only days with work)
    active_days = [x for x in days if x["work_sec"] > 0]
    best_day = max(active_days, key=lambda x: x["work_sec"]) if active_days else None
    avg_score = round(sum(x["focus_score"] for x in active_days) / len(active_days)) if active_days else 0

    # Top apps + domains across the whole week (only session time, excluding breaks)
    week_start = monday.isoformat() + "T00:00:00"
    week_end = (sunday + timedelta(days=1)).isoformat() + "T00:00:00"
    apps = conn.execute(
        "SELECT app, "
        "  (SELECT classification FROM activity_log a2 WHERE a2.app = a1.app AND a2.ts >= ? AND a2.ts < ? "
        "   GROUP BY classification ORDER BY SUM(duration_sec) DESC LIMIT 1) as cls, "
        "  SUM(duration_sec) FROM activity_log a1 "
        "WHERE ts >= ? AND ts < ? AND in_break = 0 AND app IS NOT NULL "
        "GROUP BY app ORDER BY 3 DESC LIMIT 10",
        (week_start, week_end, week_start, week_end)
    ).fetchall()
    domains = conn.execute(
        "SELECT domain, classification, SUM(duration_sec) FROM activity_log "
        "WHERE ts >= ? AND ts < ? AND in_break = 0 AND domain IS NOT NULL "
        "GROUP BY domain ORDER BY 3 DESC LIMIT 10",
        (week_start, week_end)
    ).fetchall()
    conn.close()

    return jsonify({
        "offset": offset,
        "week_start": monday.strftime("%Y-%m-%d"),
        "week_end": sunday.strftime("%Y-%m-%d"),
        "label": f"{monday.strftime('%d.%m.')} – {sunday.strftime('%d.%m.%Y')}",
        "days": days,
        "total_work_sec": total_work,
        "total_focused_sec": total_focused,
        "prev_week_work_sec": prev_work,
        "diff_sec": total_work - prev_work,
        "diff_pct": round((total_work - prev_work) / prev_work * 100) if prev_work > 0 else 0,
        "avg_focus_score": avg_score,
        "active_days": len(active_days),
        "best_day": {"date": best_day["date"], "label": best_day["label"], "work_sec": best_day["work_sec"]} if best_day else None,
        "top_apps": [{"name": a[0], "classification": a[1], "sec": a[2]} for a in apps],
        "top_domains": [{"name": d[0], "classification": d[1], "sec": d[2]} for d in domains],
    })


@app.route("/api/stats/day/<date>")
def stats_day(date):
    """Detailed breakdown for a single day (YYYY-MM-DD)."""
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    day_str = day.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    bd = _day_breakdown(conn, day_str)

    # Sessions + project names
    sessions = conn.execute(
        "SELECT s.id, s.start_time, s.end_time, s.duration_sec, s.idle_sec, s.note, "
        "s.project_id, p.name, p.color "
        "FROM sessions s LEFT JOIN projects p ON p.id = s.project_id "
        "WHERE s.start_time LIKE ? ORDER BY s.start_time",
        (f"{day_str}%",)
    ).fetchall()

    # Hour-by-hour heatmap (0-23) using sessions + breaks
    breaks = conn.execute(
        "SELECT start_time, end_time, auto FROM breaks WHERE start_time LIKE ?",
        (f"{day_str}%",)
    ).fetchall()
    hours = []
    for h in range(24):
        h_begin = datetime.fromisoformat(f"{day_str}T{h:02d}:00:00")
        h_end = h_begin + timedelta(hours=1)
        work = 0
        brk = 0
        for s in sessions:
            if not s[2]:
                continue
            s_start = datetime.fromisoformat(s[1])
            s_end = datetime.fromisoformat(s[2])
            os_, oe = max(s_start, h_begin), min(s_end, h_end)
            if os_ < oe:
                work += (oe - os_).total_seconds()
        for b in breaks:
            if not b[1]:
                continue
            b_start = datetime.fromisoformat(b[0])
            b_end = datetime.fromisoformat(b[1]) if b[1] else h_end
            os_, oe = max(b_start, h_begin), min(b_end, h_end)
            if os_ < oe:
                brk += (oe - os_).total_seconds()
        work = max(0, work - brk)
        hours.append({"hour": h, "work": int(work), "break": int(brk)})

    # Top apps + domains for this day
    day_start = day_str + "T00:00:00"
    day_end = (day + timedelta(days=1)).strftime("%Y-%m-%d") + "T00:00:00"
    apps = conn.execute(
        "SELECT app, classification, SUM(duration_sec) FROM activity_log "
        "WHERE ts >= ? AND ts < ? AND in_break = 0 AND app IS NOT NULL "
        "GROUP BY app ORDER BY 3 DESC LIMIT 15",
        (day_start, day_end)
    ).fetchall()
    domains = conn.execute(
        "SELECT domain, classification, SUM(duration_sec) FROM activity_log "
        "WHERE ts >= ? AND ts < ? AND in_break = 0 AND domain IS NOT NULL "
        "GROUP BY domain ORDER BY 3 DESC LIMIT 15",
        (day_start, day_end)
    ).fetchall()

    total_activity = sum(a[2] for a in apps) or 1
    total_dom = sum(d[2] for d in domains) or 1

    # Longest focus stretch of the day
    longest = 0
    for s in sessions:
        if s[2] and s[3] and s[4] is not None:
            net = (s[3] or 0) - (s[4] or 0)
            if net > longest:
                longest = net

    conn.close()

    return jsonify({
        **bd,
        "longest_focus_sec": longest,
        "sessions": [
            {
                "id": s[0], "start": s[1], "end": s[2],
                "duration_sec": s[3], "idle_sec": s[4], "note": s[5],
                "project_id": s[6], "project_name": s[7], "project_color": s[8],
            } for s in sessions
        ],
        "hours": hours,
        "top_apps": [
            {"name": a[0], "classification": a[1], "sec": a[2], "pct": round(a[2] / total_activity * 100, 1)}
            for a in apps
        ],
        "top_domains": [
            {"name": d[0], "classification": d[1], "sec": d[2], "pct": round(d[2] / total_dom * 100, 1)}
            for d in domains
        ],
    })


def _week_key(d):
    """ISO year-week key (YYYY-Www) for a date."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@app.route("/api/commitment", methods=["GET", "POST"])
def commitment_route():
    """Get or set today+this-week commitment.
    GET returns both; POST body: {type:'day'|'week', hours:<number>}"""
    today = datetime.now().date()
    day_key = today.strftime("%Y-%m-%d")
    wk_key = _week_key(today)
    conn = sqlite3.connect(DB_PATH)

    if request.method == "POST":
        data = request.json or {}
        ptype = data.get("type")
        if ptype not in ("day", "week"):
            conn.close()
            return jsonify({"error": "invalid type"}), 400
        try:
            hours = float(data.get("hours", 0))
        except (TypeError, ValueError):
            hours = 0
        target_sec = max(0, int(hours * 3600))
        key = day_key if ptype == "day" else wk_key
        if target_sec == 0:
            conn.execute("DELETE FROM commitments WHERE period_type = ? AND period_key = ?", (ptype, key))
        else:
            conn.execute(
                "INSERT INTO commitments (period_type, period_key, target_sec, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(period_type, period_key) DO UPDATE SET target_sec = excluded.target_sec",
                (ptype, key, target_sec, datetime.now().isoformat())
            )
        conn.commit()

    def load(ptype, key):
        r = conn.execute(
            "SELECT target_sec, created_at FROM commitments WHERE period_type = ? AND period_key = ?",
            (ptype, key)
        ).fetchone()
        return {"target_sec": r[0], "created_at": r[1]} if r else None

    day_c = load("day", day_key)
    week_c = load("week", wk_key)

    # Progress (actual work_sec)
    day_work = conn.execute(
        "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions "
        "WHERE start_time LIKE ? AND end_time IS NOT NULL",
        (f"{day_key}%",)
    ).fetchone()[0]
    # Add active session (net of already-booked idle)
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if start.strftime("%Y-%m-%d") == day_key:
            elapsed = int((datetime.now() - start).total_seconds())
            active_id = _get_active_session_id(conn)
            active_idle = 0
            if active_id:
                r = conn.execute("SELECT COALESCE(idle_sec, 0) FROM sessions WHERE id = ?", (active_id,)).fetchone()
                active_idle = r[0] if r else 0
            day_work += max(0, elapsed - active_idle)

    monday = today - timedelta(days=today.weekday())
    week_work = 0
    for i in range(7):
        d = monday + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        r = conn.execute(
            "SELECT COALESCE(SUM(duration_sec - idle_sec), 0) FROM sessions "
            "WHERE start_time LIKE ? AND end_time IS NOT NULL",
            (f"{ds}%",)
        ).fetchone()
        week_work += r[0] or 0
    if tracker_state["active"] and tracker_state["session_start"]:
        start = datetime.fromisoformat(tracker_state["session_start"])
        if monday <= start.date() <= monday + timedelta(days=6):
            week_work += int((datetime.now() - start).total_seconds())

    conn.close()
    return jsonify({
        "day": {
            "key": day_key,
            "target_sec": day_c["target_sec"] if day_c else None,
            "work_sec": day_work,
        },
        "week": {
            "key": wk_key,
            "target_sec": week_c["target_sec"] if week_c else None,
            "work_sec": week_work,
        },
    })


@app.route("/api/stats/focus-hourly")
def stats_focus_hourly():
    """Focus score per time-bucket (default 15 min) for a day.
    score = work / (work + long_auto_break) per bucket."""
    date = request.args.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "invalid date"}), 400
    # bucket_min: 5, 10, 15, 30, 60 — default 15
    try:
        bucket_min = int(request.args.get("bucket_min", 15))
    except (TypeError, ValueError):
        bucket_min = 15
    if bucket_min not in (5, 10, 15, 30, 60):
        bucket_min = 15
    bucket_sec = bucket_min * 60
    buckets_per_day = (24 * 60) // bucket_min

    day_str = day.strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    sessions = conn.execute(
        "SELECT id, start_time, end_time FROM sessions WHERE start_time LIKE ?",
        (f"{day_str}%",)
    ).fetchall()
    long_breaks = conn.execute(
        "SELECT start_time, end_time FROM breaks "
        "WHERE start_time LIKE ? AND auto = 1 AND duration_sec > 180",
        (f"{day_str}%",)
    ).fetchall()
    all_breaks = conn.execute(
        "SELECT start_time, end_time FROM breaks WHERE start_time LIKE ?",
        (f"{day_str}%",)
    ).fetchall()
    now = datetime.now()

    # Pre-parse intervals once
    s_iv = [(datetime.fromisoformat(s[1]), datetime.fromisoformat(s[2]) if s[2] else now) for s in sessions]
    all_iv = [(datetime.fromisoformat(b[0]), datetime.fromisoformat(b[1]) if b[1] else now) for b in all_breaks]
    long_iv = [(datetime.fromisoformat(b[0]), datetime.fromisoformat(b[1]) if b[1] else now) for b in long_breaks]
    day_begin = datetime.fromisoformat(f"{day_str}T00:00:00")

    buckets = []
    # Use short threshold — need >= 30s activity to count a bucket as valid
    MIN_ACTIVE = 30
    for i in range(buckets_per_day):
        b_begin = day_begin + timedelta(seconds=i * bucket_sec)
        b_end = b_begin + timedelta(seconds=bucket_sec)

        raw_sec = 0
        for s_start, s_end in s_iv:
            os_, oe = max(s_start, b_begin), min(s_end, b_end)
            if os_ < oe:
                raw_sec += (oe - os_).total_seconds()

        brk_all = 0
        for b_start, b_end2 in all_iv:
            os_, oe = max(b_start, b_begin), min(b_end2, b_end)
            if os_ < oe:
                brk_all += (oe - os_).total_seconds()

        long_brk = 0
        for b_start, b_end2 in long_iv:
            os_, oe = max(b_start, b_begin), min(b_end2, b_end)
            if os_ < oe:
                long_brk += (oe - os_).total_seconds()

        work = max(0, raw_sec - brk_all)
        denom = work + long_brk
        score = round(work / denom * 100) if denom > 0 else None
        if work + long_brk < MIN_ACTIVE:
            score = None

        # Minutes since midnight (for rendering labels)
        minute_of_day = i * bucket_min
        buckets.append({
            "minute": minute_of_day,
            "hour": minute_of_day // 60,
            "work_sec": int(work),
            "focus_score": score,
        })

    conn.close()
    return jsonify({"date": day_str, "bucket_min": bucket_min, "buckets": buckets})


@app.route("/api/stats/focus-trend")
def stats_focus_trend():
    """Daily focus score for the last N days (default 30). Days without work get null."""
    try:
        days = max(7, min(180, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    conn = sqlite3.connect(DB_PATH)
    out = []
    for i in range(days - 1, -1, -1):
        d = (datetime.now().date() - timedelta(days=i))
        bd = _day_breakdown(conn, d.strftime("%Y-%m-%d"))
        out.append({
            "date": bd["date"],
            "label": d.strftime("%d.%m."),
            "weekday": d.strftime("%a"),
            "focus_score": bd["focus_score"] if bd["work_sec"] > 0 else None,
            "work_sec": bd["work_sec"],
        })
    conn.close()
    return jsonify({"days": out})


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

    # Today's work for achievements (total incl. offline for goal/hour achievements)
    today_work = _get_today_work_sec()
    today_live_work = _get_today_work_sec(live_only=True)
    today = datetime.now().strftime("%Y-%m-%d")
    today_focus_total = today_work
    today_breaks = conn.execute(
        "SELECT COALESCE(SUM(duration_sec), 0) FROM breaks WHERE start_time LIKE ?",
        (f"{today}%",)
    ).fetchone()
    today_break_sec = today_breaks[0] if today_breaks else 0
    today_focus_denom = today_live_work + today_break_sec
    today_focus_score = round((today_live_work / today_focus_denom * 100) if today_focus_denom > 0 else 100)

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
        color = data.get("color") or "#14b8a6"
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
                if isinstance(DEFAULT_CONFIG[key], bool):
                    config[key] = bool(data[key])
                else:
                    config[key] = int(data[key])
        save_config_to_db()
        return jsonify({"ok": True})
    return jsonify(config)


@app.route("/api/ai-config", methods=["GET", "POST"])
def ai_config_route():
    """Get/set AI planner provider + keys. Keys are write-only (never returned)."""
    conn = sqlite3.connect(DB_PATH)
    if request.method == "POST":
        data = request.json or {}
        updates: list[tuple[str, str]] = []
        if "ai_provider" in data:
            val = (data["ai_provider"] or "auto").strip().lower()
            if val not in ("auto", "groq", "anthropic"):
                conn.close()
                return jsonify({"error": "ai_provider must be auto|groq|anthropic"}), 400
            updates.append(("ai_provider", val))
        for field, key in (("groq_api_key", "groq_api_key"), ("anthropic_api_key", "anthropic_api_key")):
            if field in data:
                val = (data[field] or "").strip()
                if val == "":
                    conn.execute("DELETE FROM config WHERE key = ?", (key,))
                else:
                    updates.append((key, val))
        for k, v in updates:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    # GET: return provider + which keys are set (no values)
    rows = dict(conn.execute("SELECT key, value FROM config WHERE key IN ('ai_provider','groq_api_key','anthropic_api_key')").fetchall())
    conn.close()
    return jsonify({
        "ai_provider": rows.get("ai_provider", "auto"),
        "has_groq_key": bool(rows.get("groq_api_key") or os.environ.get("GROQ_API_KEY")),
        "has_anthropic_key": bool(rows.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")),
        "groq_from_env": bool(os.environ.get("GROQ_API_KEY")),
        "anthropic_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
    })


@app.route("/api/activity-config", methods=["GET", "POST"])
def activity_config_route():
    if request.method == "POST":
        data = request.json or {}
        for key in DEFAULT_LIST_CONFIG:
            if key in data and isinstance(data[key], str):
                list_config[key] = data[key]
        save_config_to_db()
        return jsonify({"ok": True})
    return jsonify({
        **list_config,
        "current_app": tracker_state["current_app"],
        "current_url": tracker_state["current_url"],
        "current_classification": tracker_state["current_classification"],
    })


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


# ============================================================
# AI Planner Endpoints
# ============================================================

@app.route("/api/plan/generate", methods=["POST"])
def plan_generate():
    data = request.json or {}
    raw_todos = (data.get("raw_todos") or "").strip()
    if not raw_todos:
        return jsonify({"error": "raw_todos is required"}), 400

    work_hours = int(data.get("work_hours_target") or 6)
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = datetime.now().strftime("%A")

    # Fetch carry-overs from yesterday
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    carried_rows = conn.execute(
        "SELECT title, carried_from_date FROM todos WHERE done=0 AND carried_from_date IS NOT NULL AND DATE(created_at)=?",
        (today,)
    ).fetchall()
    carried_todos = [{"title": r[0], "carried_from_date": r[1] or yesterday} for r in carried_rows]

    context = {
        "work_hours_target": work_hours,
        "date": today,
        "weekday": weekday,
        "carried_todos": carried_todos,
    }

    plan = _generate_day_plan(raw_todos, context, DB_PATH)

    if "error" in plan and not plan.get("blocks"):
        conn.close()
        return jsonify(plan), 500

    import json as _json

    # Upsert day_plans row
    now_iso = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO day_plans (date, raw_input, plan_json, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET raw_input=excluded.raw_input, plan_json=excluded.plan_json",
        (today, raw_todos, _json.dumps(plan), now_iso)
    )
    plan_id = conn.execute("SELECT id FROM day_plans WHERE date=?", (today,)).fetchone()[0]

    # Insert todos for each non-break block
    for block in plan.get("blocks", []):
        if block.get("category") == "break":
            continue
        carried_from = None
        if block.get("is_carry_over"):
            carried_from = yesterday
        conn.execute(
            "INSERT INTO todos (title, done, priority, created_at, planned_start_time, planned_duration_min, day_plan_id, carried_from_date) "
            "VALUES (?,0,?,?,?,?,?,?)",
            (
                block.get("title", "Task"),
                {"critical": 3, "high": 2, "medium": 1, "low": 0}.get(block.get("priority", "medium"), 1),
                now_iso,
                block.get("start_time"),
                block.get("duration_min"),
                plan_id,
                carried_from,
            )
        )
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "plan": plan, "plan_id": plan_id})


@app.route("/api/plan/today")
def plan_today():
    import json as _json
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)

    # Auto-rollover: if no plan today, offer yesterday's open todos
    row = conn.execute("SELECT id, raw_input, plan_json, created_at FROM day_plans WHERE date=?", (today,)).fetchone()

    if not row:
        # Return carry-over candidates from yesterday
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        carryover_rows = conn.execute(
            "SELECT id, title, planned_start_time, planned_duration_min FROM todos "
            "WHERE done=0 AND day_plan_id IN (SELECT id FROM day_plans WHERE date=?) AND carried_from_date IS NULL",
            (yesterday,)
        ).fetchall()
        conn.close()
        return jsonify({
            "plan": None,
            "todos": [],
            "carryover_candidates": [
                {"id": r[0], "title": r[1], "planned_start_time": r[2], "planned_duration_min": r[3]}
                for r in carryover_rows
            ]
        })

    plan_id, raw_input, plan_json, created_at = row
    plan_data = _json.loads(plan_json) if plan_json else {}

    todos = conn.execute(
        "SELECT id, title, done, priority, planned_start_time, planned_duration_min, carried_from_date "
        "FROM todos WHERE day_plan_id=? ORDER BY planned_start_time",
        (plan_id,)
    ).fetchall()
    conn.close()

    return jsonify({
        "plan": {
            "id": plan_id,
            "date": today,
            "raw_input": raw_input,
            "created_at": created_at,
            **plan_data,
        },
        "todos": [
            {
                "id": r[0], "title": r[1], "done": bool(r[2]), "priority": r[3],
                "planned_start_time": r[4], "planned_duration_min": r[5],
                "carried_from_date": r[6],
            }
            for r in todos
        ],
        "carryover_candidates": [],
    })


@app.route("/api/plan/carryover")
def plan_carryover():
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, planned_start_time, planned_duration_min FROM todos "
        "WHERE done=0 AND day_plan_id IN (SELECT id FROM day_plans WHERE date=?)",
        (yesterday,)
    ).fetchall()
    conn.close()
    return jsonify([
        {"id": r[0], "title": r[1], "planned_start_time": r[2], "planned_duration_min": r[3]}
        for r in rows
    ])


@app.route("/api/plan/rollover", methods=["POST"])
def plan_rollover():
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    now_iso = datetime.now().isoformat()

    conn = sqlite3.connect(DB_PATH)

    # Get or create today's day_plans row
    row = conn.execute("SELECT id FROM day_plans WHERE date=?", (today,)).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO day_plans (date, raw_input, plan_json, created_at) VALUES (?,?,?,?)",
            (today, "[rollover]", None, now_iso)
        )
        plan_id = conn.execute("SELECT id FROM day_plans WHERE date=?", (today,)).fetchone()[0]
    else:
        plan_id = row[0]

    # Fetch open todos from yesterday's plan
    open_todos = conn.execute(
        "SELECT title, priority, planned_start_time, planned_duration_min FROM todos "
        "WHERE done=0 AND day_plan_id IN (SELECT id FROM day_plans WHERE date=?) AND carried_from_date IS NULL",
        (yesterday,)
    ).fetchall()

    inserted = 0
    for r in open_todos:
        conn.execute(
            "INSERT INTO todos (title, done, priority, created_at, planned_start_time, planned_duration_min, day_plan_id, carried_from_date) "
            "VALUES (?,0,?,?,?,?,?,?)",
            (r[0], r[1], now_iso, r[2], r[3], plan_id, yesterday)
        )
        inserted += 1

    conn.commit()
    conn.close()
    return jsonify({"ok": True, "rolled_over": inserted})


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
        # Same day — resume; restore project_id too
        proj_row = conn.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        recovered_pid = proj_row[0] if proj_row else None
        tracker_state["active"] = True
        tracker_state["session_start"] = start_time
        tracker_state["active_project_id"] = recovered_pid
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
