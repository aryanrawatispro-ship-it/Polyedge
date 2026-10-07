"""Favorite scanner: find outcome tokens whose executable price is in the band.

Pipeline: Gamma (all open markets in the time horizon) -> cheap pre-filter on
Gamma's cached quotes -> CLOB order books for the surviving tokens -> keep
sides whose executable VWAP for the reference stake is within
[price_min, price_max]. Both outcomes of every market are checked, so a NO at
0.95 is found just like a YES at 0.95.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .categories import SPORTS, detect_category
from .config import Settings
from .edge_calculator import EdgeMetrics, compute_edge
from .http import DataUnavailable
from .models import FeeSchedule, Market, OrderBook, parse_market
from .orderbook_engine import Fill, depth_within, max_executable, simulate_buy
from .polymarket_client import PolymarketClient
from .rules import RuleAssessment, assess_rules
from .timeutil import hours_between, humanize_hours, iso, time_bucket, utcnow

log = logging.getLogger(__name__)

# Typical wall-clock length of a game, used when a sports market only gives the
# start time. Shown in the UI as the source of the time-to-resolution figure.
SPORT_DURATION_HOURS = {
    "nba": 2.5, "wnba": 2.25, "cbb": 2.25, "ncaab": 2.25, "nfl": 3.5, "cfb": 3.5,
    "ncaaf": 3.5, "mlb": 3.25, "nhl": 2.75, "soccer": 2.0, "epl": 2.0, "ucl": 2.0,
    "mls": 2.0, "la-liga": 2.0, "serie-a": 2.0, "bundesliga": 2.0, "ligue-1": 2.0,
    "tennis": 3.0, "ufc": 0.5, "mma": 0.5, "boxing": 1.0, "cricket": 4.0, "esports": 3.0,
}
DEFAULT_SPORT_DURATION_HOURS = 3.0

STATUS_DATA_UNAVAILABLE = "DATA UNAVAILABLE"


@dataclass
class FavoriteCandidate:
    market: Market
    category: str
    outcome_index: int
    outcome: str
    token_id: str
    book: OrderBook
    fee: FeeSchedule
    entry_fill: Fill
    ask_depth_usd: float
    rules: RuleAssessment
    resolution_time: datetime | None
    time_source: str
    scanned_at: datetime
    # Filled by later stages (probability engine, risk engine, scoring).
    estimate: Any = None
    edge: EdgeMetrics | None = None
    max_exec_shares: float | None = None
    max_exec_usd: float | None = None
    confidence: Any = None
    score: Any = None
    status: str = STATUS_DATA_UNAVAILABLE
    skip_reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    opportunity_type: str | None = None

    @property
    def key(self) -> str:
        return f"{self.market.id}:{self.outcome_index}"

    @property
    def best_ask(self) -> float | None:
        return self.book.best_ask

    @property
    def best_bid(self) -> float | None:
        return self.book.best_bid

    @property
    def spread(self) -> float | None:
        return self.book.spread

    @property
    def ask_size_at_best(self) -> float:
        return self.book.asks[0].size if self.book.asks else 0.0

    @property
    def entry_price(self) -> float | None:
        return self.entry_fill.vwap

    @property
    def fee_per_share(self) -> float:
        shares = self.entry_fill.shares
        return self.entry_fill.fees / shares if shares > 0 else 0.0

    @property
    def break_even(self) -> float | None:
        return self.entry_fill.avg_cost_per_share

    @property
    def market_implied_probability(self) -> float | None:
        """Book midpoint. Display only: never used as the model's prediction."""
        return self.book.midpoint

    def hours_to_resolution(self, now: datetime | None = None) -> float | None:
        if self.resolution_time is None:
            return None
        return hours_between(now or self.scanned_at, self.resolution_time)

    @property
    def time_bucket(self) -> str:
        return time_bucket(self.hours_to_resolution())

    @property
    def base_edge(self) -> EdgeMetrics | None:
        """Price-only metrics (no probability estimate) for the entry fill."""
        if self.entry_price is None:
            return None
        return compute_edge(self.entry_price, self.fee_per_share, None)

    def to_dict(self) -> dict[str, Any]:
        market = self.market
        hours = self.hours_to_resolution()
        return {
            "key": self.key,
            "market_id": market.id,
            "condition_id": market.condition_id,
            "question": market.question,
            "event_title": market.event_title,
            "url": market.url,
            "category": self.category,
            "outcome_index": self.outcome_index,
            "outcome": self.outcome,
            "token_id": self.token_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "spread": self.spread,
            "ask_size_at_best": self.ask_size_at_best,
            "ask_depth_usd": round(self.ask_depth_usd, 2),
            "entry_price": self.entry_price,
            "fee_per_share": self.fee_per_share,
            "fee_schedule": self.fee.describe(),
            "fee_known": self.fee.known,
            "break_even": self.break_even,
            "market_implied_probability": self.market_implied_probability,
            "entry_fill": self.entry_fill.to_dict(),
            "resolution_time": iso(self.resolution_time),
            "end_date": iso(market.end_date),
            "time_source": self.time_source,
            "hours_to_resolution": hours,
            "time_remaining": humanize_hours(hours),
            "time_bucket": self.time_bucket,
            "volume": market.volume,
            "volume_24hr": market.volume_24hr,
            "liquidity": market.liquidity,
            "rules": self.rules.to_dict(),
            "book_age_seconds": self.book.age_seconds(self.scanned_at),
            "scanned_at": iso(self.scanned_at),
            "status": self.status,
            "skip_reasons": self.skip_reasons,
            "risk_flags": self.risk_flags,
            "opportunity_type": self.opportunity_type,
            "max_exec_shares": self.max_exec_shares,
            "max_exec_usd": self.max_exec_usd,
            "edge": self.edge.to_dict() if self.edge else None,
            "estimate": self.estimate.to_dict() if self.estimate is not None else None,
            "confidence": self.confidence.to_dict() if self.confidence is not None else None,
            "score": self.score.to_dict() if self.score is not None else None,
        }


