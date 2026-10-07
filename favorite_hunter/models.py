"""Core data structures and parsers for Polymarket API payloads.

Parsers accept the raw JSON exactly as the Gamma and CLOB APIs return it
(numbers as strings or numbers, list fields as JSON-encoded strings or arrays)
and never invent values: anything missing stays ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .timeutil import parse_dt, utcnow


def to_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def to_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def parse_list(value: object) -> list[Any]:
    """Gamma encodes some list fields as JSON strings ("[\"Yes\", \"No\"]")."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return []
        return list(decoded) if isinstance(decoded, list) else []
    return []


# --------------------------------------------------------------------------- fees


@dataclass(frozen=True)
class FeeSchedule:
    """Taker fee: shares * rate * (p * (1 - p)) ** exponent (USDC).

    Source: Polymarket SDK ``adjust_buy_amount_for_fees`` and the CLOB
    ``/clob-markets/{condition_id}`` ``fd`` object. Makers pay no fee; a
    marketable paper buy is a taker order, so the taker fee always applies.
    """

    rate: float
    exponent: float
    source: str
    known: bool = True
    taker_only: bool = True
    rebate_rate: float | None = None

    def fee_per_share(self, price: float) -> float:
        if self.rate <= 0:
            return 0.0
        base = max(price * (1.0 - price), 0.0)
        return self.rate * (base**self.exponent)

    def describe(self) -> str:
        if self.rate <= 0:
            return f"no taker fee ({self.source})"
        label = "" if self.known else " [ASSUMED - schedule unavailable]"
        return f"rate {self.rate:g} x (p(1-p))^{self.exponent:g} per share ({self.source}){label}"


ZERO_FEE = FeeSchedule(rate=0.0, exponent=1.0, source="feesEnabled=false")


# --------------------------------------------------------------------------- books


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float  # shares

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass
class OrderBook:
    token_id: str
    condition_id: str | None
    bids: list[BookLevel]  # best (highest) first
    asks: list[BookLevel]  # best (lowest) first
    tick_size: float | None
    min_order_size: float | None
    neg_risk: bool | None
    server_time: datetime | None
    fetched_at: datetime
    last_trade_price: float | None = None
    book_hash: str | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_ask + self.best_bid) / 2.0

    def age_seconds(self, now: datetime | None = None) -> float | None:
        reference = self.server_time or self.fetched_at
        if reference is None:
            return None
        return ((now or utcnow()) - reference).total_seconds()

    def to_dict(self, max_levels: int | None = None) -> dict[str, Any]:
        bids = self.bids[:max_levels] if max_levels else self.bids
        asks = self.asks[:max_levels] if max_levels else self.asks
        return {
            "token_id": self.token_id,
            "condition_id": self.condition_id,
            "bids": [[lvl.price, lvl.size] for lvl in bids],
            "asks": [[lvl.price, lvl.size] for lvl in asks],
            "tick_size": self.tick_size,
            "min_order_size": self.min_order_size,
            "server_time": self.server_time.isoformat() if self.server_time else None,
            "fetched_at": self.fetched_at.isoformat(),
            "last_trade_price": self.last_trade_price,
            "hash": self.book_hash,
        }


def parse_book(raw: dict[str, Any], fetched_at: datetime | None = None) -> OrderBook:
    """Parse a CLOB ``/book`` payload.

    The API returns bids ascending and asks descending (best price last); we
    sort explicitly so the result never depends on that convention.
    """

    def levels(items: object) -> list[BookLevel]:
        parsed: list[BookLevel] = []
        for item in items or []:  # type: ignore[union-attr]
            if not isinstance(item, dict):
                continue
            price = to_float(item.get("price"))
            size = to_float(item.get("size"))
            if price is None or size is None or size <= 0 or not 0 < price < 1:
                continue
            parsed.append(BookLevel(price=price, size=size))
        return parsed

    bids = sorted(levels(raw.get("bids")), key=lambda lvl: lvl.price, reverse=True)
    asks = sorted(levels(raw.get("asks")), key=lambda lvl: lvl.price)
    token_id = str(raw.get("asset_id") or raw.get("token_id") or "")
    return OrderBook(
        token_id=token_id,
        condition_id=raw.get("market") or raw.get("condition_id"),
        bids=bids,
        asks=asks,
        tick_size=to_float(raw.get("tick_size")),
        min_order_size=to_float(raw.get("min_order_size")),
        neg_risk=to_bool(raw.get("neg_risk")),
        server_time=parse_dt(raw.get("timestamp")),
        fetched_at=fetched_at or utcnow(),
        last_trade_price=to_float(raw.get("last_trade_price")),
        book_hash=raw.get("hash"),
    )


