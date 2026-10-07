from datetime import timedelta

import pytest

from favorite_hunter.config import Settings
from favorite_hunter.http import DataUnavailable
from favorite_hunter.market_scanner import MarketScanner, estimate_resolution_time
from favorite_hunter.models import FeeSchedule, parse_book, parse_market

from .factories import NOW, clob_book, gamma_market, mirrored_no_book


class FakeClient:
    def __init__(self, markets, books, fee=None, fail=False):
        self.markets = markets
        self.books = books
        self.fee = fee
        self.fail = fail
        self.fee_calls = 0
        self.requested_tokens = []

    def iter_markets(self, **kwargs):
        if self.fail:
            raise DataUnavailable("gamma", "blocked by network proxy (403 Forbidden)")
        yield from self.markets

    def get_books(self, token_ids, batch_size=50):
        self.requested_tokens = list(token_ids)
        return {t: parse_book(self.books[t]) for t in token_ids if t in self.books}

    def get_fee_schedule(self, condition_id):
        self.fee_calls += 1
        return self.fee


def make_settings(**scanner):
    settings = Settings()
    for key, value in scanner.items():
        setattr(settings.scanner, key, value)
    return settings


def test_finds_yes_and_no_favorites_and_ignores_out_of_band():
    fee = {"exponent": 1, "rate": "0.05", "takerOnly": True, "rebateRate": "0"}
    yes_fav = gamma_market("1", best_bid=0.89, best_ask=0.90, fee_schedule=fee)
    yes_book = clob_book("11", bids=[(0.89, 500)], asks=[(0.90, 400), (0.91, 1000)])
    # YES trades at 0.08/0.09 -> NO ask = 1 - 0.08 = 0.92
    no_fav = gamma_market("2", best_bid=0.08, best_ask=0.09, fee_schedule=fee)
    no_yes_book = clob_book("21", bids=[(0.08, 600)], asks=[(0.09, 300)])
    cheap = gamma_market("3", best_bid=0.55, best_ask=0.56, fee_schedule=fee)
    near_certain = gamma_market("4", best_bid=0.99, best_ask=0.995, fee_schedule=fee)
    books = {
        "11": yes_book,
        "12": mirrored_no_book("12", yes_book),
        "21": no_yes_book,
        "22": mirrored_no_book("22", no_yes_book),
        "31": clob_book("31", bids=[(0.55, 10)], asks=[(0.56, 10)]),
        "41": clob_book("41", bids=[(0.99, 10)], asks=[(0.995, 10)]),
    }
    client = FakeClient([yes_fav, no_fav, cheap, near_certain], books)
    result = MarketScanner(client, make_settings()).scan(now=NOW)

    assert result.data_available
    assert result.markets_fetched == 4 and result.tradable_markets == 4
    keys = {c.key: c for c in result.candidates}
    assert set(keys) == {"1:0", "2:1"}
    # Gamma's cached quotes keep the 0.56 and 0.995 markets out of the CLOB requests.
    assert "31" not in client.requested_tokens and "41" not in client.requested_tokens

    yes = keys["1:0"]
    assert yes.outcome == "Yes" and yes.best_ask == 0.90
    assert yes.entry_price == pytest.approx(0.90)  # $100 fits in the 0.90 level
    assert yes.fee_per_share == pytest.approx(0.0045)
    assert yes.break_even == pytest.approx(0.9045)
    assert yes.ask_depth_usd == pytest.approx(0.90 * 400 + 0.91 * 1000)
    assert yes.time_bucket == "1-6h"
    assert yes.estimate is None and yes.status == "DATA UNAVAILABLE"

    no = keys["2:1"]
    assert no.outcome == "No" and no.best_ask == pytest.approx(0.92)
    assert no.best_bid == pytest.approx(0.91)


def test_vwap_outside_band_is_excluded_even_if_best_ask_inside():
    market = gamma_market("5", best_bid=0.97, best_ask=0.98, fees_enabled=False)
    # Only 10 shares at 0.98, then 0.99: VWAP for $100 is above 0.98.
    book = clob_book("51", bids=[(0.97, 10)], asks=[(0.98, 10), (0.99, 5000)])
    client = FakeClient([market], {"51": book, "52": mirrored_no_book("52", book)})
    result = MarketScanner(client, make_settings()).scan(now=NOW)
    assert result.candidates == []


def test_closed_and_not_accepting_markets_are_skipped():
    closed = gamma_market("6", closed=True)
    paused = gamma_market("7", accepting_orders=False)
    book = clob_book("61", bids=[(0.89, 10)], asks=[(0.9, 1000)])
    client = FakeClient([closed, paused], {"61": book, "71": book})
    result = MarketScanner(client, make_settings()).scan(now=NOW)
    assert result.tradable_markets == 0 and result.candidates == []


def test_below_min_order_size_is_not_executable():
    market = gamma_market("8", best_bid=0.89, best_ask=0.9, fees_enabled=False)
    book = clob_book("81", bids=[(0.89, 10)], asks=[(0.9, 3)])  # min order 5 shares
    client = FakeClient([market], {"81": book})
    result = MarketScanner(client, make_settings()).scan(now=NOW)
    assert result.candidates == []


def test_unknown_fee_uses_clob_then_worst_case():
    market = gamma_market("9", best_bid=0.89, best_ask=0.9)
    book = clob_book("91", bids=[(0.89, 10)], asks=[(0.9, 1000)])
    client = FakeClient([market], {"91": book}, fee=FeeSchedule(rate=0.04, exponent=1, source="clob-markets.fd"))
    result = MarketScanner(client, make_settings()).scan(now=NOW)
    assert result.candidates[0].fee.rate == 0.04 and client.fee_calls == 1

    client = FakeClient([market], {"91": book}, fee=None)
    result = MarketScanner(client, make_settings()).scan(now=NOW)
    fee = result.candidates[0].fee
    assert fee.rate == Settings().fees.unknown_fee_rate and fee.known is False


def test_data_unavailable_is_reported_not_fabricated():
    result = MarketScanner(FakeClient([], {}, fail=True), make_settings()).scan(now=NOW)
    assert not result.data_available
    assert result.candidates == []
    assert "blocked by network proxy" in result.errors[0]


def test_band_is_configurable():
    market = gamma_market("10", best_bid=0.84, best_ask=0.85, fees_enabled=False)
    book = clob_book("101", bids=[(0.84, 10)], asks=[(0.85, 1000)])
    client = FakeClient([market], {"101": book})
    assert len(MarketScanner(client, make_settings()).scan(now=NOW).candidates) == 1
    narrow = make_settings(price_min=0.90, price_max=0.97)
    assert MarketScanner(client, narrow).scan(now=NOW).candidates == []


def test_sports_resolution_time_uses_game_start():
    raw = gamma_market("11", tags=["sports", "nba"], extra={"gameStartTime": "2026-10-07T23:00:00Z"})
    market = parse_market(raw)
    when, source = estimate_resolution_time(market)
    assert when == NOW.replace(hour=23) + timedelta(hours=2.5)
    assert "gameStartTime" in source
    plain = parse_market(gamma_market("12"))
    assert estimate_resolution_time(plain) == (plain.end_date, "market endDate")
