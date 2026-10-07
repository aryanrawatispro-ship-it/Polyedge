"""Politics / elections and other manually-sourced markets.

Sources, all explicit and auditable:
* polls / election-model forecasts / reputable reporting: probability entries
  in ``manual_evidence.yaml`` (with source, URL, timestamp, expiry)
* live vote counts: a vote-count model on entries with ``vote_count``
* other prediction markets: Kalshi quotes for markets mapped in the file

Without a configured source the result is DATA UNAVAILABLE.
"""

from __future__ import annotations

from datetime import datetime

from ..categories import POLITICS
from ..http import DataUnavailable
from ..market_scanner import FavoriteCandidate
from ..sources.kalshi import KalshiClient
from .base import (
    EXTERNAL_ODDS_EDGE,
    LIVE_EVENT_EDGE,
    OK,
    POLLING_EDGE,
    Evidence,
    ProbabilityEstimate,
)
from .combine import combine_estimates
from .evidence_book import EvidenceBook, EvidenceEntry
from .stats import clamp, norm_cdf

DEFAULT_REMAINING_SHARE_SD = 0.05


def vote_count_probability(
    leader_votes: float,
    trailer_votes: float,
    pct_reporting: float,
    remaining_leader_share: float,
    remaining_share_sd: float,
) -> tuple[float, list[str]]:
    """P(current leader finishes ahead) in a two-way count.

    Remaining votes R = counted x (1 - pct) / pct. The leader stays ahead iff
    their share s of the remaining two-way vote exceeds
    s* = (trailer - leader + R) / (2R), with s ~ Normal(mu, sd).
    """
    counted = leader_votes + trailer_votes
    calc = [f"Counted two-way vote: leader {leader_votes:,.0f}, trailer {trailer_votes:,.0f} ({pct_reporting:.1%} reporting)"]
    if counted <= 0 or not 0 < pct_reporting <= 1:
        raise ValueError("invalid vote counts")
    remaining = counted * (1 - pct_reporting) / pct_reporting
    lead = leader_votes - trailer_votes
    calc.append(f"Estimated remaining votes R = {counted:,.0f} x (1 - {pct_reporting:.3f}) / {pct_reporting:.3f} = {remaining:,.0f}")
    if remaining <= 0:
        calc.append("No votes remaining: leader wins if ahead")
        return (1.0 if lead > 0 else 0.0 if lead < 0 else 0.5), calc
    threshold = (trailer_votes - leader_votes + remaining) / (2 * remaining)
    calc.append(f"Leader needs share s > s* = (T - L + R) / 2R = {threshold:.4f} of remaining votes")
    if remaining_share_sd <= 0:
        p = 1.0 if remaining_leader_share > threshold else 0.0
    else:
        p = 1.0 - norm_cdf((threshold - remaining_leader_share) / remaining_share_sd)
    calc.append(f"s ~ Normal({remaining_leader_share:.4f}, {remaining_share_sd:.4f}) -> P(leader wins) = {p:.6f}")
    return clamp(p), calc


