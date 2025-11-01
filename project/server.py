from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO

from route_builder import TRACTOR_COLORS
from route_builder.grid import (
    generate_grid,
    load_geo_boundary,
    save_grid_geojson,
)
from route_builder.optimizer import (
    Tractor,
    assign_cells_kmeans,
    assign_roads_advanced,
    assign_roads_simple,
)
from route_builder.roads import load_roads
from utils.validator import validate_geo_kml
import subprocess

APP_ROOT = Path(__file__).resolve().parent
DATA_DIR = APP_ROOT / "data"
CACHE_DIR = APP_ROOT / "cache"
GRID_PATH = DATA_DIR / "grid.geojson"
ASSIGNMENTS_PATH = DATA_DIR / "assignments.json"
ROADS_ASSIGN_PATH = DATA_DIR / "roads_assignment.json"
ROUTES_KML = DATA_DIR / "routes_grid.kml"
ROADS_COLORED = DATA_DIR / "roads_colored.kml"
SETTINGS_PATH = APP_ROOT / "app_settings.json"
GEO_PATH = DATA_DIR / "GEO.kml"
ROADS_PATH = DATA_DIR / "RoadCity.kml"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(APP_ROOT / ".env", override=True)

app = Flask(__name__, template_folder=str(APP_ROOT / "templates"), static_folder=str(APP_ROOT / "static"))
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change_me")
socketio = SocketIO(app, cors_allowed_origins="*")

_build_lock = threading.Lock()
_build_thread: threading.Thread | None = None
_current_settings: Dict[str, object] = {}
_grid_error: str | None = None
_assignments: Dict[str, str] = {}
_roads_cache: dict | None = None


def _emit_log(message: str) -> None:
    payload = {"message": message}
    socketio.emit("progress", payload)
    socketio.emit("log", payload)


def load_settings() -> Dict[str, object]:
    defaults = {
        "grid_cells": 64,
        "n_units": 18,
        "target_km": float(os.getenv("TARGET_KM_PER_TRACTOR", "30000")),
        "travel_mode": os.getenv("TRAVEL_MODE", "driving"),
        "max_waypoints": int(os.getenv("GOOGLE_MAX_WAYPOINTS", "23")),
        "base_lat": float(os.getenv("CENTER_LAT", "54.920031")),
        "base_lon": float(os.getenv("CENTER_LON", "37.408090")),
        "center_lat": float(os.getenv("CENTER_LAT", "54.920031")),
        "center_lon": float(os.getenv("CENTER_LON", "37.408090")),
        "use_base_as_start": os.getenv("USE_BASE_BY_DEFAULT", "true").lower() == "true",
        "request_pause": float(os.getenv("REQUEST_PAUSE", "0.35")),
        "sample_every_m": float(os.getenv("SAMPLE_EVERY_M", "150")),
        "street_source": os.getenv("STREET_SOURCE", "google"),
        "advanced": os.getenv("ADVANCED_OPTIMIZATION", "false").lower() == "true",
        "mode": os.getenv("USE_GRID_BY_DEFAULT", "true").lower() == "true" and "grid" or "road",
    }
    if SETTINGS_PATH.exists():
        try:
            defaults.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return defaults


