"""Shared HTTP layer: per-host rate limiting, retries, and explicit failures.

Every external call goes through :class:`HttpClient`. When a source cannot be
reached the client raises :class:`DataUnavailable` with the reason; callers must
surface that as "DATA UNAVAILABLE" instead of substituting any value.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DataUnavailable(Exception):
    """An external data source could not provide the requested data."""

    def __init__(self, source: str, reason: str):
        super().__init__(f"DATA UNAVAILABLE [{source}]: {reason}")
        self.source = source
        self.reason = reason


@dataclass
class SourceHealth:
    ok: int = 0
    errors: int = 0
    last_ok: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None


class RateLimiter:
    """Simple thread-safe token bucket keyed by host."""

    def __init__(self, rates: dict[str, float]):
        self._rates = dict(rates)
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        rate = self._rates.get(host, self._rates.get("default", 5.0))
        if rate <= 0:
            return
        interval = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed.get(host, now))
            self._next_allowed[host] = slot + interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        user_agent: str = "favorite-hunter",
        rate_limits: dict[str, float] | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = RateLimiter(rate_limits or {"default": 5.0})
        self._max_retries = max_retries
        self._health: dict[str, SourceHealth] = {}
        self._health_lock = threading.Lock()

    def _record(self, source: str, ok: bool, error: str | None = None) -> None:
        now = datetime.now(UTC)
        with self._health_lock:
            health = self._health.setdefault(source, SourceHealth())
            if ok:
                health.ok += 1
                health.last_ok = now
            else:
                health.errors += 1
                health.last_error = error
                health.last_error_at = now

    def drain_health(self) -> dict[str, SourceHealth]:
        """Per-source request outcomes since the last drain."""
        with self._health_lock:
            drained, self._health = self._health, {}
        return drained

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_json(self, url: str, params: dict[str, Any] | None = None, *, source: str | None = None) -> Any:
        return self._tracked("GET", url, params, None, source)

    def post_json(self, url: str, body: Any, *, source: str | None = None) -> Any:
        return self._tracked("POST", url, None, body, source)

    def _tracked(self, method: str, url: str, params: Any, body: Any, source: str | None) -> Any:
        name = source or urlparse(url).netloc
        try:
            result = self._request(method, url, params=params, json_body=body, source=name)
        except DataUnavailable as exc:
            self._record(name, False, exc.reason)
            raise
        self._record(name, True)
        return result

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        source: str | None = None,
    ) -> Any:
        host = urlparse(url).netloc
        source = source or host
        clean_params = _clean_params(params)
        last_reason = "unknown error"
        for attempt in range(self._max_retries + 1):
            self._limiter.wait(host)
            try:
                response = self._client.request(method, url, params=clean_params, json=json_body)
            except httpx.ProxyError as exc:
                # Egress proxy refused the destination: retrying will not help.
                raise DataUnavailable(source, f"blocked by network proxy ({exc})") from exc
            except httpx.TimeoutException as exc:
                last_reason = f"timeout ({exc.__class__.__name__})"
            except httpx.TransportError as exc:
                last_reason = f"connection error ({exc})"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DataUnavailable(source, "response was not valid JSON") from exc
                if response.status_code not in RETRYABLE_STATUS:
                    raise DataUnavailable(
                        source, f"HTTP {response.status_code}: {response.text[:200].strip()}"
                    )
                last_reason = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response)
                if retry_after is not None and attempt < self._max_retries:
                    time.sleep(min(retry_after, 30.0))
                    continue
            if attempt < self._max_retries:
                time.sleep(min(2.0**attempt, 16.0) + random.uniform(0, 0.25))
        raise DataUnavailable(source, f"{last_reason} after {self._max_retries + 1} attempts")


def _clean_params(params: dict[str, Any] | None) -> list[tuple[str, str]] | None:
    """Drop ``None`` values, lower-case booleans and expand sequences."""
    if not params:
        return None
    items: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if isinstance(item, bool):
                items.append((key, "true" if item else "false"))
            else:
                items.append((key, str(item)))
    return items


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None
