#!/usr/bin/env python3
"""
planner.py - pick the most reliable Bicing pickup and drop-off stations for a trip.

For each end of the trip it looks at stations within walking distance and asks, using the
history CSV built by `citybikes.py snapshot`: "at this hour on this kind of day, how often did
this station have what I need?" (bikes of the right type at the origin, free docks at the
destination). Reliability is traded off against walking distance.

Example: the destination has three nearby stations, two usually full and one usually free.
The planner suggests the usually-free one even if it is a little further away.

Usage:
    python planner.py --from "Sagrada Família" --to "Barceloneta" --at "2026-11-11 09:00"
    python planner.py --from 41.4036,2.1744 --to 41.3784,2.1925 --ebike -o data/plan.csv
    python planner.py --from "Sants" --to "Camp Nou" --arrive-by "2026-11-11 20:30"

Standard library only. The desktop app (app.py) calls plan_trip() and nearby() directly.
"""

import argparse
import csv
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# --- tunable policy --------------------------------------------------------
MIN_BIKES = 2              # a station "works" at the origin if it has >= this many bikes of the type
MIN_DOCKS = 2              # a station "works" at the destination if it has >= this many free docks
DEFAULT_RADIUS_M = 500     # how far the rider is willing to walk to/from a station
RADIUS_STEP_M = 250        # widen by this much when no usable station is found...
MAX_RADIUS_M = 1250        # ...up to this limit
WALK_PENALTY_PER_100M = 0.04   # reliability points lost per 100 m of walking
MIN_SAMPLES = 3            # below this, fall back from the hour bucket to coarser stats
PRIOR_P = 0.5              # assumed reliability before any evidence...
PRIOR_WEIGHT = 2           # ...worth this many samples
BIKE_SPEED_KMH = 14.0
ROUTE_FACTOR = 1.3         # straight-line -> street distance
TOP_N = 3
NEARBY_FALLBACK = 5        # nearest stations shown when none is inside the radius

NEEDS = ("bikes", "ebike", "mechanical", "docks")   # what a nearby search looks for


@dataclass
class Station:
    id: str
    name: str
    lat: float
    lon: float
    online: bool
    free_bikes: int
    normal_bikes: int
    ebikes: int
    empty_slots: int


@dataclass
class TripPlan:
    origin: tuple              # (label, lat, lon)
    destination: tuple
    bike_type: str             # either | ebike | mechanical
    depart: datetime           # Barcelona local time, naive
    arrive: datetime
    ride_min: float
    picks: list                # ranked origin candidates, best first
    drops: list                # ranked destination candidates, best first
    pick_radius: int           # radius actually searched (may have been widened)
    drop_radius: int
    requested_radius: int


# --- time: Barcelona local time without needing tzdata on Windows ----------

def _last_sunday(year: int, month: int) -> datetime:
    d = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def to_barcelona(dt_utc: datetime) -> datetime:
    """CET (UTC+1), CEST (UTC+2) from last Sunday of March 01:00 UTC to last Sunday of Oct 01:00 UTC."""
    y = dt_utc.year
    dst_start = _last_sunday(y, 3).replace(hour=1)
    dst_end = _last_sunday(y, 10).replace(hour=1)
    offset = 2 if dst_start <= dt_utc < dst_end else 1
    return (dt_utc + timedelta(hours=offset)).replace(tzinfo=None)


def now_barcelona() -> datetime:
    return to_barcelona(datetime.now(timezone.utc))


def bucket(local: datetime) -> tuple[str, int]:
    return ("weekend" if local.weekday() >= 5 else "weekday", local.hour)


# --- geometry --------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --- loading ---------------------------------------------------------------

def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def load_live(path: str) -> dict[str, Station]:
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["id"]] = Station(
                id=r["id"], name=r["name"], lat=float(r["latitude"]), lon=float(r["longitude"]),
                online=r.get("online") == "True",
                free_bikes=_int(r["free_bikes"]), normal_bikes=_int(r.get("normal_bikes")),
                ebikes=_int(r.get("ebikes")), empty_slots=_int(r["empty_slots"]),
            )
    return out


def read_history(path: str):
    """Returns (hist, n_snapshots). hist is {station_id: {bucket_key: [rows]}} where bucket_key
    is (day_type, hour), plus an 'all' key holding every row for that station."""
    hist = defaultdict(lambda: defaultdict(list))
    snapshots = set()
    if not os.path.exists(path):
        return hist, 0
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            snapshots.add(r["snapshot_utc"])
            ts = datetime.fromisoformat(r["snapshot_utc"])
            row = {
                "online": r.get("online") == "True",
                "normal_bikes": _int(r.get("normal_bikes")),
                "ebikes": _int(r.get("ebikes")),
                "free_bikes": _int(r["free_bikes"]),
                "empty_slots": _int(r["empty_slots"]),
            }
            hist[r["id"]][bucket(to_barcelona(ts))].append(row)
            hist[r["id"]]["all"].append(row)
    return hist, len(snapshots)


def load_history(path: str):
    return read_history(path)[0]


