"""Performance analytics for favorites.

Answers: which entry range, category, time bucket, edge, confidence,
liquidity, volume and strategy actually make money once the asymmetric payoff
is accounted for. A favorite bought at 0.95 must win more than 95% of the time
(plus fees) to break even; a high win rate alone means nothing.

Works on any list of settled bets: model paper trades, baseline (blind
favorite) observations, or historical backtest observations.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .paper_trader import ENTRY_BUCKET_ORDER, entry_bucket
from .timeutil import TIME_BUCKET_ORDER, parse_dt

MIN_SAMPLE = 30  # below this a verdict is reported as INSUFFICIENT DATA

WINNING = "WINNING STRATEGY"
LOSING = "LOSING STRATEGY"
BREAK_EVEN = "BREAK-EVEN"
INSUFFICIENT = "INSUFFICIENT DATA"


@dataclass
class Bet:
    kind: str
    category: str | None
    entry_price: float  # VWAP per share, ex-fee
    cost_per_share: float  # all-in (price + fee) = break-even probability
    shares: float
    amount: float  # total cost
    payout_per_share: float  # 1 win, 0 loss, 0.5 split
    pnl: float
    resolved_at: datetime | None = None
    opened_at: datetime | None = None
    est_prob: float | None = None
    edge: float | None = None
    confidence: float | None = None
    score: float | None = None
    strategy: str | None = None
    time_bucket: str | None = None
    liquidity: float | None = None
    volume: float | None = None
    label: str | None = None

    @property
    def won(self) -> bool:
        return self.payout_per_share >= 0.999

    @property
    def lost(self) -> bool:
        return self.payout_per_share <= 0.001

    @property
    def entry_bucket(self) -> str:
        return entry_bucket(self.entry_price)


def bets_from_trades(rows: Iterable[dict[str, Any]]) -> list[Bet]:
    bets = []
    for r in rows:
        if r.get("status") in (None, "open") or r.get("pnl") is None:
            continue
        bets.append(
            Bet(
                kind=r["kind"],
                category=r.get("category"),
                entry_price=r["entry_price"],
                cost_per_share=r["avg_cost"],
                shares=r["shares"],
                amount=r["amount_invested"],
                payout_per_share=r["payout_per_share"],
                pnl=r["pnl"],
                resolved_at=parse_dt(r.get("resolved_at")),
                opened_at=parse_dt(r.get("opened_at")),
                est_prob=r.get("est_prob"),
                edge=r.get("edge"),
                confidence=r.get("confidence"),
                score=r.get("score"),
                strategy=r.get("opportunity_type") or r.get("strategy"),
                time_bucket=r.get("time_bucket"),
                liquidity=r.get("liquidity"),
                volume=r.get("volume"),
                label=r.get("question"),
            )
        )
    return bets


def bets_from_backtest(rows: Iterable[dict[str, Any]], stake: float = 100.0) -> list[Bet]:
    bets = []
    for r in rows:
        cost = r["price"] + (r.get("fee_per_share") or 0.0)
        shares = stake / cost
        payout = r["payout_per_share"]
        bets.append(
            Bet(
                kind="backtest",
                category=r.get("category"),
                entry_price=r["price"],
                cost_per_share=cost,
                shares=shares,
                amount=stake,
                payout_per_share=payout,
                pnl=shares * payout - stake,
                resolved_at=parse_dt(r.get("end_date")),
                opened_at=parse_dt(r.get("observed_at")),
                strategy="blind_favorite",
                time_bucket=r.get("time_bucket"),
                liquidity=r.get("liquidity"),
                volume=r.get("volume"),
                label=r.get("question"),
            )
        )
    return bets


# ------------------------------------------------------------------ statistics


def wilson_interval(successes: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Stats:
    group: str
    n_bets: int
    wins: int
    losses: int
    splits: int
    win_rate: float | None
    win_rate_ci_low: float | None
    win_rate_ci_high: float | None
    avg_entry_price: float | None
    break_even_win_rate: float | None  # average all-in cost per share
    expected_win_rate: float | None  # average model probability (model bets only)
    edge_vs_break_even: float | None  # actual win rate - break-even win rate
    total_invested: float
    total_pnl: float
    roi: float | None
    max_drawdown: float
    largest_loss: float
    longest_losing_streak: int
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    wins_needed_per_loss: float | None
    brier_score: float | None
    verdict: str
    significant: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(bets: list[Bet], group: str = "all", *, min_sample: int = MIN_SAMPLE) -> Stats:
    n = len(bets)
    ordered = sorted(bets, key=lambda b: b.resolved_at.timestamp() if b.resolved_at else 0.0)
    wins = sum(1 for b in bets if b.won)
    losses = sum(1 for b in bets if b.lost)
    splits = n - wins - losses
    invested = sum(b.amount for b in bets)
    pnl = sum(b.pnl for b in bets)
    win_rate = wins / n if n else None
    ci_low, ci_high = wilson_interval(wins, n) if n else (None, None)
    break_even = sum(b.cost_per_share for b in bets) / n if n else None
    est = [b.est_prob for b in bets if b.est_prob is not None]
    expected = sum(est) / len(est) if est else None
    win_pnls = [b.pnl for b in bets if b.pnl > 0]
    loss_pnls = [b.pnl for b in bets if b.pnl < 0]
    avg_cost = break_even
    brier = None
    if est and len(est) == n:
        brier = sum((b.est_prob - (1.0 if b.won else 0.0 if b.lost else 0.5)) ** 2 for b in bets) / n  # type: ignore[operator]

    roi = pnl / invested if invested > 0 else None
    significant = False
    if n < min_sample:
        verdict = INSUFFICIENT
        note = f"only {n} settled bets; need at least {min_sample}"
    else:
        if roi is not None and roi > 0:
            verdict = WINNING
            significant = ci_low is not None and break_even is not None and ci_low > break_even
        elif roi is not None and roi < 0:
            verdict = LOSING
            significant = ci_high is not None and break_even is not None and ci_high < break_even
        else:
            verdict = BREAK_EVEN
        note = (
            "statistically significant at 95%"
            if significant
            else "not yet statistically significant (95% win-rate interval includes break-even)"
        )
    return Stats(
        group=group,
        n_bets=n,
        wins=wins,
        losses=losses,
        splits=splits,
        win_rate=win_rate,
        win_rate_ci_low=ci_low,
        win_rate_ci_high=ci_high,
        avg_entry_price=sum(b.entry_price for b in bets) / n if n else None,
        break_even_win_rate=break_even,
        expected_win_rate=expected,
        edge_vs_break_even=None if win_rate is None or break_even is None else win_rate - break_even,
        total_invested=invested,
        total_pnl=pnl,
        roi=roi,
        max_drawdown=max_drawdown([b.pnl for b in ordered]),
        largest_loss=min((b.pnl for b in bets if b.pnl < 0), default=0.0),
        longest_losing_streak=longest_losing_streak(ordered),
        avg_win=sum(win_pnls) / len(win_pnls) if win_pnls else None,
        avg_loss=sum(loss_pnls) / len(loss_pnls) if loss_pnls else None,
        profit_factor=(sum(win_pnls) / -sum(loss_pnls)) if loss_pnls else None,
        wins_needed_per_loss=(avg_cost / (1 - avg_cost)) if avg_cost is not None and avg_cost < 1 else None,
        brier_score=brier,
        verdict=verdict,
        significant=significant,
        note=note,
    )


def max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough fall of cumulative PnL (a positive number)."""
    peak = 0.0
    cumulative = 0.0
    worst = 0.0
    for value in pnls:
        cumulative += value
        peak = max(peak, cumulative)
        worst = max(worst, peak - cumulative)
    return worst


