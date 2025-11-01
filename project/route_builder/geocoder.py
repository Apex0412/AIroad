"""Reverse geocoding helpers with caching and retry logic."""
from __future__ import annotations

import json
import time
from typing import Literal, Optional

import requests

# FIX: access config helpers via parent package to keep project.server importable
from ..config import STREETS_CACHE, env_google_key, env_yandex_key

_REQUEST_TIMEOUT = 3
_MAX_RETRIES = 3
_SLEEP_SECONDS = 0.5


def _load_cache() -> dict:
    if STREETS_CACHE.exists():
        try:
            return json.loads(STREETS_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    STREETS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    STREETS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


_CACHE = _load_cache()


def get_street_name(lat: float, lon: float, source: Literal["google", "yandex"] = "google") -> Optional[str]:
    key = f"{lat:.6f},{lon:.6f}:{source}"
    if key in _CACHE:
        return _CACHE[key]

    google_key = env_google_key()
    yandex_key = env_yandex_key()

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if source == "google":
                if not google_key:
                    return None
                url = "https://maps.googleapis.com/maps/api/geocode/json"
                params = {"latlng": f"{lat},{lon}", "key": google_key, "language": "ru"}
                resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                for result in data.get("results", []):
                    for comp in result.get("address_components", []):
                        if "route" in comp.get("types", []) or "street_address" in comp.get("types", []):
                            name = comp.get("long_name")
                            if name:
                                _CACHE[key] = name
                                _save_cache(_CACHE)
                                return name
            else:
                if not yandex_key:
                    return None
                url = "https://geocode-maps.yandex.ru/1.x/"
                params = {
                    "format": "json",
                    "geocode": f"{lon},{lat}",
                    "kind": "street",
                    "apikey": yandex_key,
                    "lang": "ru_RU",
                }
                resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                members = (
                    data
                    .get("response", {})
                    .get("GeoObjectCollection", {})
                    .get("featureMember", [])
                )
                for member in members:
                    geo = member.get("GeoObject", {})
                    name = geo.get("name") or geo.get("description")
                    if name:
                        _CACHE[key] = name
                        _save_cache(_CACHE)
                        return name
        except requests.RequestException:
            if attempt == _MAX_RETRIES:
                return None
        time.sleep(_SLEEP_SECONDS)
    return None