# --------------------------------------------------------------------------- markets


@dataclass
class EventInfo:
    id: str | None
    slug: str | None
    title: str | None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    start_time: datetime | None = None
    live: bool | None = None
    ended: bool | None = None
    score: str | None = None
    period: str | None = None
    elapsed: str | None = None
    game_status: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    neg_risk: bool | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "EventInfo":
        return cls(
            id=str(raw["id"]) if raw.get("id") is not None else None,
            slug=raw.get("slug"),
            title=raw.get("title"),
            category=raw.get("category"),
            tags=_tag_labels(raw.get("tags")),
            start_time=parse_dt(raw.get("startTime")),
            live=to_bool(raw.get("live")),
            ended=to_bool(raw.get("ended")),
            score=raw.get("score") or None,
            period=raw.get("period") or None,
            elapsed=raw.get("elapsed") or None,
            game_status=raw.get("gameStatus") or None,
            home_team=raw.get("homeTeamName") or None,
            away_team=raw.get("awayTeamName") or None,
            neg_risk=to_bool(raw.get("negRisk")),
        )


def _tag_labels(raw_tags: object) -> list[str]:
    labels: list[str] = []
    for tag in parse_list(raw_tags):
        if isinstance(tag, dict):
            for key in ("slug", "label"):
                value = tag.get(key)
                if isinstance(value, str) and value.strip():
                    labels.append(value.strip().lower())
        elif isinstance(tag, str) and tag.strip():
            labels.append(tag.strip().lower())
    # de-duplicate while keeping order
    return list(dict.fromkeys(labels))


@dataclass
class Market:
    id: str
    condition_id: str | None
    question: str
    slug: str | None
    outcomes: list[str]
    token_ids: list[str]
    outcome_prices: list[float | None]
    description: str
    resolution_source: str | None
    group_item_title: str | None
    category_raw: str | None
    tags: list[str]
    events: list[EventInfo]
    start_date: datetime | None
    end_date: datetime | None
    game_start_time: datetime | None
    active: bool | None
    closed: bool | None
    archived: bool | None
    accepting_orders: bool | None
    enable_order_book: bool | None
    neg_risk: bool | None
    best_bid: float | None  # Gamma cache, first outcome token
    best_ask: float | None
    last_trade_price: float | None
    spread: float | None
    volume: float | None
    volume_24hr: float | None
    liquidity: float | None
    order_min_size: float | None
    tick_size: float | None
    seconds_delay: float | None
    fees_enabled: bool | None
    fee_schedule: FeeSchedule | None
    uma_resolution_status: str | None
    sports_market_type: str | None
    game_id: str | None
    line: float | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def event(self) -> EventInfo | None:
        return self.events[0] if self.events else None

    @property
    def event_title(self) -> str | None:
        return self.event.title if self.event else None

    @property
    def url(self) -> str | None:
        event_slug = self.event.slug if self.event else None
        if event_slug:
            return f"https://polymarket.com/event/{event_slug}"
        if self.slug:
            return f"https://polymarket.com/market/{self.slug}"
        return None

    def all_tags(self) -> list[str]:
        tags = list(self.tags)
        for event in self.events:
            tags.extend(event.tags)
        return list(dict.fromkeys(tags))


