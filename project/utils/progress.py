"""Helpers for emitting progress updates over Socket.IO in background threads."""
from __future__ import annotations

import traceback
from threading import Thread
from typing import Any, Callable, Optional

from flask_socketio import SocketIO

__all__ = ["init_progress", "emit_progress", "run_async"]

_socketio: Optional[SocketIO] = None


def init_progress(io: SocketIO) -> None:
    """Bind the global Socket.IO instance used for progress events."""
    global _socketio
    _socketio = io


def emit_progress(stage: str, text: str, progress: float | int | None = None) -> None:
    """Send a structured progress payload and mirrored log line to the UI."""
    if _socketio is None:
        return
    payload = {
        "stage": stage.upper(),
        "text": text,
        "progress": float(progress) if progress is not None else 0.0,
    }
    _socketio.emit("progress", payload)
    _socketio.emit("log", {"message": f"[{stage.upper()}] {text}"})


def run_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Execute *func* in a daemon thread, capturing and logging exceptions."""

    def _runner() -> None:
        try:
            func(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive
            if _socketio is not None:
                _socketio.emit("log", {"message": "[ERROR] Фоновая задача завершилась с ошибкой"})
            traceback.print_exc()

    Thread(target=_runner, daemon=True).start()
