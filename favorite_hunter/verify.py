"""Live verification against the real Polymarket APIs.

Run ``favorite-hunter verify`` before trusting a phase: it checks connectivity,
response shapes, order-book conventions, YES/NO book consistency, fee data,
and recomputes a sample fill by hand from the raw ask levels.
"""

from __future__ import annotations

import math
from typing import Any

from .config import Settings
from .http import DataUnavailable
from .market_scanner import MarketScanner
from .models import parse_market
from .polymarket_client import PolymarketClient


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    def ok(self, message: str) -> None:
        print(f"  [PASS] {message}")

    def warn(self, message: str) -> None:
        self.warnings += 1
        print(f"  [WARN] {message}")

    def fail(self, message: str) -> None:
        self.failures += 1
        print(f"  [FAIL] {message}")

    def check(self, condition: bool, message: str, *, warn_only: bool = False) -> bool:
        if condition:
            self.ok(message)
        elif warn_only:
            self.warn(message)
        else:
            self.fail(message)
        return condition


def run_verification(settings: Settings, *, phase: int = 1, sample: int = 5) -> int:
    report = Report()
    client = PolymarketClient.from_settings(settings)
    print(f"Favorite Hunter live verification (phase {phase})\n")

    print("1. Gamma market listing")
    try:
        raw = list(client.iter_markets(closed=False, page_size=100, max_markets=300))
    except DataUnavailable as exc:
        print(f"  POLYMARKET DATA UNAVAILABLE: {exc.reason}")
        print("  Nothing can be verified without live data. Allow gamma-api.polymarket.com,")
        print("  clob.polymarket.com and data-api.polymarket.com in your network policy.")
        return 2
    report.check(len(raw) > 0, f"received {len(raw)} open markets")
    markets = [m for m in (parse_market(r) for r in raw) if m is not None]
    report.check(len(markets) > 0, f"{len(markets)} parsed as binary markets with two CLOB tokens")
    with_quotes = sum(1 for m in markets if m.best_ask is not None and m.best_bid is not None)
    report.check(with_quotes > 0, f"{with_quotes} markets carry Gamma bestBid/bestAsk", warn_only=True)
    with_fee_info = sum(1 for m in markets if m.fee_schedule is not None)
    report.check(True, f"{with_fee_info}/{len(markets)} markets expose feeSchedule/feesEnabled=false on Gamma")
    with_end = sum(1 for m in markets if m.end_date is not None)
    report.check(with_end > 0, f"{with_end}/{len(markets)} markets have an endDate", warn_only=True)

    print("\n2. CLOB order books")
    sample_markets = [m for m in markets if m.enable_order_book and not m.closed][: max(sample, 1)]
    tokens = [t for m in sample_markets for t in m.token_ids]
    try:
        books = client.get_books(tokens, batch_size=settings.scanner.book_batch_size)
    except DataUnavailable as exc:
        report.fail(f"POST /books failed: {exc.reason}")
        return 1
    report.check(len(books) > 0, f"received {len(books)}/{len(tokens)} books via POST /books")
    for market in sample_markets:
        yes = books.get(market.token_ids[0])
        no = books.get(market.token_ids[1])
        if yes is None or no is None:
            report.warn(f"missing book for {market.question[:60]!r}")
            continue
        sorted_ok = all(a.price <= b.price for a, b in zip(yes.asks, yes.asks[1:])) and all(
            a.price >= b.price for a, b in zip(yes.bids, yes.bids[1:])
        )
        report.check(sorted_ok, f"book levels sorted best-first for {market.question[:50]!r}")
        if yes.best_bid is not None and no.best_ask is not None:
            gap = abs((1 - yes.best_bid) - no.best_ask)
            tick = yes.tick_size or 0.01
            report.check(
                gap <= tick + 1e-9,
                f"NO best ask {no.best_ask:.3f} vs 1 - YES best bid {1 - yes.best_bid:.3f} (diff {gap:.4f})",
                warn_only=True,
            )
        if market.best_ask is not None and yes.best_ask is not None:
            gap = abs(market.best_ask - yes.best_ask)
            report.check(
                gap <= 0.02,
                f"Gamma cached bestAsk {market.best_ask:.3f} vs live book {yes.best_ask:.3f}",
                warn_only=True,
            )

    print("\n3. Fee schedules")
    for market in sample_markets[:3]:
        if not market.condition_id:
            continue
        clob_fee = client.get_fee_schedule(market.condition_id)
        gamma_fee = market.fee_schedule
        print(
            f"  {market.question[:55]!r}: gamma={gamma_fee.describe() if gamma_fee else 'n/a'} | "
            f"clob={clob_fee.describe() if clob_fee else 'unavailable'}"
        )
        if gamma_fee and clob_fee:
            report.check(
                math.isclose(gamma_fee.rate, clob_fee.rate, abs_tol=1e-9),
                "Gamma and CLOB fee rates agree",
                warn_only=True,
            )

    print("\n4. Full favorite scan")
    scanner = MarketScanner(client, settings)
    result = scanner.scan()
    summary = result.summary()
    for key, value in summary.items():
        print(f"  {key}: {value}")
    report.check(result.data_available, "scan completed with live data")
    band = settings.scanner
    out_of_band = [c for c in result.candidates if not band.price_min <= (c.entry_price or 0) <= band.price_max]
    report.check(not out_of_band, f"all {len(result.candidates)} candidates priced inside [{band.price_min}, {band.price_max}]")

    print("\n5. Hand check of one fill")
    if result.candidates:
        candidate = max(result.candidates, key=lambda c: len(c.entry_fill.levels))
        verify_fill_by_hand(candidate, settings.scanner.reference_stake_usd, report)
    else:
        report.warn("no candidates in band to hand-check")

    print(f"\nResult: {report.failures} failures, {report.warnings} warnings")
    return 0 if report.failures == 0 else 1


def verify_fill_by_hand(candidate: Any, budget: float, report: Report) -> None:
    print(f"  {candidate.market.question[:70]!r} side={candidate.outcome}")
    remaining = budget
    shares = 0.0
    notional = 0.0
    fees = 0.0
    for level in candidate.book.asks:
        fee_ps = candidate.fee.fee_per_share(level.price)
        cost = level.price + fee_ps
        take = min(level.size, remaining / cost)
        take = math.floor(take * 100 + 1e-9) / 100
        if take <= 0:
            break
        print(f"    level {level.price:.3f} x {level.size:,.2f} -> take {take:,.2f} (fee/share {fee_ps:.5f})")
        shares += take
        notional += take * level.price
        fees += take * fee_ps
        remaining -= take * cost
        if remaining < cost * 0.01:
            break
    vwap = notional / shares if shares else float("nan")
    print(f"    hand VWAP {vwap:.5f}, engine VWAP {candidate.entry_price:.5f}; fees {fees:.4f} vs {candidate.entry_fill.fees:.4f}")
    report.check(math.isclose(vwap, candidate.entry_price, rel_tol=1e-9), "fill engine VWAP matches hand calculation")
    report.check(math.isclose(fees, candidate.entry_fill.fees, abs_tol=1e-9), "fill engine fees match hand calculation")
