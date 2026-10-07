"""User-supplied, sourced evidence (``manual_evidence.yaml``).

Polls, election-model forecasts, live vote counts and Kalshi market mappings
have no single free API, so they are entered here with their source, URL and
timestamp. Entries without a source and an ``as_of`` time are rejected, and
expired entries are ignored: the bot never fills a gap on its own.

Example::

    entries:
      - match: {market_id: "123456"}
        outcome: "Yes"
        probability: 0.97
        uncertainty: 0.02
        source: "Example forecast model"
        url: "https://example.org/forecast"
        as_of: "2026-10-07T12:00:00Z"
        expires: "2026-10-08T12:00:00Z"
        type: POLLING_EDGE
      - match: {question_contains: "Governor"}
        outcome: "Yes"
        vote_count:
          leader_outcome: "Yes"
          leader_votes: 1250000
          trailer_votes: 1100000
          pct_reporting: 0.88
          remaining_leader_share: 0.47
          remaining_share_sd: 0.04
        source: "Official state results page"
        url: "https://example.org/results"
        as_of: "2026-11-03T23:10:00Z"
        expires: "2026-11-04T06:00:00Z"
      - match: {market_id: "789"}
        kalshi_ticker: "KXEXAMPLE-26"
        kalshi_yes_outcome: "Yes"
        source: "Kalshi"
        as_of: "2026-10-01T00:00:00Z"
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from ..market_scanner import FavoriteCandidate
from ..timeutil import parse_dt

log = logging.getLogger(__name__)


@dataclass
class EvidenceEntry:
    match: dict[str, Any]
    source: str
    as_of: datetime
    outcome: str | None = None
    probability: float | None = None
    uncertainty: float | None = None
    url: str | None = None
    expires: datetime | None = None
    type: str | None = None
    quality: float = 0.65
    note: str | None = None
    vote_count: dict[str, Any] | None = None
    kalshi_ticker: str | None = None
    kalshi_yes_outcome: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def matches(self, candidate: FavoriteCandidate) -> bool:
        market = candidate.market
        m = self.match
        if "market_id" in m and str(m["market_id"]) != market.id:
            return False
        if "condition_id" in m and str(m["condition_id"]).lower() != (market.condition_id or "").lower():
            return False
        if "slug" in m and m["slug"] != market.slug:
            return False
        if "question_contains" in m and str(m["question_contains"]).lower() not in market.question.lower():
            return False
        return bool(m)

    def active(self, now: datetime) -> bool:
        return self.as_of <= now and (self.expires is None or now < self.expires)


class EvidenceBook:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._mtime: float | None = None
        self._entries: list[EvidenceEntry] = []
        self.errors: list[str] = []

    def entries(self) -> list[EvidenceEntry]:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._entries, self._mtime = [], None
            return []
        if mtime != self._mtime:
            self._entries, self.errors = _load(self.path)
            self._mtime = mtime
        return self._entries

    def for_candidate(self, candidate: FavoriteCandidate, now: datetime) -> list[EvidenceEntry]:
        return [e for e in self.entries() if e.matches(candidate) and e.active(now)]


def _load(path: Path) -> tuple[list[EvidenceEntry], list[str]]:
    errors: list[str] = []
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [], [f"cannot read {path}: {exc}"]
    items = raw.get("entries") if isinstance(raw, dict) else raw
    entries = []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            errors.append(f"entry {i}: not a mapping")
            continue
        source, as_of = item.get("source"), parse_dt(item.get("as_of"))
        if not source or as_of is None:
            errors.append(f"entry {i}: 'source' and 'as_of' are required (entry ignored)")
            continue
        if not isinstance(item.get("match"), dict) or not item["match"]:
            errors.append(f"entry {i}: 'match' is required (entry ignored)")
            continue
        probability = item.get("probability")
        if probability is not None and not 0 <= float(probability) <= 1:
            errors.append(f"entry {i}: probability must be within [0, 1] (entry ignored)")
            continue
        entries.append(
            EvidenceEntry(
                match=item["match"],
                source=str(source),
                as_of=as_of,
                outcome=item.get("outcome"),
                probability=None if probability is None else float(probability),
                uncertainty=None if item.get("uncertainty") is None else float(item["uncertainty"]),
                url=item.get("url"),
                expires=parse_dt(item.get("expires")),
                type=item.get("type"),
                quality=float(item.get("quality", 0.65)),
                note=item.get("note"),
                vote_count=item.get("vote_count"),
                kalshi_ticker=item.get("kalshi_ticker"),
                kalshi_yes_outcome=item.get("kalshi_yes_outcome"),
                raw=item,
            )
        )
    for error in errors:
        log.warning("manual evidence: %s", error)
    return entries, errors
