"""Order-book execution simulation.

Buys walk the ask side level by level. Nothing assumes unlimited liquidity at
the best ask: each level only provides the shares actually resting there, and
every share pays the taker fee for the price it fills at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .models import BookLevel, FeeSchedule

SHARE_DECIMALS = 2  # CLOB order sizes are expressed with two decimals
EPS = 1e-9


def floor_to(value: float, decimals: int = SHARE_DECIMALS) -> float:
    factor = 10**decimals
    return math.floor(value * factor + EPS) / factor


@dataclass(frozen=True)
class FillLevel:
    price: float
    shares: float
    fee: float  # USDC

    @property
    def notional(self) -> float:
        return self.price * self.shares


@dataclass
class Fill:
    levels: list[FillLevel] = field(default_factory=list)
    requested_usdc: float | None = None
    requested_shares: float | None = None
    limit_price: float | None = None
    stop_reason: str = "empty_book"
    min_order_size: float | None = None

    @property
    def shares(self) -> float:
        return sum(level.shares for level in self.levels)

    @property
    def notional(self) -> float:
        return sum(level.notional for level in self.levels)

    @property
    def fees(self) -> float:
        return sum(level.fee for level in self.levels)

    @property
    def total_cost(self) -> float:
        return self.notional + self.fees

    @property
    def vwap(self) -> float | None:
        shares = self.shares
        return self.notional / shares if shares > 0 else None

    @property
    def avg_cost_per_share(self) -> float | None:
        """Average all-in cost per share; equals the break-even probability."""
        shares = self.shares
        return self.total_cost / shares if shares > 0 else None

    @property
    def worst_price(self) -> float | None:
        return self.levels[-1].price if self.levels else None

    @property
    def meets_min_size(self) -> bool:
        if self.shares <= 0:
            return False
        return self.min_order_size is None or self.shares + EPS >= self.min_order_size

    @property
    def complete(self) -> bool:
        return self.stop_reason == "filled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "shares": round(self.shares, 4),
            "notional": round(self.notional, 6),
            "fees": round(self.fees, 6),
            "total_cost": round(self.total_cost, 6),
            "vwap": self.vwap,
            "avg_cost_per_share": self.avg_cost_per_share,
            "worst_price": self.worst_price,
            "stop_reason": self.stop_reason,
            "meets_min_size": self.meets_min_size,
            "requested_usdc": self.requested_usdc,
            "requested_shares": self.requested_shares,
            "limit_price": self.limit_price,
            "levels": [
                {"price": lvl.price, "shares": lvl.shares, "fee": round(lvl.fee, 6)} for lvl in self.levels
            ],
        }


def simulate_buy(
    asks: list[BookLevel],
    fee: FeeSchedule,
    *,
    budget_usdc: float | None = None,
    shares: float | None = None,
    limit_price: float | None = None,
    max_cost_per_share: float | None = None,
    min_order_size: float | None = None,
) -> Fill:
    """Simulate a marketable buy against ``asks`` (best/lowest price first).

    ``budget_usdc`` caps total cash out (shares x price + fees); ``shares`` caps
    the share count; ``limit_price`` caps the per-share price; and
    ``max_cost_per_share`` caps price + fee at each level (an edge floor).
    """
    if budget_usdc is None and shares is None:
        raise ValueError("simulate_buy needs budget_usdc or shares")
    fill = Fill(
        requested_usdc=budget_usdc,
        requested_shares=shares,
        limit_price=limit_price,
        min_order_size=min_order_size,
    )
    remaining_budget = budget_usdc
    remaining_shares = shares
    if not asks:
        fill.stop_reason = "empty_book"
        return fill
    for level in asks:
        if limit_price is not None and level.price > limit_price + EPS:
            fill.stop_reason = "limit_price"
            return fill
        fee_per_share = fee.fee_per_share(level.price)
        cost_per_share = level.price + fee_per_share
        if max_cost_per_share is not None and cost_per_share > max_cost_per_share + EPS:
            fill.stop_reason = "edge_floor"
            return fill
        take = level.size
        if remaining_shares is not None:
            take = min(take, remaining_shares)
        if remaining_budget is not None:
            take = min(take, remaining_budget / cost_per_share)
        take = floor_to(take)
        if take <= 0:
            fill.stop_reason = "filled"
            return fill
        fill.levels.append(FillLevel(price=level.price, shares=take, fee=take * fee_per_share))
        if remaining_shares is not None:
            remaining_shares -= take
            if remaining_shares <= EPS:
                fill.stop_reason = "filled"
                return fill
        if remaining_budget is not None:
            remaining_budget -= take * cost_per_share
            # Less than one hundredth of a share left to buy.
            if remaining_budget < cost_per_share * 10**-SHARE_DECIMALS:
                fill.stop_reason = "filled"
                return fill
    fill.stop_reason = "book_exhausted"
    return fill


def max_executable(
    asks: list[BookLevel],
    fee: FeeSchedule,
    *,
    max_cost_per_share: float,
) -> tuple[float, float]:
    """Shares and USDC (fees included) buyable while every marginal share costs
    at most ``max_cost_per_share`` (i.e. keeps at least the required edge)."""
    shares = 0.0
    usdc = 0.0
    for level in asks:
        cost = level.price + fee.fee_per_share(level.price)
        if cost > max_cost_per_share + EPS:
            break
        shares += level.size
        usdc += level.size * cost
    return shares, usdc


def depth_within(levels: list[BookLevel], reference: float | None, window: float, *, side: str) -> float:
    """USDC notional resting within ``window`` of ``reference`` on one side."""
    if reference is None:
        return 0.0
    total = 0.0
    for level in levels:
        if side == "ask" and level.price > reference + window + EPS:
            break
        if side == "bid" and level.price < reference - window - EPS:
            break
        total += level.notional
    return total
