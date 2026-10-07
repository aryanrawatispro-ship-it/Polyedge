"""Paper trading: simulated fills against live order books, settlement, PnL.

Two kinds of records share one table:

* ``model``    - the bot's picks: candidates that passed every filter. Sized
                 by config, capped by bankroll, exposure and the size that
                 keeps the minimum edge, and filled level by level.
* ``baseline`` - a blind "buy every favorite" observation, recorded once per
                 market side per time bucket with a fixed stake. It is never a
                 trade decision; it measures whether favorites at each price
                 win often enough to justify the price, so the model can be
                 compared against simply buying favorites.

Nothing here can place a real order.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Settings
from .database import Database
from .http import DataUnavailable
from .market_scanner import FavoriteCandidate
from .models import OrderBook, parse_list, to_bool, to_float
from .orderbook_engine import Fill, simulate_buy
from .timeutil import iso, parse_dt, utcnow

log = logging.getLogger(__name__)

MODEL = "model"
BASELINE = "baseline"

ENTRY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0.80-0.85", 0.80, 0.85),
    ("0.85-0.90", 0.85, 0.90),
    ("0.90-0.93", 0.90, 0.93),
    ("0.93-0.95", 0.93, 0.95),
    ("0.95-0.97", 0.95, 0.97),
    ("0.97-0.98", 0.97, 0.98),
)
ENTRY_BUCKET_ORDER = ["<0.80", *[label for label, _, _ in ENTRY_BUCKETS], ">0.98"]


def entry_bucket(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < ENTRY_BUCKETS[0][1] - 1e-9:
        return "<0.80"
    for label, low, high in ENTRY_BUCKETS:
        if low - 1e-9 <= price < high - 1e-9:
            return label
    # 0.98 itself belongs to the last (inclusive) bucket
    if price <= ENTRY_BUCKETS[-1][2] + 1e-9:
        return ENTRY_BUCKETS[-1][0]
    return ">0.98"


@dataclass
class SizingDecision:
    budget_usdc: float
    reason: str


@dataclass
class Resolution:
    status: str  # won / lost / split
    outcome_index: int | None
    payouts: list[float]
    resolved_at: datetime | None
    source: str


class PaperTrader:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------- bankroll

    def bankroll(self) -> dict[str, float]:
        cfg = self.settings.paper
        rows = self.db.query(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status != 'open' THEN pnl END), 0) AS realized,
                COALESCE(SUM(CASE WHEN status = 'open' THEN amount_invested END), 0) AS open_cost,
                COALESCE(SUM(CASE WHEN status = 'open' THEN shares * COALESCE(last_mark, entry_price) END), 0) AS open_mark
            FROM paper_trades WHERE kind = ?
            """,
            (MODEL,),
        )[0]
        cash = cfg.starting_bankroll + rows["realized"] - rows["open_cost"]
        return {
            "starting": cfg.starting_bankroll,
            "realized_pnl": rows["realized"],
            "open_cost": rows["open_cost"],
            "open_mark_value": rows["open_mark"],
            "cash": cash,
            "equity_at_cost": cash + rows["open_cost"],
            "equity_marked": cash + rows["open_mark"],
        }

    def open_trades(self, kind: str | None = MODEL) -> list[dict[str, Any]]:
        if kind is None:
            return self.db.query("SELECT * FROM paper_trades WHERE status='open' ORDER BY opened_at")
        return self.db.query("SELECT * FROM paper_trades WHERE status='open' AND kind=? ORDER BY opened_at", (kind,))

    def has_open_position(self, key: str) -> bool:
        return bool(self.db.query("SELECT 1 FROM paper_trades WHERE kind=? AND key=? AND status='open' LIMIT 1", (MODEL, key)))

    def has_traded(self, key: str) -> bool:
        return bool(self.db.query("SELECT 1 FROM paper_trades WHERE kind=? AND key=? LIMIT 1", (MODEL, key)))

    def event_exposure(self, event_id: str | None) -> float:
        if not event_id:
            return 0.0
        rows = self.db.query(
            "SELECT COALESCE(SUM(amount_invested), 0) AS cost FROM paper_trades WHERE kind=? AND status='open' AND event_id=?",
            (MODEL, event_id),
        )
        return float(rows[0]["cost"])

    # --------------------------------------------------------------- sizing

    def size_trade(self, candidate: FavoriteCandidate) -> SizingDecision:
        cfg = self.settings.paper
        book = self.bankroll()
        equity = book["equity_at_cost"]
        if cfg.sizing == "kelly":
            kelly = candidate.edge.kelly_fraction if candidate.edge and candidate.edge.kelly_fraction else 0.0
            budget = equity * kelly * cfg.kelly_multiplier
            reason = f"{cfg.kelly_multiplier:g} x Kelly {kelly:.3f} x equity ${equity:,.0f}"
        else:
            budget = cfg.fixed_stake_usd
            reason = f"fixed stake ${cfg.fixed_stake_usd:,.0f}"
        caps = [(cfg.max_stake_usd, "max stake")]
        if candidate.max_exec_usd is not None:
            caps.append((candidate.max_exec_usd, "size that keeps the minimum edge"))
        caps.append((book["cash"], "available paper cash"))
        event = candidate.market.event
        caps.append((cfg.max_exposure_per_event_usd - self.event_exposure(event.id if event else None), "event exposure cap"))
        caps.append((cfg.max_total_exposure_pct * equity - book["open_cost"], "total exposure cap"))
        for cap, label in caps:
            if cap < budget:
                budget = max(cap, 0.0)
                reason += f"; capped by {label}"
        if budget < cfg.min_stake_usd:
            return SizingDecision(0.0, reason + f"; below minimum stake ${cfg.min_stake_usd:g}")
        return SizingDecision(budget, reason)

    def can_open(self, candidate: FavoriteCandidate) -> tuple[bool, str]:
        cfg = self.settings.paper
        if not cfg.enabled:
            return False, "paper trading disabled"
        if self.has_open_position(candidate.key):
            return False, "already holding this market side"
        if not cfg.allow_reentry and self.has_traded(candidate.key):
            return False, "already traded this market side (re-entry disabled)"
        if len(self.open_trades(MODEL)) >= cfg.max_open_positions:
            return False, "max open positions reached"
        # Never hold both sides of one market.
        other = f"{candidate.market.id}:{1 - candidate.outcome_index}"
        if self.has_open_position(other):
            return False, "holding the opposite side of this market"
        return True, "ok"

    # -------------------------------------------------------------- opening

    def open_model_trade(self, candidate: FavoriteCandidate, now: datetime | None = None) -> int | None:
        """Simulate buying an approved opportunity. Returns the trade id."""
        ok, why = self.can_open(candidate)
        if not ok:
            log.debug("not trading %s: %s", candidate.key, why)
            return None
        sizing = self.size_trade(candidate)
        if sizing.budget_usdc <= 0:
            log.debug("not trading %s: %s", candidate.key, sizing.reason)
            return None
        estimate = candidate.estimate
        q = estimate.probability if estimate is not None else None
        if q is None:
            return None
        min_size = candidate.book.min_order_size or candidate.market.order_min_size
        fill = simulate_buy(
            candidate.book.asks,
            candidate.fee,
            budget_usdc=sizing.budget_usdc,
            max_cost_per_share=q - self.settings.filters.min_edge,
            min_order_size=min_size,
        )
        if not fill.meets_min_size:
            log.debug("not trading %s: fill below minimum order size", candidate.key)
            return None
        return self._insert_trade(MODEL, candidate, fill, now or utcnow(), sizing_reason=sizing.reason)

    def record_baseline(self, candidate: FavoriteCandidate, now: datetime | None = None) -> int | None:
        """Record a blind-favorite observation once per market side and time bucket."""
        cfg = self.settings.paper
        if not cfg.record_baseline:
            return None
        bucket = candidate.time_bucket
        exists = self.db.query(
            "SELECT 1 FROM paper_trades WHERE kind=? AND key=? AND time_bucket=? LIMIT 1",
            (BASELINE, candidate.key, bucket),
        )
        if exists:
            return None
        # Never record both sides of one market in the same bucket.
        other = f"{candidate.market.id}:{1 - candidate.outcome_index}"
        if self.db.query(
            "SELECT 1 FROM paper_trades WHERE kind=? AND key=? AND time_bucket=? LIMIT 1", (BASELINE, other, bucket)
        ):
            return None
        min_size = candidate.book.min_order_size or candidate.market.order_min_size
        fill = simulate_buy(candidate.book.asks, candidate.fee, budget_usdc=cfg.baseline_stake_usd, min_order_size=min_size)
        if not fill.meets_min_size:
            return None
        return self._insert_trade(BASELINE, candidate, fill, now or utcnow(), sizing_reason="baseline fixed stake")

    def _insert_trade(self, kind: str, c: FavoriteCandidate, fill: Fill, now: datetime, *, sizing_reason: str) -> int:
        estimate = c.estimate
        q = estimate.probability if estimate is not None and kind == MODEL else None
        cost = fill.avg_cost_per_share
        edge = None if q is None or cost is None else q - cost
        ev_per_share = edge
        expected_profit = None if q is None else fill.shares * q - fill.total_cost
        expected_roi = None if expected_profit is None or fill.total_cost <= 0 else expected_profit / fill.total_cost
        detail = c.to_dict()
        detail["sizing"] = sizing_reason
        detail["book_at_entry"] = c.book.to_dict(max_levels=25)
        hours = c.hours_to_resolution(now)
        return self.db.insert(
            """
            INSERT INTO paper_trades (
                kind, opened_at, key, market_id, condition_id, token_id, outcome_index, outcome,
                question, event_id, category, entry_price, best_ask, fee_per_share, avg_cost, shares,
                notional, fees, amount_invested, est_prob, prob_uncertainty, edge, ev_per_share,
                expected_profit, expected_roi, confidence, score, opportunity_type, strategy,
                hours_to_resolution, time_bucket, entry_bucket, liquidity, volume, ask_depth_usd,
                spread, resolution_time, fill_json, detail_json, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')
            """,
            (
                kind, iso(now), c.key, c.market.id, c.market.condition_id, c.token_id, c.outcome_index,
                c.outcome, c.market.question, c.market.event.id if c.market.event else None, c.category,
                fill.vwap, c.best_ask, fill.fees / fill.shares if fill.shares else 0.0, cost, fill.shares,
                fill.notional, fill.fees, fill.total_cost, q,
                estimate.uncertainty if estimate is not None and kind == MODEL else None,
                edge, ev_per_share, expected_profit, expected_roi,
                _score_value(c.confidence) if kind == MODEL else None,
                _score_value(c.score) if kind == MODEL else None,
                c.opportunity_type if kind == MODEL else None,
                (estimate.engine if estimate is not None else None) if kind == MODEL else "blind_favorite",
                hours, c.time_bucket, entry_bucket(fill.vwap), c.market.liquidity, c.market.volume,
                c.ask_depth_usd, c.spread, iso(c.resolution_time), json.dumps(fill.to_dict()),
                json.dumps(detail, default=str),
            ),
        )

    # ------------------------------------------------------- mark to market

    def mark_to_market(self, books: dict[str, OrderBook], now: datetime | None = None) -> int:
        """Value open positions at the best bid (what a sale would fetch now)."""
        stamp = iso(now or utcnow())
        updated = 0
        for trade in self.open_trades(kind=None):
            book = books.get(trade["token_id"])
            if book is None or book.best_bid is None:
                continue
            self.db.execute(
                "UPDATE paper_trades SET last_mark=?, last_mark_ts=? WHERE trade_id=?",
                (book.best_bid, stamp, trade["trade_id"]),
            )
            updated += 1
        return updated

    # ---------------------------------------------------------- settlement

    def settle(self, client: Any, now: datetime | None = None) -> list[dict[str, Any]]:
        """Settle open trades whose markets have resolved. Returns settled rows."""
        open_rows = self.open_trades(kind=None)
        condition_ids = sorted({row["condition_id"] for row in open_rows if row["condition_id"]})
        if not condition_ids:
            return []
        try:
            raw_markets = client.get_markets_by_condition_ids(condition_ids, closed=True)
        except DataUnavailable as exc:
            log.warning("cannot check resolutions: %s", exc)
            self.db.record_source("polymarket-resolutions", False, str(exc))
            return []
        resolutions: dict[str, Resolution] = {}
        for raw in raw_markets:
            resolution = resolution_from_gamma(raw)
            cid = raw.get("conditionId")
            if resolution and cid:
                resolutions[cid] = resolution
        settled = []
        for row in open_rows:
            resolution = resolutions.get(row["condition_id"])
            if resolution is None:
                continue
            payout_per_share = resolution.payouts[row["outcome_index"]]
            payout = row["shares"] * payout_per_share
            pnl = payout - row["amount_invested"]
            status = "won" if payout_per_share >= 0.999 else "lost" if payout_per_share <= 0.001 else "split"
            roi = pnl / row["amount_invested"] if row["amount_invested"] else None
            self.db.execute(
                """
                UPDATE paper_trades SET status=?, resolved_at=?, resolution_outcome=?, payout_per_share=?,
                    payout=?, pnl=?, roi=? WHERE trade_id=?
                """,
                (status, iso(resolution.resolved_at or now or utcnow()), resolution.outcome_index,
                 payout_per_share, payout, pnl, roi, row["trade_id"]),
            )
            self.db.mark_resolved(
                row["market_id"], outcome=resolution.outcome_index, payouts=resolution.payouts,
                resolved_at=resolution.resolved_at,
            )
            settled.append({**row, "status": status, "pnl": pnl, "payout": payout})
        return settled


