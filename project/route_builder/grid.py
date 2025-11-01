"""Utilities for generating and loading grid GeoJSON files."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from lxml import etree
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

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("utf-8", errors="replace")

    if "<kml" in text[:1024].lower():
        return _load_kml_boundary(text)

    data = json.loads(text)
    geom = shape(data["features"][0]["geometry"])
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    if isinstance(geom, MultiPolygon):
        return geom
    raise ValueError("Неподдерживаемый тип геометрии для границы города")


def _load_kml_boundary(text: str) -> MultiPolygon:
    parser = etree.XMLParser(remove_blank_text=True, recover=True)
    root = etree.fromstring(text.encode("utf-8"), parser=parser)

    polygons: List[Polygon] = []

    for polygon_elem in root.findall(".//{*}Polygon"):
        poly = _polygon_from_element(polygon_elem)
        if poly is not None and not poly.is_empty:
            polygons.append(poly)

    if not polygons:
        for ring_elem in root.findall(".//{*}LinearRing"):
            coords = _coords_from_element(ring_elem)
            if len(coords) >= 4:
                poly = Polygon(coords)
                if not poly.is_empty:
                    polygons.append(_ensure_valid(poly))

    if not polygons:
        for line_elem in root.findall(".//{*}LineString"):
            coords = _coords_from_element(line_elem)
            if len(coords) >= 4 and coords[0] == coords[-1]:
                poly = Polygon(coords)
                if not poly.is_empty:
                    polygons.append(_ensure_valid(poly))

    if not polygons:
        counts = _collect_tag_counts(root)
        snippet = text[:200].replace("\n", " ").strip()
        found = ", ".join(
            f"{tag}: {count}" for tag, count in list(counts.items())[:10]
        ) or "тегов не найдено"
        raise ValueError(
            "GEO.kml не содержит полигонов. "
            f"Найдено: {found}. Фрагмент: {snippet}"
        )

    union = unary_union(polygons)
    if isinstance(union, Polygon):
        union = MultiPolygon([_ensure_valid(union)])
    elif isinstance(union, MultiPolygon):
        union = MultiPolygon([_ensure_valid(poly) for poly in union.geoms])
    else:
        geoms = [geom for geom in getattr(union, "geoms", []) if isinstance(geom, Polygon)]
        if not geoms:
            raise ValueError("Граница после объединения не содержит полигонов")
        union = MultiPolygon([_ensure_valid(poly) for poly in geoms])
    return union


def _polygon_from_element(element: etree._Element) -> Polygon | None:
    outer = element.find(".//{*}outerBoundaryIs/{*}LinearRing")
    coords = _coords_from_element(outer) if outer is not None else _coords_from_element(element)
    if len(coords) < 4:
        return None
    holes: List[Sequence[Tuple[float, float]]] = []
    for inner in element.findall(".//{*}innerBoundaryIs/{*}LinearRing"):
        interior = _coords_from_element(inner)
        if len(interior) >= 4:
            holes.append(interior)
    polygon = Polygon(coords, holes or None)
    if polygon.is_empty:
        return None
    return _ensure_valid(polygon)


def _coords_from_element(element: etree._Element) -> List[Tuple[float, float]]:
    if element is None:
        return []
    text = element.findtext(".//{*}coordinates") or ""
    if not text.strip():
        text = element.text or ""
    coords: List[Tuple[float, float]] = []
    for chunk in text.replace("\n", " ").replace("\t", " ").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coords.append((lon, lat))
    if coords and coords[0] != coords[-1]:
        coords.append(coords[0])
    return coords


def _collect_tag_counts(root: etree._Element) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for elem in root.iter():
        tag = etree.QName(elem).localname
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def _ensure_valid(polygon: Polygon) -> Polygon:
    if polygon.is_valid:
        return polygon
    fixed = polygon.buffer(0)
    if isinstance(fixed, Polygon):
        return fixed
    if isinstance(fixed, MultiPolygon):
        areas = list(fixed.geoms)
        areas.sort(key=lambda p: p.area, reverse=True)
        return areas[0]
    return polygon


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
