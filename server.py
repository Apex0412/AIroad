import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Dict

from flask import Flask, Response, jsonify, render_template, request, send_file
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

from route_builder_grid_v7_1 import (
    CENTER_LAT,
    CENTER_LON,
    GRID_CELLS,
    N_UNITS,
    TARGET_M,
    TRACTOR_COLORS,
)

APP_ROOT = Path(__file__).parent.resolve()
# Re-load the .env file now so an already-configured key is visible on startup.
load_dotenv(APP_ROOT / ".env")
GEO_PATH = APP_ROOT / "GEO.kml"
ROADS_PATH = APP_ROOT / "RoadCity.kml"
ASSIGNMENTS_PATH = APP_ROOT / "assignments.json"
SECTORS_GEOJSON = APP_ROOT / "static" / "sectors.geojson"
ROUTES_KML = APP_ROOT / "routes_grid.kml"

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET", "tractor-secret")
socketio = SocketIO(app, cors_allowed_origins="*")

_assignments_lock = threading.Lock()
_current_assignments: Dict[str, str] = {}
_build_lock = threading.Lock()
_ansi_regex = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_initialized = False
_dotenv_path = APP_ROOT / ".env"


def refresh_env() -> None:
    """Ensure the latest values from the .env file are available."""

    # override=True allows late edits without restarting the container the
    # application runs in. We only override keys declared in the .env file.
    if _dotenv_path.exists():
        load_dotenv(_dotenv_path, override=True)


def load_assignments() -> Dict[str, str]:
    if ASSIGNMENTS_PATH.exists():
        try:
            return json.loads(ASSIGNMENTS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_assignments(assignments: Dict[str, str]) -> None:
    ASSIGNMENTS_PATH.write_text(json.dumps(assignments, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_grid_exists() -> None:
    if SECTORS_GEOJSON.exists():
        return
    args = [
        os.sys.executable,
        str(APP_ROOT / "route_builder_grid_v7_1.py"),
        "--geo",
        str(GEO_PATH),
        "--roads",
        str(ROADS_PATH),
        "--generate-grid",
    ]
    try:
        subprocess.run(args, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        app.logger.warning(
            "Не удалось сгенерировать сетку автоматически. Проверьте входные файлы."
        )


def initialize_state() -> None:
    global _current_assignments, _initialized
    if _initialized:
        return
    refresh_env()
    ensure_grid_exists()
    with _assignments_lock:
        _current_assignments = load_assignments()
    _initialized = True


@app.route("/")
def index() -> str:
    initialize_state()
    refresh_env()
    tractors = [
        {
            "id": f"tractor_{i+1:02d}",
            "name": f"Трактор {i+1:02d}",
            "color": TRACTOR_COLORS[i % len(TRACTOR_COLORS)],
        }
        for i in range(N_UNITS)
    ]
    return render_template(
        "map.html",
        tractors=tractors,
        center_lat=CENTER_LAT,
        center_lon=CENTER_LON,
        target_km=TARGET_M / 1000,
        grid_cells=GRID_CELLS,
        google_key_present=bool(os.getenv("GOOGLE_API_KEY")),
        dotenv_path=str(_dotenv_path),
        dotenv_exists=_dotenv_path.exists(),
    )


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

    data = request.get_json(force=True)
    assignments = data.get("assignments", {}) if isinstance(data, dict) else {}
    if not isinstance(assignments, dict):
        return jsonify({"error": "Неверный формат assignments"}), 400

    with _assignments_lock:
        _current_assignments.clear()
        _current_assignments.update({str(k): str(v) for k, v in assignments.items() if v})
        save_assignments(_current_assignments)

    if not _build_lock.acquire(blocking=False):
        return jsonify({"error": "Маршрутизация уже выполняется"}), 409

    thread = threading.Thread(target=_background_build, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


def _background_build() -> None:
    try:
        refresh_env()
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
        ]
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
        socketio.emit("progress", {"message": f"Исключение: {exc}"})
        socketio.emit("build_done", {"success": False})
    finally:
        _build_lock.release()


@socketio.on("connect")
def handle_connect():
    initialize_state()
    emit("assignments", _current_assignments)


@socketio.on("assign_sector")
def assign_sector(payload):
    sector_id = payload.get("sector_id") if isinstance(payload, dict) else None
    tractor_id = payload.get("tractor_id") if isinstance(payload, dict) else None
    if not sector_id:
        return
    with _assignments_lock:
        if tractor_id:
            _current_assignments[str(sector_id)] = str(tractor_id)
        else:
            _current_assignments.pop(str(sector_id), None)
        save_assignments(_current_assignments)
    emit(
        "assign_sector",
        {"sector_id": sector_id, "tractor_id": tractor_id},
        broadcast=True,
        include_self=False,
    )


@socketio.on("assignments_reset")
def reset_assignments():
    initialize_state()
    with _assignments_lock:
        _current_assignments.clear()
        save_assignments(_current_assignments)
    emit("assignments", _current_assignments, broadcast=True)


def main() -> None:
    initialize_state()
    socketio.run(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    main()
