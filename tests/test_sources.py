"""Source clients parse their providers' public response formats (mocked HTTP)."""

from datetime import timedelta

import httpx
import pytest
import respx

from favorite_hunter.config import Settings
from favorite_hunter.http import DataUnavailable, HttpClient
from favorite_hunter.sources.crypto import ASSETS, CryptoData
from favorite_hunter.sources.espn import EspnClient, normalize_team
from favorite_hunter.sources.kalshi import KalshiClient
from favorite_hunter.sources.odds_api import OddsApiClient

from .factories import NOW

S = Settings()


def http():
    return HttpClient(max_retries=0, rate_limits={"default": 0})


@respx.mock
def test_crypto_spot_quotes_from_three_exchanges():
    respx.get(f"{S.sources.binance_url}/api/v3/ticker/price").mock(return_value=httpx.Response(200, json={"symbol": "BTCUSDT", "price": "62010.50000000"}))
    respx.get(f"{S.sources.coinbase_url}/products/BTC-USD/ticker").mock(return_value=httpx.Response(200, json={"price": "62005.12", "time": "2026-10-07T12:00:00.000Z"}))
    respx.get(f"{S.sources.kraken_url}/0/public/Ticker").mock(return_value=httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["62001.0", "0.01"]}}}))
    quotes, errors = CryptoData(http(), S).spot_quotes(ASSETS["BTC"])
    assert errors == []
    assert [q.price for q in quotes] == [62010.5, 62005.12, 62001.0]
    assert quotes[1].observed_at == NOW


@respx.mock
def test_crypto_partial_outage_is_reported():
    respx.get(f"{S.sources.binance_url}/api/v3/ticker/price").mock(return_value=httpx.Response(451, text="restricted location"))
    respx.get(f"{S.sources.coinbase_url}/products/ETH-USD/ticker").mock(return_value=httpx.Response(200, json={"price": "4000.1"}))
    respx.get(f"{S.sources.kraken_url}/0/public/Ticker").mock(return_value=httpx.Response(200, json={"error": ["EService:Unavailable"], "result": {}}))
    quotes, errors = CryptoData(http(), S).spot_quotes(ASSETS["ETH"])
    assert [q.source for q in quotes] == ["Coinbase ETH-USD"]
    assert any("HTTP 451" in e for e in errors) and any("Kraken" in e for e in errors)


@respx.mock
def test_klines_vol_and_dvol():
    start_ms = int((NOW - timedelta(hours=3)).timestamp() * 1000)
    rows = []
    price = 100.0
    for i in range(180):
        price *= 1.001 if i % 2 else 0.999
        rows.append([start_ms + i * 60_000, f"{price}", f"{price * 1.001}", f"{price * 0.999}", f"{price}", "1.0", start_ms + i * 60_000 + 59_999])
    respx.get(f"{S.sources.binance_url}/api/v3/klines").mock(return_value=httpx.Response(200, json=rows))
    respx.get(f"{S.sources.deribit_url}/api/v2/public/get_volatility_index_data").mock(
        return_value=httpx.Response(200, json={"result": {"data": [[1, 50.1, 52.0, 49.0, 51.3]], "continuation": None}})
    )
    data = CryptoData(http(), S)
    vols = data.realized_vol(ASSETS["BTC"], NOW)
    # alternating +/-0.1% one-minute moves -> ~0.1% per-minute sd, annualised
    assert vols["realized_3h"] == pytest.approx(0.001 * (365.25 * 24 * 60) ** 0.5, rel=0.02)
    assert data.implied_vol(ASSETS["BTC"], NOW) == pytest.approx(0.513)
    assert data.implied_vol(ASSETS["SOL"], NOW) is None


ESPN_NBA = {
    "events": [
        {
            "id": "401700001",
            "date": "2026-10-07T23:30Z",
            "name": "Boston Celtics at Los Angeles Lakers",
            "competitions": [
                {
                    "date": "2026-10-07T23:30Z",
                    "competitors": [
                        {"homeAway": "home", "score": "98", "team": {"id": "13", "displayName": "Los Angeles Lakers", "shortDisplayName": "Lakers", "abbreviation": "LAL", "name": "Lakers", "location": "Los Angeles"}},
                        {"homeAway": "away", "score": "91", "team": {"id": "2", "displayName": "Boston Celtics", "shortDisplayName": "Celtics", "abbreviation": "BOS", "name": "Celtics", "location": "Boston"}},
                    ],
                    "status": {"clock": 245.0, "displayClock": "4:05", "period": 4, "type": {"state": "in", "completed": False, "detail": "4:05 - 4th Quarter"}},
                    "odds": [{"provider": {"name": "ESPN BET"}, "details": "LAL -3.5", "spread": -3.5, "overUnder": 225.5,
                              "homeTeamOdds": {"moneyLine": -160, "favorite": True}, "awayTeamOdds": {"moneyLine": 135, "favorite": False}}],
                }
            ],
        }
    ]
}


