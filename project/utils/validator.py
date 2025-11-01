"""Validation helpers for input data files."""
from __future__ import annotations

from pathlib import Path
from typing import Tuple


def validate_geo_kml(path: Path) -> Tuple[bool, str | None]:
    """Basic sanity checks for GEO.kml files.

    Returns (is_valid, error_message).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, "Файл GEO.kml не найден"
    except Exception as exc:  # pragma: no cover - IO edge cases
        return False, str(exc)

    lowered = text.lower()
    if "<coordinates" not in lowered:
        return False, "Файл не содержит координат"
    if "<polygon" not in lowered and "<linearring" not in lowered:
        return False, "Нет тегов Polygon или LinearRing"
    return True, None
