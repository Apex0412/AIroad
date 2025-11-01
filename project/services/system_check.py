"""System diagnostics helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import requests

from config import GEO_PATH, ROADS_PATH
from services.kml_io import ZoneLoadError, read_roads_kml, read_zone_kml
from utils.validator import validate_geo_kml

REQUEST_TIMEOUT = 6


@dataclass
class CheckItem:
    kind: str
    label: str
    status: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "message": self.message,
        }


def _check_geo() -> CheckItem:
    if not GEO_PATH.exists():
        return CheckItem("file", "GEO.kml", "warn", "Файл GEO.kml не найден")
    valid, message = validate_geo_kml(GEO_PATH)
    if not valid:
        return CheckItem("file", "GEO.kml", "warn", message or "Некорректный GEO.kml")
    try:
        boundary = read_zone_kml(GEO_PATH)
    except ZoneLoadError as exc:  # pragma: no cover - defensive
        return CheckItem("file", "GEO.kml", "warn", str(exc))
    area_km = boundary.area * 12_400  # приблизительная площадь в км²
    return CheckItem("file", "GEO.kml", "ok", f"Граница загружена, площадь ~{area_km:.2f} км²")


def _check_roads(boundary) -> CheckItem:
    if not ROADS_PATH.exists():
        return CheckItem("file", "RoadCity.kml", "warn", "Файл RoadCity.kml не найден")
    try:
        roads = read_roads_kml(ROADS_PATH, boundary)
    except Exception as exc:  # pragma: no cover - defensive
        return CheckItem("file", "RoadCity.kml", "warn", f"Не удалось прочитать: {exc}")
    total = sum(road.geometry.length for road in roads) * 111_139
    return CheckItem("file", "RoadCity.kml", "ok", f"Найдено {len(roads)} линий, {total/1000:.1f} км")


def _check_google(api_key: str | None, origin: tuple[float, float], dest: tuple[float, float]) -> CheckItem:
    if not api_key:
        return CheckItem("api", "Google Directions", "warn", "Ключ Google API отсутствует")
    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        "origin": f"{origin[0]},{origin[1]}",
        "destination": f"{dest[0]},{dest[1]}",
        "mode": "driving",
        "key": api_key,
    }
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        data = response.json()
    except Exception as exc:  # pragma: no cover - network errors
        return CheckItem("api", "Google Directions", "warn", f"Ошибка запроса: {exc}")
    status = data.get("status")
    if status == "OK":
        legs = data["routes"][0]["legs"][0]
        distance_km = legs["distance"]["value"] / 1000
        return CheckItem("api", "Google Directions", "ok", f"Маршрут доступен ({distance_km:.2f} км)")
    return CheckItem("api", "Google Directions", "warn", f"API вернул статус {status}")


def _check_yandex(api_key: str | None, point: tuple[float, float]) -> CheckItem:
    if not api_key:
        return CheckItem("api", "Yandex Geocoder", "warn", "Ключ Yandex API отсутствует")
    params = {
        "format": "json",
        "geocode": f"{point[1]},{point[0]}",
        "apikey": api_key,
        "kind": "street",
        "results": 1,
    }
    try:
        resp = requests.get("https://geocode-maps.yandex.ru/1.x/", params=params, timeout=REQUEST_TIMEOUT)
        data = resp.json()
    except Exception as exc:  # pragma: no cover - network errors
        return CheckItem("api", "Yandex Geocoder", "warn", f"Ошибка запроса: {exc}")
    try:
        collection = data["response"]["GeoObjectCollection"]["featureMember"]
    except KeyError:  # pragma: no cover - defensive
        return CheckItem("api", "Yandex Geocoder", "warn", "Неожиданный ответ Yandex")
    if not collection:
        return CheckItem("api", "Yandex Geocoder", "warn", "Адрес не найден")
    name = collection[0]["GeoObject"]["name"]
    return CheckItem("api", "Yandex Geocoder", "ok", f"Результат: {name}")


def run_system_check(
    *,
    base_lat: float,
    base_lon: float,
    google_key: str | None,
    yandex_key: str | None,
) -> List[Dict[str, str]]:
    items: List[CheckItem] = []
    geo_item = _check_geo()
    items.append(geo_item)
    if geo_item.status == "ok":
        boundary = read_zone_kml(GEO_PATH)
    else:
        boundary = None
    if boundary is not None:
        items.append(_check_roads(boundary))
    else:
        items.append(CheckItem("file", "RoadCity.kml", "warn", "Геозона отсутствует"))

    offset = 0.01
    origin = (base_lat, base_lon)
    dest = (base_lat + offset, base_lon + offset)
    items.append(_check_google(google_key, origin, dest))
    items.append(_check_yandex(yandex_key, origin))
    return [item.to_dict() for item in items]