def load_landmarks(path: str) -> dict[str, tuple[str, float, float]]:
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            entry = (r["name"], float(r["latitude"]), float(r["longitude"]))
            for key in [r["name"], *r["aliases"].split(";")]:
                if key.strip():
                    out[key.strip().casefold()] = entry
    return out


def resolve_place(text: str, landmarks) -> tuple[str, float, float]:
    """Accepts 'lat,lon' or an exact landmark name/alias (command line only)."""
    parts = text.split(",")
    if len(parts) == 2:
        try:
            return (text, float(parts[0]), float(parts[1]))
        except ValueError:
            pass
    hit = landmarks.get(text.strip().casefold())
    if not hit:
        raise SystemExit(f"Unknown place {text!r}. Use 'lat,lon' or a name from data/landmarks.csv.")
    return hit


# --- scoring ---------------------------------------------------------------

def need_ok(row: dict, role: str, bike: str) -> bool:
    if not row["online"]:
        return False
    if role == "destination":
        return row["empty_slots"] >= MIN_DOCKS
    count = {"ebike": row["ebikes"], "mechanical": row["normal_bikes"]}.get(bike, row["free_bikes"])
    return count >= MIN_BIKES


def reliability(station_id: str, hist, key, role: str, bike: str) -> tuple[float | None, int, str]:
    """Fraction of past snapshots where the station met the need. Falls back hour -> neighbouring
    hours -> all history; returns (None, 0, 'none') when there is no history at all."""
    day_type, hour = key
    tries = [
        ([key], f"{day_type} {hour:02d}h"),
        ([(day_type, (hour + d) % 24) for d in (-1, 0, 1)], f"{day_type} {hour:02d}h±1"),
        (["all"], "all history"),
    ]
    for keys, label in tries:
        rows = [r for k in keys for r in hist[station_id].get(k, [])]
        if len(rows) >= MIN_SAMPLES or (keys == ["all"] and rows):
            return sum(need_ok(r, role, bike) for r in rows) / len(rows), len(rows), label
    return None, 0, "none"


