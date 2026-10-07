"""Builders for API payloads in Polymarket's wire format.

These are synthetic TEST fixtures (not market data). They mirror the real
shapes: numbers as strings, list fields JSON-encoded as strings, CLOB bids
ascending and asks descending (best price last).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
CONDITION = "0x" + "ab" * 32


def gamma_market(
    market_id: str = "1001",
    *,
    question: str = "Will Team A win?",
    outcomes: tuple[str, str] = ("Yes", "No"),
    tokens: tuple[str, str] | None = None,
    best_bid: float | None = 0.89,
    best_ask: float | None = 0.90,
    end: datetime | None = None,
    closed: bool = False,
    accepting_orders: bool = True,
    fee_schedule: dict[str, Any] | None = None,
    fees_enabled: bool | None = None,
    tags: list[str] | None = None,
    description: str = (
        "This market will resolve to \"Yes\" if Team A wins the match scheduled for October 7, 2026 "
        "at 7:00 PM ET, according to the official league website https://example.org. Otherwise \"No\"."
    ),
    condition_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tokens = tokens or (f"{market_id}1", f"{market_id}2")
    end = end or NOW + timedelta(hours=3)
    payload: dict[str, Any] = {
        "id": market_id,
        "question": question,
        "conditionId": condition_id or "0x" + market_id.rjust(64, "0"),
        "slug": f"market-{market_id}",
        "description": description,
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps([str(best_ask or 0.5), str(round(1 - (best_ask or 0.5), 4))]),
        "clobTokenIds": json.dumps(list(tokens)),
        "active": True,
        "closed": closed,
        "archived": False,
        "acceptingOrders": accepting_orders,
        "enableOrderBook": True,
        "negRisk": False,
        "startDate": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
        "endDate": end.isoformat().replace("+00:00", "Z"),
        "volumeNum": 125000.5,
        "volume24hr": 8000.0,
        "liquidityNum": 15000.0,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
        "events": [{"id": f"E{market_id}", "slug": f"event-{market_id}", "title": f"Event {market_id}"}],
        "tags": [{"id": str(i), "slug": t, "label": t.title()} for i, t in enumerate(tags or [])],
    }
    if best_bid is not None:
        payload["bestBid"] = best_bid
    if best_ask is not None:
        payload["bestAsk"] = best_ask
    if best_bid is not None and best_ask is not None:
        payload["spread"] = round(best_ask - best_bid, 4)
    if fee_schedule is not None:
        payload["feeSchedule"] = fee_schedule
    if fees_enabled is not None:
        payload["feesEnabled"] = fees_enabled
    payload.update(extra or {})
    return payload


def clob_book(
    token_id: str,
    *,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    condition_id: str = CONDITION,
    timestamp: datetime | None = None,
    min_order_size: str = "5",
    tick_size: str = "0.01",
) -> dict[str, Any]:
    """Wire-format book. Pass levels in any order; output follows the API convention."""
    ts = timestamp or NOW
    return {
        "market": condition_id,
        "asset_id": token_id,
        "timestamp": str(int(ts.timestamp() * 1000)),
        "hash": "0xhash" + token_id,
        "bids": [{"price": f"{p}", "size": f"{s}"} for p, s in sorted(bids)],
        "asks": [{"price": f"{p}", "size": f"{s}"} for p, s in sorted(asks, reverse=True)],
        "min_order_size": min_order_size,
        "tick_size": tick_size,
        "neg_risk": False,
        "last_trade_price": "0.5",
    }


def mirrored_no_book(token_id: str, yes_book: dict[str, Any]) -> dict[str, Any]:
    """NO book implied by a YES book: NO asks = 1 - YES bids, NO bids = 1 - YES asks."""
    bids = [(round(1 - float(l["price"]), 4), float(l["size"])) for l in yes_book["asks"]]
    asks = [(round(1 - float(l["price"]), 4), float(l["size"])) for l in yes_book["bids"]]
    return clob_book(token_id, bids=bids, asks=asks)
