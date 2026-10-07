"""Confidence score (0-100): how much to trust an opportunity's edge.

It is NOT a probability. A 92/100 confidence says the evidence is strong,
fresh, independent, clearly tied to the resolution rules and executable; the
probability itself comes only from the estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .market_scanner import FavoriteCandidate
from .probability.stats import clamp

TIME_BUCKET_FACTOR = {"<1h": 1.0, "1-6h": 0.85, "6-24h": 0.65, "1-3d": 0.45, "3d+": 0.25, "past_end": 0.5, "unknown": 0.3}
SOURCE_COUNT_FACTOR = {0: 0.0, 1: 0.4, 2: 0.75}


@dataclass
class ConfidenceScore:
    value: float
    factors: dict[str, float]
    weights: dict[str, float]
    penalties: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 1),
            "factors": {k: round(v, 3) for k, v in self.factors.items()},
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
            "penalties": self.penalties,
        }


def liquidity_factor(depth_usd: float | None) -> float:
    """$100 -> 0.5, $1k -> 0.75, $10k+ -> 1.0 (log scale)."""
    if not depth_usd or depth_usd <= 1:
        return 0.0
    return clamp(math.log10(depth_usd) / 4.0)


def spread_factor(spread: float | None, scale: float = 0.05) -> float:
    if spread is None:
        return 0.0
    return clamp(1.0 - spread / scale)


def compute_confidence(c: FavoriteCandidate, settings: Settings, now: datetime) -> ConfidenceScore:
    weights_cfg = settings.confidence_weights.model_dump()
    total_w = sum(weights_cfg.values()) or 1.0
    weights = {k: v / total_w for k, v in weights_cfg.items()}
    est = c.estimate
    if est is None or not est.available or c.edge is None or c.edge.probability_edge is None:
        return ConfidenceScore(0.0, {k: 0.0 for k in weights}, weights, ["no probability estimate"])

    edge = c.edge.probability_edge
    uncertainty = max(est.uncertainty or 0.02, 0.005)
    stake = settings.paper.fixed_stake_usd
    factors = {
        "model_edge": clamp((edge / uncertainty) / 3.0) if edge > 0 else 0.0,
        "source_quality": clamp(est.source_quality),
        "independent_sources": SOURCE_COUNT_FACTOR.get(len(est.sources), 1.0),
        "time_remaining": TIME_BUCKET_FACTOR.get(c.time_bucket, 0.3),
        "event_uncertainty": clamp(est.event_certainty),
        "liquidity": liquidity_factor(c.ask_depth_usd),
        "spread": spread_factor(c.spread),
        "depth": clamp((c.max_exec_usd or 0.0) / (2 * stake)) if stake > 0 else 0.0,
        "rule_clarity": clamp(c.rules.score / 100.0),
    }
    value = 100.0 * sum(weights[k] * factors.get(k, 0.0) for k in weights)

    penalties = []
    max_age = settings.filters.max_source_age_seconds
    if est.data_age_seconds is not None and est.data_age_seconds > 0.5 * max_age:
        value -= 5
        penalties.append(f"-5 evidence is {est.data_age_seconds:.0f}s old")
    if not c.fee.known:
        value -= 5
        penalties.append("-5 fee schedule assumed")
    hours = c.hours_to_resolution(now)
    if hours is not None and hours < 0:
        value -= 10
        penalties.append("-10 past the market's scheduled end")
    if c.rules.ambiguous:
        value -= 10
        penalties.append("-10 ambiguous resolution rules")
    return ConfidenceScore(clamp(value, 0.0, 100.0), factors, weights, penalties)