def save_settings(settings: Dict[str, object]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def tractors_for_settings(settings: Dict[str, object]) -> List[Tractor]:
    tractors: List[Tractor] = []
    for idx in range(int(settings.get("n_units", 18))):
        tractors.append(
            Tractor(
                id=f"tractor_{idx+1:02d}",
                name=f"Трактор {idx+1:02d}",
                color=TRACTOR_COLORS[idx % len(TRACTOR_COLORS)],
                base=(float(settings.get("base_lat", 0.0)), float(settings.get("base_lon", 0.0))),
            )
        )
    return tractors


def ensure_grid_exists(grid_cells: int, force: bool = False) -> None:
    global _grid_error
    if not GEO_PATH.exists():
        _grid_error = "Отсутствует GEO.kml в директории data/"
        socketio.emit("grid_error", {"message": _grid_error})
        _emit_log(f"[{_timestamp()}] [GRID ERROR] {_grid_error}")
        return
    is_valid, err = validate_geo_kml(GEO_PATH)
    if not is_valid:
        _grid_error = err or "Некорректный GEO.kml"
        socketio.emit("grid_error", {"message": _grid_error})
        _emit_log(f"[{_timestamp()}] [GRID ERROR] {_grid_error}")
        return
    try:
        if GRID_PATH.exists() and not force:
            data = json.loads(GRID_PATH.read_text(encoding="utf-8"))
            meta = data.get("metadata", {})
            if int(meta.get("grid_cells", grid_cells)) == grid_cells and data.get("features"):
                _grid_error = None
                socketio.emit("grid_error", {"message": None})
                socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
                return
        _emit_log(f"[{_timestamp()}] [GRID] Читаю GEO.kml и строю сетку на {grid_cells} клеток")
        boundary = load_geo_boundary(GEO_PATH)
        cells = generate_grid(boundary, grid_cells)
        save_grid_geojson(cells, GRID_PATH, grid_cells)
        _grid_error = None
        socketio.emit("grid_error", {"message": None})
        socketio.emit("grid_ready", {"cells": len(cells)})
        _emit_log(f"[{_timestamp()}] [GRID] Сетка обновлена ({len(cells)} клеток)")
    except Exception as exc:
        _grid_error = str(exc)
        _emit_log(f"[{_timestamp()}] [GRID ERROR] {_grid_error}")
        socketio.emit("grid_error", {"message": _grid_error})


def load_assignments() -> Dict[str, str]:
    if not ASSIGNMENTS_PATH.exists():
        return {}
    try:
        return json.loads(ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _ensure_roads_geojson():
    global _roads_cache
    if _roads_cache is not None:
        return _roads_cache
    if not (ROADS_PATH.exists() and GEO_PATH.exists()):
        _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ RoadCity.kml или GEO.kml не найдены")
        return None
    try:
        boundary = load_geo_boundary(GEO_PATH)
        roads = load_roads(ROADS_PATH, boundary)
    except Exception as exc:
        _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ Не удалось загрузить дороги: {exc}")
        return None
    total_len = sum(road.geometry.length for road in roads) * 111_139
    _emit_log(
        f"[{_timestamp()}] [ROADS] Найдено {len(roads)} линий, общая длина {total_len/1000:.1f} км"
    )
    features = []
    for road in roads:
        coords = [[lon, lat] for lon, lat in road.geometry.coords]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"id": road.id, "name": road.name},
            }
        )
    _roads_cache = {"type": "FeatureCollection", "features": features}
    return _roads_cache


@app.get("/")
def index():
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    return render_template(
        "map.html",
        settings=_current_settings,
        grid_error=_grid_error,
        has_google_key=bool(os.getenv("GOOGLE_MAPS_API_KEY")),
        has_yandex_key=bool(os.getenv("YANDEX_GEOCODER_API_KEY")),
        tractor_colors=TRACTOR_COLORS,
    )


@app.get("/grid")
def grid_geojson():
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    if _grid_error:
        return jsonify({"error": _grid_error}), 409
    return jsonify(json.loads(GRID_PATH.read_text(encoding="utf-8")))


@app.get("/routes_grid.kml")
def download_kml():
    if not ROUTES_KML.exists():
        return jsonify({"error": "Файл маршрутов отсутствует"}), 404
    return send_file(
        ROUTES_KML,
        mimetype="application/vnd.google-earth.kml+xml",
        as_attachment=True,
        download_name="routes_grid.kml",
    )


@app.get("/roads")
def roads_geojson():
    geojson = _ensure_roads_geojson()
    if geojson is None:
        return jsonify({"error": "Файл дорог отсутствует"}), 404
    return jsonify(geojson)


@app.post("/settings")
def update_settings():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Некорректный JSON"}), 400
    if not isinstance(payload, dict):
        return jsonify({"error": "Ожидался JSON-объект"}), 400
    validators = {
        "grid_cells": lambda v: max(4, int(v)),
        "n_units": lambda v: max(1, int(v)),
        "target_km": lambda v: max(1000.0, float(v)),
        "travel_mode": lambda v: str(v),
        "max_waypoints": lambda v: min(25, max(5, int(v))),
        "use_base_as_start": lambda v: bool(v) if isinstance(v, bool) else str(v).lower() == "true",
        "base_lat": float,
        "base_lon": float,
        "center_lat": float,
        "center_lon": float,
        "mode": lambda v: v if v in {"grid", "road"} else "grid",
        "advanced": lambda v: bool(v) if isinstance(v, bool) else str(v).lower() == "true",
        "street_source": lambda v: v if v in {"google", "yandex"} else "google",
        "request_pause": float,
        "sample_every_m": float,
    }
    for key, value in payload.items():
        if key in validators:
            try:
                _current_settings[key] = validators[key](value)
            except (TypeError, ValueError):
                return jsonify({"error": f"Неверное значение для {key}"}), 400
        else:
            _current_settings[key] = value
    save_settings(_current_settings)
    socketio.emit("settings", {"settings": _current_settings})
    if "grid_cells" in payload:
        ensure_grid_exists(int(payload["grid_cells"]), force=True)
        if _grid_error:
            return jsonify({"status": "error", "error": _grid_error}), 400
    return jsonify({"success": True, "settings": _current_settings})


