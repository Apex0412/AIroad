"""Robust loaders for GEO and Road KML files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from shapely.geometry import MultiPolygon

from config import GRID_GEOJSON
from route_builder.grid import generate_grid, load_geo_boundary, save_grid_geojson
from route_builder.roads import Road, load_roads
from utils.validator import validate_geo_kml


class ZoneLoadError(RuntimeError):
    pass


def read_zone_kml(path: Path) -> MultiPolygon:
    if not path.exists():
        raise ZoneLoadError(f"Файл не найден: {path}")
    valid, message = validate_geo_kml(path)
    if not valid:
        raise ZoneLoadError(message or "Некорректный GEO.kml")
    boundary = load_geo_boundary(path)
    if boundary.is_empty:
        raise ZoneLoadError("Граница пуста")
    if boundary.area <= 0:
        raise ZoneLoadError("Граница имеет нулевую площадь")
    return boundary


def read_roads_kml(path: Path, boundary: Optional[MultiPolygon] = None) -> List[Road]:
    if not path.exists():
        raise FileNotFoundError(f"RoadCity.kml отсутствует: {path}")
    boundary = boundary or None
    roads = load_roads(path, boundary)
    if not roads:
        raise RuntimeError("RoadCity.kml не содержит линий")
    return roads


def build_grid(boundary: MultiPolygon, grid_cells: int) -> Dict[str, object]:
    cells = generate_grid(boundary, grid_cells)
    save_grid_geojson(cells, GRID_GEOJSON, grid_cells)
    return json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))


def load_grid_file() -> Optional[Dict[str, object]]:
    if not GRID_GEOJSON.exists():
        return None
    try:
        return json.loads(GRID_GEOJSON.read_text(encoding="utf-8"))
    except Exception:
        return None
