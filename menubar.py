#!/usr/bin/env python3
"""FocusTracker Menu Bar — macOS Menubar icon that talks to the Flask backend."""

import json
import os
import threading
import time
import urllib.request
import urllib.error

import rumps

API = "http://localhost:5050/api"


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

        # Start update loop
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

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
                bm = status["break_sec"] // 60
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
        import subprocess
        subprocess.Popen(["open", "http://localhost:5050"])

    def quit_app(self, _):
        self._running = False
        rumps.quit_application()


if __name__ == "__main__":
    FocusTrackerMenuBar().run()
