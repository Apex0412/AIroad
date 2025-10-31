"""Route builder for tractor dispatching across a grid.

This module can be executed as a standalone script or imported by the web
application.  It reads the GEO and road KML files, prepares a grid, maps road
segments to grid cells and, using optional cell-to-tractor assignments, creates
KML routes for each tractor.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
import simplekml
from colorama import Fore, Style
from geopy.distance import geodesic
from polyline import decode as decode_polyline
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union
from tqdm import tqdm
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Configuration parameters – tweak here to adjust behaviour globally.
# ---------------------------------------------------------------------------
N_UNITS: int = 18
TARGET_M: float = 30_000.0
GRID_CELLS: int = 64
GOOGLE_API_KEY: Optional[str] = os.getenv("GOOGLE_API_KEY")
TRAVEL_MODE: str = "driving"
MAX_WAYPOINTS: int = 23
USE_BASE_AS_START: bool = True
BASE_LAT: float = 55.751244  # Example: Moscow centre
BASE_LON: float = 37.618423
CENTER_LAT: float = BASE_LAT
CENTER_LON: float = BASE_LON
TRACTOR_COLORS: List[str] = [
    "#d32f2f",
    "#c2185b",
    "#7b1fa2",
    "#512da8",
    "#303f9f",
    "#1976d2",
    "#0288d1",
    "#0097a7",
    "#00796b",
    "#388e3c",
    "#689f38",
    "#afb42b",
    "#fbc02d",
    "#ffa000",
    "#f57c00",
    "#e64a19",
    "#5d4037",
    "#455a64",
]

# ---------------------------------------------------------------------------
# Data classes used in the build process.
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    """A grid cell that intersects with the operational zone."""

    identifier: str
    polygon: Polygon

    @property
    def centroid(self) -> Point:
        return self.polygon.centroid


@dataclass
class RoadSegment:
    """A single road segment clipped to the operational zone."""

    identifier: str
    geometry: LineString
    cell_id: Optional[str]
    length_m: float

    def to_coordinate_list(self) -> List[Tuple[float, float]]:
        return [(float(x), float(y)) for x, y in self.geometry.coords]


@dataclass
class RouteResult:
    tractor_id: str
    color: str
    coordinates: List[List[Tuple[float, float]]]
    total_length: float


# ---------------------------------------------------------------------------
# Utility helpers.
# ---------------------------------------------------------------------------


def print_stage(message: str) -> None:
    """Print a progress message that is easy to parse by the web server."""

    timestamp = time.strftime("%H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    print(formatted)
    sys.stdout.flush()


def load_kml_root(path: Path) -> ET.Element:
    tree = ET.parse(path)
    return tree.getroot()


def _kml_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{"):
        return root.tag.split("}")[0].strip("{")
    return ""


def parse_geo_boundary(path: Path) -> MultiPolygon:
    """Read the GEO KML file and return a MultiPolygon of the allowed area."""

    if not path.exists():
        raise FileNotFoundError(f"GEO boundary file not found: {path}")

    print_stage("[1/8] Читаю границы GEO.kml")
    root = load_kml_root(path)
    ns = _kml_namespace(root)

    polygons: List[Polygon] = []

    for polygon_element in root.findall(f".//{{{ns}}}Polygon"):
        outer = polygon_element.find(f".//{{{ns}}}outerBoundaryIs/{{{ns}}}LinearRing/{{{ns}}}coordinates")
        if outer is None or outer.text is None:
            continue
        coords = _parse_kml_coordinates(outer.text)
        polygon = Polygon(coords)
        if not polygon.is_valid or polygon.is_empty:
            continue
        inner_polys: List[List[Tuple[float, float]]] = []
        for inner in polygon_element.findall(f".//{{{ns}}}innerBoundaryIs/{{{ns}}}LinearRing/{{{ns}}}coordinates"):
            if inner.text:
                inner_polys.append(_parse_kml_coordinates(inner.text))
        if inner_polys:
            polygon = Polygon(coords, inner_polys)
        polygons.append(polygon)

    if not polygons:
        raise ValueError("Не удалось найти полигоны в GEO.kml")

    return MultiPolygon(polygons)


def parse_roads(path: Path, zone: MultiPolygon) -> List[RoadSegment]:
    """Read roads from the KML, clip them to the zone and return segments."""

    if not path.exists():
        raise FileNotFoundError(f"Road file not found: {path}")

    print_stage("[2/8] Читаю дорожную сеть RoadCity.kml")
    root = load_kml_root(path)
    ns = _kml_namespace(root)
    segments: List[RoadSegment] = []

    road_index = 0
    for placemark in root.findall(f".//{{{ns}}}Placemark"):
        line_element = placemark.find(f".//{{{ns}}}LineString/{{{ns}}}coordinates")
        if line_element is None or not line_element.text:
            continue
        coords = _parse_kml_coordinates(line_element.text)
        line = LineString(coords)
        clipped = line.intersection(zone)
        if clipped.is_empty:
            continue

        for part in _explode_lines(clipped):
            length_m = geometry_length_m(part)
            if length_m == 0:
                continue
            seg_id = f"road_{road_index:05d}"
            segments.append(
                RoadSegment(
                    identifier=seg_id,
                    geometry=part,
                    cell_id=None,
                    length_m=length_m,
                )
            )
            road_index += 1

    if not segments:
        raise ValueError("Не удалось получить ни одной дорожной линии из RoadCity.kml")

    print_stage(f"[2/8] Загружено дорожных отрезков: {len(segments)}")
    return segments


def _parse_kml_coordinates(text: str) -> List[Tuple[float, float]]:
    coords: List[Tuple[float, float]] = []
    for token in text.strip().split():
        lon_lat = token.split(",")
        if len(lon_lat) < 2:
            continue
        lon, lat = float(lon_lat[0]), float(lon_lat[1])
        coords.append((lon, lat))
    return coords


def _explode_lines(geometry) -> Iterable[LineString]:
    if isinstance(geometry, LineString):
        yield geometry
    elif isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            if isinstance(part, LineString):
                yield part
    else:
        # Attempt to extract lines from other geometries
        for part in geometry:
            if isinstance(part, LineString):
                yield part


def build_grid(zone: MultiPolygon, cells_count: int) -> List[Cell]:
    """Create a square grid covering the zone and clip each cell."""

    print_stage("[3/8] Формирую сетку клеток")
    bounds = zone.bounds  # (minx, miny, maxx, maxy)
    minx, miny, maxx, maxy = bounds
    side = int(math.sqrt(cells_count))
    if side * side != cells_count:
        side = int(math.ceil(math.sqrt(cells_count)))
    cell_width = (maxx - minx) / side
    cell_height = (maxy - miny) / side

    cells: List[Cell] = []
    index = 1

    for row in range(side):
        for col in range(side):
            left = minx + col * cell_width
            right = left + cell_width
            bottom = miny + row * cell_height
            top = bottom + cell_height
            cell_polygon = Polygon(
                [
                    (left, bottom),
                    (right, bottom),
                    (right, top),
                    (left, top),
                ]
            )
            clipped = cell_polygon.intersection(zone)
            if clipped.is_empty:
                continue
            clipped = _ensure_polygon(clipped)
            cell_id = f"sector_{index:02d}"
            cells.append(Cell(identifier=cell_id, polygon=clipped))
            index += 1

    print_stage(f"[3/8] Получено клеток с покрытием: {len(cells)}")
    return cells


def _ensure_polygon(geometry) -> Polygon:
    if isinstance(geometry, Polygon):
        return geometry
    if isinstance(geometry, MultiPolygon):
        largest = max(geometry.geoms, key=lambda poly: poly.area)
        return Polygon(largest.exterior.coords, [list(ring.coords) for ring in largest.interiors])
    raise ValueError("Ожидался Polygon/MultiPolygon при построении сетки")


def save_cells_geojson(cells: Sequence[Cell], out_path: Path) -> None:
    print_stage("[4/8] Экспортирую сетку в GeoJSON")
    features = []
    for cell in cells:
        coords = [list(map(list, cell.polygon.exterior.coords))]
        for interior in cell.polygon.interiors:
            coords.append(list(map(list, interior.coords)))
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": coords,
            },
            "properties": {
                "id": cell.identifier,
            },
        }
        features.append(feature)

    geojson = {"type": "FeatureCollection", "features": features}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(geojson, ensure_ascii=False, indent=2), encoding="utf-8")


def load_assignments(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Assignments JSON must be an object")
        return {str(k): str(v) for k, v in data.items()}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ошибка чтения assignments.json: {exc}") from exc


def map_segments_to_cells(segments: List[RoadSegment], cells: Sequence[Cell]) -> None:
    print_stage("[5/8] Распределяю дороги по клеткам")
    for segment in tqdm(segments, desc="Привязка", unit="линий", leave=False):
        best_cell: Optional[str] = None
        best_length: float = -1.0
        geom = segment.geometry
        for cell in cells:
            inter = geom.intersection(cell.polygon)
            if inter.is_empty:
                continue
            if isinstance(inter, (LineString, MultiLineString)):
                length = geometry_length_m(inter)
            else:
                continue
            if length > best_length:
                best_length = length
                best_cell = cell.identifier
        segment.cell_id = best_cell


def geometry_length_m(geometry) -> float:
    if geometry.is_empty:
        return 0.0
    if isinstance(geometry, LineString):
        return _line_length_m(list(geometry.coords))
    if isinstance(geometry, MultiLineString):
        return sum(_line_length_m(list(line.coords)) for line in geometry.geoms)
    if hasattr(geometry, "geoms"):
        return sum(geometry_length_m(part) for part in geometry.geoms)
    return 0.0


def _line_length_m(coords: Sequence[Tuple[float, float]]) -> float:
    length = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        length += geodesic((lat1, lon1), (lat2, lon2)).meters
    return length


def build_routes_with_assignments(
    segments: List[RoadSegment],
    cells: Sequence[Cell],
    assignments: Dict[str, str],
    output_path: Path,
) -> List[RouteResult]:
    print_stage("[6/8] Строю маршруты по назначенным клеткам")
    assignments_by_tractor: Dict[str, List[str]] = {f"tractor_{i+1:02d}": [] for i in range(N_UNITS)}
    for cell_id, tractor_id in assignments.items():
        if tractor_id not in assignments_by_tractor:
            continue
        assignments_by_tractor[tractor_id].append(cell_id)

    segments_by_id = {segment.identifier: segment for segment in segments}
    unvisited: set[str] = {segment.identifier for segment in segments}
    routes: List[RouteResult] = []

    if USE_BASE_AS_START:
        current_base = (BASE_LON, BASE_LAT)
    else:
        zone_centroid = unary_union([cell.polygon for cell in cells]).centroid
        current_base = (zone_centroid.x, zone_centroid.y)

    for idx in range(N_UNITS):
        tractor_id = f"tractor_{idx+1:02d}"
        tractor_name = f"Трактор {idx+1:02d}"
        tractor_color = TRACTOR_COLORS[idx % len(TRACTOR_COLORS)]
        allowed_cells = set(assignments_by_tractor.get(tractor_id, []))
        neighbor_order = _ordered_neighbor_cells(allowed_cells, cells)

        route_segments: List[List[Tuple[float, float]]] = []
        route_length = 0.0
        current_point = current_base

        def remaining_segments(candidate_cells: Optional[set[str]] = None) -> List[RoadSegment]:
            result = []
            for seg_id in unvisited:
                seg = segments_by_id[seg_id]
                if candidate_cells is None:
                    result.append(seg)
                elif seg.cell_id and seg.cell_id in candidate_cells:
                    result.append(seg)
            return result

        while route_length < TARGET_M and unvisited:
            primary = remaining_segments(allowed_cells) if allowed_cells else []
            candidate_segments = primary
            if not candidate_segments:
                for cell_id in neighbor_order:
                    candidate_segments = remaining_segments({cell_id})
                    if candidate_segments:
                        break
            if not candidate_segments:
                candidate_segments = remaining_segments(None)
            if not candidate_segments:
                break
            next_segment = _select_nearest_segment(candidate_segments, current_point)
            if next_segment is None:
                break

            start_coord = tuple(map(float, next_segment.geometry.coords[0]))
            travel_segment = build_transition_path(current_point, start_coord)
            if travel_segment:
                route_segments.append(travel_segment.copy())
                travel_length = _line_length_m(travel_segment)
                route_length += travel_length
                current_point = travel_segment[-1]

            line_coords = list(next_segment.geometry.coords)
            if route_segments and route_segments[-1][-1] == line_coords[0]:
                route_segments[-1].extend(line_coords[1:])
            else:
                route_segments.append(line_coords)
            route_length += next_segment.length_m
            current_point = line_coords[-1]
            unvisited.remove(next_segment.identifier)

        routes.append(
            RouteResult(
                tractor_id=tractor_name,
                color=tractor_color,
                coordinates=route_segments,
                total_length=route_length,
            )
        )
        print_stage(
            f"[6/8] {tractor_name}: длина {route_length/1000:.2f} км, осталось линий {len(unvisited)}"
        )

    if unvisited:
        print_stage(
            Fore.YELLOW
            + f"[7/8] Внимание: {len(unvisited)} линий не попало в маршруты, добавляю к самым коротким."
            + Style.RESET_ALL
        )
        distribute_remaining_segments(unvisited, segments_by_id, routes)

    write_routes_kml(routes, output_path)
    print_stage("[8/8] Готово: routes_grid.kml создан")
    return routes


def _select_nearest_segment(
    segments: Sequence[RoadSegment],
    current_point: Tuple[float, float],
) -> Optional[RoadSegment]:
    best_segment: Optional[RoadSegment] = None
    best_distance: float = float("inf")
    point_latlon = (current_point[1], current_point[0])
    for segment in segments:
        start = segment.geometry.coords[0]
        end = segment.geometry.coords[-1]
        distances = [
            geodesic(point_latlon, (start[1], start[0])).meters,
            geodesic(point_latlon, (end[1], end[0])).meters,
        ]
        distance = min(distances)
        if distance < best_distance:
            best_distance = distance
            best_segment = segment
    return best_segment


def build_transition_path(
    start_point: Tuple[float, float],
    end_point: Tuple[float, float],
) -> List[Tuple[float, float]]:
    """Build a travel path between segments using Google Directions if available."""

    if start_point == end_point:
        return []

    if not GOOGLE_API_KEY:
        return [start_point, end_point]

    params = {
        "origin": f"{start_point[1]},{start_point[0]}",
        "destination": f"{end_point[1]},{end_point[0]}",
        "key": GOOGLE_API_KEY,
        "mode": TRAVEL_MODE,
    }

    try:
        response = requests.get(
            "https://maps.googleapis.com/maps/api/directions/json",
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise ValueError(data.get("status"))
        overview = data["routes"][0]["overview_polyline"]["points"]
        decoded = decode_polyline(overview)
        coords = [(float(lon), float(lat)) for lat, lon in decoded]
        if not coords:
            return [start_point, end_point]
        if coords[0] != start_point:
            coords.insert(0, start_point)
        if coords[-1] != end_point:
            coords.append(end_point)
        return coords
    except Exception as exc:  # noqa: BLE001
        print_stage(
            Fore.YELLOW
            + f"Directions API ошибка ({exc}), используем прямой отрезок"
            + Style.RESET_ALL
        )
        return [start_point, end_point]


def distribute_remaining_segments(
    remaining_ids: Iterable[str],
    segments_by_id: Dict[str, RoadSegment],
    routes: List[RouteResult],
) -> None:
    processed = list(remaining_ids)
    for seg_id in processed:
        segment = segments_by_id[seg_id]
        target_route = min(routes, key=lambda route: route.total_length)
        last_point: Tuple[float, float]
        if target_route.coordinates and target_route.coordinates[-1]:
            last_point = tuple(target_route.coordinates[-1][-1])
        else:
            last_point = (BASE_LON, BASE_LAT) if USE_BASE_AS_START else (CENTER_LON, CENTER_LAT)

        start_coord = tuple(map(float, segment.geometry.coords[0]))
        travel_segment = build_transition_path(last_point, start_coord)
        if travel_segment:
            target_route.coordinates.append(travel_segment.copy())
            target_route.total_length += _line_length_m(travel_segment)

        coords = list(segment.geometry.coords)
        if target_route.coordinates and target_route.coordinates[-1][-1] == coords[0]:
            target_route.coordinates[-1].extend(coords[1:])
        else:
            target_route.coordinates.append(coords)
        target_route.total_length += segment.length_m
    if hasattr(remaining_ids, "clear"):
        remaining_ids.clear()


def write_routes_kml(routes: Sequence[RouteResult], output_path: Path) -> None:
    kml = simplekml.Kml()
    for idx, route in enumerate(routes):
        placemark = kml.newlinestring(name=route.tractor_id)
        coords: List[Tuple[float, float, float]] = []
        for segment in route.coordinates:
            for lon, lat in segment:
                coords.append((lon, lat, 0.0))
        placemark.coords = coords
        placemark.style.linestyle.color = _color_from_hex(route.color)
        placemark.style.linestyle.width = 4
    output_path.write_bytes(kml.kml().encode("utf-8"))


def _color_from_hex(hex_color: str) -> str:
    clean = hex_color.lstrip("#")
    if len(clean) != 6:
        clean = "0000ff"  # fallback: blue
    r = int(clean[0:2], 16)
    g = int(clean[2:4], 16)
    b = int(clean[4:6], 16)
    return simplekml.Color.rgb(r, g, b, 200)


def _ordered_neighbor_cells(target_cells: set[str], cells: Sequence[Cell]) -> List[str]:
    if not target_cells:
        return [cell.identifier for cell in cells]

    target_points = [
        (cell.centroid.y, cell.centroid.x)
        for cell in cells
        if cell.identifier in target_cells
    ]
    distances: List[Tuple[float, str]] = []
    for cell in cells:
        if cell.identifier in target_cells:
            continue
        centroid = (cell.centroid.y, cell.centroid.x)
        distance = min(geodesic(centroid, tp).meters for tp in target_points)
        distances.append((distance, cell.identifier))
    distances.sort(key=lambda item: item[0])
    return [identifier for _, identifier in distances]


# ---------------------------------------------------------------------------
# Command line interface.
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build tractor routes")
    parser.add_argument("--geo", type=Path, default=Path("GEO.kml"))
    parser.add_argument("--roads", type=Path, default=Path("RoadCity.kml"))
    parser.add_argument("--assignments", type=Path, default=Path("assignments.json"))
    parser.add_argument("--output", type=Path, default=Path("routes_grid.kml"))
    parser.add_argument(
        "--generate-grid",
        action="store_true",
        help="Только сформировать GeoJSON сетки и завершиться",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)

    if not GOOGLE_API_KEY:
        print_stage(
            Fore.YELLOW
            + "GOOGLE_API_KEY не задан – переезды будут прямыми линиями"
            + Style.RESET_ALL
        )

    try:
        zone = parse_geo_boundary(args.geo)
        segments = parse_roads(args.roads, zone)
        cells = build_grid(zone, GRID_CELLS)
        save_cells_geojson(cells, Path("static") / "sectors.geojson")
        map_segments_to_cells(segments, cells)
    except Exception as exc:  # noqa: BLE001
        print_stage(Fore.RED + f"Ошибка подготовки данных: {exc}" + Style.RESET_ALL)
        raise

    if args.generate_grid:
        print_stage("Генерация сетки завершена")
        return

    try:
        assignments = load_assignments(args.assignments)
        build_routes_with_assignments(segments, cells, assignments, args.output)
    except Exception as exc:  # noqa: BLE001
        print_stage(Fore.RED + f"Ошибка построения маршрутов: {exc}" + Style.RESET_ALL)
        raise


if __name__ == "__main__":
    main()