@respx.mock
def test_espn_scoreboard_parsing_and_matching():
    respx.get(f"{S.sources.espn_url}/apis/site/v2/sports/basketball/nba/scoreboard").mock(return_value=httpx.Response(200, json=ESPN_NBA))
    espn = EspnClient(http(), S)
    game, note = espn.find_game("nba", "Lakers", "Celtics", NOW.replace(hour=23, minute=30))
    assert game is not None, note
    assert (game.home.score, game.away.score, game.period, game.clock_seconds) == (98, 91, 4, 245.0)
    assert game.state == "in" and game.odds["spread"] == -3.5 and game.odds["home_favorite"] is True
    team, score = game.team_for("Los Angeles Lakers")
    assert team is game.home and score == 1.0
    wrong, note = espn.find_game("nba", "Knicks", "Heat", NOW.replace(hour=23, minute=30))
    assert wrong is None


@respx.mock
def test_espn_soccer_red_cards():
    payload = {"events": [{"id": "9", "date": "2026-10-07T14:00Z", "name": "Chelsea at Arsenal", "competitions": [{
        "date": "2026-10-07T14:00Z",
        "competitors": [
            {"homeAway": "home", "score": "2", "team": {"id": "359", "displayName": "Arsenal"}},
            {"homeAway": "away", "score": "0", "team": {"id": "363", "displayName": "Chelsea"}},
        ],
        "status": {"clock": 4680.0, "displayClock": "78'", "period": 2, "type": {"state": "in", "completed": False, "detail": "78'"}},
        "details": [{"type": {"text": "Red Card"}, "team": {"id": "363"}}, {"type": {"text": "Goal"}, "team": {"id": "359"}}],
    }]}]}
    respx.get(f"{S.sources.espn_url}/apis/site/v2/sports/soccer/eng.1/scoreboard").mock(return_value=httpx.Response(200, json=payload))
    game, _ = EspnClient(http(), S).find_game("epl", "Arsenal", "Chelsea", NOW.replace(hour=14))
    assert (game.home.red_cards, game.away.red_cards) == (0, 1)


def test_unmapped_league_is_unavailable():
    with pytest.raises(DataUnavailable):
        EspnClient(http(), S).scoreboard("curling", NOW)
    assert normalize_team("Arsenal FC") == "arsenal"
    assert normalize_team("Atlético de Madrid") == "atletico madrid"


@respx.mock
def test_odds_api_consensus(monkeypatch):
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    payload = [{
        "id": "e1", "sport_key": "basketball_nba", "commence_time": "2026-10-07T23:30:00Z",
        "home_team": "Los Angeles Lakers", "away_team": "Boston Celtics",
        "bookmakers": [
            {"key": "pinnacle", "title": "Pinnacle", "last_update": "2026-10-07T12:00:00Z", "markets": [{"key": "h2h", "outcomes": [{"name": "Los Angeles Lakers", "price": 1.6}, {"name": "Boston Celtics", "price": 2.45}]}]},
            {"key": "draftkings", "title": "DraftKings", "markets": [{"key": "h2h", "last_update": "2026-10-07T12:00:00Z", "outcomes": [{"name": "Los Angeles Lakers", "price": 1.57}, {"name": "Boston Celtics", "price": 2.4}]}]},
        ],
    }]
    route = respx.get(f"{S.sources.odds_api_url}/v4/sports/basketball_nba/odds").mock(return_value=httpx.Response(200, json=payload))
    client = OddsApiClient(http(), Settings())
    event = client.find_event("nba", "Lakers", "Celtics")
    assert route.calls[0].request.url.params["apiKey"] == "test-key"
    median, pinnacle, values = event.consensus("Los Angeles Lakers")
    assert len(values) == 2 and pinnacle == pytest.approx(values[0])
    assert 0.6 < pinnacle < 0.62 and sum(event.books[0].probabilities.values()) == pytest.approx(1.0)


def test_odds_api_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    client = OddsApiClient(http(), Settings())
    assert not client.enabled
    with pytest.raises(DataUnavailable):
        client.events("nba")


@respx.mock
def test_kalshi_dollar_and_cent_fields():
    respx.get(f"{S.sources.kalshi_url}/markets/KX-1").mock(return_value=httpx.Response(200, json={"market": {"ticker": "KX-1", "title": "T", "yes_bid_dollars": "0.9500", "yes_ask_dollars": "0.9700", "status": "active"}}))
    respx.get(f"{S.sources.kalshi_url}/markets/KX-2").mock(return_value=httpx.Response(200, json={"market": {"ticker": "KX-2", "title": "T", "yes_bid": 91, "yes_ask": 93, "status": "active"}}))
    kalshi = KalshiClient(http(), S)
    assert kalshi.market("KX-1").yes_mid == pytest.approx(0.96)
    assert kalshi.market("KX-2").yes_mid == pytest.approx(0.92)
