import math
from datetime import timedelta

import pytest

from favorite_hunter.config import Settings
from favorite_hunter.http import DataUnavailable
from favorite_hunter.market_scanner import MarketScanner
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.probability.base import DATA_LAG, NEAR_RESOLUTION_EDGE, THRESHOLD_EDGE
from favorite_hunter.probability.crypto_engine import CryptoEngine, parse_crypto_market
from favorite_hunter.probability.stats import norm_cdf, scaled_t_cdf
from favorite_hunter.sources.crypto import ASSETS, Candle, Quote

from .factories import NOW, clob_book, gamma_market
from .fakes import FakeClient

BINANCE_RULES = "Resolves using the Binance BTC/USDT 1 minute candle close at 12:00 PM ET. Resolution source: Binance."


def crypto_candidate(question, *, outcomes=("Yes", "No"), index=0, end=None, group=None, description=BINANCE_RULES, start=None, ask=0.90, extra=None):
    raw = gamma_market("77", question=question, outcomes=outcomes, end=end or NOW + timedelta(days=1), tags=["crypto"],
                       description=description, fees_enabled=False, best_bid=ask - 0.01, best_ask=ask, extra=extra)
    if group:
        raw["groupItemTitle"] = group
    if start:
        raw["startDate"] = start.isoformat().replace("+00:00", "Z")
    market = parse_market(raw)
    book = parse_book(clob_book(market.token_ids[index], bids=[(ask - 0.01, 500)], asks=[(ask, 500)]))
    c = MarketScanner(FakeClient([], {}), Settings()).build_candidate(market, index, book, NOW)
    assert c is not None
    return c


class FakeCrypto:
    def __init__(self, spot=100.0, quotes=None, vols=None, implied=0.5, opening=None, extremes=None, fail_spot=False):
        self.spot = spot
        self.quotes = quotes
        self.vols = vols or {"realized_3h": 0.5}
        self.implied = implied
        self.opening = opening
        self.extremes = extremes
        self.fail_spot = fail_spot

    def spot_quotes(self, asset):
        if self.fail_spot:
            return [], ["Binance: blocked by network proxy"]
        quotes = self.quotes or [self.spot]
        return [Quote(f"{name} {asset.symbol}", price, NOW, "https://example") for name, price in zip(["Binance", "Coinbase", "Kraken"], quotes)], []

    def realized_vol(self, asset, now):
        return dict(self.vols)

    def implied_vol(self, asset, now):
        return None

    def price_at(self, asset, when):
        if self.opening is None:
            raise DataUnavailable("binance", "no candle")
        return Candle(when, self.opening, self.opening, self.opening, self.opening)

    def period_extremes(self, asset, start, now):
        return self.extremes


@pytest.mark.parametrize(
    ("question", "group", "kind", "strike", "high"),
    [
        ("Will the price of Bitcoin be above $110,000 on October 8?", None, "above", 110_000, None),
        ("Bitcoin above 110k on October 8?", None, "above", 110_000, None),
        ("Will Ethereum be less than $3,800 on October 8?", None, "below", 3_800, None),
        ("Will the price of Solana be between $180 and $200 on October 8?", None, "range", 180, 200),
        ("Ethereum price on October 8?", "4,000-4,200", "range", 4_000, 4_200),
        ("Will Bitcoin reach $150,000 in October?", None, "touch_up", 150_000, None),
        ("Will Ethereum dip to $3,000 in October?", None, "touch_down", 3_000, None),
        ("What price will Bitcoin hit in October?", "↑ 130,000", "touch_up", 130_000, None),
    ],
)
def test_parse_crypto_questions(question, group, kind, strike, high):
    spec = parse_crypto_market(crypto_candidate(question, group=group))
    assert spec is not None and spec.kind == kind
    assert spec.strike == pytest.approx(strike)
    assert spec.strike_high == (pytest.approx(high) if high else None)
    assert spec.resolution_source == "binance"
    if kind.startswith("touch"):
        assert spec.window_start == NOW.replace(day=1, hour=0)


def test_parse_up_down_windows():
    end = NOW + timedelta(minutes=40)
    hourly = parse_crypto_market(crypto_candidate("Bitcoin Up or Down - October 7, 12PM ET", outcomes=("Up", "Down"), end=end))
    assert hourly.kind == "up_down" and hourly.window_start == end - timedelta(hours=1) and hourly.event_index == 0
    quarter = parse_crypto_market(crypto_candidate("Bitcoin Up or Down - October 7, 12:30PM-12:45PM ET", outcomes=("Up", "Down"), end=end))
    assert quarter.window_start == end - timedelta(minutes=15)


def test_non_price_questions_are_not_parsed():
    assert parse_crypto_market(crypto_candidate("Will Bitcoin ETF inflows exceed $1,000,000,000 this week?")) is None
    assert parse_crypto_market(crypto_candidate("Will MicroStrategy buy Bitcoin above $100,000?")) is None


