"""Sports: live win probability from the game state, plus sportsbook odds.

Live state (score, clock, period, red cards) comes from ESPN's scoreboard;
the pregame line ESPN shows sets team strength. Models:

* basketball / American football: final margin ~ Normal(current margin +
  prior margin x time left, sigma x sqrt(time left)); totals likewise
* hockey / soccer: remaining goals ~ Poisson per team, strengths fitted to the
  pregame moneyline when ESPN provides one; soccer red cards scale rates
* baseball: remaining runs ~ Poisson per remaining half-inning

Model constants are published league averages and are listed in every
calculation. Sportsbook consensus (The Odds API, de-vigged) is an independent
estimate; when both exist they are combined and checked for conflict.
Anything missing (no league mapping, no game match, unclear status) yields
DATA UNAVAILABLE rather than a guess.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime

from ..categories import SPORTS
from ..http import DataUnavailable
from ..market_scanner import FavoriteCandidate
from ..sources.espn import LEAGUES, EspnClient, GameState, TeamState
from ..sources.odds_api import SPORT_KEYS, OddsApiClient
from .base import (
    DATA_LAG,
    EXTERNAL_ODDS_EDGE,
    LIVE_EVENT_EDGE,
    OK,
    Evidence,
    ProbabilityEstimate,
)
from .combine import combine_estimates
from .stats import american_to_decimal, clamp, devig_power, norm_cdf, poisson_diff_probs, poisson_margin_probs

ENGINE = "sports"

# Published league averages used as model constants (shown in calculations).
MARGIN_SPORTS = {
    "nba": {"periods": 4, "period_min": 12, "sigma_margin": 12.5, "sigma_total": 18.0, "avg_total": 228.0},
    "wnba": {"periods": 4, "period_min": 10, "sigma_margin": 11.0, "sigma_total": 15.0, "avg_total": 165.0},
    "ncaab": {"periods": 2, "period_min": 20, "sigma_margin": 11.0, "sigma_total": 16.0, "avg_total": 145.0},
    "nfl": {"periods": 4, "period_min": 15, "sigma_margin": 13.5, "sigma_total": 13.5, "avg_total": 45.0},
    "cfb": {"periods": 4, "period_min": 15, "sigma_margin": 16.0, "sigma_total": 16.0, "avg_total": 55.0},
}
GOAL_SPORTS = {
    "nhl": {"periods": 3, "period_min": 20, "goals_per_team": 3.05, "regulation_only": False},
    "soccer": {"periods": 2, "period_min": 45, "goals_per_team": 1.35, "home_edge": 1.10, "stoppage_min": 4.0, "regulation_only": True},
}
MLB_RUNS_PER_HALF_INNING = 0.5
RED_CARD_SELF = 0.70
RED_CARD_OPPONENT = 1.20
UNCLEAR_STATUS_WORDS = ("postpon", "suspend", "delay", "cancel", "abandon", "forfeit")


@dataclass
class SportsSpec:
    market_type: str  # moneyline | win | draw | spread | total
    team_a: str | None  # team the market is about (moneyline: outcome-specific)
    team_b: str | None
    league_tag: str | None
    line: float | None = None
    yes_index: int | None = None


def _league_tag(candidate: FavoriteCandidate) -> str | None:
    tags = candidate.market.all_tags()
    for tag in tags:
        if tag in LEAGUES:
            return tag
    for tag in tags:
        if tag in SPORT_KEYS:
            return tag
    return None


_VS = re.compile(r"^\s*(.+?)\s+(?:vs\.?|v\.?|@|at)\s+(.+?)\s*(?:[:|(\-–].*)?$", re.IGNORECASE)


def parse_teams(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    match = _VS.match(text)
    if not match:
        return None
    a, b = match.group(1).strip(), match.group(2).strip()
    a = re.sub(r"^(will\s+)", "", a, flags=re.IGNORECASE)
    b = re.sub(r"\s+(end in a draw|win).*$", "", b, flags=re.IGNORECASE)
    return (a, b) if a and b else None


def parse_sports_market(candidate: FavoriteCandidate) -> SportsSpec | None:
    market = candidate.market
    outcomes = [o.strip() for o in market.outcomes]
    lowered = [o.lower() for o in outcomes]
    question = market.question or ""
    event = market.event
    teams = None
    if event and event.home_team and event.away_team:
        teams = (event.home_team, event.away_team)
    teams = teams or parse_teams(event.title if event else None) or parse_teams(question)
    league = _league_tag(candidate)
    kind = (market.sports_market_type or "").lower()
    yes_index = lowered.index("yes") if "yes" in lowered else None

    if kind == "totals" or {"over", "under"} <= set(lowered) or re.search(r"\bO/U\b", question):
        line = market.line
        if line is None:
            match = re.search(r"O/U\s*(\d+(?:\.\d+)?)", question, re.IGNORECASE)
            line = float(match.group(1)) if match else None
        if line is None or teams is None:
            return None
        return SportsSpec("total", teams[0], teams[1], league, line=line)
    if kind == "spreads" or question.lower().startswith("spread"):
        match = re.search(r"spread:?\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", question, re.IGNORECASE)
        if not match:
            return None
        return SportsSpec("spread", match.group(1).strip(), None, league, line=float(match.group(2)))
    if yes_index is not None:
        if re.search(r"\bdraw\b|\btie\b", question, re.IGNORECASE):
            if teams is None:
                return None
            return SportsSpec("draw", teams[0], teams[1], league, yes_index=yes_index)
        match = re.search(r"will\s+(.+?)\s+(?:win|beat|defeat)\b", question, re.IGNORECASE)
        if match:
            team = match.group(1).strip()
            other = None
            if teams:
                other = teams[1] if _same(team, teams[0]) else teams[0]
            return SportsSpec("win", team, other, league, yes_index=yes_index)
        return None
    if len(outcomes) == 2 and kind in ("", "moneyline", "child_moneyline"):
        return SportsSpec("moneyline", outcomes[0], outcomes[1], league)
    return None


def _same(a: str, b: str) -> bool:
    from ..sources.espn import normalize_team

    na, nb = normalize_team(a), normalize_team(b)
    return na == nb or (len(na) >= 4 and (na in nb or nb in na))


class SportsEngine:
    name = ENGINE

    def __init__(self, espn: EspnClient, odds: OddsApiClient, *, conflict_threshold: float = 0.05):
        self.espn = espn
        self.odds = odds
        self.conflict_threshold = conflict_threshold

    def applies(self, candidate: FavoriteCandidate) -> bool:
        return candidate.category == SPORTS

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate:
        idx = candidate.outcome_index
        spec = parse_sports_market(candidate)
        if spec is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "unsupported or unparseable sports market type")
        if spec.league_tag is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "league not identified from market tags")
        estimates = [self._live_estimate(candidate, spec, now)]
        if spec.market_type in ("moneyline", "win", "draw"):
            estimates.append(self._odds_estimate(candidate, spec, now))
        return combine_estimates(estimates, outcome_index=idx, engine=ENGINE, conflict_threshold=self.conflict_threshold)

    # ------------------------------------------------------------ live model

    def _live_estimate(self, candidate: FavoriteCandidate, spec: SportsSpec, now: datetime) -> ProbabilityEstimate:
        idx = candidate.outcome_index
        method = "live game-state model"
        if spec.league_tag not in LEAGUES:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"no ESPN mapping for league '{spec.league_tag}'", method=method)
        team_a = spec.team_a or ""
        start = candidate.market.game_start_time or (candidate.market.event.start_time if candidate.market.event else None)
        try:
            game, note = self.espn.find_game(spec.league_tag, team_a, spec.team_b, start)
        except DataUnavailable as exc:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"ESPN: {exc.reason}", method=method)
        if game is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"ESPN: {note}", method=method)

        detail_lower = game.detail.lower()
        if any(word in detail_lower for word in UNCLEAR_STATUS_WORDS):
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"unclear event state: {game.detail}", method=method)
        if game.state == "pre":
            return ProbabilityEstimate.unavailable(idx, ENGINE, "game not started; no live state to model", method=method)
        if game.state == "post" and start is None:
            # Without a scheduled start we cannot rule out matching an earlier game
            # between the same teams (e.g. a playoff series).
            return ProbabilityEstimate.unavailable(idx, ENGINE, "final score found but the market has no start time to confirm it is this game", method=method)

        team, match_quality = game.team_for(team_a)
        if team is None or match_quality < 0.8:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"could not map '{team_a}' to an ESPN team", method=method)
        other = game.away if team is game.home else game.home
        if team.score is None or other.score is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "score missing from ESPN", method=method)

        calc = [
            f"ESPN {game.name}: {game.home.display} {game.home.score} - {game.away.score} {game.away.display}; "
            f"{game.detail} (period {game.period}, clock {game.display_clock}); {note}",
        ]
        evidence = [
            Evidence(
                "ESPN scoreboard",
                f"{game.away.display} {game.away.score} @ {game.home.display} {game.home.score}, {game.detail}",
                {"home": game.home.score, "away": game.away.score, "period": game.period, "clock": game.display_clock},
                game.url,
                game.fetched_at,
                quality=0.8,
            )
        ]
        risks: list[str] = []
        try:
            result = self._model(game, team, other, spec, calc, risks, evidence)
        except DataUnavailable as exc:
            return ProbabilityEstimate.unavailable(idx, ENGINE, exc.reason, method=method)
        p_event, uncertainty, fraction_left = result

        # Translate "event" (team wins / draw / over / covers) into the candidate's outcome.
        outcome_label = candidate.outcome
        p_side = self._side_probability(spec, candidate, p_event, team_a)
        if p_side is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"could not map outcome '{outcome_label}' to the model event", method=method)
        calc.append(f"P({outcome_label}) = {p_side:.4f} (model uncertainty +/-{uncertainty:.4f})")

        completed = game.state == "post" and game.completed
        opp_type = DATA_LAG if completed else LIVE_EVENT_EDGE
        if candidate.market.seconds_delay:
            risks.append(f"Polymarket applies a {candidate.market.seconds_delay:g}s in-play order delay")
        main = f"{team.display} {team.score}-{other.score} {other.display}, {game.detail}"
        loss = self._loss_scenarios(spec, game, team, other, p_side >= 0.5)
        return ProbabilityEstimate(
            outcome_index=idx,
            probability=p_side,
            engine=ENGINE,
            method=method,
            data_status=OK,
            uncertainty=uncertainty,
            opportunity_type=opp_type,
            evidence=evidence,
            calculation=calc,
            sources=["ESPN"],
            source_quality=0.75 if not completed else 0.85,
            event_certainty=clamp(0.5 * (1 - fraction_left) + 0.5 * abs(2 * p_side - 1)),
            risks=risks,
            loss_scenarios=loss,
            data_age_seconds=(now - game.fetched_at).total_seconds(),
            main_reason=main,
        )

    def _side_probability(self, spec: SportsSpec, candidate: FavoriteCandidate, p_event: float, team_a: str) -> float | None:
        label = candidate.outcome
        lowered = label.strip().lower()
        if spec.market_type in ("win", "draw"):
            if spec.yes_index is None:
                return None
            return p_event if candidate.outcome_index == spec.yes_index else 1.0 - p_event
        if spec.market_type == "total":
            if lowered == "over":
                return p_event
            if lowered == "under":
                return 1.0 - p_event
            return None
        # moneyline / spread: outcomes are team names; event = team_a wins/covers
        return p_event if _same(label, team_a) else 1.0 - p_event

    def _model(
        self,
        game: GameState,
        team: TeamState,
        other: TeamState,
        spec: SportsSpec,
        calc: list[str],
        risks: list[str],
        evidence: list[Evidence],
    ) -> tuple[float, float, float]:
        """Returns (P(event), uncertainty, fraction of game left)."""
        sport = game.sport
        lead = team.score - other.score  # type: ignore[operator]
        is_home = team is game.home
        if sport in MARGIN_SPORTS:
            return self._margin_model(game, lead, is_home, spec, calc, risks, evidence)
        if sport in GOAL_SPORTS:
            return self._goal_model(game, team, other, lead, is_home, spec, calc, risks, evidence)
        if sport == "mlb":
            return self._baseball_model(game, lead, is_home, spec, calc, risks)
        raise DataUnavailable("model", f"no live model for {sport}")

    # basketball / American football
    def _margin_model(self, game, lead, is_home, spec, calc, risks, evidence):
        p = MARGIN_SPORTS[game.sport]
        full = p["periods"] * p["period_min"]
        left = _minutes_left_clocked(game, p["periods"], p["period_min"])
        frac = clamp(left / full)
        calc.append(f"Time left: {left:.1f} of {full} minutes ({frac:.1%} of the game)")
        prior_margin, prior_note = _prior_margin(game, is_home)
        if prior_margin is None:
            risks.append("Pregame line unavailable: team strength DATA UNAVAILABLE, neutral prior used with wider uncertainty")
            prior_margin = 0.0
        else:
            evidence.append(Evidence(game.odds.get("provider") if game.odds else "ESPN odds", prior_note, prior_margin, game.url, game.fetched_at, quality=0.7))
        calc.append(f"Pregame expected margin for this team: {prior_margin:+.1f} ({prior_note})")
        if game.sport in ("nfl", "cfb"):
            risks.append("Possession, down/distance and field position are not modelled")
        sigma = p["sigma_margin"] * math.sqrt(frac)
        mu = prior_margin * frac

        def p_margin(m: float, mu_: float, sd: float) -> float:
            if sd <= 1e-9:
                return 1.0 if m > 0 else 0.0 if m < 0 else 0.5
            return 1 - 0.5 * norm_cdf((0.5 - m - mu_) / sd) - 0.5 * norm_cdf((-0.5 - m - mu_) / sd)

        if spec.market_type == "total":
            total_now = game.home.score + game.away.score
            expected_total = (game.odds or {}).get("over_under") or p["avg_total"]
            source = "pregame over/under" if (game.odds or {}).get("over_under") else "league average (no line available)"
            mu_t = expected_total * frac
            sd_t = p["sigma_total"] * math.sqrt(frac)
            calc.append(
                f"Total model: now {total_now}, expected remaining {mu_t:.1f} (full-game {expected_total:.1f} from {source}), "
                f"sd {sd_t:.2f}; line {spec.line}"
            )
            need = spec.line - total_now

            def p_over(sd: float) -> float:
                return 1.0 - norm_cdf((need - mu_t) / sd) if sd > 1e-9 else float(total_now > spec.line)

            point = p_over(sd_t)
            variants = [p_over(sd_t * 1.2), p_over(sd_t * 0.85)]
            unc = (max(variants + [point]) - min(variants + [point])) / 2
            calc.append(f"P(over {spec.line}) = {point:.4f}")
            return clamp(point), max(0.01, unc), frac
        margin = lead + (spec.line if spec.market_type == "spread" and spec.line is not None else 0.0)
        calc.append(
            f"Margin model: current {lead:+d}{f' with line {spec.line:+g}' if spec.market_type == 'spread' else ''}, "
            f"remaining ~ Normal(mu={mu:+.2f}, sd={sigma:.2f}) [sd_full={p['sigma_margin']}]"
        )
        point = p_margin(margin, mu, sigma)
        variants = [p_margin(margin, mu, sigma * 1.2), p_margin(margin, mu, sigma * 0.85), p_margin(margin, mu * 1.5, sigma), p_margin(margin, mu * 0.5, sigma)]
        unc = max(0.01, (max(variants + [point]) - min(variants + [point])) / 2)
        if prior_note.startswith("no "):
            unc *= 1.5
        calc.append(f"P(team {'covers' if spec.market_type == 'spread' else 'wins'}) = {point:.4f} (ties -> overtime counted as 50/50)")
        return clamp(point), unc, frac

    # hockey / soccer
    def _goal_model(self, game, team, other, lead, is_home, spec, calc, risks, evidence):
        p = GOAL_SPORTS[game.sport]
        full = p["periods"] * p["period_min"]
        if game.sport == "soccer":
            left, notes = _soccer_minutes_left(game, p["stoppage_min"])
            if game.period > 2:
                raise DataUnavailable("model", "match in extra time/penalties; regulation result already fixed by rules")
        else:
            left = _minutes_left_clocked(game, p["periods"], p["period_min"])
            notes = ""
        frac = clamp(left / full)
        calc.append(f"Time left: {left:.1f} of {full} minutes{notes}")
        base = p["goals_per_team"]
        home_rate = base * p.get("home_edge", 1.0)
        away_rate = base / p.get("home_edge", 1.0)
        strength_note = "league-average scoring rates (no pregame moneyline)"
        fitted = _fit_strength(game, home_rate, away_rate)
        if fitted is not None:
            home_rate, away_rate, strength_note = fitted
            evidence.append(Evidence(game.odds.get("provider", "ESPN odds"), strength_note, [home_rate, away_rate], game.url, game.fetched_at, quality=0.7))
        else:
            risks.append("Pregame moneyline unavailable: team strength DATA UNAVAILABLE, league averages used")
        team_rate = home_rate if is_home else away_rate
        other_rate = away_rate if is_home else home_rate
        if game.sport == "soccer":
            if team.red_cards is None or other.red_cards is None:
                risks.append("Red-card data DATA UNAVAILABLE from ESPN; numbers on the pitch assumed equal")
            else:
                extra = team.red_cards - other.red_cards
                if extra:
                    factor_self = RED_CARD_SELF ** max(extra, 0) * RED_CARD_OPPONENT ** max(-extra, 0)
                    factor_other = RED_CARD_OPPONENT ** max(extra, 0) * RED_CARD_SELF ** max(-extra, 0)
                    team_rate *= factor_self
                    other_rate *= factor_other
                    calc.append(f"Red cards: this team {team.red_cards}, opponent {other.red_cards} -> rates x{factor_self:.2f} / x{factor_other:.2f}")
        lam_t, lam_o = team_rate * frac, other_rate * frac
        calc.append(f"Remaining goals ~ Poisson({lam_t:.3f}) for this team, Poisson({lam_o:.3f}) for the opponent ({strength_note})")
        if game.sport == "nhl":
            risks.append("Empty-net situations late in games are not modelled")

        def probs(scale: float) -> tuple[float, float, float]:
            return poisson_margin_probs(lead, lam_t * scale, lam_o * scale)

        if spec.market_type == "total":
            total_now = game.home.score + game.away.score
            lam_total = (lam_t + lam_o)
            need = math.floor(spec.line - total_now) + 1  # goals needed to go over

            def p_over(lam: float) -> float:
                return 1.0 - sum(_pois(k, lam) for k in range(need)) if need > 0 else 1.0

            point = p_over(lam_total)
            variants = [p_over(lam_total * 1.25), p_over(lam_total * 0.8)]
            calc.append(f"Total: now {total_now}, line {spec.line}; P(>= {max(need, 0)} more goals) = {point:.4f}")
            unc = (max(variants + [point]) - min(variants + [point])) / 2
            return clamp(point), max(0.01, unc) if need > 0 else 0.0, frac
        win, draw, loss = probs(1.0)
        hi = probs(1.25)
        lo = probs(0.8)
        calc.append(f"Final result probabilities (lead {lead:+d}): win {win:.4f}, draw {draw:.4f}, loss {loss:.4f}")
        if spec.market_type == "draw":
            point, variants = draw, [hi[1], lo[1]]
        elif game.sport == "nhl" and not p["regulation_only"]:
            point = win + 0.5 * draw  # tie after regulation -> OT/shootout ~ coin flip
            variants = [hi[0] + 0.5 * hi[1], lo[0] + 0.5 * lo[1]]
            calc.append("NHL moneyline includes OT/shootout: P(win) = P(reg win) + 0.5 x P(reg tie)")
        else:
            point, variants = win, [hi[0], lo[0]]
            if game.sport == "soccer":
                calc.append("Soccer markets settle on regulation + stoppage time: a draw is a loss for 'win' bets")
        unc = max(0.01, (max(variants + [point]) - min(variants + [point])) / 2)
        if fitted is None:
            unc *= 1.5
        return clamp(point), unc, frac

    def _baseball_model(self, game, lead, is_home, spec, calc, risks):
        half, inning = _baseball_half(game.detail, game.period)
        if half is None:
            raise DataUnavailable("model", f"could not read inning state from '{game.detail}'")
        away_left, home_left = _half_innings_left(half, inning)
        team_left, other_left = (home_left, away_left) if is_home else (away_left, home_left)
        lam_t = MLB_RUNS_PER_HALF_INNING * team_left
        lam_o = MLB_RUNS_PER_HALF_INNING * other_left
        calc.append(
            f"{half} {inning}: remaining half-innings this team {team_left:g}, opponent {other_left:g}; "
            f"runs ~ Poisson({MLB_RUNS_PER_HALF_INNING}/half-inning)"
        )
        risks.append("Base runners, outs, pitchers and bullpens are not modelled")
        if spec.market_type == "total":
            raise DataUnavailable("model", "baseball totals not modelled")
        win, draw, _ = poisson_margin_probs(lead, lam_t, lam_o)
        point = win + 0.5 * draw
        hi = poisson_margin_probs(lead, lam_t * 1.3, lam_o * 1.3)
        lo = poisson_margin_probs(lead, lam_t * 0.75, lam_o * 0.75)
        variants = [hi[0] + 0.5 * hi[1], lo[0] + 0.5 * lo[1]]
        calc.append(f"P(win) = {point:.4f} (tie -> extra innings 50/50)")
        frac = clamp((team_left + other_left) / 18.0)
        return clamp(point), max(0.015, (max(variants + [point]) - min(variants + [point])) / 2), frac

    def _loss_scenarios(self, spec: SportsSpec, game: GameState, team: TeamState, other: TeamState, backing_event: bool) -> list[str]:
        if spec.market_type == "total":
            return ["Scoring pace deviating sharply from the model in the time left", "Stat corrections / overtime rules"]
        scenarios = []
        if backing_event:
            scenarios.append(f"Comeback by {other.display}")
        else:
            scenarios.append(f"{team.display} holding on")
        scenarios.extend(["Match abandoned/postponed (rules may void or resolve 50-50)", "Resolution-rule issue (regulation vs overtime, official result changes)"])
        if game.sport == "soccer":
            scenarios.append("Late goals in stoppage time")
        return scenarios

    # ------------------------------------------------------------- odds API

    def _odds_estimate(self, candidate: FavoriteCandidate, spec: SportsSpec, now: datetime) -> ProbabilityEstimate:
        idx = candidate.outcome_index
        method = "sportsbook consensus (de-vigged)"
        if not self.odds.enabled:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "sportsbook odds: ODDS_API_KEY not set", method=method)
        try:
            event = self.odds.find_event(spec.league_tag or "", spec.team_a or "", spec.team_b)
        except DataUnavailable as exc:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"sportsbook odds: {exc.reason}", method=method)
        if event is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "sportsbook odds: no matching event", method=method)
        if spec.market_type == "draw":
            outcome_name = "Draw"
        else:
            names = [event.home_team, event.away_team]
            outcome_name = next((n for n in names if _same(n, spec.team_a or "")), None)
        if outcome_name is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "sportsbook odds: team not found in event", method=method)
        median, pinnacle, values = event.consensus(outcome_name)
        if median is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "sportsbook odds: outcome not quoted", method=method)
        p_event = pinnacle if pinnacle is not None else median
        p_side = self._side_probability(spec, candidate, p_event, spec.team_a or "")
        if p_side is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "sportsbook odds: outcome mapping failed", method=method)
        spread = (max(values) - min(values)) / 2 if len(values) > 1 else 0.02
        latest = max((b.last_update for b in event.books if b.last_update), default=None)
        calc = [
            f"The Odds API {event.sport_key}: {event.away_team} @ {event.home_team}, {len(values)} bookmakers",
            f"No-vig P({outcome_name}): median {median:.4f}" + (f", Pinnacle {pinnacle:.4f} (used)" if pinnacle is not None else " (used)"),
            f"P({candidate.outcome}) = {p_side:.4f}",
        ]
        evidence = [
            Evidence(f"{b.bookmaker} (via The Odds API)", f"no-vig P({outcome_name}) {b.probabilities.get(outcome_name, float('nan')):.4f}, margin {b.margin:.2%}",
                     b.probabilities.get(outcome_name), event.url, b.last_update, quality=0.85 if b.bookmaker.lower() == "pinnacle" else 0.7)
            for b in event.books if outcome_name in b.probabilities
        ]
        return ProbabilityEstimate(
            outcome_index=idx,
            probability=p_side,
            engine=ENGINE,
            method=method,
            data_status=OK,
            uncertainty=max(0.01, spread),
            opportunity_type=EXTERNAL_ODDS_EDGE,
            evidence=evidence,
            calculation=calc,
            sources=["Sportsbooks"],
            source_quality=0.85 if pinnacle is not None else 0.75,
            event_certainty=abs(2 * p_side - 1) * 0.5,
            risks=["Sportsbook and Polymarket rules can differ (overtime, void conditions)"],
            loss_scenarios=["Bookmaker lines lagging the live game state"],
            data_age_seconds=(now - latest).total_seconds() if latest else None,
            main_reason=f"Sportsbooks price {outcome_name} at {p_event:.1%} (no-vig)",
        )


# ------------------------------------------------------------------ helpers


def _pois(k: int, lam: float) -> float:
    from .stats import poisson_pmf

    return poisson_pmf(k, lam)


def _minutes_left_clocked(game: GameState, periods: int, period_min: float) -> float:
    """Clock counts down within each period (basketball, football, hockey)."""
    if game.state == "post":
        return 0.0
    clock = (game.clock_seconds or 0.0) / 60.0
    if game.period <= 0:
        return periods * period_min
    if game.period > periods:  # overtime: only the OT clock remains
        return clock
    return (periods - game.period) * period_min + min(clock, period_min)


def _soccer_minutes_left(game: GameState, stoppage: float) -> tuple[float, str]:
    """ESPN's soccer clock counts elapsed match seconds."""
    if game.state == "post":
        return 0.0, ""
    elapsed = (game.clock_seconds or 0.0) / 60.0
    if "half" in game.detail.lower() and "time" in game.detail.lower():  # halftime
        return 45.0 + stoppage, " (halftime)"
    if game.period <= 1:
        return max(0.0, 45.0 - elapsed) + 2.0 + 45.0 + stoppage, " (incl. assumed stoppage time)"
    regulation_left = max(0.0, 90.0 - elapsed)
    stoppage_left = max(0.0, stoppage - max(0.0, elapsed - 90.0))
    return regulation_left + stoppage_left, f" (incl. ~{stoppage_left:.0f} assumed stoppage minutes)"


