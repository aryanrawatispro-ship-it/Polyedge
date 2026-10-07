"""Opportunity filters, statuses, risk flags and opportunity-type labels.

Statuses:
  TRADE             every filter passed (paper trade is simulated)
  NO EDGE           estimate available but edge/ROI below the minimum
  FILTERED          edge is there but a risk filter failed (reasons listed)
  CONFLICT          independent sources disagree
  DATA UNAVAILABLE  no external evidence; nothing is estimated
"""

from __future__ import annotations

from datetime import datetime

from .config import Settings
from .market_scanner import FavoriteCandidate
from .models import to_float
from .probability.base import (
    CONFLICT,
    DATA_LAG,
    ORDER_BOOK_EDGE,
    PRICE_DISLOCATION,
)

STATUS_TRADE = "TRADE"
STATUS_NO_EDGE = "NO EDGE"
STATUS_FILTERED = "FILTERED"
STATUS_CONFLICT = "CONFLICT"
STATUS_DATA_UNAVAILABLE = "DATA UNAVAILABLE"
STATUS_ORDER = [STATUS_TRADE, STATUS_FILTERED, STATUS_NO_EDGE, STATUS_CONFLICT, STATUS_DATA_UNAVAILABLE]

PRICE_DISLOCATION_DROP = 0.05  # side's price fell this much in the last hour
ORDER_BOOK_GAP = 0.03  # best ask this far below the last trade


def side_one_hour_change(c: FavoriteCandidate) -> float | None:
    change = to_float(c.market.raw.get("oneHourPriceChange"))
    if change is None:
        return None
    return change if c.outcome_index == 0 else -change


def classify_opportunity(c: FavoriteCandidate) -> str | None:
    est = c.estimate
    if est is None or not est.available:
        return None
    if est.opportunity_type == DATA_LAG:
        return DATA_LAG
    change = side_one_hour_change(c)
    if change is not None and change <= -PRICE_DISLOCATION_DROP:
        return PRICE_DISLOCATION
    last = c.book.last_trade_price
    if last is not None and c.best_ask is not None and last - c.best_ask >= ORDER_BOOK_GAP:
        return ORDER_BOOK_EDGE
    return est.opportunity_type


def apply_risk(c: FavoriteCandidate, settings: Settings, now: datetime) -> None:
    f = settings.filters
    flags: list[str] = []
    reasons: list[str] = []
    edge_reasons: list[str] = []

    if not c.fee.known:
        flags.append("fee schedule unavailable: worst-case taker fee assumed")
    hours = c.hours_to_resolution(now)
    if hours is not None and hours < 0:
        flags.append("past the market's scheduled end date")
    if c.market.seconds_delay:
        flags.append(f"{c.market.seconds_delay:g}s order matching delay (in-play)")
    if c.market.neg_risk:
        flags.append("multi-outcome (neg-risk) event")
    status_text = (c.market.uma_resolution_status or "").lower()
    if status_text == "proposed":
        flags.append("resolution proposed on UMA (challenge window open)")
    flags.extend(c.rules.risks)
    flags.extend(c.rules.flags)
    est = c.estimate
    if est is not None:
        flags.extend(est.risks)
    change = side_one_hour_change(c)
    if change is not None and change <= -PRICE_DISLOCATION_DROP:
        flags.append(f"signal: price fell {abs(change) * 100:.1f} points in the last hour")
    last = c.book.last_trade_price
    if last is not None and c.best_ask is not None and last - c.best_ask >= ORDER_BOOK_GAP:
        flags.append(f"signal: best ask {c.best_ask:.3f} is {(last - c.best_ask) * 100:.1f} points below the last trade")
    c.risk_flags = list(dict.fromkeys(flags))

    if est is None or not est.available:
        c.status = STATUS_CONFLICT if est is not None and est.data_status == CONFLICT else STATUS_DATA_UNAVAILABLE
        c.skip_reasons = [est.reason if est is not None and est.reason else "no estimate"]
        c.opportunity_type = None
        return

    edge = c.edge
    assert edge is not None and edge.probability_edge is not None
    if edge.probability_edge < f.min_edge:
        edge_reasons.append(f"edge {edge.probability_edge * 100:+.1f}pt below minimum {f.min_edge * 100:.1f}pt")
    if edge.expected_roi is not None and edge.expected_roi < f.min_roi:
        edge_reasons.append(f"expected ROI {edge.expected_roi:.1%} below minimum {f.min_roi:.1%}")
    if f.require_positive_lower_bound_edge and edge.lower_bound_edge is not None and edge.lower_bound_edge <= 0:
        edge_reasons.append(
            f"edge not positive at estimate - {f.lower_bound_z:g} x uncertainty "
            f"({edge.lower_bound_probability:.3f} vs cost {edge.effective_cost:.3f})"
        )

    if c.ask_depth_usd < f.min_liquidity_usd:
        reasons.append(f"ask depth ${c.ask_depth_usd:,.0f} below ${f.min_liquidity_usd:,.0f}")
    if c.spread is None or c.spread > f.max_spread + 1e-9:
        reasons.append("no two-sided book" if c.spread is None else f"spread {c.spread:.3f} above {f.max_spread:.3f}")
    book_age = c.book.age_seconds(now)
    if book_age is not None and book_age > f.max_book_age_seconds:
        reasons.append(f"order book stale ({book_age:.0f}s old)")
    if est.data_age_seconds is not None and est.data_age_seconds > f.max_source_age_seconds:
        reasons.append(f"external data stale ({est.data_age_seconds:.0f}s old)")
    if c.rules.score < f.min_rule_clarity:
        reasons.append(f"ambiguous resolution rules (clarity {c.rules.score:.0f}/100)")
    if status_text == "disputed":
        reasons.append("resolution disputed on UMA")
    if f.require_accepting_orders and c.market.accepting_orders is False:
        reasons.append("market not accepting orders")
    confidence = getattr(c.confidence, "value", None)
    if confidence is not None and confidence < f.min_confidence:
        reasons.append(f"confidence {confidence:.0f} below {f.min_confidence:.0f}")
    if not edge_reasons and (c.max_exec_usd or 0.0) < settings.paper.min_stake_usd:
        reasons.append(f"only ${c.max_exec_usd or 0:,.2f} executable while keeping the minimum edge")

    c.opportunity_type = classify_opportunity(c)
    if edge_reasons:
        c.status = STATUS_NO_EDGE
        c.skip_reasons = edge_reasons + reasons
    elif reasons:
        c.status = STATUS_FILTERED
        c.skip_reasons = reasons
    else:
        c.status = STATUS_TRADE
        c.skip_reasons = []
