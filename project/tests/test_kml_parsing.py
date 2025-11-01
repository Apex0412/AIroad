from pathlib import Path

import sys

import pytest

pytest.importorskip("shapely")

sys.path.append(str(Path(__file__).resolve().parents[1]))

# FIX: import loaders via package-relative path for test execution
from ..services.kml_io import read_roads_kml, read_zone_kml

SAMPLES = Path(__file__).resolve().parent / "kml_samples"


def test_read_zone_polygon():
    boundary = read_zone_kml(SAMPLES / "geo_polygon.kml")
    assert not boundary.is_empty
    assert boundary.area > 0


def test_read_roads_linestring():
    boundary = read_zone_kml(SAMPLES / "geo_polygon.kml")
    roads = read_roads_kml(SAMPLES / "roads_linestring.kml", boundary)
    assert len(roads) == 2
    assert all(road.geometry.length > 0 for road in roads)
