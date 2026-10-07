"""Read-only Polymarket API client (Gamma, CLOB, Data API).

Endpoints follow Polymarket's current unified SDK (github.com/Polymarket/py-sdk):

* Gamma ``GET /markets/keyset``  -> ``{"markets": [...], "next_cursor": ...}``
  (``limit`` + ``after_cursor``); legacy ``GET /markets`` offset paging is the
  fallback.
* CLOB ``POST /books`` (body ``[{"token_id": ...}]``) and ``GET /book``.
* CLOB ``GET /clob-markets/{condition_id}`` -> fee data ``fd: {r, e}``.
* Data ``GET /v2/prices-history`` (paged ``{"data", "pagination"}``) with the
  legacy CLOB ``GET /prices-history`` as fallback.
* Data ``GET /v2/resolutions``.

This client never places, signs or cancels orders: Favorite Hunter is paper
trading only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any

from .config import Settings
from .http import DataUnavailable, HttpClient
from .models import FeeSchedule, OrderBook, parse_book, to_float
from .timeutil import parse_dt, utcnow

log = logging.getLogger(__name__)


class PolymarketClient:
    def __init__(self, http: HttpClient, *, gamma_url: str, clob_url: str, data_url: str):
        self.http = http
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self._keyset_supported: bool | None = None
        self._fee_cache: dict[str, tuple[datetime, FeeSchedule | None]] = {}

    @classmethod
    def from_settings(cls, settings: Settings, http: HttpClient | None = None) -> "PolymarketClient":
        src = settings.sources
        http = http or HttpClient(
            timeout=src.request_timeout,
            max_retries=src.max_retries,
            user_agent=src.user_agent,
            rate_limits=src.rate_limits,
        )
        return cls(http, gamma_url=src.gamma_url, clob_url=src.clob_url, data_url=src.data_url)

    # ------------------------------------------------------------------ Gamma

    def iter_markets(
        self,
        *,
        closed: bool | None = False,
        page_size: int = 500,
        max_markets: int | None = None,
        end_date_min: datetime | None = None,
        end_date_max: datetime | None = None,
        liquidity_num_min: float | None = None,
        volume_num_min: float | None = None,
        order: str | None = None,
        ascending: bool | None = None,
        include_tag: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw Gamma market objects, following pagination."""
        params: dict[str, Any] = {
            "closed": closed,
            # Same encoding as Polymarket's SDK: datetime.isoformat() ("...+00:00").
            "end_date_min": end_date_min.isoformat() if end_date_min else None,
            "end_date_max": end_date_max.isoformat() if end_date_max else None,
            "liquidity_num_min": liquidity_num_min or None,
            "volume_num_min": volume_num_min or None,
            "order": order,
            "ascending": ascending,
            "include_tag": include_tag or None,
        }
        params.update(extra or {})
        if self._keyset_supported is not False:
            first_page_ok = [False]
            try:
                yield from self._iter_markets_keyset(params, page_size, max_markets, first_page_ok)
                return
            except DataUnavailable as exc:
                # Fall back only when the very first keyset request is rejected;
                # a failure mid-way must not restart paging and duplicate rows.
                if first_page_ok[0] or self._keyset_supported or not _is_missing_endpoint(exc):
                    raise
                log.warning("gamma /markets/keyset unavailable (%s); using offset paging", exc.reason)
                self._keyset_supported = False
        yield from self._iter_markets_offset(params, page_size, max_markets)

    def _iter_markets_keyset(
        self,
        params: dict[str, Any],
        page_size: int,
        max_markets: int | None,
        first_page_ok: list[bool],
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        yielded = 0
        while True:
            page_params = dict(params, limit=page_size, after_cursor=cursor)
            payload = self.http.get_json(f"{self.gamma_url}/markets/keyset", page_params, source="gamma")
            first_page_ok[0] = True
            self._keyset_supported = True
            if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
                raise DataUnavailable("gamma", "unexpected /markets/keyset response shape")
            for market in payload["markets"]:
                if isinstance(market, dict):
                    yield market
                    yielded += 1
                    if max_markets and yielded >= max_markets:
                        return
            cursor = payload.get("next_cursor") or None
            if not cursor or not payload["markets"]:
                return

    def _iter_markets_offset(
        self, params: dict[str, Any], page_size: int, max_markets: int | None
    ) -> Iterator[dict[str, Any]]:
        offset = 0
        yielded = 0
        while True:
            page = self.http.get_json(
                f"{self.gamma_url}/markets", dict(params, limit=page_size, offset=offset), source="gamma"
            )
            if not isinstance(page, list):
                raise DataUnavailable("gamma", "unexpected /markets response shape")
            for market in page:
                if isinstance(market, dict):
                    yield market
                    yielded += 1
                    if max_markets and yielded >= max_markets:
                        return
            if len(page) < page_size:
                return
            offset += page_size

    def get_market(self, market_id: str) -> dict[str, Any]:
        payload = self.http.get_json(f"{self.gamma_url}/markets/{market_id}", source="gamma")
        if not isinstance(payload, dict):
            raise DataUnavailable("gamma", f"unexpected /markets/{market_id} response")
        return payload

    def get_markets_by_condition_ids(self, condition_ids: Iterable[str], *, closed: bool | None = None) -> list[dict[str, Any]]:
        """Look up markets (open or closed) by condition id, 50 ids per request."""
        ids = [cid for cid in dict.fromkeys(condition_ids) if cid]
        results: list[dict[str, Any]] = []
        for start in range(0, len(ids), 50):
            chunk = ids[start : start + 50]
            results.extend(
                self.iter_markets(
                    closed=closed,
                    page_size=100,
                    include_tag=False,
                    extra={"condition_ids": chunk},
                )
            )
        return results

    def get_event(self, event_id: str) -> dict[str, Any]:
        payload = self.http.get_json(f"{self.gamma_url}/events/{event_id}", source="gamma")
        if not isinstance(payload, dict):
            raise DataUnavailable("gamma", f"unexpected /events/{event_id} response")
        return payload

    # ------------------------------------------------------------------- CLOB

    def get_books(self, token_ids: Iterable[str], *, batch_size: int = 50) -> dict[str, OrderBook]:
        """Fetch order books for many tokens via ``POST /books``.

        Tokens missing from the response are simply absent from the result.
        """
        ids = [t for t in dict.fromkeys(token_ids) if t]
        books: dict[str, OrderBook] = {}
        for start in range(0, len(ids), batch_size):
            chunk = ids[start : start + batch_size]
            payload = self.http.post_json(
                f"{self.clob_url}/books", [{"token_id": t} for t in chunk], source="clob"
            )
            fetched_at = utcnow()
            if not isinstance(payload, list):
                raise DataUnavailable("clob", "unexpected /books response shape")
            for raw in payload:
                if isinstance(raw, dict):
                    book = parse_book(raw, fetched_at)
                    if book.token_id:
                        books[book.token_id] = book
        return books

    def get_book(self, token_id: str) -> OrderBook:
        payload = self.http.get_json(f"{self.clob_url}/book", {"token_id": token_id}, source="clob")
        if not isinstance(payload, dict):
            raise DataUnavailable("clob", "unexpected /book response shape")
        return parse_book(payload, utcnow())

    def get_fee_schedule(self, condition_id: str, *, max_age_seconds: float = 3600) -> FeeSchedule | None:
        """Taker fee parameters from ``/clob-markets/{condition_id}`` (cached)."""
        cached = self._fee_cache.get(condition_id)
        now = utcnow()
        if cached and (now - cached[0]).total_seconds() < max_age_seconds:
            return cached[1]
        try:
            payload = self.http.get_json(f"{self.clob_url}/clob-markets/{condition_id}", source="clob")
        except DataUnavailable as exc:
            log.debug("fee schedule unavailable for %s: %s", condition_id, exc.reason)
            return None
        schedule: FeeSchedule | None = None
        if isinstance(payload, dict):
            fd = payload.get("fd")
            if fd is None:
                schedule = FeeSchedule(rate=0.0, exponent=0.0, source="clob-markets (no fee data)")
            elif isinstance(fd, dict):
                rate = to_float(fd.get("r"))
                exponent = to_float(fd.get("e"))
                if rate is not None:
                    schedule = FeeSchedule(rate=rate, exponent=exponent or 0.0, source="clob-markets.fd")
        self._fee_cache[condition_id] = (now, schedule)
        return schedule

    # ------------------------------------------------------------- Data API

    def get_price_history(
        self,
        token_id: str,
        *,
        start: datetime,
        end: datetime,
        bucket_seconds: int = 3600,
    ) -> list[tuple[datetime, float]]:
        """Historical prices for a token (window <= 15 days on the v2 API).

        Returns (timestamp, price) pairs sorted by time. These are traded/marked
        prices, not executable asks, so backtests built on them are optimistic.
        """
        try:
            return self._price_history_v2(token_id, start, end, bucket_seconds)
        except DataUnavailable as exc:
            log.debug("v2 prices-history failed for %s (%s); trying CLOB", token_id, exc.reason)
        payload = self.http.get_json(
            f"{self.clob_url}/prices-history",
            {
                "market": token_id,
                "startTs": int(start.timestamp()),
                "endTs": int(end.timestamp()),
                "fidelity": max(bucket_seconds // 60, 1),
            },
            source="clob",
        )
        history = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(history, list):
            raise DataUnavailable("clob", "unexpected /prices-history response shape")
        points = []
        for item in history:
            if not isinstance(item, dict):
                continue
            ts = parse_dt(item.get("t"))
            price = to_float(item.get("p"))
            if ts is not None and price is not None:
                points.append((ts, price))
        return sorted(points)

    def _price_history_v2(
        self, token_id: str, start: datetime, end: datetime, bucket_seconds: int
    ) -> list[tuple[datetime, float]]:
        points: list[tuple[datetime, float]] = []
        cursor: str | None = None
        while True:
            payload = self.http.get_json(
                f"{self.data_url}/v2/prices-history",
                {
                    "token_id": token_id,
                    "start": int(start.timestamp()),
                    "end": int(end.timestamp()),
                    "bucket_seconds": bucket_seconds,
                    "limit": 10000,
                    "cursor": cursor,
                },
                source="data-api",
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise DataUnavailable("data-api", "unexpected /v2/prices-history response shape")
            for item in payload["data"]:
                if not isinstance(item, dict):
                    continue
                ts = parse_dt(item.get("timestamp"))
                price = to_float(item.get("price"))
                if ts is not None and price is not None:
                    points.append((ts, price))
            pagination = payload.get("pagination") or {}
            cursor = pagination.get("next_cursor") if isinstance(pagination, dict) else None
            if not cursor:
                return sorted(points)

    def get_resolutions(self, condition_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [c for c in dict.fromkeys(condition_ids) if c]
        out: list[dict[str, Any]] = []
        for start in range(0, len(ids), 20):
            payload = self.http.get_json(
                f"{self.data_url}/v2/resolutions",
                {"condition_id": ",".join(ids[start : start + 20])},
                source="data-api",
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise DataUnavailable("data-api", "unexpected /v2/resolutions response shape")
            out.extend(item for item in data if isinstance(item, dict))
        return out


def _is_missing_endpoint(exc: DataUnavailable) -> bool:
    return exc.reason.startswith(("HTTP 404", "HTTP 405", "HTTP 400"))