def _prior_margin(game: GameState, is_home: bool) -> tuple[float | None, str]:
    odds = game.odds or {}
    spread = odds.get("spread")
    details = odds.get("details") or ""
    if spread is not None:
        home_margin = -spread  # ESPN spread is quoted for the home team
        # Guard against providers quoting the favourite's spread instead.
        if odds.get("home_favorite") is False and home_margin > 0:
            home_margin = -home_margin
        if odds.get("home_favorite") is True and home_margin < 0:
            home_margin = -home_margin
        margin = home_margin if is_home else -home_margin
        return margin, f"{odds.get('provider')} line '{details}'"
    home_ml, away_ml = odds.get("home_moneyline"), odds.get("away_moneyline")
    if home_ml and away_ml:
        try:
            p_home = devig_power([american_to_decimal(home_ml), american_to_decimal(away_ml)])[0]
        except ValueError:
            return None, "no usable pregame line"
        # Normal approximation: P(home) = Phi(margin / 12.5) -> margin
        z = _inv_norm(clamp(p_home, 0.01, 0.99))
        home_margin = z * 12.5
        margin = home_margin if is_home else -home_margin
        return margin, f"{odds.get('provider')} moneylines {home_ml:+.0f}/{away_ml:+.0f} -> P(home) {p_home:.3f}"
    return None, "no pregame line available"


