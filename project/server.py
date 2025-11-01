from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime
from typing import Dict, List

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from flask_socketio import SocketIO

from config import (
    APP_ROOT,
    ASSIGNMENTS_JSON,
    DATA_DIR,
    GEO_PATH,
    GRID_GEOJSON,
    ROADS_ASSIGN_JSON,
    ROADS_COLORED_KML,
    ROADS_PATH,
    ROUTES_KML,
    env_google_key,
    env_yandex_key,
    load_settings as config_load_settings,
    save_settings as config_save_settings,
)
from route_builder import TRACTOR_COLORS
from route_builder.grid import generate_grid, load_geo_boundary, save_grid_geojson
from route_builder.optimizer import (
    Tractor,
    assign_cells_kmeans,
    assign_roads_advanced,
    assign_roads_simple,
)
from route_builder.roads import load_roads
from utils.progress import emit_progress, init_progress, run_async
from utils.validator import validate_geo_kml
import subprocess

load_dotenv(APP_ROOT / ".env", override=True)

app = Flask(__name__, template_folder=str(APP_ROOT / "templates"), static_folder=str(APP_ROOT / "static"))
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change_me")
socketio = SocketIO(app, cors_allowed_origins="*")
init_progress(socketio)

_build_lock = threading.Lock()
_build_thread: threading.Thread | None = None
_current_settings: Dict[str, object] = {}
_grid_error: str | None = None
_assignments: Dict[str, str] = {}
_roads_cache: dict | None = None
_grid_build_lock = threading.Lock()
_auto_assign_lock = threading.Lock()


def _emit_log(message: str) -> None:
    app.logger.info(message)
    payload = {"message": message}
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
        "mode": "grid" if os.getenv("USE_GRID_BY_DEFAULT", "true").lower() == "true" else "road",
        "routing_provider": os.getenv("ROUTING_PROVIDER", "google"),
    }
    return config_load_settings(defaults)


def save_settings(settings: Dict[str, object]) -> None:
    config_save_settings(settings)


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


def ensure_grid_exists(grid_cells: int, force: bool = False, async_build: bool = False) -> None:
    """Ensure that a fresh grid GeoJSON exists, optionally rebuilding asynchronously."""

    global _grid_error
    if not GEO_PATH.exists():
        _grid_error = "Отсутствует GEO.kml в директории data/"
        socketio.emit("grid_error", {"message": _grid_error})
        emit_progress("grid", _grid_error, 0)
        return
    is_valid, err = validate_geo_kml(GEO_PATH)
    if not is_valid:
        _grid_error = err or "Некорректный GEO.kml"
        socketio.emit("grid_error", {"message": _grid_error})
        emit_progress("grid", _grid_error, 0)
        return

    needs_build = force or not GRID_GEOJSON.exists()
    if not needs_build:
        try:
            data = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
            meta = data.get("metadata", {})
            if not data.get("features"):
                needs_build = True
            elif int(meta.get("grid_cells", grid_cells)) != grid_cells:
                needs_build = True
            else:
                _grid_error = None
                socketio.emit("grid_error", {"message": None})
                socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
                socketio.emit("grid_updated", data)
        except Exception:
            needs_build = True

    if needs_build:
        if async_build:
            run_async(_grid_build_worker, grid_cells)
        else:
            _grid_build_worker(grid_cells)


def _grid_build_worker(grid_cells: int) -> None:
    global _grid_error, _roads_cache
    if not _grid_build_lock.acquire(blocking=False):
        return
    try:
        emit_progress("grid", f"Читаю GEO.kml…", 5)
        boundary = load_geo_boundary(GEO_PATH)
        emit_progress("grid", f"Генерирую сетку на {grid_cells} клеток…", 40)
        cells = generate_grid(boundary, grid_cells)
        emit_progress("grid", f"Сохраняю GeoJSON ({len(cells)} клеток)…", 75)
        save_grid_geojson(cells, GRID_GEOJSON, grid_cells)
        _roads_cache = None
        _grid_error = None
        data = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
        socketio.emit("grid_error", {"message": None})
        socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
        socketio.emit("grid_updated", data)
        emit_progress("grid", "✅ Сетка успешно построена", 100)
    except Exception as exc:  # pragma: no cover - defensive
        _grid_error = str(exc)
        socketio.emit("grid_error", {"message": _grid_error})
        emit_progress("grid", f"⚠️ {_grid_error}", 100)
    finally:
        _grid_build_lock.release()