class ManualEvidenceMixin:
    book: EvidenceBook
    kalshi: KalshiClient | None

    def _from_entries(self, candidate: FavoriteCandidate, entries: list[EvidenceEntry], now: datetime, engine: str) -> list[ProbabilityEstimate]:
        estimates = []
        for entry in entries:
            try:
                if entry.vote_count:
                    estimates.append(self._vote_estimate(candidate, entry, now, engine))
                elif entry.kalshi_ticker:
                    estimates.append(self._kalshi_estimate(candidate, entry, now, engine))
                elif entry.probability is not None:
                    estimates.append(self._probability_estimate(candidate, entry, now, engine))
            except (DataUnavailable, ValueError) as exc:
                reason = exc.reason if isinstance(exc, DataUnavailable) else str(exc)
                estimates.append(ProbabilityEstimate.unavailable(candidate.outcome_index, engine, f"{entry.source}: {reason}"))
        return estimates

    def _side(self, candidate: FavoriteCandidate, outcome: str | None, p_outcome: float) -> float:
        """Map a probability stated for ``outcome`` onto the candidate's outcome."""
        if outcome is None:
            raise ValueError("entry has no 'outcome' to anchor the probability")
        labels = [o.strip().lower() for o in candidate.market.outcomes]
        if outcome.strip().lower() not in labels:
            raise ValueError(f"outcome '{outcome}' is not one of {candidate.market.outcomes}")
        same = outcome.strip().lower() == candidate.outcome.strip().lower()
        return p_outcome if same else 1.0 - p_outcome

    def _probability_estimate(self, candidate: FavoriteCandidate, entry: EvidenceEntry, now: datetime, engine: str) -> ProbabilityEstimate:
        p = self._side(candidate, entry.outcome, entry.probability)  # type: ignore[arg-type]
        age = (now - entry.as_of).total_seconds()
        return ProbabilityEstimate(
            outcome_index=candidate.outcome_index,
            probability=p,
            engine=engine,
            method=f"sourced estimate: {entry.source}",
            data_status=OK,
            uncertainty=entry.uncertainty if entry.uncertainty is not None else 0.03,
            opportunity_type=entry.type or POLLING_EDGE,
            evidence=[Evidence(entry.source, entry.note or f"P({entry.outcome}) = {entry.probability:.3f}", entry.probability, entry.url, entry.as_of, quality=entry.quality)],
            calculation=[
                f"{entry.source} (as of {entry.as_of.isoformat()}): P({entry.outcome}) = {entry.probability:.4f}"
                + ("" if entry.uncertainty is not None else " (no uncertainty given: +/-0.03 used)"),
                f"P({candidate.outcome}) = {p:.4f}",
            ],
            sources=[entry.source],
            source_quality=entry.quality,
            event_certainty=abs(2 * p - 1) * 0.5,
            risks=["Manually entered evidence: verify the source is current"],
            loss_scenarios=["Polling/model error; late-breaking news"],
            data_age_seconds=age,
            main_reason=f"{entry.source}: {entry.probability:.0%} for {entry.outcome}",
        )

    def _vote_estimate(self, candidate: FavoriteCandidate, entry: EvidenceEntry, now: datetime, engine: str) -> ProbabilityEstimate:
        vc = entry.vote_count or {}
        leader_votes = float(vc["leader_votes"])
        trailer_votes = float(vc["trailer_votes"])
        pct = float(vc["pct_reporting"])
        counted_share = leader_votes / (leader_votes + trailer_votes)
        assumptions = []
        mu = vc.get("remaining_leader_share")
        if mu is None:
            mu = counted_share
            assumptions.append("remaining votes assumed to split like the counted votes (no remaining_leader_share given)")
        sd = vc.get("remaining_share_sd")
        if sd is None:
            sd = DEFAULT_REMAINING_SHARE_SD
            assumptions.append(f"remaining-share uncertainty assumed {DEFAULT_REMAINING_SHARE_SD} (no remaining_share_sd given)")
        p_leader, calc = vote_count_probability(leader_votes, trailer_votes, pct, float(mu), float(sd))
        # Sensitivity to the reporting estimate: +/- 3 points of turnout.
        variants = [
            vote_count_probability(leader_votes, trailer_votes, clamp(pct + d, 0.01, 1.0), float(mu), float(sd))[0]
            for d in (-0.03, 0.03)
        ]
        p = self._side(candidate, vc.get("leader_outcome") or entry.outcome, p_leader)
        side_variants = [self._side(candidate, vc.get("leader_outcome") or entry.outcome, v) for v in variants]
        calc.extend(f"Assumption: {a}" for a in assumptions)
        calc.append(f"Sensitivity (reporting +/-3pt): {min(side_variants):.4f} - {max(side_variants):.4f}")
        calc.append(f"P({candidate.outcome}) = {p:.4f}")
        uncertainty = max(0.005, (max(side_variants + [p]) - min(side_variants + [p])) / 2)
        if assumptions:
            uncertainty *= 1.5
        return ProbabilityEstimate(
            outcome_index=candidate.outcome_index,
            probability=p,
            engine=engine,
            method="vote-count model",
            data_status=OK,
            uncertainty=uncertainty,
            opportunity_type=LIVE_EVENT_EDGE,
            evidence=[Evidence(entry.source, f"leader {leader_votes:,.0f} vs {trailer_votes:,.0f}, {pct:.0%} reporting", vc, entry.url, entry.as_of, quality=entry.quality)],
            calculation=calc,
            sources=[entry.source],
            source_quality=entry.quality,
            event_certainty=clamp(0.5 * pct + 0.5 * abs(2 * p - 1)),
            risks=["Vote counts entered manually: confirm they are current", *assumptions],
            loss_scenarios=["Late-counted ballots breaking heavily for the trailer", "Recount, legal challenge or certification delay"],
            data_age_seconds=(now - entry.as_of).total_seconds(),
            main_reason=f"Leader ahead {leader_votes - trailer_votes:,.0f} votes with {pct:.0%} reporting",
        )

    def _kalshi_estimate(self, candidate: FavoriteCandidate, entry: EvidenceEntry, now: datetime, engine: str) -> ProbabilityEstimate:
        if self.kalshi is None:
            raise DataUnavailable("kalshi", "Kalshi client not configured")
        quote = self.kalshi.market(entry.kalshi_ticker)  # type: ignore[arg-type]
        if quote.yes_mid is None:
            raise DataUnavailable("kalshi", f"{quote.ticker} has no two-sided quote")
        p = self._side(candidate, entry.kalshi_yes_outcome or entry.outcome or "Yes", quote.yes_mid)
        return ProbabilityEstimate(
            outcome_index=candidate.outcome_index,
            probability=p,
            engine=engine,
            method=f"Kalshi {quote.ticker} mid",
            data_status=OK,
            uncertainty=max(0.01, (quote.spread or 0.02) / 2 + 0.01),
            opportunity_type=EXTERNAL_ODDS_EDGE,
            evidence=[Evidence("Kalshi", f"{quote.title}: yes bid {quote.yes_bid} / ask {quote.yes_ask}", quote.yes_mid, quote.url, quote.observed_at, quality=0.6)],
            calculation=[
                f"Kalshi {quote.ticker} yes bid/ask {quote.yes_bid}/{quote.yes_ask} -> mid {quote.yes_mid:.4f} (status {quote.status})",
                f"P({candidate.outcome}) = {p:.4f}",
            ],
            sources=["Kalshi"],
            source_quality=0.6,
            event_certainty=abs(2 * p - 1) * 0.5,
            risks=["Kalshi and Polymarket resolution rules may differ", "Another market's price is not an independent model"],
            loss_scenarios=["Both markets mispricing the same way"],
            data_age_seconds=(now - quote.observed_at).total_seconds(),
            main_reason=f"Kalshi prices this at {quote.yes_mid:.0%}",
        )