def longest_losing_streak(bets: list[Bet]) -> int:
    longest = current = 0
    for bet in bets:
        if bet.pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


# --------------------------------------------------------------------- grouping


def _edge_bucket(b: Bet) -> str:
    if b.edge is None:
        return "no estimate"
    pts = b.edge * 100
    for limit, label in ((2, "<2pt"), (4, "2-4pt"), (6, "4-6pt"), (10, "6-10pt")):
        if pts < limit:
            return label
    return "10pt+"


def _confidence_bucket(b: Bet) -> str:
    if b.confidence is None:
        return "no estimate"
    for limit, label in ((50, "<50"), (60, "50-60"), (70, "60-70"), (80, "70-80"), (90, "80-90")):
        if b.confidence < limit:
            return label
    return "90+"


def _money_bucket(value: float | None, edges: tuple[tuple[float, str], ...], top: str) -> str:
    if value is None:
        return "unknown"
    for limit, label in edges:
        if value < limit:
            return label
    return top


DIMENSIONS: dict[str, tuple[Callable[[Bet], str], list[str] | None]] = {
    "entry_range": (lambda b: b.entry_bucket, ENTRY_BUCKET_ORDER),
    "category": (lambda b: b.category or "unknown", None),
    "time_remaining": (lambda b: b.time_bucket or "unknown", TIME_BUCKET_ORDER),
    "estimated_edge": (_edge_bucket, ["<2pt", "2-4pt", "4-6pt", "6-10pt", "10pt+", "no estimate"]),
    "confidence": (_confidence_bucket, ["<50", "50-60", "60-70", "70-80", "80-90", "90+", "no estimate"]),
    "liquidity": (
        lambda b: _money_bucket(b.liquidity, ((1_000, "<$1k"), (10_000, "$1k-10k"), (100_000, "$10k-100k")), "$100k+"),
        ["<$1k", "$1k-10k", "$10k-100k", "$100k+", "unknown"],
    ),
    "volume": (
        lambda b: _money_bucket(b.volume, ((10_000, "<$10k"), (100_000, "$10k-100k"), (1_000_000, "$100k-1M")), "$1M+"),
        ["<$10k", "$10k-100k", "$100k-1M", "$1M+", "unknown"],
    ),
    "strategy": (lambda b: b.strategy or "unknown", None),
}


