"""
app_config.py - user settings for the Bicing app, stored in %APPDATA%/BicingPlanner/config.json.

The TypeSafe API key comes from the TYPESAFE_API_KEY environment variable when it is set;
otherwise from the key saved here through the Settings window (plain text in the user profile).
"""

import json
import os

from app_paths import CONFIG_DIR, CONFIG_FILE


def load() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def api_key() -> tuple[str | None, str]:
    """Returns (key, source); source is 'env', 'config' or 'missing'."""
    env = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env:
        return env, "env"
    saved = str(load().get("typesafe_api_key", "")).strip()
    return (saved, "config") if saved else (None, "missing")


def language() -> str | None:
    return load().get("language")


def set_language(code: str) -> None:
    cfg = load()
    cfg["language"] = code
    save(cfg)


def set_api_key(key: str) -> None:
    cfg = load()
    key = key.strip()
    if key:
        cfg["typesafe_api_key"] = key
    else:
        cfg.pop("typesafe_api_key", None)
    save(cfg)
