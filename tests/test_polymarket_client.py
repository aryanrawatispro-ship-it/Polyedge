import json

import httpx
import pytest
import respx

from favorite_hunter.http import DataUnavailable, HttpClient
from favorite_hunter.polymarket_client import PolymarketClient

from .factories import clob_book, gamma_market

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"


def make_client():
    http = HttpClient(max_retries=1, rate_limits={"default": 0})
    return PolymarketClient(http, gamma_url=GAMMA, clob_url=CLOB, data_url=DATA)


@respx.mock
def test_keyset_pagination_follows_cursor():
    route = respx.get(f"{GAMMA}/markets/keyset").mock(
        side_effect=[
            httpx.Response(200, json={"markets": [gamma_market("1"), gamma_market("2")], "next_cursor": "c2"}),
            httpx.Response(200, json={"markets": [gamma_market("3")], "next_cursor": None}),
        ]
    )
    markets = list(make_client().iter_markets(closed=False, page_size=2))
    assert [m["id"] for m in markets] == ["1", "2", "3"]
    second = route.calls[1].request.url.params
    assert second["after_cursor"] == "c2" and second["limit"] == "2" and second["closed"] == "false"


@respx.mock
def test_falls_back_to_offset_paging_when_keyset_missing():
    respx.get(f"{GAMMA}/markets/keyset").mock(return_value=httpx.Response(404, text="not found"))
    respx.get(f"{GAMMA}/markets").mock(
        side_effect=[
            httpx.Response(200, json=[gamma_market("1"), gamma_market("2")]),
            httpx.Response(200, json=[gamma_market("3")]),
        ]
    )
    markets = list(make_client().iter_markets(page_size=2))
    assert [m["id"] for m in markets] == ["1", "2", "3"]


@respx.mock
def test_books_are_batched_via_post():
    def respond(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=[clob_book(item["token_id"], bids=[(0.5, 1)], asks=[(0.6, 1)]) for item in body])

    route = respx.post(f"{CLOB}/books").mock(side_effect=respond)
    books = make_client().get_books([f"t{i}" for i in range(5)], batch_size=2)
    assert len(route.calls) == 3
    assert set(books) == {f"t{i}" for i in range(5)}
    assert books["t0"].best_ask == 0.6


@respx.mock
def test_fee_schedule_from_clob_markets():
    respx.get(f"{CLOB}/clob-markets/0xabc").mock(return_value=httpx.Response(200, json={"fd": {"r": 0.05, "e": 1}, "t": []}))
    fee = make_client().get_fee_schedule("0xabc")
    assert fee.rate == 0.05 and fee.exponent == 1 and fee.source == "clob-markets.fd"


@respx.mock
def test_errors_become_data_unavailable():
    respx.get(f"{GAMMA}/markets/keyset").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(DataUnavailable) as err:
        list(make_client().iter_markets())
    assert "HTTP 500" in err.value.reason


@respx.mock
def test_price_history_v2_with_clob_fallback():
    respx.get(f"{DATA}/v2/prices-history").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"timestamp": 1788800700, "price": 0.95, "resolution_seconds": 60}, {"timestamp": 1788800640, "price": 0.955, "resolution_seconds": 60}],
                "pagination": {"has_more": False, "next_cursor": None},
            },
        )
    )
    from favorite_hunter.timeutil import parse_dt

    points = make_client().get_price_history("tok", start=parse_dt(1788800000), end=parse_dt(1788801000))
    assert [p for _, p in points] == [0.955, 0.95]  # sorted by time

    respx.get(f"{DATA}/v2/prices-history").mock(return_value=httpx.Response(404))
    respx.get(f"{CLOB}/prices-history").mock(return_value=httpx.Response(200, json={"history": [{"t": 1788800640, "p": 0.9}]}))
    points = make_client().get_price_history("tok", start=parse_dt(1788800000), end=parse_dt(1788801000))
    assert points[0][1] == 0.9
