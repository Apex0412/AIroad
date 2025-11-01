"""Streaming route construction utilities."""
from __future__ import annotations

import math
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from utils.apis import ApiError, request_directions

from .optimizer import Tractor
from .roads import Road

ProgressCb = Callable[[str], None]
RouteStepCb = Callable[[str, List[Tuple[float, float]]], None]
RouteDoneCb = Callable[[str, float], None]


@dataclass
class RoutingSettings:
    travel_mode: str
    max_waypoints: int
    request_pause: float
    sample_every_m: float
    use_base_start: bool
    base_lat: float
    base_lon: float
    target_km: float
    provider: str = "google"


def build_routes(
    roads: Iterable[Road],
    assignments: Dict[str, str],
    tractors: Sequence[Tractor],
    settings: RoutingSettings,
    progress_cb: Optional[ProgressCb] = None,
    route_step_cb: Optional[RouteStepCb] = None,
    route_done_cb: Optional[RouteDoneCb] = None,
) -> Dict[str, List[Tuple[float, float]]]:
    road_map = {road.id: road for road in roads}
    grouped: Dict[str, List[Road]] = {tractor.id: [] for tractor in tractors}
    for road_id, tractor_id in assignments.items():
        if road_id not in road_map or tractor_id not in grouped:
            continue
        grouped[tractor_id].append(road_map[road_id])

    routes: Dict[str, List[Tuple[float, float]]] = {tractor.id: [] for tractor in tractors}
    provider = settings.provider or "google"

    for tractor in tractors:
        lines = grouped.get(tractor.id, [])
        if not lines:
            continue
        if progress_cb:
            progress_cb(f"[STATUS] {tractor.name}: получено {len(lines)} линий")
        current_point = (settings.base_lat, settings.base_lon)
        if not settings.use_base_start:
            first = min(
                lines,
                key=lambda r: _haversine(current_point, (r.geometry.coords[0][1], r.geometry.coords[0][0])),
            )
            current_point = (first.geometry.coords[0][1], first.geometry.coords[0][0])
        visited = set()
        total_length = 0.0
        while len(visited) < len(lines):
            next_line = min(
                (line for line in lines if line.id not in visited),
                key=lambda line: _haversine(current_point, (line.geometry.coords[0][1], line.geometry.coords[0][0])),
            )
            if progress_cb:
                progress_cb(f"[STATUS] {tractor.name}: беру {next_line.name or next_line.id}")
            start = (next_line.geometry.coords[0][1], next_line.geometry.coords[0][0])
            if current_point != start:
                try:
                    leg = _fetch_directions(
                        origin=current_point,
                        destination=start,
                        provider=provider,
                        travel_mode=settings.travel_mode,
                        pause=settings.request_pause,
                        progress_cb=progress_cb,
                        context=f"{tractor.name}: переезд к {next_line.name or next_line.id}",
                    )
                except ApiError as exc:
                    if progress_cb:
                        progress_cb(f"[STATUS] {tractor.name}: {exc} — строю прямой сегмент")
                    leg = [current_point, start]
                total_length += _emit_leg(tractor.id, leg, route_step_cb)
                routes[tractor.id].extend([(lat, lon) for lat, lon in leg])
                current_point = start
            line_coords = [(lat, lon) for lon, lat in next_line.geometry.coords]
            total_length += _emit_linestring(tractor.id, line_coords, settings.sample_every_m, route_step_cb)
            routes[tractor.id].extend(line_coords)
            current_point = (next_line.geometry.coords[-1][1], next_line.geometry.coords[-1][0])
            visited.add(next_line.id)
        if route_done_cb:
            route_done_cb(tractor.id, total_length)
        if progress_cb:
            progress_cb(f"[STATUS] {tractor.name}: маршрут готов ({total_length:.2f} км)")
    return routes


def _emit_leg(tractor_id: str, coords: Sequence[Tuple[float, float]], callback: Optional[RouteStepCb]) -> float:
    if callback and coords:
        callback(tractor_id, list(coords))
    return _length_of_coords(coords)


def _emit_linestring(
    tractor_id: str,
    coords: Sequence[Tuple[float, float]],
    sample_every_m: float,
    callback: Optional[RouteStepCb],
) -> float:
    if not coords:
        return 0.0
    total = 0.0
    sampled: List[Tuple[float, float]] = []
    last = coords[0]
    sampled.append(last)
    for point in coords[1:]:
        dist = _haversine(last, point)
        total += dist
        if dist * 1000.0 >= sample_every_m:
            sampled.append(point)
            last = point
    if sampled[-1] != coords[-1]:
        sampled.append(coords[-1])
    if callback:
        callback(tractor_id, sampled)
    return total


def _fetch_directions(
    origin: Tuple[float, float],
    destination: Tuple[float, float],
    provider: str,
    travel_mode: str,
    pause: float,
    progress_cb: Optional[ProgressCb] = None,
    context: str = "",
) -> List[Tuple[float, float]]:
    if progress_cb:
        progress_cb(f"[STATUS] {context}: запрашиваю маршрут ({provider})")
    coords = request_directions(provider, origin, destination, travel_mode)
    if progress_cb:
        progress_cb(f"[STATUS] {context}: получено {len(coords)} точек")
    time.sleep(max(pause, 0.0))
    return coords


def _length_of_coords(coords: Sequence[Tuple[float, float]]) -> float:
    total = 0.0
    for start, end in zip(coords, coords[1:]):
        total += _haversine(start, end)
    return total


def _haversine(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h)) / 1000.0
