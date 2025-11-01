from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from route_builder import TRACTOR_COLORS
from route_builder.grid import load_geo_boundary
from route_builder.kml_export import export_roads_kml, export_routes_kml
from route_builder.optimizer import Tractor
from route_builder.router import RoutingSettings, build_routes
from route_builder.roads import load_roads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route builder orchestrator")
    parser.add_argument("--geo", required=True)
    parser.add_argument("--roads", required=True)
    parser.add_argument("--roads-assignment", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--grid-cells", type=int, default=64)
    parser.add_argument("--n-units", type=int, default=18)
    parser.add_argument("--target-km", type=float, default=30_000.0)
    parser.add_argument("--travel-mode", default="driving")
    parser.add_argument("--max-waypoints", type=int, default=23)
    parser.add_argument("--base-lat", type=float, default=0.0)
    parser.add_argument("--base-lon", type=float, default=0.0)
    parser.add_argument("--center-lat", type=float, default=0.0)
    parser.add_argument("--center-lon", type=float, default=0.0)
    parser.add_argument("--use-base-start", action="store_true")
    parser.add_argument("--request-pause", type=float, default=0.35)
    parser.add_argument("--sample-every-m", type=float, default=150.0)
    parser.add_argument("--roads-colored", default=None)
    return parser.parse_args()


def load_assignments(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Файл назначений дорог не найден: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def create_tractors(n_units: int, base_lat: float, base_lon: float) -> list[Tractor]:
    tractors = []
    for idx in range(n_units):
        tractors.append(
            Tractor(
                id=f"tractor_{idx+1:02d}",
                name=f"Трактор {idx+1:02d}",
                color=TRACTOR_COLORS[idx % len(TRACTOR_COLORS)],
                base=(base_lat, base_lon),
            )
        )
    return tractors


def main() -> None:
    load_dotenv()
    args = parse_args()

    geo_path = Path(args.geo)
    roads_path = Path(args.roads)
    assignment_path = Path(args.roads_assignment)
    output_path = Path(args.output)

    print("Чтение входных файлов…", flush=True)
    boundary = load_geo_boundary(geo_path)
    roads = load_roads(roads_path, boundary=boundary)
    assignments = load_assignments(assignment_path)

    tractors = create_tractors(args.n_units, args.base_lat, args.base_lon)

    settings = RoutingSettings(
        travel_mode=args.travel_mode,
        max_waypoints=args.max_waypoints,
        request_pause=args.request_pause,
        sample_every_m=args.sample_every_m,
        use_base_start=args.use_base_start,
        base_lat=args.base_lat,
        base_lon=args.base_lon,
        target_km=args.target_km,
    )

    def progress_cb(message: str) -> None:
        print(message, flush=True)

    def route_step_cb(tractor_id: str, coords):
        for lat, lon in coords:
            print(f"[ROUTE_STEP] tractor={tractor_id} lat={lat} lon={lon}", flush=True)

    def route_done_cb(tractor_id: str, total_length: float):
        print(f"[ROUTE_DONE] tractor={tractor_id} length={total_length:.2f}", flush=True)

    print("Кластеризация дорожных сегментов…", flush=True)
    print("Построение маршрутов…", flush=True)
    routes = build_routes(
        roads,
        assignments,
        tractors,
        settings,
        progress_cb=progress_cb,
        route_step_cb=route_step_cb,
        route_done_cb=route_done_cb,
    )

    print("Экспорт в KML…", flush=True)
    export_routes_kml(routes, tractors, output_path)
    if args.roads_colored:
        export_roads_kml(assignments, roads, tractors, Path(args.roads_colored))
    print("Готово", flush=True)


if __name__ == "__main__":
    main()
