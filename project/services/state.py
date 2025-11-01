"""In-memory state storage for the dispatcher backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(slots=True)
class DispatcherState:
    settings: Dict[str, Any] = field(default_factory=dict)
    grid: Optional[Dict[str, Any]] = None
    grid_error: Optional[str] = None
    roads: Optional[Dict[str, Any]] = None
    roads_error: Optional[str] = None
    assignments: Dict[str, str] = field(default_factory=dict)
    assignments_summary: Optional[list[dict[str, Any]]] = None
    routes_ready: bool = False
    routes_error: Optional[str] = None
    route_stats: Optional[Dict[str, Any]] = None
    base_info: Optional[Dict[str, Any]] = None
    monitor: list[Dict[str, Any]] = field(default_factory=list)


STATE = DispatcherState()


def reset_routes() -> None:
    STATE.routes_ready = False
    STATE.routes_error = None


def set_grid(data: Optional[Dict[str, Any]], *, error: Optional[str] = None) -> None:
    STATE.grid = data
    STATE.grid_error = error


def set_roads(data: Optional[Dict[str, Any]], *, error: Optional[str] = None) -> None:
    STATE.roads = data
    STATE.roads_error = error


def set_assignments(assignments: Dict[str, str], summary: Optional[list[dict[str, Any]]] = None) -> None:
    STATE.assignments = assignments
    STATE.assignments_summary = summary


def set_route_stats(stats: Optional[Dict[str, Any]]) -> None:
    STATE.route_stats = stats


def append_monitor_entry(entry: Dict[str, Any]) -> None:
    STATE.monitor.append(entry)
    # keep monitor log bounded
    if len(STATE.monitor) > 2000:
        STATE.monitor = STATE.monitor[-2000:]
