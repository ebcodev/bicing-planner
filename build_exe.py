#!/usr/bin/env python3
"""
build_exe.py - packages the Bicing Planner as a single Windows executable.

    uv run python build_exe.py          ->  dist/BicingPlanner.exe

* One file, no console window, bike icon (assets/bicing_planner.ico, made by make_icon.py).
* Bundles the current live snapshot and history as seed data: on its first run the .exe copies
  them to %LOCALAPPDATA%\\BicingPlanner\\data (see app_paths.seed_data).
* No API key is bundled: the .exe reads TYPESAFE_API_KEY or the key saved in Settings.
"""

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
SEP = ";" if sys.platform == "win32" else ":"


def add_data(src: str, dest: str) -> list[str]:
    path = ROOT / src
    if not path.exists():
        hint = ("uv run python make_icon.py" if src.startswith("assets")
                else "uv run python citybikes.py snapshot")
        raise SystemExit(f"missing {src}: run `{hint}` first (see README, 'Build from a fresh copy')")
    return ["--add-data", f"{path}{SEP}{dest}"]


def main():
    args = [
        str(ROOT / "app.py"),
        "--name", "BicingPlanner",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--icon", str(ROOT / "assets" / "bicing_planner.ico"),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        *add_data("assets/bicing_planner.ico", "assets"),
        *add_data("data/bicing.csv", "seed"),
        *add_data("data/history/bicing.csv", "seed/history"),
        "--collect-data", "customtkinter",
        "--collect-data", "tkintermapview",
        "--collect-submodules", "typesafe_sdk",
        "--hidden-import", "jev_assistant",
    ]
    PyInstaller.__main__.run(args)
    print("\nbuilt", ROOT / "dist" / "BicingPlanner.exe")


if __name__ == "__main__":
    main()
