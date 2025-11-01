"""Assignment heuristics for cells and road segments."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import linemerge

from . import geocoder
from .roads import Road


@dataclass
class Tractor:
    id: str
    name: str
    color: str
    base: Tuple[float, float]


def assign_cells_kmeans(
    features: Iterable[dict], tractors: List[Tractor]
) -> Dict[str, str]:
    """Cluster cell centroids and map to tractors."""
    coords = []
    cell_ids = []
    for feature in features:
        geom = feature["geometry"]
        if geom["type"] == "Polygon":
            xs, ys = zip(*geom["coordinates"][0])
        elif geom["type"] == "MultiPolygon":
            xs, ys = zip(*geom["coordinates"][0][0])
        else:
            continue
        lon = float(np.mean(xs))
        lat = float(np.mean(ys))
        coords.append([lon, lat])
        cell_ids.append(feature["properties"]["id"])
    if not coords:
        return {}
    X = np.array(coords)
    n_clusters = min(len(tractors), len(X))
    from sklearn.cluster import KMeans

    kmeans = KMeans(n_clusters=n_clusters, n_init="auto", random_state=42)
    labels = kmeans.fit_predict(X)
    assignments = {}
    for idx, cell_id in enumerate(cell_ids):
        tractor = tractors[labels[idx] % len(tractors)]
        assignments[cell_id] = tractor.id
    return _smooth_assignments(assignments, features)


def _smooth_assignments(assignments: Dict[str, str], features: Iterable[dict]) -> Dict[str, str]:
    adjacency: Dict[str, List[str]] = defaultdict(list)
    id_to_geom: Dict[str, Polygon] = {}
    for feature in features:
        geom = feature["geometry"]
        polygon = Polygon(geom["coordinates"][0])
        sid = feature["properties"]["id"]
        id_to_geom[sid] = polygon
    ids = list(id_to_geom.keys())
    for i, sid in enumerate(ids):
        for other in ids[i + 1 :]:
            if id_to_geom[sid].touches(id_to_geom[other]):
                adjacency[sid].append(other)
                adjacency[other].append(sid)
    visited = set()
    smoothed = assignments.copy()
    for sid in ids:
        if sid in visited:
            continue
        cluster = []
        queue = deque([sid])
        while queue:
            cid = queue.popleft()
            if cid in visited:
                continue
            visited.add(cid)
            cluster.append(cid)
            for neigh in adjacency[cid]:
                if assignments.get(neigh) == assignments.get(cid):
                    queue.append(neigh)
        # nothing else to do currently, placeholder for smoothing
    return smoothed


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
