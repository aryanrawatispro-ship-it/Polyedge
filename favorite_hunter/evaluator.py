"""Evaluation pipeline for scanned favorites:

estimate (external data) -> edge (fees included) -> max executable size ->
confidence -> Favorite Edge Score -> filters/status/opportunity type.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .confidence import compute_confidence
from .config import Settings
from .edge_calculator import compute_edge
from .market_scanner import FavoriteCandidate, max_executable_for_edge
from .probability.engine import ProbabilityEngine
from .risk_engine import apply_risk

log = logging.getLogger(__name__)

Scorer = Callable[[FavoriteCandidate, Settings, datetime], Any]


class Evaluator:
    def __init__(self, settings: Settings, engine: ProbabilityEngine, scorer: Scorer | None = None):
        self.settings = settings
        self.engine = engine
        self.scorer = scorer

    @classmethod
    def from_settings(cls, settings: Settings) -> "Evaluator":
        return cls(settings, ProbabilityEngine.from_settings(settings))

    def __call__(self, candidates: list[FavoriteCandidate], now: datetime) -> None:
        for candidate in candidates:
            self.evaluate(candidate, now)

    def evaluate(self, c: FavoriteCandidate, now: datetime) -> None:
        f = self.settings.filters
        estimate = self.engine.estimate(c, now)
        c.estimate = estimate
        q = estimate.probability if estimate.available else None
        if c.entry_price is not None:
            c.edge = compute_edge(
                c.entry_price,
                c.fee_per_share,
                q,
                probability_uncertainty=estimate.uncertainty if q is not None else None,
                lower_bound_z=f.lower_bound_z,
            )
        if q is not None:
            c.max_exec_shares, c.max_exec_usd = max_executable_for_edge(c, q, f.min_edge)
        else:
            c.max_exec_shares = c.max_exec_usd = None
        c.confidence = compute_confidence(c, self.settings, now)
        if self.scorer is not None:
            c.score = self.scorer(c, self.settings, now)
        apply_risk(c, self.settings, now)