def load_assignments() -> Dict[str, str]:
    if not ASSIGNMENTS_JSON.exists():
        return {}
    try:
        payload = json.loads(ASSIGNMENTS_JSON.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "assignments" in payload:
            return dict(payload.get("assignments") or {})
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}


def _ensure_roads_geojson():
    global _roads_cache
    if _roads_cache is not None:
        return _roads_cache
    if not (ROADS_PATH.exists() and GEO_PATH.exists()):
        _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ RoadCity.kml или GEO.kml не найдены")
        emit_progress("roads", "RoadCity.kml не найден", 0)
        return None
    try:
        emit_progress("roads", "Читаю RoadCity.kml…", 60)
        boundary = load_geo_boundary(GEO_PATH)
        roads = load_roads(ROADS_PATH, boundary)
    except Exception as exc:
        _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ Не удалось загрузить дороги: {exc}")
        emit_progress("roads", f"⚠️ Не удалось загрузить дороги: {exc}", 60)
        return None
    total_len = sum(road.geometry.length for road in roads) * 111_139
    _emit_log(
        f"[{_timestamp()}] [ROADS] Загрузил {len(roads)} линий, суммарно {total_len/1000:.1f} км"
    )
    emit_progress("roads", f"Загружено {len(roads)} линий", 75)
    features = []
    for road in roads:
        coords = [[lon, lat] for lon, lat in road.geometry.coords]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "id": road.id,
                    "name": road.name,
                    "length_m": round(road.geometry.length * 111_139, 1),
                },
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
        has_google_key=bool(env_google_key()),
        has_yandex_key=bool(env_yandex_key()),
        tractor_colors=TRACTOR_COLORS,
    )


@app.get("/grid")
def grid_geojson():
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    if _grid_error:
        return jsonify({"error": _grid_error}), 409
    return jsonify(json.loads(GRID_GEOJSON.read_text(encoding="utf-8")))


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
        "routing_provider": lambda v: v if v in {"google", "yandex"} else "google",
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
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)), force=True, async_build=True)
    if _grid_error:
        return jsonify({"status": "error", "error": _grid_error}), 400
    return jsonify({"success": True, "status": "started", "settings": _current_settings})


@app.post("/auto_assign")
def auto_assign():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", _current_settings.get("mode", "grid"))
    advanced = bool(payload.get("advanced", _current_settings.get("advanced", False)))
    street_source = payload.get("streetSource", _current_settings.get("street_source", "google"))
    tractors = tractors_for_settings(_current_settings)
    if not tractors:
        return jsonify({"error": "Нет тракторов"}), 400
    if mode == "grid" and _grid_error:
        return jsonify({"error": _grid_error}), 400
    if mode == "road" and not ROADS_PATH.exists():
        return jsonify({"error": "Нет RoadCity.kml"}), 400

    run_async(_auto_assign_task, mode, advanced, street_source, tractors)
    return jsonify({"ok": True, "status": "started"})


