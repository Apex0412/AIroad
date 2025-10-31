import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_socketio import SocketIO, emit

from config_defaults import BuildDefaults, GRID_CELL_OPTIONS, TRACTOR_COLORS

APP_ROOT = Path(__file__).parent.resolve()
load_dotenv(APP_ROOT / ".env")

DEFAULTS = BuildDefaults()
SETTINGS_PATH = APP_ROOT / "app_settings.json"
LEGACY_GRID_PATH = APP_ROOT / "grid_settings.json"
GEO_PATH = APP_ROOT / "GEO.kml"
ROADS_PATH = APP_ROOT / "RoadCity.kml"
ASSIGNMENTS_PATH = APP_ROOT / "assignments.json"
SECTORS_GEOJSON = APP_ROOT / "static" / "sectors.geojson"
ROUTES_KML = APP_ROOT / "routes_grid.kml"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "tractor-secret")
socketio = SocketIO(app, cors_allowed_origins="*")

_assignments_lock = threading.Lock()
_settings_lock = threading.Lock()
_build_lock = threading.Lock()
_current_assignments: Dict[str, str] = {}
_current_settings: Dict[str, Any] = {}
_grid_error: Optional[str] = None
_initialized = False
_ansi_regex = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_dotenv_path = APP_ROOT / ".env"


