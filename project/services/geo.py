"""Geospatial helper utilities."""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from shapely.geometry import LineString, MultiLineString

EARTH_M_PER_DEG = 111_139.0
EARTH_KM_PER_DEG = 111.139


def is_valid_coord(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def ensure_closed(coords: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not coords:
        return []
    if coords[0] != coords[-1]:
        return list(coords) + [coords[0]]
    return list(coords)


def linestring_length_m(geom: LineString | MultiLineString) -> float:
    if isinstance(geom, LineString):
        return geom.length * EARTH_M_PER_DEG
    return sum(part.length for part in geom.geoms) * EARTH_M_PER_DEG


def point_distance_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) * EARTH_KM_PER_DEG


def to_leaflet_coords(geom: LineString | MultiLineString) -> List[List[float]]:
    if isinstance(geom, LineString):
        return [[lat, lon] for lon, lat in geom.coords]
    coords: List[List[float]] = []
    for part in geom.geoms:
        coords.extend([[lat, lon] for lon, lat in part.coords])
    return coords