def _auto_assign_task(
    mode: str,
    advanced: bool,
    street_source: str,
    tractors: List[Tractor],
) -> None:
    global _assignments
    try:
        if mode == "grid":
            ensure_grid_exists(int(_current_settings.get("grid_cells", 64)), force=False)
            if _grid_error:
                emit_progress("assign", f"⚠️ {_grid_error}", 100)
                return
            data = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
            features = data.get("features", [])
            total = len(features)
            if not features:
                emit_progress("assign", "⚠️ Сетка пуста — нечего распределять", 100)
                return
            roads_for_cells = None
            if advanced and ROADS_PATH.exists():
                try:
                    boundary = load_geo_boundary(GEO_PATH)
                    roads_for_cells = load_roads(ROADS_PATH, boundary)
                except Exception as exc:
                    emit_progress("assign", f"⚠️ Не удалось загрузить дороги: {exc}", 10)
            emit_progress("assign", f"Запускаю автораспределение {total} клеток", 5)

            def progress_cb(done: int, total_cells: int) -> None:
                percent = int((done / total_cells) * 100) if total_cells else 0
                emit_progress("assign", f"Распределяю клетки: {done}/{total_cells}", percent)

            def cell_cb(cell_id: str, tractor_id: str, done: int, total_cells: int) -> None:
                if done <= 3 or done % 10 == 0 or done == total_cells:
                    socketio.emit(
                        "update_cell_assignment",
                        {"cell_id": cell_id, "tractor": tractor_id},
                    )

            assignments, summary = assign_cells_kmeans(
                features,
                tractors,
                advanced=advanced,
                roads=roads_for_cells,
                progress_callback=progress_cb,
                step_callback=cell_cb,
            )
            with _auto_assign_lock:
                _assignments.clear()
                _assignments.update(assignments)
                ASSIGNMENTS_JSON.write_text(
                    json.dumps(assignments, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            socketio.emit("assignments_updated", {"assignments": assignments, "summary": summary})
            for item in summary:
                _emit_log(
                    f"[{_timestamp()}] [ASSIGN] {item['tractor']} → {int(item['cells'])} клеток, "
                    f"{item['area_km2']:.2f} км², дорог {item['roads_m']:.0f} м"
                )
            emit_progress("assign", "✅ Автораспределение завершено", 100)
        else:
            emit_progress("assign", "Запускаю автораспределение дорог", 10)
            global _roads_cache
            _roads_cache = None
            boundary = load_geo_boundary(GEO_PATH)
            roads = load_roads(ROADS_PATH, boundary)
            if advanced:
                assignments, stats, summary = assign_roads_advanced(
                    roads,
                    tractors,
                    float(_current_settings.get("target_km", 30_000.0)),
                    street_source=street_source,
                )
                DATA_DIR.joinpath("street_summary.csv").write_text(
                    "Трактор;Улица;Длина,м\n"
                    + "\n".join(
                        f"{tractor};{street};{length:.1f}" for tractor, street, length in summary
                    ),
                    encoding="utf-8",
                )
            else:
                assignments, stats = assign_roads_simple(roads, tractors)
                summary = []
            ROADS_ASSIGN_JSON.write_text(
                json.dumps(assignments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            socketio.emit("roads_assignment", assignments)
            _emit_log(
                f"[{_timestamp()}] [ASSIGN] Автораспределение дорог завершено. {sum(stats.values()):.0f} м покрыто"
            )
            emit_progress("assign", "✅ Автораспределение дорог завершено", 100)
    except Exception as exc:  # pragma: no cover - defensive
        emit_progress("assign", f"⚠️ Ошибка автораспределения: {exc}", 100)


@app.post("/build_routes")
def build_routes_endpoint():
    global _build_thread
    if _grid_error:
        return jsonify({"error": _grid_error}), 400
    if _build_thread and _build_thread.is_alive():
        return jsonify({"error": "Маршрутизация уже запущена"}), 409
    if not ROADS_ASSIGN_JSON.exists():
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
        emit_progress("route", "Маршрутизатор уже запущен", 0)
        return
    socketio.emit("build_status", {"running": True})
    try:
        emit_progress("route", "🚀 Запуск маршрутизации…", 5)
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
            str(ROADS_COLORED_KML),
            "--provider",
            str(_current_settings.get("routing_provider", "google")),
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
            "чтение": ("📘 Загружаю исходные файлы", 20),
            "кластер": ("🔹 Распределяю участки", 45),
            "построение": ("🛠 Строю маршруты", 70),
            "экспорт": ("💾 Сохраняю результат", 90),
        }
        current_progress = 5
        for raw_line in iter(process.stdout.readline, ""):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("[STATUS]"):
                _emit_log(line.split("]", 1)[1].strip())
                continue
            if line.startswith("[ROUTE_STEP]"):
                payload = line.split("]", 1)[1].strip()
                if "coords=" in payload:
                    tractor_part, coords_part = payload.split("coords=", 1)
                    tractor = tractor_part.replace("tractor=", "").strip()
                    try:
                        coords = json.loads(coords_part)
                    except json.JSONDecodeError:
                        continue
                    if tractor and isinstance(coords, list):
                        socketio.emit(
                            "route_step",
                            {
                                "tractor_id": tractor,
                                "polyline": coords,
                            },
                        )
                continue
            if line.startswith("[ROUTE_DONE]"):
                _emit_log(line)
                payload = line.split("]", 1)[1].strip()
                info = {}
                for chunk in payload.split():
                    if "=" in chunk:
                        key, value = chunk.split("=", 1)
                        info[key] = value
                socketio.emit("tractor_done", info)
                continue
            lowered = line.lower()
            matched = False
            for key, msg in stage_keywords.items():
                if key in lowered:
                    text, percent = msg
                    current_progress = percent
                    emit_progress("route", text, current_progress)
                    matched = True
                    break
            if matched:
                continue
            _emit_log(line)
        code = process.wait()
        if code == 0:
            emit_progress("route", "✅ Маршруты построены", 100)
            socketio.emit("build_done", {"success": True})
            if ROUTES_KML.exists():
                socketio.emit("routes_ready", {})
        else:
            emit_progress("route", f"❌ Ошибка subprocess ({code})", 100)
            socketio.emit("build_done", {"success": False})
    except Exception as exc:
        emit_progress("route", f"❌ Ошибка запуска: {exc}", 100)
        socketio.emit("build_done", {"success": False})
    finally:
        global _build_thread
        _build_thread = None
        _build_lock.release()
        socketio.emit("build_status", {"running": False})


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


@socketio.on("connect")
def on_connect():
    socketio.emit("settings", {"settings": _current_settings})
    if _assignments:
        socketio.emit("assignments_updated", {"assignments": _assignments})
    if ROADS_ASSIGN_JSON.exists():
        socketio.emit("roads_assignment", json.loads(ROADS_ASSIGN_JSON.read_text(encoding="utf-8")))
    if _grid_error:
        socketio.emit("grid_error", {"message": _grid_error})
    else:
        socketio.emit("grid_error", {"message": None})
        if GRID_GEOJSON.exists():
            try:
                data = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
                socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
                socketio.emit("grid_updated", data)
            except Exception:
                pass


@socketio.on("assign_sector")
def on_assign_sector(payload):
    sector_id = payload.get("sector_id")
    tractor_id = payload.get("tractor_id")
    if not sector_id or not tractor_id:
        return
    _assignments[sector_id] = tractor_id
    ASSIGNMENTS_JSON.write_text(json.dumps(_assignments, ensure_ascii=False, indent=2), encoding="utf-8")
    socketio.emit("assignments_updated", {"assignments": _assignments})


@socketio.on("assignments_reset")
def on_assignments_reset(payload):
    tractor_id = payload.get("tractor_id") if isinstance(payload, dict) else None
    if tractor_id:
        remaining = {k: v for k, v in _assignments.items() if v != tractor_id}
        _assignments.clear()
        _assignments.update(remaining)
    else:
        _assignments.clear()
    ASSIGNMENTS_JSON.write_text(json.dumps(_assignments, ensure_ascii=False, indent=2), encoding="utf-8")
    socketio.emit("assignments_updated", {"assignments": _assignments})


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
