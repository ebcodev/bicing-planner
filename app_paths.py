"""
app_paths.py - where the Bicing app keeps its files.

* Run from source: station data in the project's `data` folder.
* Packaged .exe:   station data in %LOCALAPPDATA%/BicingPlanner/data, so the .exe can live
  anywhere (Downloads, Program Files) and the history survives replacing it. On first run the
  live snapshot and history bundled into the .exe are copied there (seed_data).
* Settings (API key, language) always live in %APPDATA%/BicingPlanner/config.json.
"""

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

APP_NAME = "BicingPlanner"
FROZEN = getattr(sys, "frozen", False)          # True inside the PyInstaller .exe
SOURCE_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))   # files bundled into the .exe

if FROZEN:
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_NAME / "data"
else:
    DATA_DIR = SOURCE_DIR / "data"

LIVE_CSV = DATA_DIR / "bicing.csv"
HISTORY_CSV = DATA_DIR / "history" / "bicing.csv"
GEOCODE_CACHE = DATA_DIR / "geocode_cache.json"
ICON_FILE = RESOURCE_DIR / "assets" / "bicing_planner.ico"
SEED_DIR = RESOURCE_DIR / "seed"

CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"


def _last_snapshot_time(history_csv: Path) -> float | None:
    """UTC time of the newest snapshot in a history CSV (its last line starts with snapshot_utc)."""
    try:
        with open(history_csv, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            last = f.read().decode("utf-8", "ignore").strip().splitlines()[-1]
        return datetime.fromisoformat(last.split(",", 1)[0]).timestamp()
    except (OSError, ValueError, IndexError):
        return None


def seed_data() -> None:
    """First run of the packaged app: copy the bundled snapshot and history (never overwrites).
    The live CSV gets the date of the newest bundled snapshot, so the app sees it as old data and
    offers an update; the file's own date would be the moment the .exe unpacked it."""
    copied_live = False
    for name in ("bicing.csv", "history/bicing.csv"):
        src, dst = SEED_DIR / name, DATA_DIR / name
        if src.exists() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied_live |= name == "bicing.csv"
    if copied_live:
        ts = _last_snapshot_time(SEED_DIR / "history" / "bicing.csv")
        if ts:
            os.utime(DATA_DIR / "bicing.csv", (ts, ts))
