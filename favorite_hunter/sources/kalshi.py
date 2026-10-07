"""Kalshi public market data (no key needed for reads)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..http import DataUnavailable, HttpClient
from ..models import to_float
from ..timeutil import utcnow
from . import TTLCache


@dataclass
class KalshiQuote:
    ticker: str
    title: str
    yes_bid: float | None
    yes_ask: float | None
    last_price: float | None
    status: str
    observed_at: datetime
    url: str

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None or self.yes_ask <= 0:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


def _price(market: dict, key: str) -> float | None:
    dollars = to_float(market.get(f"{key}_dollars"))
    if dollars is not None:
        return dollars
    cents = to_float(market.get(key))
    return None if cents is None else cents / 100.0


class KalshiClient:
    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.base = settings.sources.kalshi_url.rstrip("/")
        self.cache = TTLCache()

    def market(self, ticker: str) -> KalshiQuote:
        url = f"{self.base}/markets/{ticker}"

        def fetch() -> KalshiQuote:
            data = self.http.get_json(url, source="kalshi")
            market = data.get("market") if isinstance(data, dict) else None
            if not isinstance(market, dict):
                raise DataUnavailable("kalshi", f"unexpected response for {ticker}")
            return KalshiQuote(
                ticker=ticker,
                title=market.get("title") or ticker,
                yes_bid=_price(market, "yes_bid"),
                yes_ask=_price(market, "yes_ask"),
                last_price=_price(market, "last_price"),
                status=market.get("status") or "",
                observed_at=utcnow(),
                url=f"https://kalshi.com/markets/{ticker}",
            )

        return self.cache.get_or_set(("market", ticker), 30.0, fetch)
