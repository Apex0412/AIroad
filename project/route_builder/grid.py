"""Utilities for generating and loading grid GeoJSON files."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Tuple

from shapely.geometry import MultiPolygon, Polygon, box, shape
from shapely.ops import unary_union

__all__ = [
    "load_geo_boundary",
    "generate_grid",
    "save_grid_geojson",
    "load_grid_features",
]


def load_geo_boundary(path: Path) -> MultiPolygon:
    """Load GEO boundary KML/GeoJSON into a MultiPolygon."""
    if not path.exists():
        raise FileNotFoundError(f"Граничный файл не найден: {path}")

    text = path.read_text(encoding="utf-8")
    if "<kml" in text[:256].lower():
        from lxml import etree

        tree = etree.fromstring(text.encode("utf-8"))
        nsmap = tree.nsmap.copy()
        nsmap.setdefault(None, "http://www.opengis.net/kml/2.2")
        polygons: List[Polygon] = []
        for polygon in tree.findall(
            ".//{http://www.opengis.net/kml/2.2}Polygon"
        ):
            coords_text = polygon.findtext(
                ".//{http://www.opengis.net/kml/2.2}coordinates"
            )
            if not coords_text:
                continue
            ring = [
                tuple(map(float, part.split(",")[:2]))
                for part in coords_text.strip().split()
            ]
            if len(ring) < 3:
                continue
            polygons.append(Polygon([(lon, lat) for lon, lat in ring]))
        if not polygons:
            raise ValueError("Не удалось извлечь полигоны из GEO.kml")
        mp = unary_union(polygons)
        if isinstance(mp, Polygon):
            return MultiPolygon([mp])
        return MultiPolygon(mp.geoms)

    data = json.loads(text)
    geom = shape(data["features"][0]["geometry"])
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    raise ValueError("Неподдерживаемый тип геометрии для границы города")


def _grid_dimensions(target_cells: int) -> Tuple[int, int]:
    side = int(math.sqrt(target_cells))
    while side * side < target_cells:
        side += 1
    return side, side


def generate_grid(boundary: MultiPolygon, grid_cells: int) -> List[Polygon]:
    """Return polygons covering the boundary approximated by a square grid."""
    cols, rows = _grid_dimensions(grid_cells)
    minx, miny, maxx, maxy = boundary.bounds
    cell_width = (maxx - minx) / cols
    cell_height = (maxy - miny) / rows

    cells: List[Polygon] = []
    for c in range(cols):
        for r in range(rows):
            cell = box(
                minx + c * cell_width,
                miny + r * cell_height,
                minx + (c + 1) * cell_width,
                miny + (r + 1) * cell_height,
            )
            clipped = cell.intersection(boundary)
            if clipped.is_empty:
                continue
            if isinstance(clipped, (Polygon, MultiPolygon)):
                if isinstance(clipped, Polygon):
                    cells.append(clipped)
                else:
                    cells.extend(list(clipped.geoms))
    if not cells:
        raise ValueError("Не удалось построить сетку: все клетки пусты")
    return cells


def save_grid_geojson(
    cells: Iterable[Polygon],
    output_path: Path,
    grid_cells: int,
) -> None:
    """Persist grid polygons to GeoJSON with metadata."""
    features = []
    for idx, polygon in enumerate(cells, start=1):
        coords = [
            [[lon, lat] for lon, lat in polygon.exterior.coords]
        ]
        properties = {"id": f"sector_{idx:03d}"}
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": coords,
                },
                "properties": properties,
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "grid_cells": grid_cells,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_grid_features(path: Path) -> List[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Файл сетки не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("features", [])
