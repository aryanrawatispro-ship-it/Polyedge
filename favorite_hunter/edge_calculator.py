"""Expected-value math for buying a binary outcome share.

A share pays $1 if the outcome happens and $0 otherwise. With an all-in cost
``c`` per share (executable price + taker fee) and an estimated probability
``q``:

* profit if correct  = 1 - c
* loss if wrong      = c
* break-even prob    = c
* edge               = q - c
* EV per share       = q * 1 - c
* ROI                = (q - c) / c
* full Kelly stake   = (q - c) / (1 - c) of bankroll

The strategy optimises EV and risk-adjusted return, not win rate: at c = 0.90
one loss erases nine wins.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EdgeMetrics:
    purchase_price: float  # executable VWAP per share (ex-fee)
    fee_per_share: float
    effective_cost: float  # purchase_price + fee_per_share
    payout_if_correct: float
    profit_if_correct: float
    loss_if_wrong: float
    break_even_probability: float
    risk_reward_ratio: float  # dollars risked per dollar of potential profit
    estimated_probability: float | None
    probability_edge: float | None  # q - c (after fees)
    gross_edge: float | None  # q - price (before fees)
    ev_per_share: float | None
    expected_roi: float | None
    kelly_fraction: float | None
    sharpe_per_bet: float | None  # EV / stdev of one bet's payoff
    lower_bound_probability: float | None
    lower_bound_edge: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_edge(
    purchase_price: float,
    fee_per_share: float,
    estimated_probability: float | None,
    *,
    probability_uncertainty: float | None = None,
    lower_bound_z: float = 1.0,
) -> EdgeMetrics:
    if not 0 < purchase_price < 1:
        raise ValueError(f"purchase price must be in (0, 1), got {purchase_price}")
    cost = purchase_price + fee_per_share
    profit = 1.0 - cost
    risk_reward = cost / profit if profit > 0 else math.inf

    q = estimated_probability
    if q is None:
        return EdgeMetrics(
            purchase_price=purchase_price,
            fee_per_share=fee_per_share,
            effective_cost=cost,
            payout_if_correct=1.0,
            profit_if_correct=profit,
            loss_if_wrong=cost,
            break_even_probability=cost,
            risk_reward_ratio=risk_reward,
            estimated_probability=None,
            probability_edge=None,
            gross_edge=None,
            ev_per_share=None,
            expected_roi=None,
            kelly_fraction=None,
            sharpe_per_bet=None,
            lower_bound_probability=None,
            lower_bound_edge=None,
        )
    if not 0 <= q <= 1:
        raise ValueError(f"estimated probability must be in [0, 1], got {q}")

    edge = q - cost
    ev = q * 1.0 - cost
    roi = ev / cost
    kelly = edge / profit if profit > 0 else 0.0
    stdev = math.sqrt(q * (1.0 - q))
    sharpe = ev / stdev if stdev > 0 else (math.inf if ev > 0 else 0.0)
    lower_q = None
    lower_edge = None
    if probability_uncertainty is not None:
        lower_q = max(0.0, q - lower_bound_z * probability_uncertainty)
        lower_edge = lower_q - cost
    return EdgeMetrics(
        purchase_price=purchase_price,
        fee_per_share=fee_per_share,
        effective_cost=cost,
        payout_if_correct=1.0,
        profit_if_correct=profit,
        loss_if_wrong=cost,
        break_even_probability=cost,
        risk_reward_ratio=risk_reward,
        estimated_probability=q,
        probability_edge=edge,
        gross_edge=q - purchase_price,
        ev_per_share=ev,
        expected_roi=roi,
        kelly_fraction=max(kelly, 0.0),
        sharpe_per_bet=sharpe,
        lower_bound_probability=lower_q,
        lower_bound_edge=lower_edge,
    )


def wins_to_recover_one_loss(cost: float) -> float:
    """How many winning bets at this cost one losing bet erases (c / (1 - c))."""
    return cost / (1.0 - cost) if cost < 1 else math.inf
