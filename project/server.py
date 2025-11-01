from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, List

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_socketio import SocketIO
from shapely.geometry import MultiPolygon, Point, Polygon

from config import (
    APP_ROOT,
    ASSIGNMENTS_JSON,
    DATA_DIR,
    GEO_PATH,
    GRID_GEOJSON,
    LOGS_DIR,
    ROADS_ASSIGN_JSON,
    ROADS_COLORED_KML,
    ROADS_PATH,
    ROUTES_KML,
    STATE_JSON,
    env_google_key,
    env_yandex_key,
    load_settings as config_load_settings,
    save_settings as config_save_settings,
)
from route_builder import TRACTOR_COLORS
from route_builder.optimizer import Tractor
from services import state as state_store
from services.bootstrap import ensure_initial_files
from services.state import (
    append_monitor_entry,
    reset_routes,
    set_assignments,
    set_grid,
    set_roads,
    set_route_stats,
    set_system_status,
)
from services.system_check import run_system_check
from services.assign import (
    auto_assign_cells,
    auto_assign_roads,
    load_cell_assignments,
)
from services.kml_io import (
    ZoneLoadError,
    build_grid,
    load_grid_file,
    read_roads_kml,
    read_zone_kml,
)
from services.routing import build_routes
from services.tasks import run_async
from utils.progress import emit_progress, init_progress
from utils.validator import validate_geo_kml

load_dotenv(APP_ROOT / ".env", override=True)

app = Flask(__name__, template_folder=str(APP_ROOT / "templates"), static_folder=str(APP_ROOT / "static"))
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change_me")
socketio = SocketIO(app, cors_allowed_origins="*")
# // FIX: socket server initialised without broadcast flags; keep reference for new health reporting

_session_log: List[str] = []
_log_file_path = None

STATE = state_store.STATE


def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _append_log_line(message: str) -> None:
    global _log_file_path
    stamped = message
    if not (stamped.startswith("[") and len(stamped) > 3 and stamped[1:3].isdigit()):
        stamped = f"[{_timestamp()}] {stamped}"
    _session_log.append(stamped)
    if len(_session_log) > 5000:
        del _session_log[: len(_session_log) - 5000]
    if _log_file_path is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        _log_file_path = LOGS_DIR / f"session_{timestamp}.log"
    try:
        with (_log_file_path).open("a", encoding="utf-8") as handle:
            handle.write(stamped + "\n")
    except OSError:
        pass


init_progress(socketio, log_hook=_append_log_line)

_build_lock = threading.Lock()
_build_thread: threading.Thread | None = None
_current_settings: Dict[str, object] = {}
_grid_error: str | None = None
_assignments: Dict[str, str] = {}
_roads_cache: dict | None = None
_grid_build_lock = threading.Lock()
_auto_assign_lock = threading.Lock()
_roads_lock = threading.Lock()
_build_start_time: float | None = None
_system_check_lock = threading.Lock()


