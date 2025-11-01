"""Assignment heuristics for cells and road segments."""
from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point, Polygon, shape
from shapely.ops import linemerge
from shapely.strtree import STRtree

from . import geocoder
from .roads import Road


@dataclass
class Tractor:
    id: str
    name: str
    color: str
    base: Tuple[float, float]


@dataclass
class _Cell:
    id: str
    polygon: Polygon
    centroid: Point
    label: int
    weight: float
    area_km2: float
    road_m: float


EARTH_KM_PER_DEG = 111.139
EARTH_M_PER_DEG = 111_139.0


def assign_cells_kmeans(
    features: Iterable[dict],
    tractors: Sequence[Tractor],
    advanced: bool = False,
    roads: Optional[Sequence[Road]] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
    step_callback: Optional[Callable[[str, str, int, int], None]] = None,
) -> Tuple[Dict[str, str], List[Dict[str, float]]]:
    """Cluster cells into contiguous districts and map to tractors."""

    cells: List[_Cell] = []
    for feature in features:
        geom = shape(feature.get("geometry"))
        if geom.is_empty:
            continue
        if isinstance(geom, Polygon):
            polygon = geom
        else:
            # take the largest polygon part
            polygon = max(geom.geoms, key=lambda g: g.area)
        centroid = polygon.centroid
        area_km2 = polygon.area * (EARTH_KM_PER_DEG ** 2)
        road_m = 0.0
        cell = _Cell(
            id=str(feature.get("properties", {}).get("id")),
            polygon=polygon,
            centroid=centroid,
            label=0,
            weight=max(area_km2 * 1000.0, 1.0),
            area_km2=area_km2,
            road_m=road_m,
        )
        cells.append(cell)

    cells = [cell for cell in cells if cell.id]
    if not cells or not tractors:
        return {}, []

    if advanced and roads:
        _assign_road_weights(cells, roads)

    coords = np.array([[cell.centroid.x, cell.centroid.y] for cell in cells])
    n_clusters = min(len(tractors), len(coords))
    from sklearn.cluster import KMeans

    kmeans = KMeans(n_clusters=n_clusters, n_init="auto", random_state=42)
    labels = kmeans.fit_predict(coords)
    centers = kmeans.cluster_centers_

    for cell, label in zip(cells, labels):
        cell.label = int(label)

    adjacency = _build_adjacency(cells)
    cell_by_id = {cell.id: cell for cell in cells}

    active_tractors = list(tractors[:n_clusters])
    cluster_to_tractor = {idx: active_tractors[idx] for idx in range(len(active_tractors))}

    assignments: Dict[str, str] = {}
    weights = {tractor.id: 0.0 for tractor in active_tractors}
    total_weight = sum(cell.weight for cell in cells)
    target_weight = total_weight / max(len(active_tractors), 1)
    total_cells = len(cells)
    assigned_count = 0

    if progress_callback:
        progress_callback(0, total_cells)

    def _mark_assignment(cell_id: str, tractor_id: str) -> None:
        nonlocal assigned_count
        assignments[cell_id] = tractor_id
        assigned_count += 1
        if step_callback:
            step_callback(cell_id, tractor_id, assigned_count, total_cells)
        if progress_callback:
            progress_callback(assigned_count, total_cells)

    available = {cell.id for cell in cells}
    queue: List[Tuple[float, int, str]] = []

    for idx, tractor in cluster_to_tractor.items():
        cluster_cells = [cell for cell in cells if cell.label == idx]
        if not cluster_cells:
            continue
        center = centers[idx]
        seed = min(
            cluster_cells,
            key=lambda c: _distance((c.centroid.x, c.centroid.y), center),
        )
        _mark_assignment(seed.id, tractor.id)
        weights[tractor.id] += seed.weight
        available.discard(seed.id)
        for neighbor in adjacency.get(seed.id, []):
            if neighbor in available:
                penalty = 0.0
                queue.append(
                    (
                        _distance(
                            (cell_by_id[neighbor].centroid.x, cell_by_id[neighbor].centroid.y),
                            center,
                        ),
                        idx,
                        neighbor,
                    )
                )

    heapq.heapify(queue)

    while available:
        if not queue:
            # fallback: grab nearest cell to any center
            fallback_cell_id = min(
                available,
                key=lambda cid: min(
                    _distance(
                        (cell_by_id[cid].centroid.x, cell_by_id[cid].centroid.y),
                        centers[idx],
                    )
                    for idx in cluster_to_tractor
                ),
            )
            best_cluster = min(
                cluster_to_tractor,
                key=lambda idx: _distance(
                    (cell_by_id[fallback_cell_id].centroid.x, cell_by_id[fallback_cell_id].centroid.y),
                    centers[idx],
                ),
            )
            queue.append((0.0, best_cluster, fallback_cell_id))
            heapq.heapify(queue)
            continue

        priority, cluster_idx, cell_id = heapq.heappop(queue)
        if cell_id not in available:
            continue
        tractor = cluster_to_tractor.get(cluster_idx)
        if tractor is None:
            tractor = active_tractors[cluster_idx % len(active_tractors)]
        cell = cell_by_id[cell_id]

        projected = weights[tractor.id] + cell.weight
        if projected > target_weight * 1.15 and any(
            weights[other.id] < target_weight * 0.9 for other in active_tractors
        ):
            heapq.heappush(queue, (priority + 5.0, cluster_idx, cell_id))
            continue

        _mark_assignment(cell_id, tractor.id)
        weights[tractor.id] = projected
        available.remove(cell_id)

        for neighbor in adjacency.get(cell_id, []):
            if neighbor not in available:
                continue
            neighbor_cell = cell_by_id[neighbor]
            penalty = 0.0 if neighbor_cell.label == cluster_idx else 2.0
            if advanced:
                projected_neighbor = weights[tractor.id] + neighbor_cell.weight
                if projected_neighbor > target_weight:
                    penalty += (projected_neighbor - target_weight) / max(target_weight, 1.0)
            heapq.heappush(
                queue,
                (
                    _distance(
                        (neighbor_cell.centroid.x, neighbor_cell.centroid.y),
                        centers[cluster_idx],
                    )
                    + penalty,
                    cluster_idx,
                    neighbor,
                ),
            )

    if progress_callback:
        progress_callback(total_cells, total_cells)

    summary: List[Dict[str, float]] = []
    for tractor in tractors:
        ids = [cid for cid, tid in assignments.items() if tid == tractor.id]
        area_km2 = sum(cell_by_id[cid].area_km2 for cid in ids if cid in cell_by_id)
        road_m = sum(cell_by_id[cid].road_m for cid in ids if cid in cell_by_id)
        summary.append(
            {
                "tractor": tractor.name,
                "cells": len(ids),
                "area_km2": area_km2,
                "roads_m": road_m,
            }
        )

    return assignments, summary


