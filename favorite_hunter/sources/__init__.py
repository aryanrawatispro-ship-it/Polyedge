"""External data sources used by the probability engines."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class TTLCache:
    """Tiny thread-safe TTL cache. Failures are cached briefly too, so a dead
    source is not hammered once per candidate within a scan."""

    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, bool, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Any, ttl: float, fn: Callable[[], T], *, error_ttl: float = 30.0) -> T:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
        if hit is not None and hit[0] > now:
            ok, value = hit[1], hit[2]
            if ok:
                return value
            raise value
        try:
            value = fn()
        except Exception as exc:  # cached and re-raised to every caller in the window
            with self._lock:
                self._data[key] = (now + error_ttl, False, exc)
            raise
        with self._lock:
            self._data[key] = (now + ttl, True, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