def _inv_norm(p: float) -> float:
    lo, hi = -8.0, 8.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _fit_strength(game: GameState, home_rate: float, away_rate: float) -> tuple[float, float, str] | None:
    """Scale team scoring rates so the full-game model reproduces the pregame
    de-vigged moneyline (2-way for hockey, home/away part of 1X2 for soccer)."""
    odds = game.odds or {}
    home_ml, away_ml = odds.get("home_moneyline"), odds.get("away_moneyline")
    if not home_ml or not away_ml:
        return None
    try:
        decimals = [american_to_decimal(home_ml), american_to_decimal(away_ml)]
    except ValueError:
        return None
    target_home = devig_power(decimals)[0]  # P(home beats away | not draw) style ratio
    total = home_rate + away_rate
    lo, hi = 0.2, 5.0
    for _ in range(60):
        ratio = math.sqrt(lo * hi)
        h = total * ratio / (1 + ratio)
        a = total / (1 + ratio)
        win, draw, loss = poisson_diff_probs(h, a)
        p_home = win / (win + loss) if win + loss > 0 else 0.5
        if p_home < target_home:
            lo = ratio
        else:
            hi = ratio
    ratio = math.sqrt(lo * hi)
    h, a = total * ratio / (1 + ratio), total / (1 + ratio)
    note = f"rates fitted to {odds.get('provider')} moneylines {home_ml:+.0f}/{away_ml:+.0f} (home {h:.2f}, away {a:.2f} per game)"
    return h, a, note


_ORDINAL = re.compile(r"\b(top|bot|bottom|mid|middle|end)\w*\s+(?:of\s+(?:the\s+)?)?(\d+)", re.IGNORECASE)


def _baseball_half(detail: str, period: int) -> tuple[str | None, int]:
    match = _ORDINAL.search(detail)
    if not match:
        return None, period
    word = match.group(1).lower()
    half = {"top": "Top", "bot": "Bottom", "bottom": "Bottom", "mid": "Middle", "middle": "Middle", "end": "End"}[word]
    return half, int(match.group(2))


def _half_innings_left(half: str, inning: int) -> tuple[float, float]:
    """(away, home) half-innings left, counting the current one as half used."""
    regulation = max(9, inning)
    later = regulation - inning
    if half == "Top":
        return later + 0.5, later + 1.0
    if half == "Middle":
        return float(later), later + 1.0
    if half == "Bottom":
        return float(later), later + 0.5
    return float(later), float(later)  # End


__all__ = ["SportsEngine", "parse_sports_market", "parse_teams"]