def test_above_estimate_hand_checked_and_conservative():
    # spot 100, strike 95, sigma 50%/yr, 1 day left
    c = crypto_candidate("Will the price of Bitcoin be above $95 on October 8?", end=NOW + timedelta(days=1))
    est = CryptoEngine(FakeCrypto(spot=100.0, vols={"realized_3h": 0.5})).estimate(c, NOW)
    assert est.available and est.opportunity_type == THRESHOLD_EDGE
    sd = 0.5 * math.sqrt(1 / 365.25)
    # basis buffer 0.02% (Binance rules + Binance price): lowest of spot*(1 +/- 0.0002)
    worst_spot = 100 * (1 - 0.0002)
    x = math.log(95 / worst_spot) + 0.5 * sd * sd
    expected = min(1 - scaled_t_cdf(x, sd, 4), 1 - norm_cdf(x / sd))
    assert est.probability == pytest.approx(expected, abs=1e-9)
    assert est.probability == pytest.approx(0.9741, abs=5e-4)
    # The NO side is evaluated conservatively too: the two never sum above 1.
    no = crypto_candidate("Will the price of Bitcoin be above $95 on October 8?", index=1, end=NOW + timedelta(days=1))
    est_no = CryptoEngine(FakeCrypto(spot=100.0, vols={"realized_3h": 0.5})).estimate(no, NOW)
    assert est.probability + est_no.probability <= 1.0
    assert any("scenario" in line for line in est.calculation)


def test_higher_vol_scenario_wins_for_the_favorite():
    c = crypto_candidate("Will the price of Bitcoin be above $95 on October 8?")
    low = CryptoEngine(FakeCrypto(vols={"realized_3h": 0.3})).estimate(c, NOW).probability
    both = CryptoEngine(FakeCrypto(vols={"realized_3h": 0.3, "realized_7d": 0.8})).estimate(c, NOW).probability
    assert both < low


def test_up_down_uses_window_open_and_near_resolution_label():
    end = NOW + timedelta(minutes=30)
    c = crypto_candidate("Bitcoin Up or Down - October 7, 12PM ET", outcomes=("Up", "Down"), end=end)
    est = CryptoEngine(FakeCrypto(spot=101.0, opening=100.0, vols={"realized_3h": 0.5})).estimate(c, NOW)
    assert est.available and est.opportunity_type == NEAR_RESOLUTION_EDGE
    assert est.probability > 0.97  # +1% with 30 minutes left at 50% vol
    assert "window open 100.0000" in est.evidence[-1].description


def test_touch_already_happened_is_data_lag():
    c = crypto_candidate("Will Bitcoin reach $105 in October?", end=NOW + timedelta(days=20))
    est = CryptoEngine(FakeCrypto(spot=101.0, extremes=(106.0, 90.0))).estimate(c, NOW)
    assert est.probability == 1.0 and est.opportunity_type == DATA_LAG


def test_touch_not_yet_uses_reflection():
    c = crypto_candidate("Will Bitcoin reach $150 in October?", index=1, ask=0.95, end=NOW + timedelta(days=20))
    est = CryptoEngine(FakeCrypto(spot=100.0, extremes=(110.0, 90.0), vols={"realized_3h": 0.6})).estimate(c, NOW)
    sd = 0.6 * math.sqrt(20 / 365.25)
    p_touch_normal = 2 * (1 - norm_cdf(math.log(150 / 100.1) / sd))  # spot nudged up 0.1% (non-Binance buffer)
    assert est.probability <= 1 - p_touch_normal + 1e-9


def test_missing_or_conflicting_data_is_never_filled():
    c = crypto_candidate("Will the price of Bitcoin be above $95 on October 8?")
    est = CryptoEngine(FakeCrypto(fail_spot=True)).estimate(c, NOW)
    assert est.probability is None and est.data_status == "DATA UNAVAILABLE" and "blocked" in est.reason
    conflict = CryptoEngine(FakeCrypto(quotes=[100.0, 101.5])).estimate(c, NOW)
    assert conflict.probability is None and conflict.data_status == "CONFLICTING SOURCES"
    implausible = crypto_candidate("Will the price of Bitcoin be above $1,000,000 on October 8?")
    est = CryptoEngine(FakeCrypto(spot=100_000.0)).estimate(implausible, NOW)
    assert est.probability is None and "implausible" in est.reason


def test_asset_lookup():
    from favorite_hunter.sources.crypto import find_asset

    assert find_asset("Will Bitcoin hit 150k?") is ASSETS["BTC"]
    assert find_asset("ETH above 4000") is ASSETS["ETH"]
    assert find_asset("Solana price") is ASSETS["SOL"]
    assert find_asset("Will the console sell out?") is None
