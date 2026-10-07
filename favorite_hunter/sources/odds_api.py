"""Sportsbook odds from The Odds API (https://the-odds-api.com, key required).

Each bookmaker's moneyline is de-vigged (power method) and the median across
bookmakers is used; Pinnacle, the sharpest widely-quoted book, is reported
separately when present.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..http import DataUnavailable, HttpClient
from ..models import to_float
from ..probability.stats import devig_multiplicative, devig_power, overround
from ..timeutil import parse_dt
from . import TTLCache
from .espn import normalize_team

# Polymarket tag -> Odds API sport key
SPORT_KEYS: dict[str, str] = {
    "nba": "basketball_nba",
    "wnba": "basketball_wnba",
    "cbb": "basketball_ncaab",
    "ncaab": "basketball_ncaab",
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "ncaaf": "americanfootball_ncaaf",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "epl": "soccer_epl",
    "premier-league": "soccer_epl",
    "la-liga": "soccer_spain_la_liga",
    "serie-a": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue-1": "soccer_france_ligue_one",
    "mls": "soccer_usa_mls",
    "ucl": "soccer_uefa_champs_league",
    "champions-league": "soccer_uefa_champs_league",
    "uel": "soccer_uefa_europa_league",
    "ufc": "mma_mixed_martial_arts",
    "mma": "mma_mixed_martial_arts",
}


@dataclass
class BookPrice:
    bookmaker: str
    probabilities: dict[str, float]  # outcome name -> no-vig probability
    margin: float
    last_update: datetime | None


@dataclass
class OddsEvent:
    sport_key: str
    home_team: str
    away_team: str
    commence_time: datetime | None
    books: list[BookPrice]
    url: str

    def consensus(self, outcome: str) -> tuple[float | None, float | None, list[float]]:
        """(median no-vig probability, Pinnacle probability, all book probabilities)."""
        values = []
        pinnacle = None
        for book in self.books:
            p = book.probabilities.get(outcome)
            if p is None:
                continue
            values.append(p)
            if book.bookmaker.lower() == "pinnacle":
                pinnacle = p
        return (statistics.median(values) if values else None, pinnacle, values)


class OddsApiClient:
    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.base = settings.sources.odds_api_url.rstrip("/")
        self.api_key = settings.odds_api_key
        self.cache_seconds = settings.sources.odds_api_cache_seconds
        self.cache = TTLCache()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def events(self, league_tag: str) -> list[OddsEvent]:
        if not self.api_key:
            raise DataUnavailable("odds-api", "ODDS_API_KEY not set")
        sport_key = SPORT_KEYS.get(league_tag)
        if sport_key is None:
            raise DataUnavailable("odds-api", f"league '{league_tag}' not mapped to an Odds API sport")
        url = f"{self.base}/v4/sports/{sport_key}/odds"

        def fetch() -> list[OddsEvent]:
            data = self.http.get_json(
                url,
                {"apiKey": self.api_key, "regions": "us,eu", "markets": "h2h", "oddsFormat": "decimal"},
                source="odds-api",
            )
            if not isinstance(data, list):
                raise DataUnavailable("odds-api", "unexpected odds response")
            return [e for e in (_parse_event(item, sport_key, url) for item in data) if e is not None]

        # Small monthly quota on the free tier: cache per sport (configurable).
        return self.cache.get_or_set(("odds", sport_key), self.cache_seconds, fetch, error_ttl=300.0)

    def find_event(self, league_tag: str, team_a: str, team_b: str | None) -> OddsEvent | None:
        a = normalize_team(team_a)
        b = normalize_team(team_b) if team_b else None
        for event in self.events(league_tag):
            names = {normalize_team(event.home_team), normalize_team(event.away_team)}
            if _fuzzy_in(a, names) and (b is None or _fuzzy_in(b, names)):
                return event
        return None


def _fuzzy_in(name: str, names: set[str]) -> bool:
    for candidate in names:
        if name == candidate or (len(name) >= 4 and (name in candidate or candidate in name)):
            return True
    return False


def _parse_event(item: dict, sport_key: str, url: str) -> OddsEvent | None:
    if not isinstance(item, dict):
        return None
    books = []
    for book in item.get("bookmakers") or []:
        for market in book.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            outcomes = [(o.get("name"), to_float(o.get("price"))) for o in market.get("outcomes") or []]
            outcomes = [(n, p) for n, p in outcomes if n and p and p > 1.0]
            if len(outcomes) < 2:
                continue
            prices = [p for _, p in outcomes]
            try:
                probs = devig_power(prices)
            except (ValueError, ZeroDivisionError):
                probs = devig_multiplicative(prices)
            books.append(
                BookPrice(
                    bookmaker=book.get("title") or book.get("key") or "?",
                    probabilities={n: p for (n, _), p in zip(outcomes, probs)},
                    margin=overround(prices),
                    last_update=parse_dt(market.get("last_update") or book.get("last_update")),
                )
            )
    if not books:
        return None
    return OddsEvent(
        sport_key=sport_key,
        home_team=item.get("home_team") or "",
        away_team=item.get("away_team") or "",
        commence_time=parse_dt(item.get("commence_time")),
        books=books,
        url=url,
    )