@dataclass
class ScanResult:
    started_at: datetime
    finished_at: datetime
    markets_fetched: int = 0
    binary_markets: int = 0
    tradable_markets: int = 0
    prefiltered_sides: int = 0
    books_requested: int = 0
    books_received: int = 0
    candidates: list[FavoriteCandidate] = field(default_factory=list)
    markets: dict[str, Market] = field(default_factory=dict)
    books: dict[str, OrderBook] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    data_available: bool = True

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def summary(self) -> dict[str, Any]:
        return {
            "started_at": iso(self.started_at),
            "duration_seconds": round(self.duration_seconds, 2),
            "markets_fetched": self.markets_fetched,
            "binary_markets": self.binary_markets,
            "tradable_markets": self.tradable_markets,
            "prefiltered_sides": self.prefiltered_sides,
            "books_requested": self.books_requested,
            "books_received": self.books_received,
            "favorites_in_band": len(self.candidates),
            "data_available": self.data_available,
            "errors": self.errors,
        }


class MarketScanner:
    def __init__(self, client: PolymarketClient, settings: Settings):
        self.client = client
        self.settings = settings

    # ---------------------------------------------------------------- fetch

    def fetch_markets(self, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[Market]]:
        cfg = self.settings.scanner
        now = now or utcnow()
        end_max = now + timedelta(hours=cfg.max_hours_to_end) if cfg.max_hours_to_end else None
        end_min = None if cfg.include_past_end else now
        raw_markets = list(
            self.client.iter_markets(
                closed=False,
                page_size=cfg.page_size,
                max_markets=cfg.max_markets,
                end_date_min=end_min,
                end_date_max=end_max,
                liquidity_num_min=cfg.min_gamma_liquidity,
                volume_num_min=cfg.min_gamma_volume,
            )
        )
        markets = [m for m in (parse_market(raw) for raw in raw_markets) if m is not None]
        return raw_markets, markets

    def is_tradable(self, market: Market) -> bool:
        if market.closed or market.archived:
            return False
        if market.active is False or market.enable_order_book is False:
            return False
        if self.settings.filters.require_accepting_orders and market.accepting_orders is False:
            return False
        return True

    def prefilter_sides(self, market: Market) -> list[int]:
        """Outcome indexes worth fetching a book for, using Gamma's cached quotes."""
        cfg = self.settings.scanner
        low, high = cfg.price_min - cfg.prefilter_pad_low, cfg.price_max + cfg.prefilter_pad_high
        sides: list[int] = []
        approx_asks: list[float | None] = [None, None]
        if market.best_ask is not None:
            approx_asks[0] = market.best_ask
        if market.best_bid is not None:
            approx_asks[1] = 1.0 - market.best_bid
        for index in (0, 1):
            approx = approx_asks[index]
            if approx is None:
                approx = market.outcome_prices[index]
            if approx is not None and low <= approx <= high:
                sides.append(index)
        return sides

    # ----------------------------------------------------------------- scan

    def scan(self, now: datetime | None = None) -> ScanResult:
        started = utcnow()
        now = now or started
        result = ScanResult(started_at=started, finished_at=started)
        try:
            raw_markets, markets = self.fetch_markets(now)
        except DataUnavailable as exc:
            result.errors.append(str(exc))
            result.data_available = False
            result.finished_at = utcnow()
            return result
        result.markets_fetched = len(raw_markets)
        result.binary_markets = len(markets)
        tradable = [m for m in markets if self.is_tradable(m)]
        result.tradable_markets = len(tradable)
        result.markets = {m.id: m for m in tradable}

        wanted: list[tuple[Market, int]] = []
        for market in tradable:
            for index in self.prefilter_sides(market):
                wanted.append((market, index))
        result.prefiltered_sides = len(wanted)

        token_ids = [market.token_ids[index] for market, index in wanted]
        result.books_requested = len(set(token_ids))
        t0 = time.monotonic()
        try:
            books = self.client.get_books(token_ids, batch_size=self.settings.scanner.book_batch_size)
        except DataUnavailable as exc:
            result.errors.append(str(exc))
            result.data_available = False
            result.finished_at = utcnow()
            return result
        log.debug("fetched %d books in %.1fs", len(books), time.monotonic() - t0)
        result.books_received = len(books)
        result.books = books

        for market, index in wanted:
            book = books.get(market.token_ids[index])
            if book is None:
                continue
            candidate = self.build_candidate(market, index, book, now)
            if candidate is not None:
                result.candidates.append(candidate)
        result.finished_at = utcnow()
        return result

    def resolve_fee(self, market: Market) -> FeeSchedule:
        if market.fee_schedule is not None:
            return market.fee_schedule
        if market.condition_id:
            schedule = self.client.get_fee_schedule(market.condition_id)
            if schedule is not None:
                return schedule
        cfg = self.settings.fees
        return FeeSchedule(
            rate=cfg.unknown_fee_rate,
            exponent=cfg.unknown_fee_exponent,
            source="worst-case assumption",
            known=False,
        )

    def build_candidate(
        self, market: Market, index: int, book: OrderBook, now: datetime
    ) -> FavoriteCandidate | None:
        cfg = self.settings.scanner
        if book.best_ask is None:
            return None
        # Cheap reject before any fee lookups: the best ask must be in the band.
        if not cfg.price_min <= book.best_ask <= cfg.price_max:
            return None
        fee = self.resolve_fee(market)
        min_size = book.min_order_size or market.order_min_size
        fill = simulate_buy(book.asks, fee, budget_usdc=cfg.reference_stake_usd, min_order_size=min_size)
        if fill.vwap is None or not fill.meets_min_size:
            return None
        if not cfg.price_min <= fill.vwap <= cfg.price_max:
            return None
        resolution_time, time_source = estimate_resolution_time(market)
        category = detect_category(market)
        return FavoriteCandidate(
            market=market,
            category=category,
            outcome_index=index,
            outcome=market.outcomes[index],
            token_id=market.token_ids[index],
            book=book,
            fee=fee,
            entry_fill=fill,
            ask_depth_usd=depth_within(book.asks, book.best_ask, cfg.depth_window, side="ask"),
            rules=assess_rules(market),
            resolution_time=resolution_time,
            time_source=time_source,
            scanned_at=now,
        )


def estimate_resolution_time(market: Market) -> tuple[datetime | None, str]:
    """Best estimate of when the outcome is decided, with its provenance."""
    if market.game_start_time is not None and detect_category(market) == SPORTS:
        tags = set(market.all_tags())
        duration = next(
            (hours for sport, hours in SPORT_DURATION_HOURS.items() if sport in tags),
            DEFAULT_SPORT_DURATION_HOURS,
        )
        return (
            market.game_start_time + timedelta(hours=duration),
            f"gameStartTime + typical duration ({duration:g}h)",
        )
    if market.end_date is not None:
        return market.end_date, "market endDate"
    return None, "unknown (no endDate)"


def max_executable_for_edge(
    candidate: FavoriteCandidate, estimated_probability: float, min_edge: float
) -> tuple[float, float]:
    """Max shares/USDC buyable while every marginal share keeps >= min_edge."""
    return max_executable(
        candidate.book.asks,
        candidate.fee,
        max_cost_per_share=estimated_probability - min_edge,
    )
