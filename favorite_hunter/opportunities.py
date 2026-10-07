"""Persist evaluated candidates (opportunities) with their full audit trail.

To keep the table manageable a row is written for a market side when its
status changes, its edge moves by at least ``min_edge_change``, or
``interval_seconds`` have passed since its last row.
"""

from __future__ import annotations

import json
from datetime import datetime

from .database import Database
from .market_scanner import FavoriteCandidate
from .timeutil import iso


class OpportunityRecorder:
    def __init__(self, db: Database, *, interval_seconds: float = 600.0, min_edge_change: float = 0.005):
        self.db = db
        self.interval_seconds = interval_seconds
        self.min_edge_change = min_edge_change
        self._last: dict[str, tuple[datetime, str, float | None]] = {}

    def record(self, scan_id: int, candidates: list[FavoriteCandidate], now: datetime) -> int:
        rows = []
        for c in candidates:
            edge = c.edge.probability_edge if c.edge else None
            last = self._last.get(c.key)
            if last is not None:
                elapsed = (now - last[0]).total_seconds()
                edge_moved = (
                    edge is not None and last[2] is not None and abs(edge - last[2]) >= self.min_edge_change
                ) or ((edge is None) != (last[2] is None))
                if last[1] == c.status and not edge_moved and elapsed < self.interval_seconds:
                    continue
            self._last[c.key] = (now, c.status, edge)
            estimate = c.estimate
            rows.append(
                (
                    scan_id, iso(now), c.key, c.market.id, c.token_id, c.outcome_index, c.outcome,
                    c.market.question, c.category, c.entry_price, c.best_ask, c.fee_per_share, c.break_even,
                    estimate.probability if estimate is not None else None,
                    estimate.uncertainty if estimate is not None else None,
                    edge,
                    c.edge.ev_per_share if c.edge else None,
                    c.edge.expected_roi if c.edge else None,
                    getattr(c.confidence, "value", None),
                    getattr(c.score, "value", None),
                    c.status, c.opportunity_type, c.hours_to_resolution(now), c.time_bucket,
                    c.market.liquidity, c.market.volume, c.ask_depth_usd, c.max_exec_usd, c.spread,
                    json.dumps(c.skip_reasons), json.dumps(c.risk_flags),
                    json.dumps(c.to_dict(), default=str),
                )
            )
        if not rows:
            return 0
        with self.db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO opportunities (
                    scan_id, ts, key, market_id, token_id, outcome_index, outcome, question, category,
                    entry_price, best_ask, fee_per_share, break_even, est_prob, prob_uncertainty, edge,
                    ev_per_share, roi, confidence, score, status, opportunity_type, hours_to_resolution,
                    time_bucket, liquidity, volume, ask_depth_usd, max_exec_usd, spread, skip_reasons,
                    risk_flags, detail_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        return len(rows)
