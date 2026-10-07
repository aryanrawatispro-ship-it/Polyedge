"""ESPN public scoreboard: live score, clock, period, status, red cards and the
pregame line ESPN shows. No API key; unofficial but widely used."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from ..config import Settings
from ..http import DataUnavailable, HttpClient
from ..models import to_float
from ..timeutil import parse_dt, utcnow
from . import TTLCache

# Polymarket tag -> (ESPN sport, ESPN league, model sport key)
LEAGUES: dict[str, tuple[str, str, str]] = {
    "nba": ("basketball", "nba", "nba"),
    "wnba": ("basketball", "wnba", "wnba"),
    "cbb": ("basketball", "mens-college-basketball", "ncaab"),
    "ncaab": ("basketball", "mens-college-basketball", "ncaab"),
    "college-basketball": ("basketball", "mens-college-basketball", "ncaab"),
    "nfl": ("football", "nfl", "nfl"),
    "cfb": ("football", "college-football", "cfb"),
    "ncaaf": ("football", "college-football", "cfb"),
    "college-football": ("football", "college-football", "cfb"),
    "mlb": ("baseball", "mlb", "mlb"),
    "nhl": ("hockey", "nhl", "nhl"),
    "epl": ("soccer", "eng.1", "soccer"),
    "premier-league": ("soccer", "eng.1", "soccer"),
    "la-liga": ("soccer", "esp.1", "soccer"),
    "serie-a": ("soccer", "ita.1", "soccer"),
    "bundesliga": ("soccer", "ger.1", "soccer"),
    "ligue-1": ("soccer", "fra.1", "soccer"),
    "mls": ("soccer", "usa.1", "soccer"),
    "ucl": ("soccer", "uefa.champions", "soccer"),
    "champions-league": ("soccer", "uefa.champions", "soccer"),
    "uel": ("soccer", "uefa.europa", "soccer"),
    "europa-league": ("soccer", "uefa.europa", "soccer"),
    "eredivisie": ("soccer", "ned.1", "soccer"),
    "liga-mx": ("soccer", "mex.1", "soccer"),
}

_STRIP_WORDS = {"fc", "cf", "afc", "sc", "ac", "club", "the", "de", "cd", "ssc", "as", "bc"}


def normalize_team(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    tokens = [t for t in text.split() if t not in _STRIP_WORDS]
    return " ".join(tokens)


@dataclass
class TeamState:
    names: list[str]  # display, short, abbreviation, nickname, location + nickname
    score: int | None
    home_away: str
    red_cards: int | None = None

    @property
    def display(self) -> str:
        return self.names[0] if self.names else "?"

    def match_score(self, label: str) -> float:
        target = normalize_team(label)
        if not target:
            return 0.0
        best = 0.0
        for name in self.names:
            candidate = normalize_team(name)
            if not candidate:
                continue
            if candidate == target:
                return 1.0
            ratio = SequenceMatcher(None, candidate, target).ratio()
            short, long_ = sorted((candidate.split(), target.split()), key=len)
            if short and all(tok in long_ for tok in short) and len(" ".join(short)) >= 4:
                ratio = max(ratio, 0.9)
            best = max(best, ratio)
        return best


@dataclass
class GameState:
    source: str
    sport: str  # model key: nba, nfl, soccer, ...
    league: str
    event_id: str
    name: str
    home: TeamState
    away: TeamState
    state: str  # pre / in / post
    completed: bool
    period: int
    clock_seconds: float | None
    display_clock: str
    detail: str
    start_time: datetime | None
    fetched_at: datetime
    url: str
    odds: dict[str, Any] | None = None
    raw_status: dict[str, Any] = field(default_factory=dict)

    def team_for(self, label: str) -> tuple[TeamState | None, float]:
        home_score, away_score = self.home.match_score(label), self.away.match_score(label)
        if home_score >= away_score:
            return self.home, home_score
        return self.away, away_score


class EspnClient:
    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.base = settings.sources.espn_url.rstrip("/")
        self.cache = TTLCache()

    def scoreboard(self, league_tag: str, day: datetime) -> list[GameState]:
        if league_tag not in LEAGUES:
            raise DataUnavailable("espn", f"league '{league_tag}' not mapped to ESPN")
        sport, league, model_sport = LEAGUES[league_tag]
        date_str = day.strftime("%Y%m%d")
        url = f"{self.base}/apis/site/v2/sports/{sport}/{league}/scoreboard"

        def fetch() -> list[GameState]:
            data = self.http.get_json(url, {"dates": date_str}, source="espn")
            if not isinstance(data, dict):
                raise DataUnavailable("espn", "unexpected scoreboard response")
            fetched = utcnow()
            games = []
            for event in data.get("events") or []:
                game = _parse_event(event, model_sport, league, f"{url}?dates={date_str}", fetched)
                if game is not None:
                    games.append(game)
            return games

        return self.cache.get_or_set(("scoreboard", sport, league, date_str), 30.0, fetch)

    def find_game(self, league_tag: str, team_a: str, team_b: str | None, start: datetime | None) -> tuple[GameState | None, str]:
        """Find the game featuring both teams near ``start``. Returns (game, note)."""
        reference = start or utcnow()
        days = {reference.date(), (reference - timedelta(hours=12)).date(), (reference + timedelta(hours=12)).date()}
        best: tuple[float, GameState] | None = None
        errors = []
        for day in sorted(days):
            try:
                games = self.scoreboard(league_tag, datetime(day.year, day.month, day.day))
            except DataUnavailable as exc:
                errors.append(exc.reason)
                continue
            for game in games:
                _, score_a = game.team_for(team_a)
                score_b = 1.0
                if team_b:
                    _, score_b = game.team_for(team_b)
                    team_obj_a, _ = game.team_for(team_a)
                    team_obj_b, _ = game.team_for(team_b)
                    if team_obj_a is team_obj_b:
                        continue
                if start and game.start_time and abs((game.start_time - start).total_seconds()) > 6 * 3600:
                    continue
                score = min(score_a, score_b)
                if best is None or score > best[0]:
                    best = (score, game)
        if best is None:
            reason = "; ".join(dict.fromkeys(errors)) if errors else "no ESPN game matched both teams and the start time"
            return None, reason
        if best[0] < 0.8:
            return None, f"closest ESPN game '{best[1].name}' matched only {best[0]:.2f}"
        return best[1], f"matched ESPN game '{best[1].name}' (score {best[0]:.2f})"


def _team(competitor: dict[str, Any]) -> TeamState:
    team = competitor.get("team") or {}
    names = [
        team.get("displayName"),
        team.get("shortDisplayName"),
        team.get("name"),
        team.get("abbreviation"),
        " ".join(filter(None, [team.get("location"), team.get("name")])),
        team.get("location"),
    ]
    score = to_float(competitor.get("score"))
    return TeamState(
        names=[n for n in dict.fromkeys(names) if n],
        score=int(score) if score is not None else None,
        home_away=competitor.get("homeAway") or "",
    )


def _parse_event(event: dict[str, Any], model_sport: str, league: str, url: str, fetched: datetime) -> GameState | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    comp = competitions[0]
    competitors = comp.get("competitors") or []
    home = next((_team(c) for c in competitors if c.get("homeAway") == "home"), None)
    away = next((_team(c) for c in competitors if c.get("homeAway") == "away"), None)
    if home is None or away is None:
        return None
    status = comp.get("status") or event.get("status") or {}
    stype = status.get("type") or {}
    if model_sport == "soccer":
        reds = _red_cards(comp, competitors)
        if reds is not None:
            home.red_cards, away.red_cards = reds
    return GameState(
        source="ESPN",
        sport=model_sport,
        league=league,
        event_id=str(event.get("id")),
        name=event.get("name") or f"{away.display} at {home.display}",
        home=home,
        away=away,
        state=stype.get("state") or "",
        completed=bool(stype.get("completed")),
        period=int(to_float(status.get("period")) or 0),
        clock_seconds=to_float(status.get("clock")),
        display_clock=str(status.get("displayClock") or ""),
        detail=str(stype.get("detail") or stype.get("shortDetail") or stype.get("description") or ""),
        start_time=parse_dt(comp.get("date") or event.get("date")),
        fetched_at=fetched,
        url=url,
        odds=_odds(comp),
        raw_status=status,
    )


def _red_cards(comp: dict[str, Any], competitors: list[dict[str, Any]]) -> tuple[int, int] | None:
    details = comp.get("details")
    if not isinstance(details, list):
        return None  # ESPN did not provide match events: red cards unknown
    ids = {str((c.get("team") or {}).get("id")): c.get("homeAway") for c in competitors}
    counts = {"home": 0, "away": 0}
    for item in details:
        kind = ((item.get("type") or {}).get("text") or "").lower()
        if "red card" not in kind and not item.get("redCard"):
            continue
        side = ids.get(str((item.get("team") or {}).get("id")))
        if side in counts:
            counts[side] += 1
    return counts["home"], counts["away"]


def _odds(comp: dict[str, Any]) -> dict[str, Any] | None:
    for entry in comp.get("odds") or []:
        home = entry.get("homeTeamOdds") or {}
        away = entry.get("awayTeamOdds") or {}
        spread = to_float(entry.get("spread"))
        result = {
            "provider": ((entry.get("provider") or {}).get("name")) or "ESPN odds",
            "details": entry.get("details"),
            "spread": spread,  # home-team spread when ESPN provides it numerically
            "over_under": to_float(entry.get("overUnder")),
            "home_moneyline": to_float(home.get("moneyLine")),
            "away_moneyline": to_float(away.get("moneyLine")),
            "home_favorite": home.get("favorite"),
        }
        if any(result[k] is not None for k in ("spread", "home_moneyline", "over_under")):
            return result
    return None
