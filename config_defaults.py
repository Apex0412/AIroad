"""Shared default configuration for the tractor dispatcher."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class BuildDefaults:
    """Container for configurable build parameters."""

    n_units: int = 18
    target_km: float = 30.0
    grid_cells: int = 64
    travel_mode: str = "driving"
    max_waypoints: int = 23
    use_base_as_start: bool = True
    base_lat: float = 54.920031
    base_lon: float = 37.40809
    center_lat: float = 54.920031
    center_lon: float = 37.40809


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

GRID_CELL_OPTIONS = [36, 49, 64, 81, 100, 121, 144, 196]
"""Allowed grid densities for the UI selector."""
