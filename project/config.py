"""Central configuration for dispatcher application."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
STATIC_DIR = APP_ROOT / "static"
TEMPLATES_DIR = APP_ROOT / "templates"
CACHE_DIR = APP_ROOT / "cache"
LOGS_DIR = APP_ROOT / "logs"

ENV_PATH = APP_ROOT / ".env"
load_dotenv(ENV_PATH, override=True)

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

GEO_PATH = DATA_DIR / "GEO.kml"
ROADS_PATH = DATA_DIR / "RoadCity.kml"
GRID_GEOJSON = DATA_DIR / "grid.geojson"
ASSIGNMENTS_JSON = DATA_DIR / "assignments.json"
ROADS_ASSIGN_JSON = DATA_DIR / "roads_assignment.json"
ROUTES_KML = DATA_DIR / "routes_grid.kml"
ROADS_COLORED_KML = DATA_DIR / "roads_colored.kml"
STREETS_CACHE = CACHE_DIR / "streets_cache.json"
SETTINGS_PATH = APP_ROOT / "app_settings.json"


def env_google_key() -> str | None:
    return os.getenv("GOOGLE_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY")


def env_yandex_key() -> str | None:
    return os.getenv("YANDEX_API_KEY") or os.getenv("YANDEX_GEOCODER_API_KEY")


def load_settings(defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Load persisted settings from app_settings.json overriding defaults."""
    if SETTINGS_PATH.exists():
        try:
            payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            defaults.update(payload)
        except Exception:
            pass
    return defaults


def save_settings(settings: Dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
