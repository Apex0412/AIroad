"""Road ingestion and preprocessing utilities."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import linemerge, split
from shapely.strtree import STRtree

LOGGER = logging.getLogger(__name__)


@dataclass
class Road:
    id: str
    name: Optional[str]
    geometry: LineString


def _iter_kml_lines(path: Path) -> Iterable[tuple[str, Optional[str], LineString]]:
    from lxml import etree

    root = etree.fromstring(path.read_bytes())
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    for placemark in root.findall(".//k:Placemark", ns):
        name = placemark.findtext("k:name", namespaces=ns)
        for linestring in placemark.findall(".//k:LineString", ns):
            coords_text = linestring.findtext("k:coordinates", namespaces=ns)
            if not coords_text:
                continue
            coords = []
            for part in coords_text.replace("\n", " ").split():
                lon, lat = map(float, part.split(",")[:2])
                coords.append((lon, lat))
            if len(coords) < 2:
                continue
            yield (name or None, LineString(coords))


def load_roads(path: Path, boundary=None) -> List[Road]:
    if not path.exists():
        raise FileNotFoundError(f"Файл дорог не найден: {path}")

    roads: List[Road] = []
    for idx, (name, geom) in enumerate(_iter_kml_lines(path), start=1):
        line = geom
        if boundary is not None:
            line = line.intersection(boundary)
            if line.is_empty:
                continue
            if isinstance(line, MultiLineString):
                for part_idx, part in enumerate(line.geoms, start=1):
                    rid = f"road_{idx:05d}_{part_idx:02d}"
                    roads.append(Road(id=rid, name=name, geometry=LineString(part.coords)))
                continue
        rid = f"road_{idx:05d}"
        roads.append(Road(id=rid, name=name, geometry=LineString(line.coords)))
    if not roads:
        raise ValueError("Не найдено дорожных сегментов в RoadCity.kml")
    return roads


def build_spatial_index(roads: Iterable[Road]) -> STRtree:
    return STRtree([r.geometry for r in roads])


def merge_short_segments(roads: List[Road], min_length_m: float, tolerance_m: float) -> List[Road]:
    merged: List[Road] = []
    accumulator: List[LineString] = []
    accumulator_names: List[str] = []
    current_total = 0.0

    for road in roads:
        length = road.geometry.length * 111_139  # rough meters conversion
        accumulator.append(road.geometry)
        accumulator_names.append(road.name or "")
        current_total += length
        if current_total >= min_length_m:
            merged_line = linemerge(accumulator)
            if isinstance(merged_line, LineString):
                merged.append(
                    Road(
                        id=f"merged_{len(merged):05d}",
                        name=_pick_name(accumulator_names),
                        geometry=merged_line,
                    )
                )
            elif isinstance(merged_line, MultiLineString):
                for part in merged_line.geoms:
                    merged.append(
                        Road(
                            id=f"merged_{len(merged):05d}",
                            name=_pick_name(accumulator_names),
                            geometry=LineString(part.coords),
                        )
                    )
            accumulator.clear()
            accumulator_names.clear()
            current_total = 0.0

    if accumulator:
        merged_line = linemerge(accumulator)
        if isinstance(merged_line, LineString):
            merged.append(
                Road(
                    id=f"merged_{len(merged):05d}",
                    name=_pick_name(accumulator_names),
                    geometry=merged_line,
                )
            )
    return merged or roads


def split_long_line(line: LineString, max_len_m: float) -> List[LineString]:
    if line.length * 111_139 <= max_len_m:
        return [line]
    parts: List[LineString] = []
    total_len = line.length
    step = max_len_m / 111_139
    distance = step
    while distance < total_len:
        splitter = Point(line.interpolate(distance, normalized=False))
        line = split(line, splitter)[0]
        parts.append(LineString(line.coords))
        distance += step
    parts.append(line)
    return parts


def _pick_name(names: List[str]) -> Optional[str]:
    for name in names:
        if name:
            return name
    return None
