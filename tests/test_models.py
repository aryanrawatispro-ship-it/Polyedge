from datetime import UTC, datetime

import pytest

from favorite_hunter.models import parse_book, parse_market, resolved_outcome_index
from favorite_hunter.timeutil import humanize_hours, parse_dt, time_bucket

from .factories import NOW, clob_book, gamma_market


def test_parse_market_decodes_json_string_lists_and_numbers():
    raw = gamma_market("42", best_bid=0.89, best_ask=0.9, tags=["sports", "nba"])
    market = parse_market(raw)
    assert market is not None
    assert market.outcomes == ["Yes", "No"]
    assert market.token_ids == ["421", "422"]
    assert market.best_ask == 0.9 and market.best_bid == 0.89
    assert market.volume == 125000.5 and market.liquidity == 15000.0
    assert market.order_min_size == 5 and market.tick_size == 0.01
    assert market.end_date == NOW.replace(hour=15)
    assert "nba" in market.all_tags()
    assert market.url == "https://polymarket.com/event/event-42"


def test_parse_market_accepts_native_lists():
    raw = gamma_market("7")
    raw["outcomes"] = ["Up", "Down"]
    raw["clobTokenIds"] = ["a", "b"]
    market = parse_market(raw)
    assert market.outcomes == ["Up", "Down"] and market.token_ids == ["a", "b"]


def test_parse_market_rejects_non_binary_or_tokenless():
    raw = gamma_market("8")
    raw["outcomes"] = '["A", "B", "C"]'
    assert parse_market(raw) is None
    raw = gamma_market("9")
    raw["clobTokenIds"] = None
    assert parse_market(raw) is None


def test_fee_schedule_parsing():
    market = parse_market(gamma_market("1", fee_schedule={"exponent": 1, "rate": "0.05", "takerOnly": True, "rebateRate": "0.15"}))
    assert market.fee_schedule.rate == 0.05 and market.fee_schedule.exponent == 1
    assert market.fee_schedule.fee_per_share(0.9) == pytest.approx(0.0045)
    free = parse_market(gamma_market("2", fees_enabled=False))
    assert free.fee_schedule.rate == 0.0
    unknown = parse_market(gamma_market("3"))
    assert unknown.fee_schedule is None  # resolved later from the CLOB, never guessed here


def test_parse_book_sorts_best_first_and_parses_ms_timestamp():
    raw = clob_book("t1", bids=[(0.85, 10), (0.87, 5)], asks=[(0.9, 7), (0.88, 3)])
    # wire order: bids ascending, asks descending
    assert raw["asks"][0]["price"] == "0.9"
    book = parse_book(raw)
    assert [l.price for l in book.asks] == [0.88, 0.9]
    assert [l.price for l in book.bids] == [0.87, 0.85]
    assert book.best_ask == 0.88 and book.best_bid == 0.87
    assert book.spread == pytest.approx(0.01)
    assert book.server_time == NOW
    assert book.min_order_size == 5


def test_parse_book_drops_invalid_levels():
    raw = clob_book("t1", bids=[(0.5, 10)], asks=[(0.9, 7)])
    raw["asks"].append({"price": "1.5", "size": "3"})
    raw["asks"].append({"price": "0.95", "size": "0"})
    raw["asks"].append({"price": "abc", "size": "3"})
    book = parse_book(raw)
    assert [l.price for l in book.asks] == [0.9]


def test_resolved_outcome_index():
    assert resolved_outcome_index({"closed": True, "outcomePrices": '["1", "0"]'}) == 0
    assert resolved_outcome_index({"closed": True, "outcomePrices": '["0", "1"]'}) == 1
    assert resolved_outcome_index({"closed": True, "outcomePrices": '["0.5", "0.5"]'}) is None
    assert resolved_outcome_index({"closed": False, "outcomePrices": '["1", "0"]'}) is None


def test_parse_dt_formats():
    assert parse_dt("2026-10-07T12:00:00Z") == NOW
    assert parse_dt("2026-10-07T12:00:00.000Z") == NOW
    assert parse_dt("2026-10-07 12:00:00+00") == NOW
    assert parse_dt(str(int(NOW.timestamp() * 1000))) == NOW
    assert parse_dt(int(NOW.timestamp())) == NOW
    assert parse_dt("2026-10-07") == datetime(2026, 10, 7, tzinfo=UTC)
    assert parse_dt("") is None and parse_dt(None) is None and parse_dt("garbage") is None


@pytest.mark.parametrize(
    ("hours", "bucket"),
    [(0.5, "<1h"), (1.0, "1-6h"), (5.99, "1-6h"), (6, "6-24h"), (23.9, "6-24h"), (24, "1-3d"), (71.9, "1-3d"), (72, "3d+"), (-1, "past_end"), (None, "unknown")],
)
def test_time_buckets(hours, bucket):
    assert time_bucket(hours) == bucket


def test_humanize_hours():
    assert humanize_hours(0.7) == "42m"
    assert humanize_hours(3.5) == "3h 30m"
    assert humanize_hours(50) == "2d 2h"
    assert humanize_hours(-2) == "ended 2h 00m ago"