def parse_fee_schedule(raw: dict[str, Any]) -> FeeSchedule | None:
    """Fee schedule from a Gamma market payload, if determinable."""
    fees_enabled = to_bool(raw.get("feesEnabled"))
    schedule = raw.get("feeSchedule")
    if isinstance(schedule, dict):
        rate = to_float(schedule.get("rate"))
        exponent = to_float(schedule.get("exponent"))
        if rate is not None and exponent is not None:
            if fees_enabled is False:
                return ZERO_FEE
            taker_only = to_bool(schedule.get("takerOnly"))
            return FeeSchedule(
                rate=rate,
                exponent=exponent,
                source="gamma.feeSchedule",
                taker_only=True if taker_only is None else taker_only,
                rebate_rate=to_float(schedule.get("rebateRate")),
            )
    if fees_enabled is False:
        return ZERO_FEE
    return None


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Parse a Gamma market. Returns ``None`` for non-binary or token-less markets."""
    outcomes = [str(o) for o in parse_list(raw.get("outcomes"))]
    token_ids = [str(t) for t in parse_list(raw.get("clobTokenIds"))]
    if len(outcomes) != 2 or len(token_ids) != 2 or not all(token_ids):
        return None
    prices = [to_float(p) for p in parse_list(raw.get("outcomePrices"))]
    while len(prices) < 2:
        prices.append(None)
    events = [EventInfo.from_raw(e) for e in parse_list(raw.get("events")) if isinstance(e, dict)]
    question = raw.get("question") or (events[0].title if events else None) or ""
    return Market(
        id=str(raw.get("id")),
        condition_id=raw.get("conditionId") or None,
        question=question,
        slug=raw.get("slug"),
        outcomes=outcomes,
        token_ids=token_ids,
        outcome_prices=prices[:2],
        description=raw.get("description") or "",
        resolution_source=raw.get("resolutionSource") or None,
        group_item_title=raw.get("groupItemTitle") or None,
        category_raw=raw.get("category") or None,
        tags=_tag_labels(raw.get("tags")),
        events=events,
        start_date=parse_dt(raw.get("startDate")),
        end_date=parse_dt(raw.get("endDate")),
        game_start_time=parse_dt(raw.get("gameStartTime")),
        active=to_bool(raw.get("active")),
        closed=to_bool(raw.get("closed")),
        archived=to_bool(raw.get("archived")),
        accepting_orders=to_bool(raw.get("acceptingOrders")),
        enable_order_book=to_bool(raw.get("enableOrderBook")),
        neg_risk=to_bool(raw.get("negRisk")),
        best_bid=to_float(raw.get("bestBid")),
        best_ask=to_float(raw.get("bestAsk")),
        last_trade_price=to_float(raw.get("lastTradePrice")),
        spread=to_float(raw.get("spread")),
        volume=to_float(raw.get("volumeNum")) if raw.get("volumeNum") is not None else to_float(raw.get("volume")),
        volume_24hr=to_float(raw.get("volume24hr")),
        liquidity=to_float(raw.get("liquidityNum")) if raw.get("liquidityNum") is not None else to_float(raw.get("liquidity")),
        order_min_size=to_float(raw.get("orderMinSize")),
        tick_size=to_float(raw.get("orderPriceMinTickSize")),
        seconds_delay=to_float(raw.get("secondsDelay")),
        fees_enabled=to_bool(raw.get("feesEnabled")),
        fee_schedule=parse_fee_schedule(raw),
        uma_resolution_status=(raw.get("umaResolutionStatus") or None),
        sports_market_type=raw.get("sportsMarketType") or None,
        game_id=str(raw["gameId"]) if raw.get("gameId") not in (None, "") else None,
        line=to_float(raw.get("line")),
        raw=raw,
    )


def resolved_outcome_index(raw: dict[str, Any]) -> int | None:
    """Winning outcome index of a closed Gamma market, if unambiguous.

    A resolved binary market reports outcomePrices of ["1", "0"] or ["0", "1"].
    Anything else (50/50 splits, still-trading prices) returns ``None``.
    """
    if not to_bool(raw.get("closed")):
        return None
    prices = [to_float(p) for p in parse_list(raw.get("outcomePrices"))]
    if len(prices) != 2 or any(p is None for p in prices):
        return None
    if prices[0] >= 0.999 and prices[1] <= 0.001:  # type: ignore[operator]
        return 0
    if prices[1] >= 0.999 and prices[0] <= 0.001:  # type: ignore[operator]
        return 1
    return None