def resolution_from_gamma(raw: dict[str, Any]) -> Resolution | None:
    """Final payouts of a closed Gamma market, or None if not final yet.

    Only exact payout vectors count: [1, 0], [0, 1] or a [0.5, 0.5] split.
    Closed markets still awaiting the oracle, or disputed ones, are skipped.
    """
    if not to_bool(raw.get("closed")):
        return None
    status = (raw.get("umaResolutionStatus") or "").lower()
    if status in {"disputed", "proposed", "requested"}:
        return None
    prices = [to_float(p) for p in parse_list(raw.get("outcomePrices"))]
    if len(prices) != 2 or any(p is None for p in prices):
        return None
    rounded = [round(p, 6) for p in prices]  # type: ignore[arg-type]
    resolved_at = parse_dt(raw.get("closedTime")) or parse_dt(raw.get("umaEndDate")) or parse_dt(raw.get("endDate"))
    if rounded == [1.0, 0.0]:
        return Resolution("won", 0, [1.0, 0.0], resolved_at, "gamma outcomePrices")
    if rounded == [0.0, 1.0]:
        return Resolution("won", 1, [0.0, 1.0], resolved_at, "gamma outcomePrices")
    if rounded == [0.5, 0.5]:
        return Resolution("split", None, [0.5, 0.5], resolved_at, "gamma outcomePrices")
    return None


def _score_value(obj: Any) -> float | None:
    if obj is None:
        return None
    return float(getattr(obj, "value", obj))
