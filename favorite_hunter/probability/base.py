"""Shared types for probability estimation.

An estimate is only ever built from external evidence. When the evidence a
model needs is missing, the estimate carries ``probability=None`` and
``data_status=DATA_UNAVAILABLE`` with the reason; nothing is filled in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..timeutil import iso

OK = "OK"
DATA_UNAVAILABLE = "DATA UNAVAILABLE"
CONFLICT = "CONFLICTING SOURCES"
NOT_APPLICABLE = "NOT APPLICABLE"

# Opportunity types: why an edge exists.
LIVE_EVENT_EDGE = "LIVE_EVENT_EDGE"
EXTERNAL_ODDS_EDGE = "EXTERNAL_ODDS_EDGE"
NEAR_RESOLUTION_EDGE = "NEAR_RESOLUTION_EDGE"
DATA_LAG = "DATA_LAG"
THRESHOLD_EDGE = "THRESHOLD_EDGE"
POLLING_EDGE = "POLLING_EDGE"
PRICE_DISLOCATION = "PRICE_DISLOCATION"
ORDER_BOOK_EDGE = "ORDER_BOOK_EDGE"
OPPORTUNITY_TYPES = (
    LIVE_EVENT_EDGE, EXTERNAL_ODDS_EDGE, NEAR_RESOLUTION_EDGE, DATA_LAG,
    THRESHOLD_EDGE, POLLING_EDGE, PRICE_DISLOCATION, ORDER_BOOK_EDGE,
)


@dataclass
class Evidence:
    source: str  # e.g. "Binance BTCUSDT spot"
    description: str  # human-readable statement of what was observed
    value: Any = None
    url: str | None = None
    observed_at: datetime | None = None
    quality: float = 0.5  # 0-1 reliability of this source

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "description": self.description,
            "value": self.value,
            "url": self.url,
            "observed_at": iso(self.observed_at),
            "quality": self.quality,
        }


@dataclass
class ProbabilityEstimate:
    outcome_index: int
    probability: float | None  # P(this outcome resolves as the winner)
    engine: str
    method: str
    data_status: str = OK
    uncertainty: float | None = None  # 1-sigma uncertainty of the probability itself
    opportunity_type: str | None = None
    evidence: list[Evidence] = field(default_factory=list)
    calculation: list[str] = field(default_factory=list)  # auditable step-by-step
    sources: list[str] = field(default_factory=list)  # independent source names
    source_quality: float = 0.0  # 0-1
    event_certainty: float = 0.0  # 0-1: how much of the outcome uncertainty is already gone
    risks: list[str] = field(default_factory=list)
    loss_scenarios: list[str] = field(default_factory=list)
    data_age_seconds: float | None = None
    reason: str | None = None  # why data is unavailable / conflicting
    main_reason: str | None = None  # one-line summary for alerts
    components: list["ProbabilityEstimate"] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.probability is not None and self.data_status == OK

    @classmethod
    def unavailable(cls, outcome_index: int, engine: str, reason: str, *, method: str = "-") -> "ProbabilityEstimate":
        return cls(
            outcome_index=outcome_index,
            probability=None,
            engine=engine,
            method=method,
            data_status=DATA_UNAVAILABLE,
            reason=reason,
        )

    def complement(self) -> "ProbabilityEstimate":
        """The same estimate expressed for the other outcome of a binary market."""
        prob = None if self.probability is None else 1.0 - self.probability
        clone = ProbabilityEstimate(**{**self.__dict__})
        clone.outcome_index = 1 - self.outcome_index
        clone.probability = prob
        clone.calculation = [*self.calculation, f"other outcome: 1 - {self.probability:.4f} = {prob:.4f}"] if prob is not None else list(self.calculation)
        clone.components = [c.complement() for c in self.components]
        return clone

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_index": self.outcome_index,
            "probability": self.probability,
            "engine": self.engine,
            "method": self.method,
            "data_status": self.data_status,
            "uncertainty": self.uncertainty,
            "opportunity_type": self.opportunity_type,
            "evidence": [e.to_dict() for e in self.evidence],
            "calculation": self.calculation,
            "sources": self.sources,
            "source_quality": self.source_quality,
            "event_certainty": self.event_certainty,
            "risks": self.risks,
            "loss_scenarios": self.loss_scenarios,
            "data_age_seconds": self.data_age_seconds,
            "reason": self.reason,
            "main_reason": self.main_reason,
            "components": [c.to_dict() for c in self.components],
        }