def breakdown(bets: list[Bet], dimension: str, *, min_sample: int = MIN_SAMPLE) -> list[Stats]:
    key_fn, order = DIMENSIONS[dimension]
    groups: dict[str, list[Bet]] = {}
    for bet in bets:
        groups.setdefault(key_fn(bet), []).append(bet)
    labels = [g for g in (order or []) if g in groups] + sorted(g for g in groups if not order or g not in order)
    return [summarize(groups[label], label, min_sample=min_sample) for label in labels]


EXTREME_THRESHOLDS = (0.90, 0.95, 0.97)


def extreme_favorites(bets: list[Bet], *, min_sample: int = MIN_SAMPLE) -> list[Stats]:
    """90c+, 95c+ and 97c+ views: is the realised win rate high enough?"""
    return [
        summarize([b for b in bets if b.entry_price >= threshold - 1e-9], f"{int(round(threshold * 100))}c+", min_sample=min_sample)
        for threshold in EXTREME_THRESHOLDS
    ]


@dataclass
class CalibrationBin:
    label: str
    n: int
    predicted: float | None
    actual: float | None
    ci_low: float | None
    ci_high: float | None


def calibration(bets: list[Bet], *, use: str = "model") -> list[CalibrationBin]:
    """Predicted vs realised frequency.

    ``use="model"`` bins by the model's probability; ``use="price"`` bins by the
    all-in cost, i.e. the market's implied probability (favorite-longshot bias).
    """
    edges = [0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99, 1.001]
    bins: list[CalibrationBin] = []
    for low, high in zip(edges, edges[1:]):
        members = []
        for b in bets:
            x = b.est_prob if use == "model" else b.cost_per_share
            if x is not None and low <= x < high:
                members.append((x, b))
        n = len(members)
        wins = sum(1 for _, b in members if b.won)
        ci = wilson_interval(wins, n) if n else (None, None)
        bins.append(
            CalibrationBin(
                label=f"{low:.2f}-{min(high, 1.0):.2f}",
                n=n,
                predicted=sum(x for x, _ in members) / n if n else None,
                actual=wins / n if n else None,
                ci_low=ci[0],
                ci_high=ci[1],
            )
        )
    return bins


@dataclass
class Report:
    dataset: str
    overall: Stats
    extreme: list[Stats]
    breakdowns: dict[str, list[Stats]] = field(default_factory=dict)
    calibration_model: list[CalibrationBin] = field(default_factory=list)
    calibration_price: list[CalibrationBin] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "overall": self.overall.to_dict(),
            "extreme": [s.to_dict() for s in self.extreme],
            "breakdowns": {k: [s.to_dict() for s in v] for k, v in self.breakdowns.items()},
            "calibration_model": [asdict(b) for b in self.calibration_model],
            "calibration_price": [asdict(b) for b in self.calibration_price],
            "equity_curve": self.equity_curve,
        }


def build_report(bets: list[Bet], dataset: str, *, min_sample: int = MIN_SAMPLE) -> Report:
    ordered = sorted((b for b in bets if b.resolved_at), key=lambda b: b.resolved_at)  # type: ignore[arg-type, return-value]
    curve = []
    cumulative = 0.0
    for bet in ordered:
        cumulative += bet.pnl
        curve.append((bet.resolved_at.isoformat(), round(cumulative, 4)))  # type: ignore[union-attr]
    return Report(
        dataset=dataset,
        overall=summarize(bets, "all", min_sample=min_sample),
        extreme=extreme_favorites(bets, min_sample=min_sample),
        breakdowns={dim: breakdown(bets, dim, min_sample=min_sample) for dim in DIMENSIONS},
        calibration_model=calibration(bets, use="model") if any(b.est_prob is not None for b in bets) else [],
        calibration_price=calibration(bets, use="price"),
        equity_curve=curve,
    )
