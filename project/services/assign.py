"""High-level auto-assignment helpers."""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Sequence

# FIX: pull config paths via parent package for compatibility with repo-level entrypoint
from ..config import ASSIGNMENTS_JSON, ROADS_ASSIGN_JSON
# FIX: address route builder imports via project package namespace
from ..route_builder.optimizer import (
    Tractor,
    assign_cells_kmeans,
    assign_roads_advanced,
    assign_roads_simple,
)
from ..route_builder.roads import Road
from ..utils.progress import emit_progress


def save_cell_assignments(assignments: Dict[str, str]) -> None:
    ASSIGNMENTS_JSON.write_text(
        json.dumps(assignments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_cell_assignments() -> Dict[str, str]:
    if not ASSIGNMENTS_JSON.exists():
        return {}
    try:
        data = json.loads(ASSIGNMENTS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(data, dict) and "assignments" in data:
        data = data["assignments"]
    return {str(k): str(v) for k, v in (data or {}).items() if isinstance(k, str) and isinstance(v, str)}


def auto_assign_cells(
    features: Iterable[dict],
    tractors: Sequence[Tractor],
    *,
    advanced: bool = False,
    roads: Optional[Sequence[Road]] = None,
    progress_cb=None,
    step_cb=None,
) -> tuple[Dict[str, str], List[Dict[str, float]]]:
    emit_progress("assign", "🔁 Автораспределение клеток запущено", 5)
    assignments, summary = assign_cells_kmeans(
        features,
        tractors,
        advanced=advanced,
        roads=roads,
        progress_callback=progress_cb,
        step_callback=step_cb,
    )
    save_cell_assignments(assignments)
    return assignments, summary


def auto_assign_roads(
    roads: List[Road],
    tractors: Sequence[Tractor],
    *,
    advanced: bool = False,
    target_km: float = 30_000.0,
    street_source: str = "google",
) -> tuple[Dict[str, str], Dict[str, float], Optional[List[tuple[str, str, float]]]]:
    emit_progress("assign", "🔁 Автораспределение дорог", 5)
    summary: Optional[List[tuple[str, str, float]]] = None
    if advanced:
        assignments, stats, summary = assign_roads_advanced(
            roads,
            list(tractors),
            target_km,
            street_source=street_source,
        )
    else:
        assignments, stats = assign_roads_simple(roads, list(tractors))
        summary = []
    ROADS_ASSIGN_JSON.write_text(
        json.dumps(assignments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return assignments, stats, summary