def _assign_road_weights(cells: Sequence[_Cell], roads: Sequence[Road]) -> None:
    geometries = [road.geometry for road in roads]
    if not geometries:
        return
    tree = STRtree(geometries)
    geom_to_road = {id(geom): road for geom, road in zip(geometries, roads)}

    for cell in cells:
        total_len = 0.0
        for candidate in tree.query(cell.polygon):
            road = geom_to_road[id(candidate)]
            inter = candidate.intersection(cell.polygon)
            if inter.is_empty:
                continue
            if isinstance(inter, LineString):
                total_len += inter.length
            elif isinstance(inter, MultiLineString):
                total_len += sum(part.length for part in inter.geoms)
        length_m = total_len * EARTH_M_PER_DEG
        if length_m > 0:
            cell.weight = max(length_m, cell.weight)
            cell.road_m = length_m


def _build_adjacency(cells: Sequence[_Cell]) -> Dict[str, List[str]]:
    adjacency: Dict[str, List[str]] = defaultdict(list)
    for idx, cell in enumerate(cells):
        for other in cells[idx + 1 :]:
            if cell.polygon.touches(other.polygon):
                adjacency[cell.id].append(other.id)
                adjacency[other.id].append(cell.id)
    return adjacency


def _distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def assign_roads_simple(roads: List[Road], tractors: List[Tractor]) -> Tuple[Dict[str, str], Dict[str, float]]:
    centroids = np.array([[road.geometry.centroid.x, road.geometry.centroid.y] for road in roads])
    from sklearn.cluster import KMeans

    n_clusters = min(len(tractors), len(centroids))
    if n_clusters == 0:
        return {}, {}
    kmeans = KMeans(n_clusters=n_clusters, n_init="auto", random_state=42)
    labels = kmeans.fit_predict(centroids)
    assignments: Dict[str, str] = {}
    stats = defaultdict(float)
    for idx, road in enumerate(roads):
        tractor = tractors[labels[idx]]
        assignments[road.id] = tractor.id
        stats[tractor.id] += road.geometry.length * 111_139
    return assignments, dict(stats)