def _collect_state_snapshot() -> Dict[str, object]:
    settings_copy = dict(_current_settings)
    assignments_copy = dict(_assignments)
    grid_meta = {
        "cells": len(STATE.grid.get("features", [])) if STATE.grid else 0,
        "exists": GRID_GEOJSON.exists(),
        "error": STATE.grid_error,
    }
    routes_meta = {
        "ready": STATE.routes_ready,
        "error": STATE.routes_error,
        "stats": STATE.route_stats,
    }
    snapshot = {
        "settings": settings_copy,
        "assignments": assignments_copy,
        "assignments_summary": STATE.assignments_summary,
        "grid": grid_meta,
        "routes": routes_meta,
        "theme": settings_copy.get("theme", "light"),
        "system_status": STATE.system_status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return snapshot


def _persist_state() -> None:
    try:
        snapshot = _collect_state_snapshot()
        STATE_JSON.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.warning("Не удалось сохранить state.json: %s", exc)


def _compose_route_stats(summary: Dict[str, object], duration: float) -> Dict[str, object]:
    lengths_raw = summary.get("lengths") or {}
    segments_raw = summary.get("segments") or {}
    lengths = {str(k): float(v) for k, v in lengths_raw.items()}
    segments = {str(k): int(v) for k, v in segments_raw.items()}
    total_km = sum(lengths.values())
    route_count = len(lengths)
    stats = {
        "routes": route_count,
        "total_km": round(total_km, 3),
        "average_km": round(total_km / route_count, 3) if route_count else 0.0,
        "cells": len(_assignments),
        "lengths": lengths,
        "segments": segments,
        "duration_sec": round(duration, 2),
        "provider": _current_settings.get("routing_provider"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return stats


def _load_state_file() -> None:
    global _assignments
    if not STATE_JSON.exists():
        return
    try:
        payload = json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        _emit_log(f"[{_timestamp()}] [STATE] ⚠️ Не удалось прочитать state.json: {exc}")
        return

    settings_payload = payload.get("settings")
    if isinstance(settings_payload, dict):
        _current_settings.update(settings_payload)

    theme_value = payload.get("theme")
    if isinstance(theme_value, str):
        _current_settings["theme"] = theme_value

    assignments_payload = payload.get("assignments")
    if isinstance(assignments_payload, dict):
        _assignments = {str(k): str(v) for k, v in assignments_payload.items()}
        set_assignments(_assignments, payload.get("assignments_summary"))
        if _assignments:
            ASSIGNMENTS_JSON.write_text(
                json.dumps(_assignments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    routes_payload = payload.get("routes") or {}
    STATE.routes_ready = bool(routes_payload.get("ready"))
    STATE.routes_error = routes_payload.get("error")
    stats_payload = routes_payload.get("stats")
    if stats_payload:
        set_route_stats(stats_payload)

    STATE.settings = dict(_current_settings)
    system_status_payload = payload.get("system_status")
    if isinstance(system_status_payload, list):
        set_system_status(system_status_payload)

    tractors = int(_current_settings.get("n_units", 0))
    grid_cells = payload.get("grid", {}).get("cells") or _current_settings.get("grid_cells")
    _emit_log(
        f"[{_timestamp()}] [STATE] Восстановлено предыдущее состояние — {tractors} тракторов, {grid_cells} клеток",
    )


def _emit_log(message: str) -> None:
    app.logger.info(message)
    payload = {"message": message}
    socketio.emit("log", payload)
    _append_log_line(message)
    append_monitor_entry({"message": message, "time": _timestamp()})


def _temporary_boundary() -> MultiPolygon:
    lat = float(_current_settings.get("center_lat", _current_settings.get("base_lat", 0.0)) or 0.0)
    lon = float(_current_settings.get("center_lon", _current_settings.get("base_lon", 0.0)) or 0.0)
    size = 0.05
    polygon = Polygon(
        [
            (lon - size, lat - size),
            (lon + size, lat - size),
            (lon + size, lat + size),
            (lon - size, lat + size),
            (lon - size, lat - size),
        ]
    )
    return MultiPolygon([polygon])


def _nearest_distance_km(lat: float, lon: float, roads) -> float | None:
    if not roads:
        return None
    point = Point(lon, lat)
    nearest = min((road.geometry.distance(point) for road in roads), default=None)
    if nearest is None:
        return None
    return nearest * 111.139


def _current_system_status() -> List[Dict[str, str]]:
    statuses: List[Dict[str, str]] = []
    statuses.append({
        "kind": "system",
        "label": "Flask",
        "status": "ok",
        "message": "Сервер активен",
    })
    statuses.append({
        "kind": "system",
        "label": "Socket.IO",
        "status": "ok",
        "message": "Веб-сокеты готовы",
    })
    statuses.append({
        "kind": "file",
        "label": "GEO.kml",
        "status": "ok" if GEO_PATH.exists() else "warn",
        "message": "GEO.kml найден" if GEO_PATH.exists() else "GEO.kml отсутствует",
    })
    statuses.append({
        "kind": "file",
        "label": "RoadCity.kml",
        "status": "ok" if ROADS_PATH.exists() else "warn",
        "message": "RoadCity.kml найден" if ROADS_PATH.exists() else "RoadCity.kml отсутствует",
    })
    statuses.append({
        "kind": "env",
        "label": "Google API",
        "status": "ok" if env_google_key() else "warn",
        "message": "Ключ Google API активен" if env_google_key() else "Ключ Google API отсутствует",
    })
    statuses.append({
        "kind": "env",
        "label": "Yandex API",
        "status": "ok" if env_yandex_key() else "warn",
        "message": "Ключ Yandex API активен" if env_yandex_key() else "Ключ Yandex API отсутствует",
    })
    return statuses


def _health_report() -> Dict[str, object]:
    # // FIX: compile detailed diagnostics for the /api/health endpoint
    report: Dict[str, object] = {"files": {}, "apis": {}, "settings": dict(_current_settings)}
    overall_status = "ok"
    boundary = None
    try:
        boundary = read_zone_kml(GEO_PATH)
        poly_count = len(list(boundary.geoms)) if getattr(boundary, "geoms", None) else 1
        report["files"]["geo"] = {"exists": True, "polygons": poly_count, "status": "ok"}
    except Exception as exc:
        overall_status = "warn"
        report["files"]["geo"] = {
            "exists": GEO_PATH.exists(),
            "polygons": 0,
            "status": "error",
            "error": str(exc),
        }
    try:
        roads = read_roads_kml(ROADS_PATH, boundary)
        report["files"]["roads"] = {"exists": True, "lines": len(roads), "status": "ok"}
    except Exception as exc:
        overall_status = "warn"
        report["files"]["roads"] = {
            "exists": ROADS_PATH.exists(),
            "lines": 0,
            "status": "error",
            "error": str(exc),
        }
    if GRID_GEOJSON.exists():
        try:
            grid_payload = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
            report["files"]["grid"] = {
                "exists": True,
                "cells": len(grid_payload.get("features", [])),
                "status": "ok",
            }
        except Exception as exc:
            overall_status = "warn"
            report["files"]["grid"] = {
                "exists": True,
                "cells": 0,
                "status": "error",
                "error": str(exc),
            }
    else:
        report["files"]["grid"] = {"exists": False, "cells": 0, "status": "warn"}
    report["apis"]["google_directions"] = {"configured": bool(env_google_key())}
    report["apis"]["yandex_geocoder"] = {"configured": bool(env_yandex_key())}
    report["routes_ready"] = bool(STATE.routes_ready and ROUTES_KML.exists())
    report["status"] = overall_status
    return report


def load_settings() -> Dict[str, object]:
    base_lat_env = os.getenv("BASE_LAT") or os.getenv("CENTER_LAT", "54.920031")
    base_lon_env = os.getenv("BASE_LON") or os.getenv("CENTER_LON", "37.408090")
    street_source_env = os.getenv("STREETS_PROVIDER") or os.getenv("STREET_SOURCE", "google")
    routing_provider_env = os.getenv("ROUTES_PROVIDER") or os.getenv("ROUTING_PROVIDER", "google")

    defaults = {
        "grid_cells": 64,
        "n_units": 18,
        "target_km": float(os.getenv("TARGET_KM_PER_TRACTOR", "30000")),
        "travel_mode": os.getenv("TRAVEL_MODE", "driving"),
        "max_waypoints": int(os.getenv("GOOGLE_MAX_WAYPOINTS", "23")),
        "base_lat": float(base_lat_env),
        "base_lon": float(base_lon_env),
        "center_lat": float(os.getenv("CENTER_LAT", "54.920031")),
        "center_lon": float(os.getenv("CENTER_LON", "37.408090")),
        "use_base_as_start": os.getenv("USE_BASE_BY_DEFAULT", "true").lower() == "true",
        "request_pause": float(os.getenv("REQUEST_PAUSE", "0.35")),
        "sample_every_m": float(os.getenv("SAMPLE_EVERY_M", "150")),
        "street_source": street_source_env,
        "advanced": os.getenv("ADVANCED_OPTIMIZATION", "false").lower() == "true",
        "mode": "grid" if os.getenv("USE_GRID_BY_DEFAULT", "true").lower() == "true" else "road",
        "routing_provider": routing_provider_env,
        "validate_coord_order": os.getenv("VALIDATE_COORD_ORDER", "true").lower() == "true",
        "theme": os.getenv("UI_THEME", "light"),
    }
    return config_load_settings(defaults)


def save_settings(settings: Dict[str, object]) -> None:
    config_save_settings(settings)
    STATE.settings = dict(settings)
    _persist_state()


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
                set_grid(data, error=None)
                socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
                socketio.emit("grid_updated", data)
                socketio.emit("map:update", {"grid": data})
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
        emit_progress("grid", "Читаю GEO.kml…", 5)
        try:
            if GEO_PATH.exists():
                is_valid, err = validate_geo_kml(GEO_PATH)
                if not is_valid:
                    raise ZoneLoadError(err or "Некорректный GEO.kml")
                boundary = read_zone_kml(GEO_PATH)
                _emit_log(f"[{_timestamp()}] [LOAD] Загружаю GEO.kml…")
            else:
                raise ZoneLoadError("GEO.kml отсутствует")
        except ZoneLoadError as exc:
            _emit_log(
                f"[{_timestamp()}] [LOAD] ⚠️ {exc} — создаю временную зону по координатам центра"
            )
            boundary = _temporary_boundary()
        emit_progress("grid", f"Генерирую сетку на {grid_cells} клеток…", 40)
        data = build_grid(boundary, grid_cells)
        emit_progress(
            "grid", f"Сохраняю GeoJSON ({len(data.get('features', []))} клеток)…", 75
        )
        _roads_cache = None
        _grid_error = None
        set_grid(data, error=None)
        # if build_grid already saved file, data already read
        socketio.emit("grid_error", {"message": None})
        socketio.emit("grid_ready", {"cells": len(data.get("features", []))})
        socketio.emit("grid_updated", data)
        socketio.emit("map:update", {"grid": data})
        emit_progress("grid", "✅ Сетка успешно построена", 100)
        _persist_state()
    except Exception as exc:  # pragma: no cover - defensive
        _grid_error = str(exc)
        set_grid(None, error=_grid_error)
        socketio.emit("grid_error", {"message": _grid_error})
        emit_progress("grid", f"⚠️ {_grid_error}", 100)
    finally:
        _grid_build_lock.release()


def load_assignments() -> Dict[str, str]:
    return load_cell_assignments()


def _ensure_roads_geojson():
    global _roads_cache
    if _roads_cache is not None:
        return _roads_cache
    if not _roads_lock.acquire(blocking=False):
        return _roads_cache
    try:
        if not ROADS_PATH.exists():
            message = "RoadCity.kml отсутствует"
            _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ {message}")
            emit_progress("roads", f"⚠️ {message}", 10)
            set_roads(None, error=message)
            return None
        try:
            boundary = read_zone_kml(GEO_PATH)
        except ZoneLoadError:
            boundary = _temporary_boundary()
        emit_progress("roads", "Читаю RoadCity.kml…", 60)
        roads = read_roads_kml(ROADS_PATH, boundary)
    except Exception as exc:
        message = f"Не удалось загрузить дороги: {exc}"
        _emit_log(f"[{_timestamp()}] [ROADS] ⚠️ {message}")
        emit_progress("roads", f"⚠️ {message}", 60)
        set_roads(None, error=message)
        return None
    finally:
        _roads_lock.release()
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
    set_roads(_roads_cache, error=None)
    socketio.emit("map:update", {"roads": _roads_cache})
    _persist_state()
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
        routes_ready=STATE.routes_ready and ROUTES_KML.exists(),
        route_stats=STATE.route_stats,
        system_status=STATE.system_status or _current_system_status(),
    )


@app.get("/grid")
def grid_geojson():
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    if _grid_error:
        return jsonify({"error": _grid_error}), 409
    return jsonify(json.loads(GRID_GEOJSON.read_text(encoding="utf-8")))


@app.post("/grid/generate")
def generate_grid_api():
    # // FIX: allow the UI to force grid regeneration on demand
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)), force=True)
    if _grid_error:
        return jsonify({"ok": False, "error": _grid_error}), 400
    payload = json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
    socketio.emit("grid_updated", payload)
    return jsonify({"ok": True, "cells": len(payload.get("features", [])), "grid": payload})


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


# // FIX: provide REST-style export endpoint while keeping existing downloader
@app.get("/export/kml")
def export_kml():
    return download_kml()


@app.get("/roads")
def roads_geojson():
    geojson = _ensure_roads_geojson()
    if geojson is None:
        return jsonify({"error": "Файл дорог отсутствует"}), 404
    return jsonify(geojson)


@app.get("/system/check")
def system_check():
    if not _system_check_lock.acquire(blocking=False):
        return jsonify({"status": "running"}), 202

    def worker():
        try:
            emit_progress("system", "🔍 Проверка системы…", 5)
            statuses = run_system_check(
                base_lat=float(_current_settings.get("base_lat", 0.0)),
                base_lon=float(_current_settings.get("base_lon", 0.0)),
                google_key=env_google_key(),
                yandex_key=env_yandex_key(),
            )
            set_system_status(statuses)
            for item in statuses:
                _emit_log(f"[{_timestamp()}] [TEST] {item['label']}: {item['message']}")
            socketio.emit("system_status", {"items": statuses})
            emit_progress("system", "✅ Проверка завершена", 100)
            socketio.emit("system_check_done", {"success": True})
            _persist_state()
        except Exception as exc:  # pragma: no cover - defensive
            _emit_log(f"[{_timestamp()}] [TEST] ⚠️ Ошибка проверки: {exc}")
            emit_progress("system", f"⚠️ {exc}", 100)
            socketio.emit("system_check_done", {"success": False, "error": str(exc)})
        finally:
            _system_check_lock.release()

    run_async(worker)
    return jsonify({"status": "started"})


@app.get("/api/state")
def api_state():
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    state_payload = {
        "settings": _current_settings,
        "grid": STATE.grid,
        "grid_error": STATE.grid_error,
        "roads": STATE.roads,
        "roads_error": STATE.roads_error,
        "assignments": STATE.assignments,
        "assignments_summary": STATE.assignments_summary,
        "routes_ready": STATE.routes_ready,
        "routes_error": STATE.routes_error,
        "route_stats": STATE.route_stats,
        "system_status": STATE.system_status,
    }
    return jsonify(state_payload)


@app.get("/api/health")
def api_health():
    report = _health_report()
    # // FIX: surface aggregated diagnostics for the system check panel
    _emit_log(
        f"[{_timestamp()}] [TEST] GEO: {report['files'].get('geo', {}).get('status')} | ROADS: {report['files'].get('roads', {}).get('status')}"
    )
    return jsonify(report)


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


@app.post("/theme")
def update_theme():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Некорректный JSON"}), 400
    theme = str(payload.get("theme", "")).strip().lower()
    if theme not in {"light", "dark"}:
        return jsonify({"error": "Неизвестная тема"}), 400
    _current_settings["theme"] = theme
    save_settings(_current_settings)
    socketio.emit("settings", {"settings": _current_settings})
    return jsonify({"success": True, "theme": theme})


# // FIX: expose legacy and new endpoints for auto assignment triggers
@app.post("/assign/auto")
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
                    boundary = read_zone_kml(GEO_PATH)
                except ZoneLoadError:
                    boundary = _temporary_boundary()
                try:
                    roads_for_cells = read_roads_kml(ROADS_PATH, boundary)
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

            assignments, summary = auto_assign_cells(
                features,
                tractors,
                advanced=advanced,
                roads=roads_for_cells,
                progress_cb=progress_cb,
                step_cb=cell_cb,
            )
            with _auto_assign_lock:
                _assignments.clear()
                _assignments.update(assignments)
                ASSIGNMENTS_JSON.write_text(
                    json.dumps(assignments, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            set_assignments(assignments, summary)
            socketio.emit("assignments_updated", {"assignments": assignments, "summary": summary})
            for item in summary:
                _emit_log(
                    f"[{_timestamp()}] [ASSIGN] {item['tractor']} → {int(item['cells'])} клеток, "
                    f"{item['area_km2']:.2f} км², дорог {item['roads_m']:.0f} м"
                )
            emit_progress("assign", "✅ Автораспределение завершено", 100)
            _persist_state()
        else:
            emit_progress("assign", "Запускаю автораспределение дорог", 10)
            global _roads_cache
            _roads_cache = None
            boundary = read_zone_kml(GEO_PATH)
            roads = read_roads_kml(ROADS_PATH, boundary)
            assignments, stats, summary = auto_assign_roads(
                roads,
                tractors,
                advanced=advanced,
                target_km=float(_current_settings.get("target_km", 30_000.0)),
                street_source=street_source,
            )
            if summary:
                DATA_DIR.joinpath("street_summary.csv").write_text(
                    "Трактор;Улица;Длина,м\n"
                    + "\n".join(
                        f"{tractor};{street};{length:.1f}" for tractor, street, length in summary
                    ),
                    encoding="utf-8",
                )
            ROADS_ASSIGN_JSON.write_text(
                json.dumps(assignments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            set_assignments(
                assignments,
                [
                    {
                        "tractor": tractor.name,
                        "roads_m": stats.get(tractor.id, 0.0),
                        "cells": 0,
                        "area_km2": 0.0,
                    }
                    for tractor in tractors
                ],
            )
            socketio.emit("roads_assignment", assignments)
            _emit_log(
                f"[{_timestamp()}] [ASSIGN] Автораспределение дорог завершено. {sum(stats.values()):.0f} м покрыто"
            )
            emit_progress("assign", "✅ Автораспределение дорог завершено", 100)
            _persist_state()
    except Exception as exc:  # pragma: no cover - defensive
        emit_progress("assign", f"⚠️ Ошибка автораспределения: {exc}", 100)


@app.post("/check_base")
def check_base():
    base_lat = float(_current_settings.get("base_lat", 0.0))
    base_lon = float(_current_settings.get("base_lon", 0.0))
    if not GEO_PATH.exists() or not ROADS_PATH.exists():
        message = "Нет GEO.kml или RoadCity.kml для проверки"
        emit_progress("base", f"⚠️ {message}", 0)
        return jsonify({"ok": False, "error": message}), 400
    try:
        try:
            boundary = read_zone_kml(GEO_PATH)
        except ZoneLoadError:
            boundary = _temporary_boundary()
        roads = read_roads_kml(ROADS_PATH, boundary)
        distance = _nearest_distance_km(base_lat, base_lon, roads)
        warning = None
        if distance is None:
            warning = "Дороги не найдены"
        elif distance > 10:
            warning = "Нет дорог рядом с базой, проверь координаты"
        progress_text = (
            f"Проверка базы завершена: {distance:.2f} км" if distance is not None else "Дороги не найдены"
        )
        emit_progress("base", progress_text, 55)
        return jsonify(
            {
                "ok": True,
                "distance_km": distance,
                "base_lat": base_lat,
                "base_lon": base_lon,
                "warning": warning,
            }
        )
    except Exception as exc:  # pragma: no cover - defensive
        emit_progress("base", f"⚠️ Ошибка проверки базы: {exc}", 0)
        return jsonify({"ok": False, "error": str(exc)}), 500


# // FIX: support both legacy and RESTful endpoints for route builds
@app.post("/routes/build")
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
    reset_routes()
    set_route_stats(None)
    socketio.emit("clear_routes", {})
    _persist_state()
    return jsonify({"success": True})


@app.get("/logs/current")
def download_log():
    filename = "dispatcher_log.txt"
    content = "\n".join(_session_log) if _session_log else "Лог пуст."
    headers = {
        "Content-Disposition": f"attachment; filename={filename}",
        "Cache-Control": "no-store",
    }
    return Response(content, mimetype="text/plain; charset=utf-8", headers=headers)


def _background_build() -> None:
    if not _build_lock.acquire(blocking=False):
        emit_progress("route", "Маршрутизатор уже запущен", 0)
        return
    socketio.emit("build_status", {"running": True})
    reset_routes()
    set_route_stats(None)
    try:
        emit_progress("route", "🚀 Запуск маршрутизации…", 5)
        started = time.time()
        summary = build_routes(_current_settings, socketio)
        exit_code = summary.get("exit_code", 1)
        if exit_code == 0:
            duration = summary.get("duration_sec") or (time.time() - started)
            stats_payload = _compose_route_stats(summary, duration)
            STATE.routes_ready = True
            STATE.routes_error = None
            set_route_stats(stats_payload)
            socketio.emit("route_stats", stats_payload)
            if ROUTES_KML.exists():
                socketio.emit("routes_ready", stats_payload)
            socketio.emit("build_done", {"success": True, "stats": stats_payload})
        else:
            STATE.routes_ready = False
            error_message = f"Ошибка построения (код {exit_code})"
            STATE.routes_error = error_message
            set_route_stats(None)
            socketio.emit("build_done", {"success": False, "error": error_message})
    except Exception as exc:
        emit_progress("route", f"❌ Ошибка запуска: {exc}", 100)
        STATE.routes_ready = False
        STATE.routes_error = str(exc)
        set_route_stats(None)
        socketio.emit("build_done", {"success": False, "error": str(exc)})
    finally:
        global _build_thread
        _build_thread = None
        _build_lock.release()
        socketio.emit("build_status", {"running": False})
        _persist_state()


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
                socketio.emit("map:update", {"grid": data})
            except Exception:
                pass
    if STATE.roads:
        socketio.emit("map:update", {"roads": STATE.roads})
    if STATE.route_stats:
        socketio.emit("route_stats", STATE.route_stats)
    if STATE.routes_ready and ROUTES_KML.exists():
        socketio.emit("routes_ready", STATE.route_stats or {})


@socketio.on("assign_sector")
def on_assign_sector(payload):
    sector_id = payload.get("sector_id")
    tractor_id = payload.get("tractor_id")
    if not sector_id or not tractor_id:
        return
    _assignments[sector_id] = tractor_id
    ASSIGNMENTS_JSON.write_text(json.dumps(_assignments, ensure_ascii=False, indent=2), encoding="utf-8")
    set_assignments(_assignments)
    socketio.emit("assignments_updated", {"assignments": _assignments})
    _persist_state()


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
    set_assignments(_assignments)
    socketio.emit("assignments_updated", {"assignments": _assignments})
    _persist_state()


@app.errorhandler(Exception)
def handle_error(exc):
    return jsonify({"error": str(exc)}), 500


def bootstrap():
    global _current_settings, _assignments, _roads_cache
    _current_settings = load_settings()
    STATE.settings = dict(_current_settings)
    for message in ensure_initial_files(
        float(_current_settings.get("base_lat", 54.909901)),
        float(_current_settings.get("base_lon", 37.363422)),
    ):
        _emit_log(f"[{_timestamp()}] {message}")
    set_system_status(_current_system_status())
    _assignments = load_assignments()
    set_assignments(_assignments)
    _roads_cache = None
    _load_state_file()
    _emit_log(f"[{_timestamp()}] [LOAD] Загружаю GEO.kml…")
    ensure_grid_exists(int(_current_settings.get("grid_cells", 64)))
    _emit_log(f"[{_timestamp()}] [LOAD] Загружаю RoadCity.kml…")
    _ensure_roads_geojson()
    _persist_state()


if __name__ == "__main__":
    bootstrap()
    socketio.run(
        app,
        host=os.getenv("FLASK_HOST", "0.0.0.0"),
        port=int(os.getenv("FLASK_PORT", "5000")),
    )
