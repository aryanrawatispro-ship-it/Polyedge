"""FAVORITE EDGE SCORE (0-100): how attractive a favorite's mispricing is.

Default weights (configurable under ``score_weights``):
  30% probability edge, 20% source reliability, 15% time remaining,
  15% liquidity, 10% spread, 10% event certainty.

Each component is scored 0-100 and the weighted average is the score.
Markets without a probability estimate get no score (DATA UNAVAILABLE).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .confidence import SOURCE_COUNT_FACTOR, TIME_BUCKET_FACTOR, liquidity_factor, spread_factor
from .config import Settings
from .market_scanner import FavoriteCandidate
from .probability.stats import clamp


@dataclass
class FavoriteEdgeScore:
    value: float
    components: dict[str, float]  # each 0-100
    weights: dict[str, float]  # normalised to sum to 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "weights": {k: round(v, 3) for k, v in self.weights.items()},
        }


def score_components(c: FavoriteCandidate, edge_full_marks: float) -> dict[str, float] | None:
    est = c.estimate
    if est is None or not est.available or c.edge is None or c.edge.probability_edge is None:
        return None
    edge = c.edge.probability_edge
    n_sources = len(est.sources)
    return {
        "probability_edge": 100.0 * clamp(edge / edge_full_marks) if edge > 0 else 0.0,
        "source_reliability": 100.0 * (0.75 * clamp(est.source_quality) + 0.25 * SOURCE_COUNT_FACTOR.get(n_sources, 1.0)),
        "time_remaining": 100.0 * TIME_BUCKET_FACTOR.get(c.time_bucket, 0.3),
        "liquidity": 100.0 * liquidity_factor(c.ask_depth_usd),
        "spread": 100.0 * spread_factor(c.spread),
        "event_certainty": 100.0 * clamp(est.event_certainty),
    }


def compute_favorite_edge_score(c: FavoriteCandidate, settings: Settings, now: datetime) -> FavoriteEdgeScore | None:
    components = score_components(c, settings.scoring.edge_full_marks)
    if components is None:
        return None
    raw_weights = settings.score_weights.model_dump()
    total = sum(raw_weights.values()) or 1.0
    weights = {k: v / total for k, v in raw_weights.items()}
    value = sum(weights[k] * components.get(k, 0.0) for k in weights)
    return FavoriteEdgeScore(clamp(value, 0.0, 100.0), components, weights)
