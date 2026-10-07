"""Crypto market data: spot prices, candles, realised and implied volatility.

Sources (all public, no API key):
* Binance spot market data (data-api.binance.vision) - also the resolution
  source of most Polymarket crypto price markets
* Coinbase Exchange and Kraken spot tickers - independent cross-checks
* Deribit DVOL index - 30-day options-implied volatility for BTC and ETH
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Settings
from ..http import DataUnavailable, HttpClient
from ..models import to_float
from ..timeutil import parse_dt, utcnow
from . import TTLCache

MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass(frozen=True)
class Asset:
    symbol: str
    names: tuple[str, ...]
    binance: str | None
    coinbase: str | None
    kraken: str | None
    deribit: str | None = None


ASSETS: dict[str, Asset] = {
    "BTC": Asset("BTC", ("bitcoin", "btc"), "BTCUSDT", "BTC-USD", "XBTUSD", "BTC"),
    "ETH": Asset("ETH", ("ethereum", "eth", "ether"), "ETHUSDT", "ETH-USD", "ETHUSD", "ETH"),
    "SOL": Asset("SOL", ("solana", "sol"), "SOLUSDT", "SOL-USD", "SOLUSD"),
    "XRP": Asset("XRP", ("xrp", "ripple"), "XRPUSDT", "XRP-USD", "XRPUSD"),
    "DOGE": Asset("DOGE", ("dogecoin", "doge"), "DOGEUSDT", "DOGE-USD", "XDGUSD"),
    "BNB": Asset("BNB", ("bnb",), "BNBUSDT", None, None),
    "ADA": Asset("ADA", ("cardano", "ada"), "ADAUSDT", "ADA-USD", "ADAUSD"),
    "LTC": Asset("LTC", ("litecoin", "ltc"), "LTCUSDT", "LTC-USD", "LTCUSD"),
}


@dataclass(frozen=True)
class Quote:
    source: str
    price: float
    observed_at: datetime
    url: str


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


class CryptoData:
    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.src = settings.sources
        self.cache = TTLCache()

    # ------------------------------------------------------------------ spot

    def spot_quotes(self, asset: Asset) -> tuple[list[Quote], list[str]]:
        """Spot quotes from every reachable exchange, plus error strings."""
        quotes: list[Quote] = []
        errors: list[str] = []
        for name, fn in (("Binance", self._binance_spot), ("Coinbase", self._coinbase_spot), ("Kraken", self._kraken_spot)):
            try:
                quote = self.cache.get_or_set(("spot", name, asset.symbol), 10.0, lambda fn=fn: fn(asset))
            except DataUnavailable as exc:
                errors.append(f"{name}: {exc.reason}")
                continue
            if quote is not None:
                quotes.append(quote)
        return quotes, errors

    def _binance_spot(self, asset: Asset) -> Quote | None:
        if not asset.binance:
            return None
        url = f"{self.src.binance_url}/api/v3/ticker/price"
        data = self.http.get_json(url, {"symbol": asset.binance}, source="binance")
        price = to_float(data.get("price")) if isinstance(data, dict) else None
        if price is None:
            raise DataUnavailable("binance", "unexpected ticker response")
        return Quote(f"Binance {asset.binance}", price, utcnow(), f"{url}?symbol={asset.binance}")

    def _coinbase_spot(self, asset: Asset) -> Quote | None:
        if not asset.coinbase:
            return None
        url = f"{self.src.coinbase_url}/products/{asset.coinbase}/ticker"
        data = self.http.get_json(url, source="coinbase")
        price = to_float(data.get("price")) if isinstance(data, dict) else None
        if price is None:
            raise DataUnavailable("coinbase", "unexpected ticker response")
        observed = parse_dt(data.get("time")) or utcnow()
        return Quote(f"Coinbase {asset.coinbase}", price, observed, url)

    def _kraken_spot(self, asset: Asset) -> Quote | None:
        if not asset.kraken:
            return None
        url = f"{self.src.kraken_url}/0/public/Ticker"
        data = self.http.get_json(url, {"pair": asset.kraken}, source="kraken")
        if not isinstance(data, dict) or data.get("error"):
            raise DataUnavailable("kraken", f"error {data.get('error') if isinstance(data, dict) else data}")
        result = data.get("result") or {}
        for payload in result.values():
            last = payload.get("c") if isinstance(payload, dict) else None
            if isinstance(last, list) and last:
                price = to_float(last[0])
                if price is not None:
                    return Quote(f"Kraken {asset.kraken}", price, utcnow(), f"{url}?pair={asset.kraken}")
        raise DataUnavailable("kraken", "unexpected ticker response")

    # --------------------------------------------------------------- candles

    def candles(self, asset: Asset, interval: str, start: datetime, end: datetime) -> list[Candle]:
        """Binance klines (interval '1m', '5m', '1h', '1d'), paging as needed."""
        if not asset.binance:
            raise DataUnavailable("binance", f"no Binance market for {asset.symbol}")
        key = ("klines", asset.binance, interval, int(start.timestamp()) // 60, int(end.timestamp()) // 60)
        return self.cache.get_or_set(key, 30.0, lambda: self._fetch_klines(asset.binance, interval, start, end))

    def _fetch_klines(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]:
        url = f"{self.src.binance_url}/api/v3/klines"
        out: list[Candle] = []
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        for _ in range(20):
            data = self.http.get_json(
                url,
                {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
                source="binance",
            )
            if not isinstance(data, list):
                raise DataUnavailable("binance", "unexpected klines response")
            for row in data:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                values = [to_float(v) for v in row[1:5]]
                opened = parse_dt(int(row[0]))
                if opened is None or any(v is None for v in values):
                    continue
                out.append(Candle(opened, *values))  # type: ignore[arg-type]
            if len(data) < 1000:
                break
            cursor = int(data[-1][0]) + 1
        return out

    # ------------------------------------------------------------ volatility

    def realized_vol(self, asset: Asset, now: datetime | None = None) -> dict[str, float]:
        """Annualised realised volatility over the last 3 hours (1m candles)
        and the last 7 days (1h candles)."""
        now = now or utcnow()
        out: dict[str, float] = {}
        short = self.candles(asset, "1m", now - timedelta(hours=3), now)
        sigma = _annualised_sigma([c.close for c in short], minutes_per_step=1)
        if sigma is not None:
            out["realized_3h"] = sigma
        long = self.candles(asset, "1h", now - timedelta(days=7), now)
        sigma = _annualised_sigma([c.close for c in long], minutes_per_step=60)
        if sigma is not None:
            out["realized_7d"] = sigma
        if not out:
            raise DataUnavailable("binance", "not enough candles for realised volatility")
        return out

    def implied_vol(self, asset: Asset, now: datetime | None = None) -> float | None:
        """Deribit DVOL (annualised, decimal) for BTC/ETH; None for other assets."""
        if not asset.deribit:
            return None
        now = now or utcnow()

        def fetch() -> float:
            data = self.http.get_json(
                f"{self.src.deribit_url}/api/v2/public/get_volatility_index_data",
                {
                    "currency": asset.deribit,
                    "start_timestamp": int((now - timedelta(hours=3)).timestamp() * 1000),
                    "end_timestamp": int(now.timestamp() * 1000),
                    "resolution": 3600,
                },
                source="deribit",
            )
            rows = (data.get("result") or {}).get("data") if isinstance(data, dict) else None
            if not rows:
                raise DataUnavailable("deribit", "no DVOL data")
            close = to_float(rows[-1][4])
            if close is None:
                raise DataUnavailable("deribit", "bad DVOL row")
            return close / 100.0

        return self.cache.get_or_set(("dvol", asset.deribit), 300.0, fetch)

    def period_extremes(self, asset: Asset, start: datetime, now: datetime | None = None) -> tuple[float, float]:
        """Highest high and lowest low since ``start`` (Binance 1h candles,
        including the current partial hour)."""
        now = now or utcnow()
        candles = self.candles(asset, "1h", start - timedelta(hours=1), now)
        candles = [c for c in candles if c.open_time + timedelta(hours=1) > start]
        if not candles:
            raise DataUnavailable("binance", "no candles for the period")
        return max(c.high for c in candles), min(c.low for c in candles)

    def price_at(self, asset: Asset, when: datetime) -> Candle:
        """The Binance 1m candle that opens at ``when`` (window opening price)."""
        start = when.replace(second=0, microsecond=0)
        candles = self.candles(asset, "1m", start, start + timedelta(minutes=1))
        for candle in candles:
            if candle.open_time == start:
                return candle
        raise DataUnavailable("binance", f"no 1m candle at {start.isoformat()}")


def _annualised_sigma(closes: list[float], *, minutes_per_step: float) -> float | None:
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(returns) < 20:
        return None
    sd = statistics.pstdev(returns)
    return sd * math.sqrt(MINUTES_PER_YEAR / minutes_per_step)


def find_asset(text: str) -> Asset | None:
    lowered = text.lower()
    for asset in ASSETS.values():
        for name in asset.names:
            if len(name) <= 3:
                if _word_in(lowered, name):
                    return asset
            elif name in lowered:
                return asset
    return None


def _word_in(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text) is not None


__all__ = ["ASSETS", "Asset", "Candle", "CryptoData", "Quote", "find_asset"]
