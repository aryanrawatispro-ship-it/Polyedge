"""Combining independent probability estimates and detecting conflicts."""

from __future__ import annotations

import math
from dataclasses import replace

from .base import CONFLICT, DATA_UNAVAILABLE, OK, ProbabilityEstimate


def combine_estimates(
    estimates: list[ProbabilityEstimate],
    *,
    outcome_index: int,
    engine: str,
    conflict_threshold: float,
) -> ProbabilityEstimate:
    """Inverse-variance weighted average of the available estimates.

    If any two available estimates differ by more than ``conflict_threshold``
    the result is marked CONFLICTING SOURCES and carries no probability.
    """
    available = [e for e in estimates if e.available]
    if not available:
        reasons = "; ".join(f"{e.engine}/{e.method}: {e.reason}" for e in estimates if e.reason) or "no applicable data source"
        conflict = any(e.data_status == CONFLICT for e in estimates)
        est = ProbabilityEstimate.unavailable(outcome_index, engine, reasons)
        est.data_status = CONFLICT if conflict else DATA_UNAVAILABLE
        est.components = estimates
        for e in estimates:
            est.evidence.extend(e.evidence)
            est.calculation.extend(e.calculation)
        return est
    if len(available) == 1:
        only = available[0]
        if len(estimates) == 1:
            return only
        # A new object, so the result never lists itself as its own component.
        notes = [f"[{e.engine}: {e.method}] {e.data_status} - {e.reason}" for e in estimates if e is not only and e.reason]
        return replace(only, components=list(estimates), calculation=[*only.calculation, *notes])

    probs = [e.probability for e in available]  # type: ignore[misc]
    spread = max(probs) - min(probs)  # type: ignore[type-var, operator]
    evidence = [ev for e in available for ev in e.evidence]
    calculation = []
    for e in available:
        calculation.append(f"[{e.engine}: {e.method}] P = {e.probability:.4f} +/- {e.uncertainty or 0:.4f}")
        calculation.extend(f"    {line}" for line in e.calculation)
    if spread > conflict_threshold:
        est = ProbabilityEstimate.unavailable(
            outcome_index,
            engine,
            f"sources disagree by {spread * 100:.1f} points (> {conflict_threshold * 100:.0f}): "
            + ", ".join(f"{e.method} {e.probability:.3f}" for e in available),
        )
        est.data_status = CONFLICT
        est.evidence = evidence
        est.calculation = calculation
        est.components = estimates
        return est

    weights = [1.0 / max(e.uncertainty or 0.02, 0.005) ** 2 for e in available]
    total = sum(weights)
    probability = sum(w * e.probability for w, e in zip(weights, available)) / total  # type: ignore[operator]
    # Do not let averaging shrink the uncertainty below the disagreement itself.
    uncertainty = max(math.sqrt(1.0 / total), spread / 2)
    calculation.append(
        "Combined (inverse-variance weights "
        + ", ".join(f"{e.method} {w / total:.0%}" for w, e in zip(weights, available))
        + f"): P = {probability:.4f} +/- {uncertainty:.4f}"
    )
    primary = max(available, key=lambda e: e.source_quality)
    return ProbabilityEstimate(
        outcome_index=outcome_index,
        probability=probability,
        engine=engine,
        method=" + ".join(e.method for e in available),
        data_status=OK,
        uncertainty=uncertainty,
        opportunity_type=primary.opportunity_type,
        evidence=evidence,
        calculation=calculation,
        sources=sorted({s for e in available for s in e.sources}),
        source_quality=min(1.0, max(e.source_quality for e in available) + 0.05 * (len(available) - 1)),
        event_certainty=max(e.event_certainty for e in available),
        risks=list(dict.fromkeys(r for e in available for r in e.risks)),
        loss_scenarios=list(dict.fromkeys(r for e in available for r in e.loss_scenarios)),
        data_age_seconds=max((e.data_age_seconds or 0.0) for e in available),
        main_reason=primary.main_reason,
        components=estimates,
    )
