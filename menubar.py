#!/usr/bin/env python3
"""FocusTracker Menu Bar — macOS Menubar icon that talks to the Flask backend."""

import json
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error

import rumps

PORT = int(os.environ.get("FOCUSTRACKER_PORT", "5050"))
API = f"http://localhost:{PORT}/api"

# ---------------------------------------------------------------------------
# Env-Var config
# ---------------------------------------------------------------------------
HOTKEY_COMBO = os.environ.get("FOCUSTRACKER_HOTKEY", "<alt>+<cmd>+f")
NUDGE_MORNING = os.environ.get("FOCUSTRACKER_NUDGE_MORNING", "09:00")
NUDGE_EVENING = os.environ.get("FOCUSTRACKER_NUDGE_EVENING", "20:00")
NUDGE_WEEKDAYS_ONLY = os.environ.get("FOCUSTRACKER_NUDGE_WEEKDAYS_ONLY", "1") == "1"


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(path):
    try:
        with urllib.request.urlopen(f"{API}/{path}", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def api_post(path, data=None):
    try:
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(
            f"{API}/{path}", data=body, method="POST",
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fmt_hm(sec):
    h, m = divmod(int(sec), 3600)
    m = m // 60
    return f"{h}:{m:02d}"


def fmt_hms(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Notification helper — rumps first, osascript fallback
# ---------------------------------------------------------------------------
_notify_method = None  # determined on first call: "rumps" or "osascript"


def notify(title, subtitle="", message="", sound=False):
    """Send a macOS notification. Tries rumps, falls back to osascript."""
    global _notify_method

    if _notify_method is None:
        # Try rumps — it requires a bundle id (works when app is packaged or
        # when Info.plist is present). Attempt once and catch silently.
        try:
            rumps.notification(title, subtitle, message, sound=sound)
            _notify_method = "rumps"
            return
        except Exception:
            _notify_method = "osascript"

    if _notify_method == "rumps":
        try:
            rumps.notification(title, subtitle, message, sound=sound)
            return
        except Exception:
            _notify_method = "osascript"

    # osascript fallback
    combined = f"{subtitle} {message}".strip() if subtitle else message
    script = f'display notification {json.dumps(combined)} with title {json.dumps(title)}'
    if sound:
        script += ' sound name "Ping"'
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception as e:
        print(f"[notify] osascript failed: {e}")


# ---------------------------------------------------------------------------
# Global hotkey (pynput)
# ---------------------------------------------------------------------------

def _check_accessibility():
    """Return True if Accessibility/input monitoring is allowed."""
    try:
        import ctypes, ctypes.util
        lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return False


def _start_hotkey(app_instance):
    """Register global hotkey — skipped if Accessibility not granted (avoids CPU spin)."""
    if not _check_accessibility():
        print("[hotkey] Accessibility not granted — global hotkey disabled (no CPU impact)")
        return
    try:
        from pynput import keyboard as _kb

        def on_activate():
            app_instance.toggle(None)

        hotkeys = {HOTKEY_COMBO: on_activate}
        listener = _kb.GlobalHotKeys(hotkeys)
        listener.daemon = True
        listener.start()
        print(f"[hotkey] Registered: {HOTKEY_COMBO}")
    except ImportError:
        print("[hotkey] pynput not installed — global hotkey disabled")
    except Exception as e:
        print(f"[hotkey] Failed to register hotkey: {e}")


# ---------------------------------------------------------------------------
# Lock/Unlock detection (PyObjC NSDistributedNotificationCenter)
# ---------------------------------------------------------------------------

def _start_lock_listener(app_instance):
    """Listen for screen lock/unlock via NSDistributedNotificationCenter."""
    try:
        from AppKit import NSDistributedNotificationCenter
        from Foundation import NSRunLoop, NSDate

        center = NSDistributedNotificationCenter.defaultCenter()

        class LockObserver:
            def screenLocked_(self, note):
                status = api_get("status")
                if status and status.get("active"):
                    api_post("stop", {"note": "Mac gesperrt"})
                    print("[lock] Session stopped — screen locked")

            def screenUnlocked_(self, note):
                notify("FocusTracker", "Weitermachen?", f"{HOTKEY_COMBO.upper()} zum Starten")
                print("[lock] Screen unlocked — nudge sent")

        observer = LockObserver()
        center.addObserver_selector_name_object_(
            observer,
            "screenLocked:",
            "com.apple.screenIsLocked",
            None,
        )
        center.addObserver_selector_name_object_(
            observer,
            "screenUnlocked:",
            "com.apple.screenIsUnlocked",
            None,
        )

        print("[lock] Lock/unlock listener active")
        # Block the thread with a simple sleep loop — the observer callbacks
        # are dispatched on the main thread, so we don't need to spin a
        # secondary run loop (which causes high CPU in daemon threads).
        while True:
            time.sleep(60)

    except ImportError:
        print("[lock] pyobjc-framework-Cocoa not installed — lock detection disabled")
    except Exception as e:
        print(f"[lock] Failed to start listener: {e}")


# ---------------------------------------------------------------------------
# Scheduler (morning nudge + evening recap) — APScheduler
# ---------------------------------------------------------------------------

def _build_morning_nudge():
    notify(
        "FocusTracker",
        "Was ist heute dein 1 Ding?",
        f"⌥⌘F drücken zum Starten",
    )
    print("[nudge] Morning nudge sent")


def _build_evening_recap():
    today = api_get("today")
    streak_data = api_get("streak")
    if today is None or streak_data is None:
        print("[nudge] Evening recap skipped — backend down")
        return

    total_sec = today.get("total_work_sec", 0)
    goal_pct = today.get("goal_pct", 0)
    streak = streak_data.get("streak", 0)
    fire = "🔥" if streak > 0 else ""

    h, rem = divmod(int(total_sec), 3600)
    m = rem // 60
    msg = f"{h}h {m:02d}min fokussiert · Tagesziel {goal_pct}% · Streak {streak} Tage {fire}"
    notify("FocusTracker — Heute", "", msg)
    print(f"[nudge] Evening recap sent: {msg}")


def _start_scheduler():
    """Set up morning nudge and evening recap via APScheduler."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        sched = BackgroundScheduler()

        # Parse times
        morning_h, morning_m = (int(x) for x in NUDGE_MORNING.split(":"))
        evening_h, evening_m = (int(x) for x in NUDGE_EVENING.split(":"))

        day_of_week = "mon-fri" if NUDGE_WEEKDAYS_ONLY else "mon-sun"

        sched.add_job(
            _build_morning_nudge,
            trigger="cron",
            hour=morning_h, minute=morning_m,
            day_of_week=day_of_week,
            id="morning_nudge",
        )
        sched.add_job(
            _build_evening_recap,
            trigger="cron",
            hour=evening_h, minute=evening_m,
            day_of_week=day_of_week,
            id="evening_recap",
        )

        sched.start()
        print(f"[scheduler] Morning nudge at {NUDGE_MORNING}, evening recap at {NUDGE_EVENING} ({day_of_week})")
        return sched

    except ImportError:
        print("[scheduler] apscheduler not installed — nudges disabled")
        return None
    except Exception as e:
        print(f"[scheduler] Failed to start: {e}")
        return None


# ---------------------------------------------------------------------------
# Menubar App
# ---------------------------------------------------------------------------

class FocusTrackerMenuBar(rumps.App):
    def __init__(self):
        super().__init__("", quit_button=None)
        self.icon = os.path.join(os.path.dirname(__file__), "icons", "bolt.png")
        self.template = True

        # Menu items
        self.toggle_item = rumps.MenuItem("Start", callback=self.toggle)
        self.pomo_item = rumps.MenuItem("Pomodoro: Aus", callback=self.toggle_pomo)
        self.status_item = rumps.MenuItem("--")
        self.status_item.set_callback(None)
        self.goal_item = rumps.MenuItem("Tagesziel: --")
        self.goal_item.set_callback(None)
        self.streak_item = rumps.MenuItem("Streak: --")
        self.streak_item.set_callback(None)
        self.level_item = rumps.MenuItem("Level: --")
        self.level_item.set_callback(None)
        self.dash_item = rumps.MenuItem("Dashboard oeffnen", callback=self.open_dash)
        self.quit_item = rumps.MenuItem("Beenden", callback=self.quit_app)

        self.menu = [
            self.toggle_item,
            self.pomo_item,
            None,  # separator
            self.status_item,
            self.goal_item,
            self.streak_item,
            self.level_item,
            None,
            self.dash_item,
            self.quit_item,
        ]

        self._running = True
        self._scheduler = None

        # Start background services
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=_start_hotkey, args=(self,), daemon=True).start()
        threading.Thread(target=_start_lock_listener, args=(self,), daemon=True).start()
        self._scheduler = _start_scheduler()

    def _poll_loop(self):
        while self._running:
            self._update()
            time.sleep(3)

    def _update(self):
        status = api_get("status")
        today = api_get("today")
        streak = api_get("streak")
        gamification = api_get("gamification")

        if not status:
            self.title = " ?"
            return

        # Title in menu bar
        if status["active"]:
            elapsed = fmt_hms(status["elapsed_sec"])
            if status["on_break"]:
                self.title = f"⏸ {elapsed}"
            elif status.get("pomodoro_enabled") and status.get("pomodoro_phase"):
                rem = status["pomodoro_remaining"]
                phase = "F" if status["pomodoro_phase"] == "focus" else "P"
                self.title = f"🍅{phase} {rem // 60}:{rem % 60:02d}"
            else:
                self.title = f"▶ {elapsed}"

            self.toggle_item.title = "Stop"
        else:
            self.title = ""
            self.toggle_item.title = "Start"

        # Status line
        if status["active"]:
            if status["on_break"]:
                self.status_item.title = f"Pause seit {status['break_sec'] // 60} Min"
            else:
                self.status_item.title = f"Aktiv — {fmt_hms(status['elapsed_sec'])}"
        else:
            self.status_item.title = "Nicht aktiv"

        # Pomodoro
        if status.get("pomodoro_enabled"):
            self.pomo_item.title = f"Pomodoro: An ({status.get('pomodoro_count', 0)} Bloecke)"
        else:
            self.pomo_item.title = "Pomodoro: Aus"

        # Today
        if today:
            self.goal_item.title = f"Tagesziel: {today['goal_pct']}% ({fmt_hm(today['total_work_sec'])} / {today['daily_goal'] // 3600}h)"

        # Streak
        if streak:
            fire = "🔥" if streak["streak"] > 0 else ""
            self.streak_item.title = f"Streak: {streak['streak']} Tage {fire}"

        # Level
        if gamification:
            self.level_item.title = f"Level {gamification['level']} — {gamification['total_xp']} XP"

    def toggle(self, _):
        status = api_get("status")
        if not status:
            return
        if status["active"]:
            api_post("stop", {"note": ""})
        else:
            api_post("start")

    def toggle_pomo(self, _):
        api_post("pomodoro")

    def open_dash(self, _):
        subprocess.Popen(["open", f"http://localhost:{PORT}"])

    def quit_app(self, _):
        self._running = False
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
        rumps.quit_application()


if __name__ == "__main__":
    FocusTrackerMenuBar().run()
