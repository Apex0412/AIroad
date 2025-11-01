"""Export helpers for KML outputs."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import simplekml

from .optimizer import Tractor


def export_routes_kml(
    routes: Dict[str, List[Tuple[float, float]]],
    tractors: Iterable[Tractor],
    output_path: Path,
) -> None:
    kml = simplekml.Kml()
    tractor_by_id = {tractor.id: tractor for tractor in tractors}
    for tractor_id, coords in routes.items():
        tractor = tractor_by_id.get(tractor_id)
        if not tractor or len(coords) < 2:
            continue
        ls = kml.newlinestring(name=tractor.name)
        ls.coords = [(lon, lat) for lat, lon in coords]
        ls.style.linestyle.color = _hex_to_kml_color(tractor.color)
        ls.style.linestyle.width = 4
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kml.save(str(output_path))


def export_roads_kml(assignments: Dict[str, str], roads, tractors: Iterable[Tractor], output_path: Path) -> None:
    kml = simplekml.Kml()
    tractor_by_id = {tractor.id: tractor for tractor in tractors}
    for road in roads:
        tid = assignments.get(road.id)
        if not tid:
            continue
        tractor = tractor_by_id.get(tid)
        if not tractor:
            continue
        ls = kml.newlinestring(name=f"{tractor.name}: {road.name or road.id}")
        ls.coords = [(lon, lat) for lon, lat in road.geometry.coords]
        ls.style.linestyle.color = _hex_to_kml_color(tractor.color)
        ls.style.linestyle.width = 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kml.save(str(output_path))


def _hex_to_kml_color(color: str) -> str:
    color = color.lstrip("#")
    if len(color) != 6:
        color = "0000FF"
    r, g, b = color[0:2], color[2:4], color[4:6]
    return f"ff{b}{g}{r}"