class PoliticsEngine(ManualEvidenceMixin):
    name = "politics"

    def __init__(self, book: EvidenceBook, kalshi: KalshiClient | None, *, conflict_threshold: float = 0.05):
        self.book = book
        self.kalshi = kalshi
        self.conflict_threshold = conflict_threshold

    def applies(self, candidate: FavoriteCandidate) -> bool:
        return candidate.category == POLITICS

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate:
        entries = self.book.for_candidate(candidate, now)
        if not entries:
            return ProbabilityEstimate.unavailable(
                candidate.outcome_index,
                self.name,
                "no polls, model forecast, vote count or cross-market mapping configured for this market "
                f"(add one to {self.book.path})",
            )
        estimates = self._from_entries(candidate, entries, now, self.name)
        return combine_estimates(estimates, outcome_index=candidate.outcome_index, engine=self.name, conflict_threshold=self.conflict_threshold)


class ManualEngine(ManualEvidenceMixin):
    """Sourced evidence for markets outside politics (any category)."""

    name = "manual"

    def __init__(self, book: EvidenceBook, kalshi: KalshiClient | None):
        self.book = book
        self.kalshi = kalshi

    def applies(self, candidate: FavoriteCandidate) -> bool:
        return candidate.category != POLITICS and any(e.matches(candidate) for e in self.book.entries())

    def estimate(self, candidate: FavoriteCandidate, now: datetime) -> ProbabilityEstimate:
        entries = self.book.for_candidate(candidate, now)
        if not entries:
            return ProbabilityEstimate.unavailable(candidate.outcome_index, self.name, "no manual evidence for this market")
        estimates = self._from_entries(candidate, entries, now, self.name)
        return combine_estimates(estimates, outcome_index=candidate.outcome_index, engine=self.name, conflict_threshold=1.0)
