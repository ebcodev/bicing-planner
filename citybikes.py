#!/usr/bin/env python3
"""
citybikes.py - minimal client for Barcelona's Bicing network (CityBikes v2 API) that
flattens station data into CSV.

Usage:
    python citybikes.py stations                # -> data/bicing.csv (live snapshot)
    python citybikes.py snapshot --every 600    # -> data/history/bicing.csv (+ refreshes data/bicing.csv)

Uses only the standard library so it runs before `uv sync`.
"""

import argparse
import csv
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.citybik.es/v2"
NETWORK_ID = "bicing"
LIVE_CSV = "data/bicing.csv"
HISTORY_CSV = "data/history/bicing.csv"

STATION_COLUMNS = [
    "network_id", "id", "name", "latitude", "longitude", "timestamp",
    "free_bikes", "empty_slots", "normal_bikes", "ebikes", "has_ebikes", "online",
]
HISTORY_COLUMNS = ["snapshot_utc"] + STATION_COLUMNS


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": "citibik-demo/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_stations() -> list[dict]:
    """Bicing's `extra` holds uid, online, normal_bikes, ebikes and has_ebikes."""
    rows = []
    for s in _get(f"/networks/{NETWORK_ID}")["network"].get("stations", []):
        extra = s.get("extra") or {}
        rows.append({
            "network_id": NETWORK_ID,
            "id": s["id"],
            "name": (s.get("name") or "").strip(),
            "latitude": s.get("latitude"),
            "longitude": s.get("longitude"),
            "timestamp": s.get("timestamp"),
            "free_bikes": s.get("free_bikes"),
            "empty_slots": s.get("empty_slots"),
            "normal_bikes": extra.get("normal_bikes"),
            "ebikes": extra.get("ebikes"),
            "has_ebikes": extra.get("has_ebikes"),
            "online": extra.get("online"),
        })
    return rows


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=STATION_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def append_snapshot(path: str, rows: list[dict] | None = None) -> int:
    """Append one timestamped snapshot of every station to the history CSV."""
    rows = fetch_stations() if rows is None else rows
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(dict(r, snapshot_utc=now) for r in rows)
    return len(rows)


def update_snapshot(live_path: str = LIVE_CSV, history_path: str = HISTORY_CSV) -> int:
    """Fetch once, then refresh the live CSV and add the same data to the history."""
    rows = fetch_stations()
    write_csv(rows, live_path)
    append_snapshot(history_path, rows)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Dump Bicing (Barcelona) station data to CSV.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_st = sub.add_parser("stations", help="Write the live snapshot.")
    p_st.add_argument("-o", "--out", default=LIVE_CSV)
    p_sn = sub.add_parser("snapshot", help="Append a timestamped snapshot to the history CSV.")
    p_sn.add_argument("-o", "--out", default=HISTORY_CSV)
    p_sn.add_argument("--every", type=int, default=0,
                      help="Repeat every N seconds (min 300 to be polite to the API); 0 = once.")
    args = parser.parse_args()

    if args.cmd == "stations":
        rows = fetch_stations()
        write_csv(rows, args.out)
        print(f"wrote {len(rows)} rows to {args.out}")
    else:
        while True:
            try:
                n = update_snapshot(LIVE_CSV, args.out)
                print(f"{datetime.now().isoformat(timespec='seconds')} appended {n} rows to {args.out}"
                      f" (and refreshed {LIVE_CSV})")
            except Exception as e:  # keep a long-running logger alive through transient errors
                print(f"snapshot failed: {e}")
            if args.every <= 0:
                break
            time.sleep(max(args.every, 300))


if __name__ == "__main__":
    main()
