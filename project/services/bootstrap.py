"""Initial data bootstrap helpers."""
from __future__ import annotations

from typing import List

# FIX: reference config via parent package to support repo-root server entrypoint
from ..config import GEO_PATH, ROADS_PATH, STATE_JSON

TEST_GEO_TEMPLATE = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<kml xmlns=\"http://www.opengis.net/kml/2.2\">\n  <Placemark>\n    <name>Test boundary</name>\n    <Polygon>\n      <outerBoundaryIs>\n        <LinearRing>\n          <coordinates>\n            {coords}\n          </coordinates>\n        </LinearRing>\n      </outerBoundaryIs>\n    </Polygon>\n  </Placemark>\n</kml>\n"""

TEST_ROADS_TEMPLATE = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<kml xmlns=\"http://www.opengis.net/kml/2.2\">\n  <Document>\n    <name>Test roads</name>\n    {placemarks}\n  </Document>\n</kml>\n"""

ROAD_PLACEMARK = """  <Placemark>\n    <name>Road {idx}</name>\n    <LineString>\n      <coordinates>\n        {coords}\n      </coordinates>\n    </LineString>\n  </Placemark>\n"""


def _format_coords(points: List[tuple[float, float]]) -> str:
    return " ".join(f"{lon:.6f},{lat:.6f},0" for lon, lat in points)


def ensure_sample_geo(base_lat: float, base_lon: float) -> str:
    """Create a fallback GEO.kml if it is missing."""
    if GEO_PATH.exists():
        return "[INIT] GEO.kml найден"
    half = 0.005
    coords = [
        (base_lon - half, base_lat - half),
        (base_lon + half, base_lat - half),
        (base_lon + half, base_lat + half),
        (base_lon - half, base_lat + half),
        (base_lon - half, base_lat - half),
    ]
    GEO_PATH.write_text(TEST_GEO_TEMPLATE.format(coords=_format_coords(coords)), encoding="utf-8")
    return "[INIT] GEO.kml не найден — создан тестовый полигон"


def ensure_sample_roads(base_lat: float, base_lon: float) -> str:
    if ROADS_PATH.exists():
        return "[INIT] RoadCity.kml найден"
    offset = 0.002
    placemarks: List[str] = []
    lines = [
        [
            (base_lon - offset, base_lat - offset),
            (base_lon + offset, base_lat - offset),
        ],
        [
            (base_lon - offset, base_lat + offset),
            (base_lon + offset, base_lat + offset),
        ],
        [
            (base_lon - offset, base_lat - offset),
            (base_lon + offset, base_lat + offset),
        ],
    ]
    for idx, points in enumerate(lines, start=1):
        coords = _format_coords(points)
        placemarks.append(ROAD_PLACEMARK.format(idx=idx, coords=coords))
    ROADS_PATH.write_text(
        TEST_ROADS_TEMPLATE.format(placemarks="\n".join(placemarks)),
        encoding="utf-8",
    )
    return "[INIT] RoadCity.kml не найден — создан тестовый набор дорог"


def ensure_state_file() -> str:
    if STATE_JSON.exists():
        return "[INIT] state.json найден"
    STATE_JSON.write_text("{}\n", encoding="utf-8")
    return "[INIT] state.json создан"


def ensure_initial_files(base_lat: float, base_lon: float) -> List[str]:
    """Guarantee that required data files exist, creating test fixtures if needed."""
    messages = [
        ensure_sample_geo(base_lat, base_lon),
        ensure_sample_roads(base_lat, base_lon),
        ensure_state_file(),
    ]
    return messages
