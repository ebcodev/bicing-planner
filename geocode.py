"""
geocode.py - address search limited to Barcelona, using OpenStreetMap's Nominatim service.

Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/):
  * at most 1 request per second      -> enforced with a lock and a minimum interval
  * identify the application           -> USER_AGENT
  * no search-as-you-type              -> the app only searches on Enter / Find
  * cache results                      -> data/geocode_cache.json
"""

import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from app_paths import GEOCODE_CACHE

URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "BicingPlanner/0.1 (desktop app)"
VIEWBOX = "2.05,41.47,2.24,41.32"   # Barcelona city: left, top, right, bottom (lon, lat)
MIN_INTERVAL_S = 1.1
MAX_LABEL = 70


@dataclass
class Place:
    label: str   # short text for the UI
    full: str    # Nominatim's full display_name
    lat: float
    lon: float


_lock = threading.Lock()
_last_request = 0.0
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    GEOCODE_CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1), encoding="utf-8")


def _short_label(display_name: str) -> str:
    parts = [p.strip() for p in display_name.split(",")]
    label = ", ".join(parts[:3])
    return label if len(label) <= MAX_LABEL else label[: MAX_LABEL - 1] + "…"


def search(query: str, limit: int = 5, lang: str = "en") -> list[Place]:
    """Best matches first; [] when nothing in Barcelona matches."""
    global _last_request
    key = f"{lang}|{query.strip().casefold()}"
    with _lock:
        cache = _load_cache()
        if key in cache:
            return [Place(**p) for p in cache[key]]
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        params = urllib.parse.urlencode({
            "q": query, "format": "jsonv2", "limit": limit, "viewbox": VIEWBOX, "bounded": 1,
            "countrycodes": "es", "accept-language": lang,
        })
        req = urllib.request.Request(f"{URL}?{params}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        finally:
            _last_request = time.monotonic()
        places = [Place(label=_short_label(d["display_name"]), full=d["display_name"],
                        lat=float(d["lat"]), lon=float(d["lon"])) for d in data]
        cache[key] = [asdict(p) for p in places]
        _save_cache()
        return places
