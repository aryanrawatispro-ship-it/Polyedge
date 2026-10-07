"""Heuristic clarity assessment of a market's resolution rules.

This is a text heuristic, not legal interpretation: it looks for a named
resolution source, an explicit deadline/time zone and an objective condition,
and penalises discretionary or vague wording. The full rule text is always
shown next to the score so a human can audit it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Market

_SOURCE_PATTERNS = [
    (r"https?://\S+", "explicit URL"),
    (r"\bbinance\b", "Binance"),
    (r"\bchainlink\b", "Chainlink"),
    (r"\bcoinbase\b", "Coinbase"),
    (r"\bassociated press\b|\bAP\b", "Associated Press"),
    (r"\bofficial\b", "official source"),
    (r"\b(nba|nfl|mlb|nhl|uefa|fifa|atp|wta|espn)\.com\b", "league site"),
    (r"\baccording to\b", "named attribution"),
    (r"\bresolution source\b", "resolution source stated"),
    (r"\bfederal reserve\b|\bbls\b|\bbureau of labor\b", "official statistics"),
    (r"\bnoaa\b|\bnational weather service\b|\bwunderground\b", "weather authority"),
]
_TIME_PATTERNS = [
    r"\b\d{1,2}:\d{2}\s*(am|pm)?\s*(et|est|edt|utc|gmt|pt|pst|pdt)\b",
    r"\b(et|utc|gmt)\b",
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}",
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\bby the end of\b|\bbefore\b|\bno later than\b",
]
_VAGUE_PATTERNS = [
    (r"consensus of credible reporting", "relies on 'consensus of credible reporting'"),
    (r"\bsole discretion\b|\bat the discretion\b", "discretionary resolution"),
    (r"\bin the spirit of\b|\bintended\b|\binterpret", "interpretive language"),
    (r"\bambiguous\b|\bunclear\b|\bsubjective\b", "acknowledged ambiguity"),
    (r"\bmay resolve\b|\bmay be resolved\b", "optional resolution path"),
    (r"\bsignificant(ly)?\b|\bsubstantial(ly)?\b", "qualitative threshold wording"),
]
_RISK_PATTERNS = [
    (r"50[-/ ]50|fifty[- ]fifty", "may resolve 50-50 (postponement/cancellation/tie) - favorite would lose ~half"),
    (r"postpon|cancel|abandon|suspend", "postponement/cancellation clause"),
    (r"\bovertime\b|\bextra time\b|\bpenalt", "overtime/extra-time handling matters"),
    (r"\bdispute\b|\bclarification\b", "rule clarification/dispute language"),
    (r"\bearly\b.*\bresolv|\bresolve early\b|\bimmediately resolve\b", "can resolve early"),
    (r"\brevis|\bamend|\bupdated\b", "revisions/updates may affect resolution"),
]


@dataclass
class RuleAssessment:
    score: float  # 0-100, higher = clearer
    sources: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # clarity problems
    risks: list[str] = field(default_factory=list)  # resolution risks worth showing
    has_deadline: bool = False

    @property
    def ambiguous(self) -> bool:
        return self.score < 40

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "sources": self.sources,
            "flags": self.flags,
            "risks": self.risks,
            "has_deadline": self.has_deadline,
        }


def assess_rules(market: Market) -> RuleAssessment:
    text = " ".join(filter(None, [market.description, market.resolution_source or ""]))
    lowered = text.lower()
    score = 50.0
    flags: list[str] = []
    risks: list[str] = []

    if len(text.strip()) < 60:
        score -= 30
        flags.append("rules text very short or missing")

    sources: list[str] = []
    for pattern, label in _SOURCE_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            sources.append(label)
    if market.resolution_source:
        sources.append("resolutionSource field")
    sources = list(dict.fromkeys(sources))
    if sources:
        score += min(25, 10 + 5 * len(sources))
    else:
        score -= 15
        flags.append("no named resolution source")

    has_deadline = any(re.search(p, lowered) for p in _TIME_PATTERNS) or market.end_date is not None
    if any(re.search(p, lowered) for p in _TIME_PATTERNS):
        score += 10
    else:
        flags.append("no explicit time/time zone in rules")

    if re.search(r"\$?\d[\d,]*(\.\d+)?", text):
        score += 5  # an explicit number usually means an objective threshold

    for pattern, label in _VAGUE_PATTERNS:
        if re.search(pattern, lowered):
            score -= 12
            flags.append(label)
    for pattern, label in _RISK_PATTERNS:
        if re.search(pattern, lowered):
            risks.append(label)

    if market.uma_resolution_status and market.uma_resolution_status.lower() == "disputed":
        score -= 40
        flags.append("UMA resolution currently DISPUTED")

    return RuleAssessment(
        score=max(0.0, min(100.0, score)),
        sources=sources,
        flags=list(dict.fromkeys(flags)),
        risks=list(dict.fromkeys(risks)),
        has_deadline=has_deadline,
    )
