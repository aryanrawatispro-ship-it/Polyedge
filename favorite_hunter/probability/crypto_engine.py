"""Crypto price markets: digital (above/below/between at a time), up/down
windows, and touch/barrier ("reach", "dip to") markets.

Probability comes from the live spot price, the strike, the time left and
volatility (realised and, for BTC/ETH, options-implied). Log returns are
modelled with fat tails (Student-t, nu=4). For the side being evaluated the
*lowest* probability across the volatility scenarios is used, so missing
tail risk cannot inflate the favorite's probability.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..categories import CRYPTO
from ..http import DataUnavailable
from ..market_scanner import FavoriteCandidate
from ..sources.crypto import Asset, CryptoData, find_asset
from ..timeutil import humanize_hours, short_utc
from .base import (
    DATA_LAG,
    NEAR_RESOLUTION_EDGE,
    OK,
    THRESHOLD_EDGE,
    Evidence,
    ProbabilityEstimate,
)
from .stats import clamp, scaled_t_cdf

ENGINE = "crypto"
TAIL_NU = 4.0
YEAR_HOURS = 365.25 * 24
MAX_SPOT_DEVIATION = 0.005  # 0.5% disagreement between exchanges -> conflict
UNCERTAINTY_FLOOR = 0.005

_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?"
_ABOVE_WORDS = r"(?:above|over|higher than|greater than|at least|more than|exceed|>=?|≥)"
_BELOW_WORDS = r"(?:below|under|lower than|less than|<=?|≤)"
_TOUCH_UP = r"(?:reach|hit|touch|climb to|rise to|surpass|break)"
_TOUCH_DOWN = r"(?:dip to|drop to|fall to|sink to|crash to|go below|dip below|fall below)"
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"], start=1)}


def _number(text: str, suffix: str | None) -> float:
    value = float(text.replace(",", ""))
    if suffix:
        value *= {"k": 1e3, "m": 1e6}[suffix.lower()]
    return value


@dataclass
class CryptoSpec:
    kind: str  # up_down | above | below | range | touch_up | touch_down
    asset: Asset
    event_index: int  # outcome index that wins if the described event happens
    settle_time: datetime
    strike: float | None = None
    strike_high: float | None = None
    window_start: datetime | None = None
    resolution_source: str = "unknown"
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == "up_down":
            return f"{self.asset.symbol} up/down over {short_utc(self.window_start)} -> {short_utc(self.settle_time)}"
        if self.kind == "range":
            return f"{self.asset.symbol} between {self.strike:,.2f} and {self.strike_high:,.2f} at {short_utc(self.settle_time)}"
        if self.kind in ("touch_up", "touch_down"):
            verb = "reaches" if self.kind == "touch_up" else "dips to"
            return f"{self.asset.symbol} {verb} {self.strike:,.2f} between {short_utc(self.window_start)} and {short_utc(self.settle_time)}"
        return f"{self.asset.symbol} {self.kind} {self.strike:,.2f} at {short_utc(self.settle_time)}"


def _outcome_index(outcomes: list[str], *labels: str) -> int | None:
    lowered = [o.strip().lower() for o in outcomes]
    for label in labels:
        if label in lowered:
            return lowered.index(label)
    return None


def detect_resolution_source(text: str) -> str:
    lowered = text.lower()
    if "chainlink" in lowered:
        return "chainlink"
    if "binance" in lowered:
        return "binance"
    if "coinbase" in lowered:
        return "coinbase"
    return "unknown"


# Questions about crypto that are not about the asset's price.
_NON_PRICE = re.compile(
    r"\b(etf|flows?|inflows?|outflows?|market ?cap|dominance|hash ?rate|fees?|transactions?|addresses|"
    r"reserves?|treasury|holdings?|volume|supply|mining|halving|listing|approve|approval|launch|"
    r"airdrop|tvl|staking|buy|sell|hold|ban|legal|tweet|says?|mention)\b",
    re.IGNORECASE,
)
# Strike must be within this ratio of spot, else the parse is treated as wrong.
STRIKE_RATIO_BOUNDS = (0.25, 4.0)


def parse_crypto_market(candidate: FavoriteCandidate) -> CryptoSpec | None:
    market = candidate.market
    if _NON_PRICE.search(market.question or ""):
        return None
    text = " ".join(filter(None, [market.question, market.group_item_title or ""]))
    asset = find_asset(" ".join(filter(None, [market.question, market.event_title or ""])))
    if asset is None or market.end_date is None:
        return None
    rules = market.description or ""
    source = detect_resolution_source(rules + " " + (market.resolution_source or ""))
    lowered = text.lower()
    yes_index = _outcome_index(market.outcomes, "yes")

    # Up / Down windows
    if "up or down" in lowered:
        up = _outcome_index(market.outcomes, "up")
        if up is None:
            return None
        start = _window_start(market.question, market.end_date, market.raw)
        if start is None:
            return None
        return CryptoSpec("up_down", asset, up, market.end_date, window_start=start, resolution_source=source)

    if yes_index is None:
        return None

    # Range: "between $X and $Y" or group item "110,000-112,000"
    match = re.search(rf"between\s+{_NUM}\s+and\s+{_NUM}", text, re.IGNORECASE)
    if match:
        low = _number(match.group(1), match.group(2))
        high = _number(match.group(3), match.group(4))
        return CryptoSpec("range", asset, yes_index, market.end_date, strike=low, strike_high=high, resolution_source=source)
    group = (market.group_item_title or "").strip()
    match = re.fullmatch(rf"{_NUM}\s*[-–]\s*{_NUM}", group)
    if match:
        low = _number(match.group(1), match.group(2))
        high = _number(match.group(3), match.group(4))
        return CryptoSpec("range", asset, yes_index, market.end_date, strike=low, strike_high=high, resolution_source=source)

    # Touch / barrier markets
    arrow_up = group.startswith(("↑", "▲")) or re.search(_TOUCH_UP, lowered)
    arrow_down = group.startswith(("↓", "▼")) or re.search(_TOUCH_DOWN, lowered)
    if arrow_up or arrow_down:
        match = re.search(_NUM, group) if group.startswith(("↑", "↓", "▲", "▼")) else None
        if match is None:
            pattern = _TOUCH_DOWN if arrow_down else _TOUCH_UP
            match = re.search(rf"{pattern}\s+{_NUM}", text, re.IGNORECASE)
            if match:
                strike = _number(match.group(1), match.group(2))
            else:
                return None
        else:
            strike = _number(match.group(1), match.group(2))
        start = _touch_period_start(market.question, rules, market.start_date, market.end_date)
        if start is None:
            return None
        kind = "touch_down" if arrow_down and not (group.startswith(("↑", "▲"))) else "touch_up"
        return CryptoSpec(kind, asset, yes_index, market.end_date, strike=strike, window_start=start, resolution_source=source)

    # Digital: above / below at the settlement time
    match = re.search(rf"{_ABOVE_WORDS}\s+{_NUM}", text, re.IGNORECASE)
    if match:
        return CryptoSpec("above", asset, yes_index, market.end_date, strike=_number(match.group(1), match.group(2)), resolution_source=source)
    match = re.search(rf"{_BELOW_WORDS}\s+{_NUM}", text, re.IGNORECASE)
    if match:
        return CryptoSpec("below", asset, yes_index, market.end_date, strike=_number(match.group(1), match.group(2)), resolution_source=source)
    if group.startswith((">", "≥")) or group.startswith("<"):
        match = re.search(_NUM, group)
        if match:
            kind = "above" if group.startswith((">", "≥")) else "below"
            return CryptoSpec(kind, asset, yes_index, market.end_date, strike=_number(match.group(1), match.group(2)), resolution_source=source)
    return None


def _window_start(question: str, end: datetime, raw: dict) -> datetime | None:
    """Start of an up/down window. Prefer the API's eventStartTime; else infer
    the window length from the title ("3:00PM-3:15PM ET", "3PM ET", "on <date>")."""
    from ..timeutil import parse_dt

    for key in ("eventStartTime", "startTime"):
        start = parse_dt(raw.get(key))
        if start is not None and start < end:
            return start
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*[-–]\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)", question, re.IGNORECASE)
    if match:
        def minutes(h: str, m: str | None, ampm: str) -> int:
            hour = int(h) % 12 + (12 if ampm.lower() == "pm" else 0)
            return hour * 60 + int(m or 0)

        span = (minutes(match.group(4), match.group(5), match.group(6)) - minutes(match.group(1), match.group(2), match.group(3))) % (24 * 60)
        if span > 0:
            return end - timedelta(minutes=span)
    if re.search(r"\d{1,2}\s*(?::\d{2})?\s*[ap]m\s*(et|est|edt)?", question, re.IGNORECASE):
        return end - timedelta(hours=1)
    if re.search(r"\bon\s+(" + "|".join(_MONTHS) + r")\s+\d{1,2}", question, re.IGNORECASE):
        return end - timedelta(hours=24)
    return None


def _touch_period_start(question: str, rules: str, start_date: datetime | None, end: datetime) -> datetime | None:
    """Start of the observation period for touch markets.

    "... in October" -> the calendar month (UTC midnight, conservative by a few
    hours vs ET); otherwise the market's own start date ("by <date>" markets
    count any time after creation). Unknown -> None (no estimate)."""
    match = re.search(r"\bin\s+(" + "|".join(_MONTHS) + r")\b", question, re.IGNORECASE)
    if match:
        month = _MONTHS[match.group(1).lower()]
        year = end.year if month <= end.month else end.year - 1
        return end.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_date is not None and re.search(r"\b(by|before)\b", question + " " + rules, re.IGNORECASE):
        return start_date
    return None


class CryptoEngine:
    name = ENGINE

    def __init__(self, data: CryptoData):
        self.data = data

    def applies(self, candidate: FavoriteCandidate) -> bool:
        return candidate.category == CRYPTO

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate:
        spec = parse_crypto_market(candidate)
        idx = candidate.outcome_index
        if spec is None:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "could not parse asset/strike/settlement time from the question")
        method = "crypto price model (fat-tailed and normal returns, conservative volatility)"
        calc: list[str] = [f"Market parsed as: {spec.describe()}"]
        evidence: list[Evidence] = []

        quotes, errors = self.data.spot_quotes(spec.asset)
        if not quotes:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "no spot price: " + "; ".join(errors), method=method)
        reference = quotes[0].price
        for level in (spec.strike, spec.strike_high):
            if level is not None and not STRIKE_RATIO_BOUNDS[0] <= level / reference <= STRIKE_RATIO_BOUNDS[1]:
                return ProbabilityEstimate.unavailable(
                    idx, ENGINE, f"parsed strike {level:,.2f} is implausible vs spot {reference:,.2f}; question not modelled", method=method
                )
        for q in quotes:
            evidence.append(Evidence(q.source, f"spot {q.price:,.4f}", q.price, q.url, q.observed_at, quality=0.85))
        primary = next((q for q in quotes if q.source.startswith("Binance")), quotes[0])
        spot = primary.price
        deviation = max(abs(q.price / spot - 1) for q in quotes)
        calc.append("Spot: " + ", ".join(f"{q.source} {q.price:,.4f}" for q in quotes) + f" (max deviation {deviation:.3%})")
        if errors:
            calc.append("Unavailable spot sources: " + "; ".join(errors))
        if deviation > MAX_SPOT_DEVIATION:
            est = ProbabilityEstimate.unavailable(idx, ENGINE, f"exchanges disagree by {deviation:.2%}", method=method)
            est.data_status = "CONFLICTING SOURCES"
            est.evidence = evidence
            est.calculation = calc
            return est

        try:
            vols = self.data.realized_vol(spec.asset, now)
        except DataUnavailable as exc:
            return ProbabilityEstimate.unavailable(idx, ENGINE, f"volatility unavailable: {exc.reason}", method=method)
        evidence.append(Evidence("Binance candles", "realised volatility " + ", ".join(f"{k} {v:.1%}" for k, v in vols.items()), dict(vols), None, now, quality=0.8))
        try:
            implied = self.data.implied_vol(spec.asset, now)
        except DataUnavailable as exc:
            implied = None
            calc.append(f"Implied vol DATA UNAVAILABLE ({exc.reason})")
        if implied:
            vols["deribit_dvol"] = implied
            evidence.append(Evidence("Deribit DVOL", f"30-day implied volatility {implied:.1%}", implied, "https://www.deribit.com", now, quality=0.8))
        calc.append("Volatility scenarios (annualised): " + ", ".join(f"{k} {v:.1%}" for k, v in vols.items()))

        tau_hours = (spec.settle_time - now).total_seconds() / 3600.0
        if tau_hours <= 0:
            return ProbabilityEstimate.unavailable(idx, ENGINE, "settlement time has passed; awaiting resolution data", method=method)
        tau = tau_hours / YEAR_HOURS
        calc.append(f"Time to settlement: {humanize_hours(tau_hours)} ({tau:.6f} years)")

        # Basis buffer: price source differs from the resolution source.
        same_source = spec.resolution_source == "binance" and primary.source.startswith("Binance")
        buffer = 0.0002 if same_source else 0.001
        calc.append(
            f"Resolution source: {spec.resolution_source}; pricing source: {primary.source}; "
            f"spot evaluated at +/-{buffer:.2%} for basis risk (lower probability kept)"
        )

        risks: list[str] = []
        loss: list[str] = []
        opp_type = THRESHOLD_EDGE
        settled = False

        if spec.kind == "up_down":
            try:
                opening = self.data.price_at(spec.asset, spec.window_start)  # type: ignore[arg-type]
            except DataUnavailable as exc:
                return ProbabilityEstimate.unavailable(idx, ENGINE, f"window opening price unavailable: {exc.reason}", method=method)
            strike = opening.open
            evidence.append(Evidence("Binance 1m candle", f"window open {strike:,.4f} at {short_utc(spec.window_start)}", strike, None, spec.window_start, quality=0.9))
            calc.append(f"Window opening price (Binance 1m open at {short_utc(spec.window_start)}): {strike:,.4f}; now {spot:,.4f} ({spot / strike - 1:+.3%})")
            event_fn = lambda s, sd, nu: _p_above(s, strike, sd, nu)  # noqa: E731
        elif spec.kind in ("above", "below"):
            strike = spec.strike  # type: ignore[assignment]
            calc.append(f"Strike {strike:,.4f} ({spec.kind}); spot is {spot / strike - 1:+.3%} from it")
            if spec.kind == "above":
                event_fn = lambda s, sd, nu: _p_above(s, strike, sd, nu)  # noqa: E731
            else:
                event_fn = lambda s, sd, nu: 1.0 - _p_above(s, strike, sd, nu)  # noqa: E731
        elif spec.kind == "range":
            low, high = spec.strike, spec.strike_high
            strike = low if abs(low / spot - 1) < abs(high / spot - 1) else high  # nearest edge
            calc.append(f"Range {low:,.4f} - {high:,.4f}; spot {spot:,.4f}")
            event_fn = lambda s, sd, nu: _p_above(s, low, sd, nu) - _p_above(s, high, sd, nu)  # noqa: E731
        else:  # touch_up / touch_down
            strike = spec.strike  # type: ignore[assignment]
            try:
                period_high, period_low = self.data.period_extremes(spec.asset, spec.window_start, now)  # type: ignore[arg-type]
            except DataUnavailable as exc:
                return ProbabilityEstimate.unavailable(idx, ENGINE, f"period high/low unavailable: {exc.reason}", method=method)
            evidence.append(Evidence("Binance 1h candles", f"period high {period_high:,.4f}, low {period_low:,.4f} since {short_utc(spec.window_start)}", [period_high, period_low], None, now, quality=0.85))
            calc.append(f"Barrier {strike:,.4f}; period high {period_high:,.4f}, low {period_low:,.4f} since {short_utc(spec.window_start)}")
            settled = period_high >= strike if spec.kind == "touch_up" else period_low <= strike
            if settled:
                opp_type = DATA_LAG
                calc.append("Barrier already touched during the period -> the event has happened; market awaits resolution")
                risks.append("Confirm the touch on the exact resolution source and candle type")
            if spec.kind == "touch_up":
                event_fn = lambda s, sd, nu: _p_touch_up(s, strike, sd, nu)  # noqa: E731
            else:
                event_fn = lambda s, sd, nu: _p_touch_down(s, strike, sd, nu)  # noqa: E731

        side_is_event = idx == spec.event_index

        def side_prob(sd: float) -> float:
            """Lowest side probability across fat-tailed and normal returns and a
            spot nudged +/- buffer (basis risk). A variance-matched t has thinner
            shoulders than a normal near 2 sd, so neither alone is conservative."""
            if settled:
                return 1.0 if side_is_event else 0.0
            values = []
            for s in (spot * (1 + buffer), spot * (1 - buffer)):
                for nu in (TAIL_NU, math.inf):
                    p_event = clamp(event_fn(s, sd, nu))
                    values.append(p_event if side_is_event else 1.0 - p_event)
            return min(values)

        scenarios = [(name, sigma * math.sqrt(tau)) for name, sigma in vols.items()]
        probs = []
        for name, sd in scenarios:
            p = side_prob(sd)
            probs.append(p)
            calc.append(f"  scenario {name}: sd(ln return to settlement) = {sd:.4%} -> P({candidate.outcome}) = {p:.4f}")
        stress = side_prob(max(vols.values()) * 1.25 * math.sqrt(tau))
        point = min(probs)
        uncertainty = max(UNCERTAINTY_FLOOR, (max(probs + [stress]) - min(probs + [stress])) / 2)
        calc.append(
            f"Formula: P(S_T > K) = 1 - F((ln(K/S) + sd^2/2) / sd) with F = Student-t(nu={TAIL_NU:g}) and normal, "
            "both scaled to sd, lower probability kept; touch: P = 2 x P(terminal beyond barrier) (reflection)"
        )
        calc.append(
            f"Conservative estimate (lowest across volatility scenarios): P({candidate.outcome}) = {point:.4f}; "
            f"stress (highest vol x1.25) {stress:.4f}; uncertainty +/-{uncertainty:.4f}"
        )

        if tau_hours < 1 and not settled:
            opp_type = NEAR_RESOLUTION_EDGE
        sources = sorted({q.source.split()[0] for q in quotes} | ({"Deribit"} if implied else set()))
        quality = 0.9 if same_source else 0.75 if spec.resolution_source in ("binance", "chainlink", "coinbase") else 0.6
        if spec.window_start is not None:
            window_hours = (spec.settle_time - spec.window_start).total_seconds() / 3600.0
        elif candidate.market.start_date is not None:
            window_hours = (spec.settle_time - candidate.market.start_date).total_seconds() / 3600.0
        else:
            window_hours = None
        elapsed = clamp(1 - tau_hours / window_hours) if window_hours and window_hours > 0 else 0.0
        certainty = 1.0 if settled else clamp(0.5 * elapsed + 0.5 * abs(2 * point - 1))

        move = abs(strike / spot - 1)
        if settled:
            loss.append("The resolution source does not register the touch (different candle data or rule reading)")
        elif spec.kind in ("touch_up", "touch_down"):
            direction = "up" if spec.kind == "touch_up" else "down"
            if side_is_event:
                loss.append(f"{spec.asset.symbol} not moving {move:.2%} {direction} to {strike:,.2f} before {short_utc(spec.settle_time)}")
            else:
                loss.append(f"{spec.asset.symbol} trading {move:.2%} {direction} to {strike:,.2f} at any moment before {short_utc(spec.settle_time)}")
        else:
            loss.append(f"A {move:.2%} move in {spec.asset.symbol} through {strike:,.2f} against the position before {short_utc(spec.settle_time)}")
        loss.append("Wick or outage on the resolution exchange around settlement")
        risks.append(f"Resolution source: {spec.resolution_source}; other venues' prices can differ (basis)")
        if spec.resolution_source == "unknown":
            risks.append("Resolution source not identified in the rules")
        if settled:
            main = f"{spec.asset.symbol} already touched {strike:,.2f} during the period"
        elif spec.kind == "up_down":
            main = f"{spec.asset.symbol} {spot:,.2f} vs window open {strike:,.2f} ({spot / strike - 1:+.2%}) with {humanize_hours(tau_hours)} left"
        else:
            main = f"{spec.asset.symbol} {spot:,.2f} vs {strike:,.2f} ({spot / strike - 1:+.2%}) with {humanize_hours(tau_hours)} left"

        return ProbabilityEstimate(
            outcome_index=idx,
            probability=point,
            engine=ENGINE,
            method=method,
            data_status=OK,
            uncertainty=uncertainty,
            opportunity_type=opp_type,
            evidence=evidence,
            calculation=calc,
            sources=sources,
            source_quality=quality,
            event_certainty=certainty,
            risks=risks,
            loss_scenarios=loss,
            data_age_seconds=max((now - q.observed_at).total_seconds() for q in quotes),
            main_reason=main,
        )


def _p_above(spot: float, strike: float, sd: float, nu: float = TAIL_NU) -> float:
    """P(S_T > K) for a driftless (martingale) price; log returns Student-t
    with ``nu`` degrees of freedom scaled to standard deviation ``sd``."""
    if sd <= 0:
        return 1.0 if spot > strike else 0.0
    x = math.log(strike / spot) + 0.5 * sd * sd
    return 1.0 - scaled_t_cdf(x, sd, nu)


def _p_touch_up(spot: float, barrier: float, sd: float, nu: float = TAIL_NU) -> float:
    if spot >= barrier:
        return 1.0
    return clamp(2.0 * (1.0 - scaled_t_cdf(math.log(barrier / spot), sd, nu)))


def _p_touch_down(spot: float, barrier: float, sd: float, nu: float = TAIL_NU) -> float:
    if spot <= barrier:
        return 1.0
    return clamp(2.0 * scaled_t_cdf(math.log(barrier / spot), sd, nu))