def assign_roads_advanced(
    roads: List[Road],
    tractors: List[Tractor],
    target_km: float,
    street_source: str = "google",
) -> Tuple[Dict[str, str], Dict[str, float], List[Tuple[str, str, float]]]:
    grouped: Dict[str, List[Road]] = defaultdict(list)
    for road in roads:
        name = road.name
        if not name:
            centroid = road.geometry.centroid
            name = geocoder.get_street_name(centroid.y, centroid.x, source=street_source) or "Без названия"
        grouped[name].append(road)

    merged_segments: List[tuple[str, LineString, List[Road]]] = []
    for name, segments in grouped.items():
        line = linemerge([seg.geometry for seg in segments])
        if isinstance(line, LineString):
            pieces = [line]
        else:
            pieces = list(line.geoms)
        for piece in pieces:
            merged_segments.append((name, LineString(piece.coords), segments))

    centroids = np.array([[seg[1].centroid.x, seg[1].centroid.y] for seg in merged_segments])
    from sklearn.cluster import KMeans

    if len(merged_segments) == 0:
        return {}, {}, []
    n_clusters = min(len(tractors), len(merged_segments))
    kmeans = KMeans(n_clusters=n_clusters, n_init="auto", random_state=42)
    labels = kmeans.fit_predict(centroids)

    assignments: Dict[str, str] = {}
    stats = defaultdict(float)
    street_summary: List[Tuple[str, str, float]] = []

    target_m = target_km

    for idx, (name, geom, original_roads) in enumerate(merged_segments):
        tractor = tractors[labels[idx] % len(tractors)]
        length_m = geom.length * 111_139
        stats[tractor.id] += length_m
        street_summary.append((tractor.name, name, length_m))
        for road in original_roads:
            assignments[road.id] = tractor.id

    # fallback: assign remaining roads by proximity
    for road in roads:
        if road.id in assignments:
            continue
        centroid = road.geometry.centroid
        best = min(
            tractors,
            key=lambda t: _distance_point(t.base, centroid),
        )
        assignments[road.id] = best.id
        stats[best.id] += road.geometry.length * 111_139
        street_summary.append((best.name, road.name or "Без названия", road.geometry.length * 111_139))

    # simple balancing
    avg = sum(stats.values()) / max(len(tractors), 1)
    for _ in range(5):
        over = {tid: val for tid, val in stats.items() if val > avg * 1.15}
        under = {tid: val for tid, val in stats.items() if val < avg * 0.9}
        if not over or not under:
            break
        for rid, tid in list(assignments.items()):
            if tid not in over:
                continue
            road = next((r for r in roads if r.id == rid), None)
            if road is None:
                continue
            centroid = road.geometry.centroid
            target_tractor = min(
                under,
                key=lambda other: _distance_point(
                    next(t for t in tractors if t.id == other).base, centroid
                ),
            )
            assignments[rid] = target_tractor
            stats[tid] -= road.geometry.length * 111_139
            stats[target_tractor] += road.geometry.length * 111_139
            if stats[tid] <= avg * 1.1:
                break
    return assignments, dict(stats), street_summary


def _distance_point(base: Tuple[float, float], centroid: Point) -> float:
    lat, lon = base
    return math.hypot(lon - centroid.x, lat - centroid.y)
