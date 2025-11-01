"""HTTP client helpers for Google and Yandex services."""
from __future__ import annotations

import time
from typing import Iterable, List, Literal, Optional, Sequence, Tuple

import requests

from config import env_google_key, env_yandex_key

Coordinate = Tuple[float, float]


class ApiError(RuntimeError):
    """Raised when a remote API call fails."""


def _request_with_retry(url: str, *, params: dict, retries: int = 3, pause: float = 0.5) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:  # pragma: no cover - network errors
            last_exc = exc
            if attempt == retries:
                raise ApiError(str(exc)) from exc
            time.sleep(pause)
    raise ApiError(str(last_exc) if last_exc else "Неизвестная ошибка API")


def decode_polyline(polyline: str) -> List[Coordinate]:
    points: List[Coordinate] = []
    index = 0
    lat = 0
    lng = 0

    while index < len(polyline):
        result = 1
        shift = 0
        while True:
            b = ord(polyline[index]) - 63
            index += 1
            result += (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lat = ~(result >> 1) if result & 1 else result >> 1
        lat += delta_lat

        result = 1
        shift = 0
        while True:
            b = ord(polyline[index]) - 63
            index += 1
            result += (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        delta_lng = ~(result >> 1) if result & 1 else result >> 1
        lng += delta_lng

        points.append((lat / 1e5, lng / 1e5))
    return points


def google_directions(origin: Coordinate, destination: Coordinate, travel_mode: str) -> List[Coordinate]:
    key = env_google_key()
    if not key:
        raise ApiError("GOOGLE_API_KEY отсутствует")
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{destination[0]},{destination[1]}",
        "mode": travel_mode,
        "language": "ru",
        "key": key,
    }
    data = _request_with_retry(url, params=params).json()
    routes = data.get("routes", [])
    if not routes:
        raise ApiError("Google Directions вернул пустой ответ")
    coords: List[Coordinate] = []
    for leg in routes[0].get("legs", []):
        for step in leg.get("steps", []):
            poly = step.get("polyline", {}).get("points")
            if poly:
                coords.extend(decode_polyline(poly))
    if not coords:
        raise ApiError("Google Directions не дал координаты")
    return coords


def yandex_directions(origin: Coordinate, destination: Coordinate, travel_mode: str) -> List[Coordinate]:
    key = env_yandex_key()
    if not key:
        raise ApiError("YANDEX_API_KEY отсутствует")
    url = "https://api.routing.yandex.net/v2/route"
    params = {
        "apikey": key,
        "transport": travel_mode if travel_mode in {"auto", "pedestrian"} else "auto",
        "format": "json",
        "lang": "ru_RU",
        "waypoints": f"{origin[1]},{origin[0]}|{destination[1]},{destination[0]}",
    }
    data = _request_with_retry(url, params=params).json()
    if "routes" not in data:
        raise ApiError("Yandex Routing не вернул маршруты")
    first = data["routes"][0]
    coords: List[Coordinate] = []
    for section in first.get("sections", []):
        geometry = section.get("geometry", {})
        polyline = geometry.get("polyline")
        if isinstance(polyline, list):
            coords.extend([(pt[1], pt[0]) for pt in polyline])
        elif isinstance(polyline, str):
            coords.extend(decode_polyline(polyline))
    if not coords:
        raise ApiError("Yandex Routing не дал координаты")
    return coords


def request_directions(
    provider: Literal["google", "yandex"],
    origin: Coordinate,
    destination: Coordinate,
    travel_mode: str,
) -> List[Coordinate]:
    if provider == "yandex":
        return yandex_directions(origin, destination, travel_mode)
    return google_directions(origin, destination, travel_mode)
