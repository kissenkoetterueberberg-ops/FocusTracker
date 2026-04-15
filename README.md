# FocusTracker

Lokaler Arbeitszeit-Tracker für macOS mit Idle-Detection, Pomodoro, Projekten,
Wochen-/Monatszielen, Streaks und Achievements. Alles läuft offline auf deinem
Rechner — keine Cloud, keine Accounts.

## Features

- **Auto-Tracking** mit Idle-Detection (`ioreg HIDIdleTime`) — pausiert
  automatisch wenn du längere Zeit weg bist
- **Pomodoro-Modus** mit konfigurierbaren Fokus-/Pausen-Längen
- **Projekte** mit Farben, Goal-Stunden und offenen Todos
- **Goals**: Daily / Weekly / Monthly / Custom-Range
- **Streaks** für Tage an denen das Tagesziel erreicht wurde
- **Achievements & XP/Level**-Gamification
- **Fokussiert vs. unruhig** — Klassifikation pro Arbeits-Segment
  (≥10 min ununterbrochen = fokussiert)
- **Tagesrating** (S/A/B/C/D) und Focus-Score
- **CSV/JSON-Export** der Sessions
- **Menubar-App** (rumps) mit Live-Status
- **Auto-Backups** der DB (letzte 10)
- Optional: macOS „Nicht Stören" via Shortcuts toggeln

## Installation

```bash
git clone <repo-url> FocusTracker
cd FocusTracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Starten

**Web-Dashboard:**
```bash
python app.py
```
→ http://localhost:5050

**Menubar-Icon (zusätzlich):**
```bash
python menubar.py
```

## Konfiguration

Optionale ENV-Variablen (siehe `.env.example`):

| Variable | Default | Beschreibung |
|---|---|---|
| `FOCUSTRACKER_DATA_DIR` | `~/.focustracker` | Speicherort für SQLite-DB und Backups |
| `FOCUSTRACKER_DND_SHORTCUT` | (aus) | Name eines macOS-Shortcuts der DND togglet |

Beispiel:
```bash
export FOCUSTRACKER_DND_SHORTCUT="Nicht Stoeren"
python app.py
```

App-interne Einstellungen (Pomodoro-Längen, Idle-Schwellwerte, Tagesziel)
werden im Dashboard unter „Settings" gepflegt und in der DB persistiert.

## Daten

Alles liegt in `~/.focustracker/`:
- `focus.db` — SQLite-DB mit Sessions, Breaks, Projekten, Goals, Todos
- `backups/` — automatische DB-Backups (rotierend, letzte 10)

## Multi-User auf macOS (geteilte DB)

Wenn mehrere macOS-Accounts dieselbe DB nutzen sollen, lohnt sich das mitgelieferte
Wrapper-Skript:

1. Code zentral nach `/Users/Shared/FocusTrackerCode/` klonen
2. Beide Accounts starten via `bash /Users/Shared/FocusTrackerCode/start.sh`

`start.sh` setzt `FOCUSTRACKER_DATA_DIR=/Users/Shared/FocusTracker` und legt die
venv user-lokal unter `~/.focustracker-venv` an. Die DB wird zwischen den Accounts
geteilt, der Code wird nur an einer Stelle gepflegt (`git pull` im Shared-Ordner).

## Voraussetzungen

- macOS (Idle-Detection nutzt `ioreg`, Notifications nutzen `osascript`)
- Python 3.10+
