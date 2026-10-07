"""Routes each candidate to the engines for its category and combines them.

The Polymarket price is never an input. If no engine has data, the result is
DATA UNAVAILABLE with every engine's reason attached.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from ..config import Settings
from ..http import DataUnavailable, HttpClient
from ..market_scanner import FavoriteCandidate
from ..sources.crypto import CryptoData
from ..sources.espn import EspnClient
from ..sources.kalshi import KalshiClient
from ..sources.odds_api import OddsApiClient
from .base import ProbabilityEstimate
from .combine import combine_estimates
from .crypto_engine import CryptoEngine
from .evidence_book import EvidenceBook
from .politics_engine import ManualEngine, PoliticsEngine
from .sports_engine import SportsEngine

log = logging.getLogger(__name__)


class Engine(Protocol):
    name: str

    def applies(self, candidate: FavoriteCandidate) -> bool: ...

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate: ...


class ProbabilityEngine:
    def __init__(self, settings: Settings, engines: list[Engine]):
        self.settings = settings
        self.engines = engines

    @classmethod
    def from_settings(cls, settings: Settings, http: HttpClient | None = None) -> "ProbabilityEngine":
        src = settings.sources
        http = http or HttpClient(
            timeout=src.request_timeout,
            max_retries=1,  # external sources: fail fast, report DATA UNAVAILABLE
            user_agent=src.user_agent,
            rate_limits=src.rate_limits,
        )
        book = EvidenceBook(src.manual_evidence_path)
        kalshi = KalshiClient(http, settings)
        threshold = settings.filters.conflict_threshold
        return cls(
            settings,
            [
                CryptoEngine(CryptoData(http, settings)),
                SportsEngine(EspnClient(http, settings), OddsApiClient(http, settings), conflict_threshold=threshold),
                PoliticsEngine(book, kalshi, conflict_threshold=threshold),
                ManualEngine(book, kalshi),
            ],
        )

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate:
        idx = candidate.outcome_index
        estimates: list[ProbabilityEstimate] = []
        for engine in self.engines:
            if not engine.applies(candidate):
                continue
            try:
                estimate = engine.estimate(candidate, now)
            except DataUnavailable as exc:
                estimate = ProbabilityEstimate.unavailable(idx, engine.name, f"{exc.source}: {exc.reason}")
            except Exception as exc:  # an engine bug must not take down the scan
                log.exception("%s engine failed on %s", engine.name, candidate.key)
                estimate = ProbabilityEstimate.unavailable(idx, engine.name, f"engine error: {exc}")
            estimates.append(estimate)
        if not estimates:
            return ProbabilityEstimate.unavailable(
                idx, "none", f"no probability model or sourced evidence for category '{candidate.category}'"
            )
        return combine_estimates(
            estimates,
            outcome_index=idx,
            engine="+".join(e.engine for e in estimates),
            conflict_threshold=self.settings.filters.conflict_threshold,
        )