@app.post("/auto_assign")
def auto_assign():
    mode = request.args.get("mode", "grid")
    advanced = request.args.get("advanced", "false").lower() == "true"
    street_source = request.args.get("streetSource", _current_settings.get("street_source", "google"))
    tractors = tractors_for_settings(_current_settings)
    if not tractors:
        return jsonify({"error": "Нет тракторов"}), 400
    if mode == "grid":
        if not GRID_PATH.exists():
            ensure_grid_exists(int(_current_settings.get("grid_cells", 64)), force=True)
        features = json.loads(GRID_PATH.read_text(encoding="utf-8"))
        roads_for_cells = None
        if advanced and ROADS_PATH.exists():
            try:
                boundary = load_geo_boundary(GEO_PATH)
                roads_for_cells = load_roads(ROADS_PATH, boundary)
            except Exception as exc:
                socketio.emit(
                    "progress",
                    {
                        "message": f"[{_timestamp()}] ⚠️ Не удалось загрузить дороги для оптимизации сетки: {exc}"
                    },
                )
        _emit_log(
            f"[{_timestamp()}] [ASSIGN] Автораспределение сетки (advanced={advanced})"
        )
        assignments, summary = assign_cells_kmeans(
            features["features"],
            tractors,
            advanced=advanced,
            roads=roads_for_cells,
        )
        _assignments.clear()
        _assignments.update(assignments)
        ASSIGNMENTS_PATH.write_text(json.dumps(assignments, ensure_ascii=False, indent=2), encoding="utf-8")
        socketio.emit("assignments", assignments)
        for item in summary:
            _emit_log(
                f"[{_timestamp()}] [ASSIGN] {item['tractor']} → {int(item['cells'])} клеток, "
                f"{item['area_km2']:.2f} км², дорог {item['roads_m']:.0f} м"
            )
        return jsonify({"success": True, "assigned": len(assignments), "summary": summary})
    else:
        if not ROADS_PATH.exists():
            return jsonify({"error": "Нет RoadCity.kml"}), 400
        global _roads_cache
        _roads_cache = None
        try:
            boundary = load_geo_boundary(GEO_PATH)
            roads = load_roads(ROADS_PATH, boundary)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        if advanced:
            assignments, stats, summary = assign_roads_advanced(
                roads,
                tractors,
                float(_current_settings.get("target_km", 30_000.0)),
                street_source=street_source,
            )
            DATA_DIR.joinpath("street_summary.csv").write_text(
                "Трактор;Улица;Длина,м\n" + "\n".join(
                    f"{tractor};{street};{length:.1f}" for tractor, street, length in summary
                ),
                encoding="utf-8",
            )
        else:
            assignments, stats = assign_roads_simple(roads, tractors)
            summary = []
        ROADS_ASSIGN_PATH.write_text(json.dumps(assignments, ensure_ascii=False, indent=2), encoding="utf-8")
        socketio.emit("roads_assignment", assignments)
        _emit_log(
            f"[{_timestamp()}] [ASSIGN] Автораспределение дорог завершено. {sum(stats.values()):.0f} м покрыто"
        )
        return jsonify({"success": True, "assigned": len(assignments), "summary": summary})


@app.post("/build_routes")
def build_routes_endpoint():
    global _build_thread
    if _grid_error:
        return jsonify({"error": _grid_error}), 400
    if _build_thread and _build_thread.is_alive():
        return jsonify({"error": "Маршрутизация уже запущена"}), 409
    if not ROADS_ASSIGN_PATH.exists():
        return jsonify({"error": "Нет назначений дорог"}), 400
    socketio.emit("clear_routes", {})
    _emit_log(f"[{_timestamp()}] [ROUTE] Очистка маршрутов и запуск расчёта")
    _build_thread = threading.Thread(target=_background_build, daemon=True)
    _build_thread.start()
    return jsonify({"success": True})


@app.post("/clear_routes")
def clear_routes_endpoint():
    if ROUTES_KML.exists():
        try:
            ROUTES_KML.unlink()
        except OSError:
            pass
    socketio.emit("clear_routes", {})
    return jsonify({"success": True})


