"""Routing orchestration helpers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from typing import Dict

# FIX: rely on package-relative config imports when server is launched from repo root
from ..config import (
    APP_ROOT,
    GEO_PATH,
    ROADS_ASSIGN_JSON,
    ROADS_COLORED_KML,
    ROADS_PATH,
    ROUTES_KML,
)
from shapely.geometry import Point

from .kml_io import read_roads_kml, read_zone_kml
# FIX: resolve shared utilities relative to the project package
from ..utils.progress import emit_progress

from .geo import is_valid_coord


def build_routes(settings: Dict[str, object], socketio) -> Dict[str, object]:
    if not ROADS_ASSIGN_JSON.exists():
        emit_progress("route", "⚠️ Нет назначений дорог", 0)
        return {"exit_code": 0, "lengths": {}, "segments": {}}

    args = [
        sys.executable,
        str(APP_ROOT / "route_builder_grid_v7_1.py"),
        "--geo",
        str(GEO_PATH),
        "--roads",
        str(ROADS_PATH),
        "--roads-assignment",
        str(ROADS_ASSIGN_JSON),
        "--output",
        str(ROUTES_KML),
        "--grid-cells",
        str(settings.get("grid_cells", 64)),
        "--n-units",
        str(settings.get("n_units", 18)),
        "--target-km",
        str(settings.get("target_km", 30_000.0)),
        "--travel-mode",
        str(settings.get("travel_mode", "driving")),
        "--max-waypoints",
        str(settings.get("max_waypoints", 23)),
        "--base-lat",
        str(settings.get("base_lat", 0.0)),
        "--base-lon",
        str(settings.get("base_lon", 0.0)),
        "--request-pause",
        str(settings.get("request_pause", 0.35)),
        "--sample-every-m",
        str(settings.get("sample_every_m", 150.0)),
        "--roads-colored",
        str(ROADS_COLORED_KML),
    ]
    if settings.get("use_base_as_start", True):
        args.append("--use-base-start")
    routing_provider = str(settings.get("routing_provider", "google"))
    args.extend(["--provider", routing_provider])

    base_lat = float(settings.get("base_lat", 0.0))
    base_lon = float(settings.get("base_lon", 0.0))
    if settings.get("validate_coord_order", True) and not is_valid_coord(base_lat, base_lon):
        emit_progress("route", "⚠️ Неверные координаты базы", 0)
        return
    emit_progress(
        "route",
        f"[BASE] Старт маршрутов от базы: {base_lat:.6f}, {base_lon:.6f}",
        8,
    )
    try:
        boundary = read_zone_kml(GEO_PATH)
        roads = read_roads_kml(ROADS_PATH, boundary)
        if roads:
            base_point = Point(base_lon, base_lat)
            nearest = min((road.geometry.distance(base_point) for road in roads), default=None)
            if nearest is not None:
                nearest_km = nearest * 111.139
                emit_progress("route", f"[DEBUG] Проверено расстояние до ближайшей линии: {nearest_km:.2f} км", 10)
                if nearest_km > 100:
                    emit_progress("route", "⚠️ Координаты базы вне зоны", 12)
                elif nearest_km > 10:
                    emit_progress("route", "⚠️ Нет дорог рядом с базой", 12)
    except Exception as exc:
        emit_progress("route", f"⚠️ Не удалось проверить координаты базы: {exc}", 10)

    env = os.environ.copy()
    process = subprocess.Popen(
        args,
        cwd=str(APP_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    stage_keywords = {
        "чтение": ("📘 Читаю входные файлы", 10),
        "кластер": ("🔹 Готовлю линии", 30),
        "построение": ("🛠 Строю маршруты", 60),
        "экспорт": ("💾 Экспортирую KML", 90),
    }

    segment_counts: dict[str, int] = defaultdict(int)
    route_lengths: dict[str, float] = {}
    build_started = time.time()

    for raw in iter(process.stdout.readline, ""):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[ROUTE_STEP]"):
            try:
                payload = line.split("]", 1)[1].strip()
                tractor_part, coords_part = payload.split("coords=", 1)
                tractor_id = tractor_part.replace("tractor=", "").strip()
                coords = json.loads(coords_part)
            except Exception:
                continue
            emit_progress("route", f"Маршрут {tractor_id}: {len(coords)} точек", None)
            socketio.emit("route_step", {"tractor_id": tractor_id, "polyline": coords})
            if tractor_id:
                segment_counts[tractor_id] += max(len(coords) - 1, 0)
        elif line.startswith("[ROUTE_DONE]"):
            message = line.split("]", 1)[1].strip()
            emit_progress("route", message, 95)
            tokens = {
                part.split("=", 1)[0].strip(): part.split("=", 1)[1].strip()
                for part in message.replace("\u202f", " ").split()
                if "=" in part
            }
            tractor_id = tokens.get("tractor")
            length_val = tokens.get("length")
            length = float(length_val) if length_val is not None else None
            if tractor_id and length is not None:
                route_lengths[tractor_id] = length
                socketio.emit("tractor_done", {"tractor": tractor_id, "length": length})
            else:
                socketio.emit("tractor_done", {})
        else:
            lowered = line.lower()
            matched = False
            for key, (text, pct) in stage_keywords.items():
                if key in lowered:
                    emit_progress("route", text, pct)
                    matched = True
                    break
            if not matched:
                emit_progress("route", line, None)

    code = process.wait()
    duration = time.time() - build_started

    if code == 0:
        emit_progress("route", "✅ Маршруты построены", 100)
    else:
        emit_progress("route", f"❌ Ошибка построения (код {code})", 100)
    return {
        "exit_code": code,
        "lengths": route_lengths,
        "segments": dict(segment_counts),
        "duration_sec": duration,
    }