def default_settings() -> Dict[str, Any]:
    return {
        "grid_cells": DEFAULTS.grid_cells,
        "n_units": DEFAULTS.n_units,
        "target_km": DEFAULTS.target_km,
        "travel_mode": DEFAULTS.travel_mode,
        "max_waypoints": DEFAULTS.max_waypoints,
        "use_base_as_start": DEFAULTS.use_base_as_start,
        "base_lat": DEFAULTS.base_lat,
        "base_lon": DEFAULTS.base_lon,
        "center_lat": DEFAULTS.center_lat,
        "center_lon": DEFAULTS.center_lon,
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_float(value: Any, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    result = float(value)
    if minimum is not None and result < minimum:
        raise ValueError("значение меньше допустимого диапазона")
    if maximum is not None and result > maximum:
        raise ValueError("значение больше допустимого диапазона")
    return result


def _coerce_int(value: Any, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    result = int(value)
    if minimum is not None and result < minimum:
        raise ValueError("значение меньше допустимого диапазона")
    if maximum is not None and result > maximum:
        raise ValueError("значение больше допустимого диапазона")
    return result


def load_settings() -> Dict[str, Any]:
    settings = default_settings()
    if LEGACY_GRID_PATH.exists() and not SETTINGS_PATH.exists():
        try:
            data = json.loads(LEGACY_GRID_PATH.read_text(encoding="utf-8"))
            grid_value = int(data.get("grid_cells", settings["grid_cells"]))
            if grid_value > 0:
                settings["grid_cells"] = grid_value
        except (ValueError, json.JSONDecodeError):
            app.logger.warning("Не удалось прочитать legacy grid_settings.json")
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            app.logger.warning("Некорректный app_settings.json – используются значения по умолчанию")
            return settings
        for key, value in data.items():
            if key == "grid_cells":
                try:
                    settings[key] = _coerce_int(value, minimum=1, maximum=400)
                except ValueError:
                    app.logger.warning("grid_cells вне диапазона – используется значение по умолчанию")
            elif key == "n_units":
                try:
                    settings[key] = _coerce_int(value, minimum=1, maximum=64)
                except ValueError:
                    app.logger.warning("n_units вне диапазона – используется значение по умолчанию")
            elif key == "target_km":
                try:
                    settings[key] = _coerce_float(value, minimum=0.5, maximum=200.0)
                except ValueError:
                    app.logger.warning("target_km вне диапазона – используется значение по умолчанию")
            elif key == "travel_mode":
                settings[key] = str(value or "driving").strip()
            elif key == "max_waypoints":
                try:
                    settings[key] = _coerce_int(value, minimum=1, maximum=100)
                except ValueError:
                    app.logger.warning("max_waypoints вне диапазона – используется значение по умолчанию")
            elif key == "use_base_as_start":
                settings[key] = _coerce_bool(value)
            elif key in {"base_lat", "center_lat"}:
                try:
                    settings[key] = _coerce_float(value, minimum=-90.0, maximum=90.0)
                except ValueError:
                    app.logger.warning("Координата широты вне диапазона – используется значение по умолчанию")
            elif key in {"base_lon", "center_lon"}:
                try:
                    settings[key] = _coerce_float(value, minimum=-180.0, maximum=180.0)
                except ValueError:
                    app.logger.warning("Координата долготы вне диапазона – используется значение по умолчанию")
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_env() -> None:
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path, override=True)


def load_assignments() -> Dict[str, str]:
    if ASSIGNMENTS_PATH.exists():
        try:
            return json.loads(ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            app.logger.warning("assignments.json повреждён – создаётся новый файл")
    return {}


def save_assignments(assignments: Dict[str, str]) -> None:
    ASSIGNMENTS_PATH.write_text(json.dumps(assignments, ensure_ascii=False, indent=2), encoding="utf-8")


def valid_tractor_ids(settings: Dict[str, Any]) -> set[str]:
    return {f"tractor_{i+1:02d}" for i in range(int(settings["n_units"]))}


def prune_assignments(settings: Dict[str, Any]) -> None:
    allowed = valid_tractor_ids(settings)
    removed = []
    for sector_id, tractor_id in list(_current_assignments.items()):
        if tractor_id not in allowed:
            removed.append(sector_id)
            _current_assignments.pop(sector_id, None)
    if removed:
        app.logger.info("Удалено %s назначений из-за изменения количества тракторов", len(removed))
        save_assignments(_current_assignments)


def ensure_grid_exists(grid_cells: Optional[int] = None, force: bool = False) -> None:
    desired_cells = grid_cells or int(_current_settings.get("grid_cells") or DEFAULTS.grid_cells)
    if not force and SECTORS_GEOJSON.exists():
        try:
            data = json.loads(SECTORS_GEOJSON.read_text(encoding="utf-8"))
            meta = data.get("metadata", {})
            if int(meta.get("grid_cells", desired_cells)) == desired_cells:
                return
        except (ValueError, json.JSONDecodeError, OSError, AttributeError):
            pass
    app.logger.info("Генерация сетки (%s клеток)...", desired_cells)
    args = [
        os.sys.executable,
        str(APP_ROOT / "route_builder_grid_v7_1.py"),
        "--geo",
        str(GEO_PATH),
        "--roads",
        str(ROADS_PATH),
        "--generate-grid",
        "--grid-cells",
        str(desired_cells),
    ]
    try:
        subprocess.run(args, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        app.logger.error("Не удалось сгенерировать сетку", exc_info=exc)
        raise RuntimeError("Ошибка генерации сетки") from exc
    if not SECTORS_GEOJSON.exists():
        raise RuntimeError("Файл sectors.geojson не создан")


def initialize_state() -> None:
    global _initialized, _grid_error
    if _initialized:
        return
    app.logger.info("Инициализация сервера...")
    refresh_env()
    with _settings_lock:
        _current_settings.clear()
        _current_settings.update(load_settings())
        save_settings(_current_settings)
    try:
        ensure_grid_exists(_current_settings.get("grid_cells"))
        _grid_error = None
    except Exception as exc:  # noqa: BLE001
        _grid_error = str(exc)
        app.logger.error("Ошибка генерации сетки", exc_info=exc)
    with _assignments_lock:
        _current_assignments.clear()
        _current_assignments.update(load_assignments())
        prune_assignments(_current_settings)
    _initialized = True
    app.logger.info("Инициализация завершена")


def tractors_for_settings(settings: Dict[str, Any]) -> list[Dict[str, str]]:
    count = int(settings["n_units"])
    tractors = []
    for idx in range(count):
        tractors.append(
            {
                "id": f"tractor_{idx+1:02d}",
                "name": f"Трактор {idx+1:02d}",
                "color": TRACTOR_COLORS[idx % len(TRACTOR_COLORS)],
            }
        )
    return tractors


@app.route("/")
def index() -> str:
    initialize_state()
    refresh_env()
    settings_snapshot = with_settings_copy()
    tractors = tractors_for_settings(settings_snapshot)
    return render_template(
        "map.html",
        tractors=tractors,
        tractor_palette=TRACTOR_COLORS,
        settings=settings_snapshot,
        grid_options=GRID_CELL_OPTIONS,
        google_key_present=bool(os.getenv("GOOGLE_API_KEY")),
        dotenv_path=str(_dotenv_path),
        dotenv_exists=_dotenv_path.exists(),
        grid_error=_grid_error,
    )


def with_settings_copy() -> Dict[str, Any]:
    with _settings_lock:
        return dict(_current_settings)


@app.route("/geo")
def geojson() -> Response:
    if not SECTORS_GEOJSON.exists():
        return jsonify({"type": "FeatureCollection", "features": []})
    return send_file(SECTORS_GEOJSON, mimetype="application/geo+json")


@app.route("/download")
def download() -> Response:
    if not ROUTES_KML.exists():
        return jsonify({"error": "routes_grid.kml ещё не создан"}), 404
    return send_file(ROUTES_KML, mimetype="application/vnd.google-earth.kml+xml", as_attachment=True)


@app.post("/run")
def run_builder() -> Response:
    initialize_state()
    refresh_env()
    if not GEO_PATH.exists() or not ROADS_PATH.exists():
        return jsonify({"error": "Файлы GEO.kml и RoadCity.kml должны находиться в корне проекта"}), 400
    data = request.get_json(force=True, silent=True) or {}
    assignments = data.get("assignments", {}) if isinstance(data, dict) else {}
    if not isinstance(assignments, dict):
        return jsonify({"error": "Неверный формат assignments"}), 400
    with _assignments_lock:
        _current_assignments.clear()
        allowed = valid_tractor_ids(_current_settings)
        for sector_id, tractor_id in assignments.items():
            if tractor_id in allowed:
                _current_assignments[str(sector_id)] = str(tractor_id)
        save_assignments(_current_assignments)
    if not _build_lock.acquire(blocking=False):
        return jsonify({"error": "Маршрутизация уже выполняется"}), 409
    thread = threading.Thread(target=_background_build, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


def _background_build() -> None:
    global _grid_error
    try:
        refresh_env()
        settings_snapshot = with_settings_copy()
        ensure_grid_exists(settings_snapshot.get("grid_cells"), force=True)
        _grid_error = None
        args = [
            os.sys.executable,
            str(APP_ROOT / "route_builder_grid_v7_1.py"),
            "--geo",
            str(GEO_PATH),
            "--roads",
            str(ROADS_PATH),
            "--assignments",
            str(ASSIGNMENTS_PATH),
            "--output",
            str(ROUTES_KML),
            "--grid-cells",
            str(settings_snapshot["grid_cells"]),
            "--n-units",
            str(settings_snapshot["n_units"]),
            "--target-km",
            str(settings_snapshot["target_km"]),
            "--travel-mode",
            settings_snapshot["travel_mode"],
            "--max-waypoints",
            str(settings_snapshot["max_waypoints"]),
            "--base-lat",
            str(settings_snapshot["base_lat"]),
            "--base-lon",
            str(settings_snapshot["base_lon"]),
            "--center-lat",
            str(settings_snapshot["center_lat"]),
            "--center-lon",
            str(settings_snapshot["center_lon"]),
        ]
        if settings_snapshot.get("use_base_as_start", True):
            args.append("--use-base-start")
        else:
            args.append("--no-use-base-start")
        app.logger.info("Запускаю построение маршрутов")
        process = subprocess.Popen(
            args,
            cwd=APP_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            cleaned = _ansi_regex.sub("", line).strip()
            if not cleaned:
                continue
            socketio.emit("progress", {"message": cleaned})
        returncode = process.wait()
        if returncode == 0:
            socketio.emit("progress", {"message": "Маршруты построены"})
            socketio.emit("build_done", {"success": True})
        else:
            socketio.emit("progress", {"message": f"Ошибка построения (код {returncode})"})
            socketio.emit("build_done", {"success": False})
    except Exception as exc:  # noqa: BLE001
        _grid_error = str(exc)
        socketio.emit("progress", {"message": f"Исключение: {exc}"})
        socketio.emit("build_done", {"success": False})
    finally:
        _build_lock.release()


@app.post("/settings")
def update_settings() -> Response:
    initialize_state()
    data = request.get_json(force=True, silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Ожидался JSON-объект"}), 400
    with _settings_lock:
        new_settings = dict(_current_settings)
    changed: Dict[str, Any] = {}
    errors: list[str] = []
    for key, value in data.items():
        try:
            if key == "grid_cells":
                new_value = _coerce_int(value, minimum=1, maximum=400)
            elif key == "n_units":
                new_value = _coerce_int(value, minimum=1, maximum=64)
            elif key == "target_km":
                new_value = _coerce_float(value, minimum=0.5, maximum=200.0)
            elif key == "travel_mode":
                new_value = str(value or "driving").strip()
            elif key == "max_waypoints":
                new_value = _coerce_int(value, minimum=1, maximum=100)
            elif key == "use_base_as_start":
                new_value = _coerce_bool(value)
            elif key in {"base_lat", "center_lat"}:
                new_value = _coerce_float(value, minimum=-90.0, maximum=90.0)
            elif key in {"base_lon", "center_lon"}:
                new_value = _coerce_float(value, minimum=-180.0, maximum=180.0)
            else:
                continue
        except ValueError as exc:
            errors.append(f"Поле {key}: {exc}")
            continue
        if new_settings.get(key) != new_value:
            new_settings[key] = new_value
            changed[key] = new_value
    if errors:
        return jsonify({"error": "; ".join(errors)}), 400
    if not changed:
        return jsonify({"status": "unchanged", "settings": new_settings})

    grid_changed = "grid_cells" in changed
    n_units_changed = "n_units" in changed

    if grid_changed:
        try:
            ensure_grid_exists(changed["grid_cells"], force=True)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Не удалось сформировать сетку: {exc}"}), 500

    with _settings_lock:
        _current_settings.clear()
        _current_settings.update(new_settings)
        save_settings(_current_settings)

    assignments_cleared = False
    if grid_changed:
        with _assignments_lock:
            _current_assignments.clear()
            save_assignments(_current_assignments)
            assignments_cleared = True
    elif n_units_changed:
        with _assignments_lock:
            before = len(_current_assignments)
            prune_assignments(_current_settings)
            assignments_cleared = len(_current_assignments) < before

    settings_snapshot = with_settings_copy()
    socketio.emit("settings", {"settings": settings_snapshot, "tractors": tractors_for_settings(settings_snapshot)}, broadcast=True)
    if assignments_cleared:
        socketio.emit("assignments", _current_assignments, broadcast=True)

    return jsonify({"status": "ok", "settings": settings_snapshot, "tractors": tractors_for_settings(settings_snapshot), "assignments_cleared": assignments_cleared})


@socketio.on("connect")
def handle_connect():
    initialize_state()
    emit("assignments", _current_assignments)
    snapshot = with_settings_copy()
    emit("settings", {"settings": snapshot, "tractors": tractors_for_settings(snapshot)})


@socketio.on("assign_sector")
def assign_sector(payload):
    settings_snapshot = with_settings_copy()
    sector_id = payload.get("sector_id") if isinstance(payload, dict) else None
    tractor_id = payload.get("tractor_id") if isinstance(payload, dict) else None
    if not sector_id:
        return
    if tractor_id and tractor_id not in valid_tractor_ids(settings_snapshot):
        return
    with _assignments_lock:
        if tractor_id:
            _current_assignments[str(sector_id)] = str(tractor_id)
        else:
            _current_assignments.pop(str(sector_id), None)
        save_assignments(_current_assignments)
    emit("assign_sector", {"sector_id": sector_id, "tractor_id": tractor_id}, broadcast=True, include_self=False)


@socketio.on("assignments_reset")
def reset_assignments():
    initialize_state()
    with _assignments_lock:
        _current_assignments.clear()
        save_assignments(_current_assignments)
    emit("assignments", _current_assignments, broadcast=True)


def main() -> None:
    initialize_state()
    app.logger.info("Сервер запущен на http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