def rank(point, stations, hist, key, role, bike, radius_m):
    """All stations within radius_m of point, best first."""
    _, lat, lon = point
    out = []
    for s in stations.values():
        walk = haversine_m(lat, lon, s.lat, s.lon)
        if walk > radius_m:
            continue
        live_row = {"online": s.online, "normal_bikes": s.normal_bikes, "ebikes": s.ebikes,
                    "free_bikes": s.free_bikes, "empty_slots": s.empty_slots}
        live_ok = need_ok(live_row, role, bike)
        p, n, basis = reliability(s.id, hist, key, role, bike)
        if p is None:  # no history yet: use the live snapshot as a single weak sample
            p, n, basis = (1.0 if live_ok else 0.0), 1, "live only"
        # Shrink toward the prior so few samples count as uncertain: 1/1 -> 0.67, 18/20 -> 0.86
        p = (p * n + PRIOR_P * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
        score = p - WALK_PENALTY_PER_100M * walk / 100
        out.append({
            "role": role, "station_id": s.id, "station": s.name, "lat": s.lat, "lon": s.lon,
            "walk_m": round(walk), "reliability": round(p, 2), "samples": n, "basis": basis,
            "score": round(score, 3), "live_ok": live_ok, "online": s.online,
            "free_bikes": s.free_bikes, "normal_bikes": s.normal_bikes, "ebikes": s.ebikes,
            "empty_slots": s.empty_slots,
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def rank_widening(point, stations, hist, key, role, bike, radius_m):
    """Like rank(), but widens the radius step by step up to MAX_RADIUS_M while nothing usable
    is found. Returns (ranked, radius_used)."""
    radius = radius_m
    while True:
        ranked = rank(point, stations, hist, key, role, bike, radius)
        if any(r["reliability"] > 0 for r in ranked) or radius >= MAX_RADIUS_M:
            return ranked, radius
        radius = min(radius + RADIUS_STEP_M, MAX_RADIUS_M)


def plan_trip(origin, destination, stations, hist, *, bike_type="either",
              radius=DEFAULT_RADIUS_M, depart=None, arrive_by=None) -> TripPlan:
    """origin/destination are (label, lat, lon). Give depart OR arrive_by (Barcelona local
    time); with neither, the trip leaves now."""
    ride_m = haversine_m(origin[1], origin[2], destination[1], destination[2]) * ROUTE_FACTOR
    ride_min = ride_m / 1000 / BIKE_SPEED_KMH * 60
    if arrive_by is not None:
        arrive = arrive_by
        depart = arrive_by - timedelta(minutes=ride_min)
    else:
        depart = depart or now_barcelona()
        arrive = depart + timedelta(minutes=ride_min)
    picks, r_pick = rank_widening(origin, stations, hist, bucket(depart), "origin", bike_type, radius)
    drops, r_drop = rank_widening(destination, stations, hist, bucket(arrive), "destination",
                                  bike_type, radius)
    return TripPlan(origin, destination, bike_type, depart, arrive, ride_min,
                    picks, drops, r_pick, r_drop, radius)


def nearby(point, stations, *, need="bikes", radius=DEFAULT_RADIUS_M) -> tuple[list, bool]:
    """Live view of stations around point, nearest first. need is one of NEEDS.
    Returns (rows, inside_radius); when no station is inside the radius, the nearest
    NEARBY_FALLBACK stations are returned with inside_radius=False."""
    _, lat, lon = point
    rows = []
    for s in stations.values():
        walk = haversine_m(lat, lon, s.lat, s.lon)
        count = {"bikes": s.free_bikes, "ebike": s.ebikes, "mechanical": s.normal_bikes,
                 "docks": s.empty_slots}[need]
        threshold = MIN_DOCKS if need == "docks" else MIN_BIKES
        if not s.online:
            status = "offline"
        elif count >= threshold:
            status = "ok"
        else:
            status = "low" if count > 0 else "none"
        rows.append({
            "station_id": s.id, "station": s.name, "lat": s.lat, "lon": s.lon,
            "walk_m": round(walk), "need": need, "count": count, "status": status,
            "online": s.online, "free_bikes": s.free_bikes, "normal_bikes": s.normal_bikes,
            "ebikes": s.ebikes, "empty_slots": s.empty_slots,
        })
    rows.sort(key=lambda r: r["walk_m"])
    inside = [r for r in rows if r["walk_m"] <= radius]
    return (inside, True) if inside else (rows[:NEARBY_FALLBACK], False)


def explain(choice, alternatives, role):
    need = "free docks" if role == "destination" else "bikes"
    msg = (f"{choice['station']} ({choice['walk_m']} m walk): had {need} "
           f"{choice['reliability']:.0%} of the time [{choice['basis']}, n={choice['samples']}]")
    worse = [a for a in alternatives if a is not choice and a["walk_m"] < choice["walk_m"]][:3]
    if worse:
        msg += "; skipped closer " + ", ".join(
            f"{a['station']} ({a['walk_m']} m, {a['reliability']:.0%})" for a in worse)
    if not choice["live_ok"]:
        msg += "  WARNING: does not meet the need right now"
    return msg


def main():
    ap = argparse.ArgumentParser(description="Plan a Bicing trip using historical station availability.")
    ap.add_argument("--from", dest="origin", required=True, help="'lat,lon' or landmark name")
    ap.add_argument("--to", dest="destination", required=True, help="'lat,lon' or landmark name")
    when = ap.add_mutually_exclusive_group()
    when.add_argument("--at", help="Departure time in Barcelona local time 'YYYY-MM-DD HH:MM' (default: now)")
    when.add_argument("--arrive-by", help="Arrival deadline in Barcelona local time 'YYYY-MM-DD HH:MM'")
    bike = ap.add_mutually_exclusive_group()
    bike.add_argument("--ebike", action="store_const", const="ebike", dest="bike")
    bike.add_argument("--mechanical", action="store_const", const="mechanical", dest="bike")
    ap.add_argument("--radius", type=int, default=DEFAULT_RADIUS_M, help="Max walk to/from a station (m)")
    ap.add_argument("--live", default="data/bicing.csv")
    ap.add_argument("--history", default="data/history/bicing.csv")
    ap.add_argument("--landmarks", default="data/landmarks.csv")
    ap.add_argument("-o", "--out", help="Write ranked candidates to this CSV")
    args = ap.parse_args()
    bike_type = args.bike or "either"
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Catalan accents on Windows consoles
    except AttributeError:
        pass

    landmarks = load_landmarks(args.landmarks)
    origin = resolve_place(args.origin, landmarks)
    dest = resolve_place(args.destination, landmarks)
    stations = load_live(args.live)
    hist = load_history(args.history)

    fmt = "%Y-%m-%d %H:%M"
    plan = plan_trip(origin, dest, stations, hist, bike_type=bike_type, radius=args.radius,
                     depart=datetime.strptime(args.at, fmt) if args.at else None,
                     arrive_by=datetime.strptime(args.arrive_by, fmt) if args.arrive_by else None)

    label = {"either": "any bike", "ebike": "e-bike", "mechanical": "mechanical bike"}[bike_type]
    print(f"\n{origin[0]} -> {dest[0]}  | {label} | depart {plan.depart:%a %H:%M}, "
          f"~{plan.ride_min:.0f} min ride, arrive ~{plan.arrive:%H:%M}")
    for role, ranked, radius in (("Pick up at", plan.picks, plan.pick_radius),
                                 ("Drop off at", plan.drops, plan.drop_radius)):
        widened = f" (widened search to {radius} m)" if radius > args.radius else ""
        if ranked:
            print(f"  {role}: {explain(ranked[0], ranked, ranked[0]['role'])}{widened}")
        else:
            print(f"  {role}: no station within {radius} m")
    if not hist:
        print("  note: no history yet, ranked on the live snapshot only. "
              "Run `python citybikes.py snapshot --every 600` for a few days.")
    print()

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        rows = plan.picks[:TOP_N] + plan.drops[:TOP_N]
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["role"])
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {len(rows)} candidates to {args.out}")


if __name__ == "__main__":
    main()
