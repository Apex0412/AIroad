"""Helpers for running background tasks with shared progress reporting."""
from __future__ import annotations

from functools import wraps
from threading import Lock, Thread
from typing import Any, Callable

from utils.progress import emit_progress


class TaskLock:
    """Reentrant-unsafe lock with helper context."""

    def __init__(self) -> None:
        self._lock = Lock()

    def acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()

    def guarded(self, func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> None:
            if not self.acquire():
                emit_progress("SYSTEM", "⚠️ Задача уже выполняется", 0)
                return
            try:
                func(*args, **kwargs)
            finally:
                self.release()

        return wrapper


def run_async(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    Thread(target=func, args=args, kwargs=kwargs, daemon=True).start()