def _background_build() -> None:
    if not _build_lock.acquire(blocking=False):
        socketio.emit("progress", {"message": "Маршрутизатор уже запущен"})
        return
    try:
        _emit_log(f"[{_timestamp()}] [ROUTE] Запуск маршрутизации…")
        args = [
            sys.executable,
            str(APP_ROOT / "route_builder_grid_v7_1.py"),
            "--geo",
            str(GEO_PATH),
            "--roads",
            str(ROADS_PATH),
            "--roads-assignment",
            str(ROADS_ASSIGN_PATH),
            "--output",
            str(ROUTES_KML),
            "--grid-cells",
            str(_current_settings.get("grid_cells", 64)),
            "--n-units",
            str(_current_settings.get("n_units", 18)),
            "--target-km",
            str(_current_settings.get("target_km", 30000.0)),
            "--travel-mode",
            str(_current_settings.get("travel_mode", "driving")),
            "--max-waypoints",
            str(_current_settings.get("max_waypoints", 23)),
            "--base-lat",
            str(_current_settings.get("base_lat", 0.0)),
            "--base-lon",
            str(_current_settings.get("base_lon", 0.0)),
            "--center-lat",
            str(_current_settings.get("center_lat", 0.0)),
            "--center-lon",
            str(_current_settings.get("center_lon", 0.0)),
            "--request-pause",
            str(_current_settings.get("request_pause", 0.35)),
            "--sample-every-m",
            str(_current_settings.get("sample_every_m", 150.0)),
            "--roads-colored",
            str(ROADS_COLORED),
        ]
        if bool(_current_settings.get("use_base_as_start", True)):
            args.append("--use-base-start")
        process = subprocess.Popen(
            args,
            cwd=str(APP_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        stage_keywords = {
            "чтение": "📘 Загружаю исходные файлы",
            "построение": "🛠 Строю маршруты",
            "экспорт": "💾 Сохраняю результат",
        }
        for raw_line in iter(process.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[STATUS]"):
                _emit_log(line.split("]", 1)[1].strip())
                continue
            if line.startswith("[ROUTE_STEP]"):
                payload = line.split("]", 1)[1].strip()
                parts = {}
                for chunk in payload.split():
                    if "=" in chunk:
                        key, value = chunk.split("=", 1)
                        parts[key] = value
                tractor = parts.get("tractor")
                lat = parts.get("lat")
                lon = parts.get("lon")
                if tractor and lat and lon:
                    socketio.emit(
                        "route_step",
                        {
                            "tractor_id": tractor,
                            "coords": [[float(lat), float(lon)]],
                        },
                    )
                continue
            if line.startswith("[ROUTE_DONE]"):
                _emit_log(line)
                socketio.emit("tractor_done", {})
                continue
            lowered = line.lower()
            for key, msg in stage_keywords.items():
                if key in lowered:
                    _emit_log(msg)
                    break
            _emit_log(line)
        code = process.wait()
        if code == 0:
            _emit_log(f"[{_timestamp()}] [DONE] ✅ Маршруты построены")
            socketio.emit("build_done", {"success": True})
            if ROUTES_KML.exists():
                socketio.emit("routes_ready", {})
        else:
            _emit_log(f"[{_timestamp()}] [ERROR] ❌ Ошибка subprocess ({code})")
            socketio.emit("build_done", {"success": False})
    except Exception as exc:
        _emit_log(f"[{_timestamp()}] [ERROR] Ошибка запуска: {exc}")
        socketio.emit("build_done", {"success": False})
    finally:
        global _build_thread
        _build_thread = None
        _build_lock.release()


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


@socketio.on("connect")
def on_connect():
    socketio.emit("settings", {"settings": _current_settings})
    if _assignments:
        socketio.emit("assignments", _assignments)
    if ROADS_ASSIGN_PATH.exists():
        socketio.emit("roads_assignment", json.loads(ROADS_ASSIGN_PATH.read_text(encoding="utf-8")))
    if _grid_error:
        socketio.emit("grid_error", {"message": _grid_error})
    else:
        socketio.emit("grid_error", {"message": None})
        if GRID_PATH.exists():
            try:
                data = json.loads(GRID_PATH.read_text(encoding="utf-8"))
                socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
            except Exception:
                pass


@socketio.on("assign_sector")
def on_assign_sector(payload):
    sector_id = payload.get("sector_id")
    tractor_id = payload.get("tractor_id")
    if not sector_id or not tractor_id:
        return
    _assignments[sector_id] = tractor_id
    ASSIGNMENTS_PATH.write_text(json.dumps(_assignments, ensure_ascii=False, indent=2), encoding="utf-8")
    socketio.emit("assignments", _assignments)


@socketio.on("assignments_reset")
def on_assignments_reset(payload):
    tractor_id = payload.get("tractor_id") if isinstance(payload, dict) else None
    if tractor_id:
        remaining = {k: v for k, v in _assignments.items() if v != tractor_id}
        _assignments.clear()
        _assignments.update(remaining)
    else:
        _assignments.clear()
    ASSIGNMENTS_PATH.write_text(json.dumps(_assignments, ensure_ascii=False, indent=2), encoding="utf-8")
    socketio.emit("assignments", _assignments)


@app.errorhandler(Exception)
def handle_error(exc):
    return jsonify({"error": str(exc)}), 500


def bootstrap():
    global _current_settings, _assignments, _roads_cache
    _current_settings = load_settings()
    _assignments = load_assignments()
    _roads_cache = None
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))


if __name__ == "__main__":
    bootstrap()
    socketio.run(
        app,
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "5000")),
    )
