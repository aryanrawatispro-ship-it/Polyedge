"""The verification suite runs end to end (mocked network, synthetic data)."""

import json
from datetime import timedelta

import fastapi.testclient  # noqa: F401  (import before httpx.Client is patched below)
import httpx

from favorite_hunter.config import Settings
from favorite_hunter.timeutil import iso, utcnow
from favorite_hunter.verify import run_verification

from .factories import clob_book, gamma_market, mirrored_no_book


def build_handler():
    now = utcnow()
    rules = "Resolves Yes if the Binance BTC/USDT 1 minute candle close at 12:00 ET is above the strike. Resolution source: Binance."
    open_markets = [
        gamma_market("1", question="Will the price of Bitcoin be above $60,000 on October 8?", best_bid=0.89, best_ask=0.90,
                     end=now + timedelta(hours=5), tags=["crypto"], description=rules, fees_enabled=False),
        gamma_market("2", question="Will the album debut at #1?", best_bid=0.93, best_ask=0.94, end=now + timedelta(days=1), fees_enabled=False),
    ]
    closed = gamma_market("9", closed=True, end=now - timedelta(days=1))
    closed["outcomePrices"] = '["1", "0"]'
    closed["umaResolutionStatus"] = "resolved"
    closed["closedTime"] = iso(now - timedelta(days=1))
    books = {}
    for mid, bid, ask in (("1", 0.89, 0.90), ("2", 0.93, 0.94)):
        yes = clob_book(mid + "1", bids=[(bid, 2000)], asks=[(ask, 2000)], timestamp=now)
        books[yes["asset_id"]] = yes
        no = mirrored_no_book(mid + "2", yes)
        no["timestamp"] = yes["timestamp"]
        books[no["asset_id"]] = no
    start = int((now - timedelta(days=7)).timestamp() * 1000)
    klines = [[start + i * 60_000, "64000", "64010", "63990", str(64000 * (1.0005 if i % 2 else 0.9995)), "1", 0] for i in range(400)]
    history = [{"timestamp": int((now - timedelta(days=1, hours=h)).timestamp()), "price": 0.9, "resolution_seconds": 300} for h in range(130, -1, -1)]

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        host = url.host
        if host == "gamma-api.polymarket.com" and path == "/markets/keyset":
            if url.params.get("closed") == "true":
                return httpx.Response(200, json={"markets": [closed], "next_cursor": None})
            return httpx.Response(200, json={"markets": open_markets, "next_cursor": None})
        if host == "clob.polymarket.com":
            if path == "/books":
                return httpx.Response(200, json=[books[i["token_id"]] for i in json.loads(request.content) if i["token_id"] in books])
            if path.startswith("/clob-markets/"):
                return httpx.Response(200, json={"fd": {"r": 0.0, "e": 1}, "t": []})
        if host == "data-api.polymarket.com" and path == "/v2/prices-history":
            return httpx.Response(200, json={"data": history, "pagination": {"has_more": False, "next_cursor": None}})
        if host == "data-api.binance.vision":
            if path == "/api/v3/ticker/price":
                return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "64000"})
            if path == "/api/v3/klines":
                return httpx.Response(200, json=klines)
        if host == "api.exchange.coinbase.com":
            return httpx.Response(200, json={"price": "63995"})
        if host == "api.kraken.com":
            return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["63998", "1"]}}})
        if host == "www.deribit.com":
            if path.endswith("get_volatility_index_data"):
                return httpx.Response(200, json={"result": {"data": [[0, 45, 45, 45, 45.0]]}})
            return httpx.Response(200, json={"result": {"index_price": 64000}})
        if host == "site.api.espn.com":
            return httpx.Response(200, json={"events": []})
        if host == "api.elections.kalshi.com":
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(404, text=f"not mocked: {url}")

    return handler


def test_verify_all_phases_complete_with_mocked_network(monkeypatch, capsys, tmp_path):
    handler = build_handler()
    real_client = httpx.Client

    def mocked_client(*args, **kwargs):
        # Leave clients that bring their own transport (the dashboard TestClient) alone.
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", mocked_client)
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    settings = Settings()
    settings.sources.manual_evidence_path = str(tmp_path / "none.yaml")
    code = run_verification(settings, phase="all", sample=2)
    out = capsys.readouterr().out
    assert "[FAIL]" not in out, out
    assert code == 0
    for n in range(1, 8):
        assert f"=== Phase {n}" in out
    assert "VWAP by hand" in out and "edge = estimated probability - (price + fee)" in out
    assert "PAPER TRADE ONLY." in out


def test_verify_reports_blocked_network(monkeypatch, capsys):
    real_client = httpx.Client

    def blocked(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ProxyError("403 Forbidden")))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", blocked)
    code = run_verification(Settings(), phase="all")
    out = capsys.readouterr().out
    assert code == 1 and "DATA UNAVAILABLE" in out and "blocked by network proxy" in out
    assert "=== Phase 2" not in out  # nothing downstream runs on missing data
