"""Phase-by-phase verification against the real APIs.

    favorite-hunter verify               # all phases
    favorite-hunter verify --phase 1     # one phase

Each phase checks live data and recomputes key numbers by hand. Results go to a
temporary database, so the real one is never touched. Anything that cannot be
reached is reported as DATA UNAVAILABLE and fails that phase; nothing is
simulated in its place.
"""

from __future__ import annotations

import math
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .http import DataUnavailable, HttpClient
from .market_scanner import MarketScanner, ScanResult
from .models import parse_market
from .polymarket_client import PolymarketClient
from .timeutil import utcnow

ALL_PHASES = (1, 2, 3, 4, 5, 6, 7)


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

    def info(self, message: str) -> None:
        print(f"         {message}")

    def check(self, condition: bool, message: str, *, warn_only: bool = False) -> bool:
        if condition:
            self.ok(message)
        elif warn_only:
            self.warn(message)
        else:
            self.fail(message)
        return condition


class Context:
    def __init__(self, settings: Settings, tmp: Path, sample: int):
        self.settings = settings.model_copy(deep=True)
        self.settings.database_path = str(tmp / "verify.db")
        self.client = PolymarketClient.from_settings(self.settings)
        self.sample = sample
        self.scan: ScanResult | None = None
        self.db: Any = None


def run_verification(settings: Settings, *, phase: int | str = "all", sample: int = 5, send_alert: bool = False) -> int:
    phases = ALL_PHASES if phase in ("all", 0, None) else (int(phase),)
    report = Report()
    with tempfile.TemporaryDirectory(prefix="fh-verify-") as tmp:
        ctx = Context(settings, Path(tmp), sample)
        print(f"Favorite Hunter live verification - phases {', '.join(map(str, phases))}")
        print("PAPER TRADING ONLY. Results are written to a temporary database.\n")
        for number in phases:
            title, fn = PHASES[number]
            print(f"=== Phase {number}: {title} ===")
            try:
                if number == 7:
                    fn(ctx, report, send_alert)
                else:
                    fn(ctx, report)
            except DataUnavailable as exc:
                report.fail(f"DATA UNAVAILABLE - {exc.source}: {exc.reason}")
                if number == 1:
                    report.info("Allow gamma-api.polymarket.com, clob.polymarket.com, data-api.polymarket.com")
                    break
            print()
    print(f"Result: {report.failures} failures, {report.warnings} warnings")
    return 0 if report.failures == 0 else 1


# ----------------------------------------------------------------- phase 1


def phase1(ctx: Context, report: Report) -> None:
    client = ctx.client
    raw = list(client.iter_markets(closed=False, page_size=100, max_markets=300))
    report.check(len(raw) > 0, f"Gamma /markets/keyset returned {len(raw)} open markets")
    markets = [m for m in (parse_market(r) for r in raw) if m is not None]
    report.check(len(markets) > 0, f"{len(markets)} parsed as binary markets with two CLOB tokens")
    report.check(any(m.best_ask is not None for m in markets), "Gamma markets carry bestBid/bestAsk", warn_only=True)
    report.info(f"{sum(1 for m in markets if m.fee_schedule is not None)}/{len(markets)} expose a fee schedule on Gamma")

    sample = [m for m in markets if m.enable_order_book and not m.closed][: max(ctx.sample, 1)]
    books = client.get_books([t for m in sample for t in m.token_ids], batch_size=ctx.settings.scanner.book_batch_size)
    report.check(len(books) > 0, f"POST /books returned {len(books)} order books")
    for market in sample:
        yes, no = books.get(market.token_ids[0]), books.get(market.token_ids[1])
        if yes is None or no is None:
            report.warn(f"missing book for {market.question[:60]!r}")
            continue
        report.check(
            all(a.price <= b.price for a, b in zip(yes.asks, yes.asks[1:])) and all(a.price >= b.price for a, b in zip(yes.bids, yes.bids[1:])),
            f"levels sorted best-first: {market.question[:50]!r}",
        )
        if yes.best_bid is not None and no.best_ask is not None:
            gap = abs((1 - yes.best_bid) - no.best_ask)
            report.check(gap <= (yes.tick_size or 0.01) + 1e-9, f"NO ask {no.best_ask:.3f} vs 1 - YES bid {1 - yes.best_bid:.3f}", warn_only=True)

    for market in sample[:3]:
        if market.condition_id:
            clob_fee = client.get_fee_schedule(market.condition_id)
            report.info(
                f"fee {market.question[:45]!r}: gamma={market.fee_schedule.describe() if market.fee_schedule else 'n/a'} "
                f"| clob={clob_fee.describe() if clob_fee else 'unavailable'}"
            )

    scanner = MarketScanner(client, ctx.settings)
    result = scanner.scan()
    ctx.scan = result
    for key, value in result.summary().items():
        report.info(f"{key}: {value}")
    report.check(result.data_available, "full scan completed with live data")
    band = ctx.settings.scanner
    report.check(
        all(band.price_min <= (c.entry_price or 0) <= band.price_max for c in result.candidates),
        f"all {len(result.candidates)} favorites priced inside [{band.price_min}, {band.price_max}]",
    )
    if result.candidates:
        verify_fill_by_hand(max(result.candidates, key=lambda c: len(c.entry_fill.levels)), band.reference_stake_usd, report)
        print("  Top favorites by time to resolution:")
        for c in sorted(result.candidates, key=lambda c: c.hours_to_resolution() or 1e9)[:10]:
            report.info(
                f"{c.market.question[:60]:60s} {c.outcome[:8]:8s} price {c.entry_price:.3f} fee {c.fee_per_share:.4f} "
                f"break-even {c.break_even:.3f} depth ${c.ask_depth_usd:,.0f} {c.time_bucket}"
            )


def verify_fill_by_hand(candidate: Any, budget: float, report: Report) -> None:
    report.info(f"hand check of the fill for {candidate.market.question[:60]!r} ({candidate.outcome})")
    remaining, shares, notional, fees = budget, 0.0, 0.0, 0.0
    for level in candidate.book.asks:
        fee_ps = candidate.fee.fee_per_share(level.price)
        cost = level.price + fee_ps
        take = math.floor(min(level.size, remaining / cost) * 100 + 1e-9) / 100
        if take <= 0:
            break
        report.info(f"  {level.price:.3f} x {level.size:,.2f} available -> take {take:,.2f} (fee/share {fee_ps:.5f})")
        shares += take
        notional += take * level.price
        fees += take * fee_ps
        remaining -= take * cost
        if remaining < cost * 0.01:
            break
    vwap = notional / shares if shares else float("nan")
    report.check(math.isclose(vwap, candidate.entry_price, rel_tol=1e-9), f"VWAP by hand {vwap:.5f} = engine {candidate.entry_price:.5f}")
    report.check(math.isclose(fees, candidate.entry_fill.fees, abs_tol=1e-9), f"fees by hand {fees:.5f} = engine {candidate.entry_fill.fees:.5f}")


# ----------------------------------------------------------------- phase 2


def phase2(ctx: Context, report: Report) -> None:
    from .database import Database
    from .runner import Runner

    ctx.db = Database(ctx.settings.database_path, snapshot_interval_seconds=0)
    runner = Runner(ctx.settings, client=ctx.client, db=ctx.db)
    for i in range(2):
        summary = runner.run_once().summary()
        report.check(summary["data_available"], f"cycle {i + 1}: {summary['favorites_in_band']} favorites, {summary['price_snapshots_written']} price / {summary['book_snapshots_written']} book snapshots")
    stats = ctx.db.stats()
    report.check(stats["price_snapshots"] > 0 and stats["markets"] > 0, f"stored rows: {stats}")
    row = ctx.db.query("SELECT token_id FROM price_snapshots LIMIT 1")
    if row:
        history = ctx.db.price_history(row[0]["token_id"])
        report.check(len(history) >= 1, f"history for one token: {[(h['ts'][11:19], h['best_ask']) for h in history]}")


# ----------------------------------------------------------------- phase 3


def phase3(ctx: Context, report: Report) -> None:
    from .paper_trader import BASELINE, resolution_from_gamma

    if ctx.db is None:
        phase2(ctx, report)
    baselines = ctx.db.query("SELECT * FROM paper_trades WHERE kind=?", (BASELINE,))
    report.check(len(baselines) > 0, f"{len(baselines)} baseline observations recorded with depth-walking fills", warn_only=True)
    for row in baselines[:3]:
        report.check(
            math.isclose(row["amount_invested"], row["notional"] + row["fees"], rel_tol=1e-9) and row["amount_invested"] <= ctx.settings.paper.baseline_stake_usd + 1e-6,
            f"#{row['trade_id']} {row['question'][:50]!r}: {row['shares']:.2f} sh, cost ${row['amount_invested']:.2f} <= stake",
        )
    now = utcnow()
    closed = list(ctx.client.iter_markets(closed=True, page_size=100, max_markets=100, end_date_min=now - timedelta(days=3), end_date_max=now))
    final = [r for r in closed if resolution_from_gamma(r) is not None]
    report.check(len(closed) > 0, f"{len(closed)} recently closed markets fetched; {len(final)} have final payouts")
    if final:
        raw = final[0]
        res = resolution_from_gamma(raw)
        shares, cost = 111.11, 99.999
        report.info(f"settlement example {raw.get('question', '')[:60]!r}: payouts {res.payouts}")
        report.info(f"  a $100 position on outcome 0 at 0.90 -> payout {shares * res.payouts[0]:.2f}, PnL {shares * res.payouts[0] - cost:+.2f}")


# ----------------------------------------------------------------- phase 4


def phase4(ctx: Context, report: Report) -> None:
    from .analytics import bets_from_backtest, build_report
    from .backtest import BacktestConfig, load_observations, run_backtest
    from .database import Database
    from .reporting import render_report

    db = ctx.db or Database(ctx.settings.database_path)
    run = run_backtest(ctx.client, db, ctx.settings, BacktestConfig(days=14, max_markets=60, min_volume=10_000))
    for key, value in run.summary().items():
        report.info(f"{key}: {value}")
    report.check(not run.errors and run.observations > 0, f"backtest produced {run.observations} observations from real resolved markets")
    if run.observations:
        bets = bets_from_backtest(load_observations(db, run.run_id))
        print(render_report(build_report(bets, "HISTORICAL BACKTEST (small verification sample)", min_sample=30), dimensions=["entry_range", "time_remaining"]))


# ----------------------------------------------------------------- phase 5


SOURCE_PROBES = [
    ("Binance spot", lambda s: f"{s.sources.binance_url}/api/v3/ticker/price", {"symbol": "BTCUSDT"}),
    ("Coinbase spot", lambda s: f"{s.sources.coinbase_url}/products/BTC-USD/ticker", None),
    ("Kraken spot", lambda s: f"{s.sources.kraken_url}/0/public/Ticker", {"pair": "XBTUSD"}),
    ("Deribit DVOL", lambda s: f"{s.sources.deribit_url}/api/v2/public/get_index_price", {"index_name": "btc_usd"}),
    ("ESPN NBA scoreboard", lambda s: f"{s.sources.espn_url}/apis/site/v2/sports/basketball/nba/scoreboard", None),
    ("ESPN EPL scoreboard", lambda s: f"{s.sources.espn_url}/apis/site/v2/sports/soccer/eng.1/scoreboard", None),
    ("Kalshi markets", lambda s: f"{s.sources.kalshi_url}/markets", {"limit": 1}),
]


def phase5(ctx: Context, report: Report) -> None:
    from collections import Counter

    from .evaluator import Evaluator
    from .scoring import compute_favorite_edge_score

    http = HttpClient(timeout=ctx.settings.sources.request_timeout, max_retries=0, user_agent=ctx.settings.sources.user_agent)
    for name, url_fn, params in SOURCE_PROBES:
        try:
            http.get_json(url_fn(ctx.settings), params, source=name)
            report.ok(f"{name} reachable")
        except DataUnavailable as exc:
            report.warn(f"{name}: DATA UNAVAILABLE ({exc.reason})")
    if ctx.settings.odds_api_key:
        try:
            http.get_json(f"{ctx.settings.sources.odds_api_url}/v4/sports", {"apiKey": ctx.settings.odds_api_key}, source="odds-api")
            report.ok("The Odds API reachable with ODDS_API_KEY")
        except DataUnavailable as exc:
            report.warn(f"The Odds API: {exc.reason}")
    else:
        report.warn("ODDS_API_KEY not set: sportsbook comparison disabled")

    if ctx.scan is None:
        ctx.scan = MarketScanner(ctx.client, ctx.settings).scan()
    candidates = ctx.scan.candidates
    evaluator = Evaluator.from_settings(ctx.settings)
    evaluator.scorer = compute_favorite_edge_score
    evaluator(candidates, ctx.scan.started_at)
    statuses = Counter(c.status for c in candidates)
    engines = Counter((c.category, "estimate" if c.estimate and c.estimate.available else "no estimate") for c in candidates)
    report.info(f"statuses: {dict(statuses)}")
    report.info(f"by category: {dict(engines)}")
    estimated = [c for c in candidates if c.estimate is not None and c.estimate.available]
    report.check(len(estimated) > 0, f"{len(estimated)} favorites received an external probability estimate", warn_only=True)
    for c in estimated:
        cost = c.entry_price + c.fee_per_share
        if not math.isclose(c.edge.probability_edge, c.estimate.probability - cost, abs_tol=1e-9):
            report.fail(f"edge mismatch for {c.key}")
            break
    else:
        if estimated:
            report.ok("edge = estimated probability - (price + fee) holds for every estimate")
    for c in sorted(estimated, key=lambda c: -(c.edge.ev_per_share or 0))[:3]:
        print(f"  --- {c.market.question[:70]} [{c.outcome}] {c.status} {c.opportunity_type}")
        for line in c.estimate.calculation[:12]:
            report.info(line)
        report.info(f"edge {c.edge.probability_edge * 100:+.2f}pt, ROI {c.edge.expected_roi * 100:+.2f}%, confidence {c.confidence.value:.0f}, score {getattr(c.score, 'value', 0):.0f}")


# ----------------------------------------------------------------- phase 6


def phase6(ctx: Context, report: Report) -> None:
    from fastapi.testclient import TestClient

    from .dashboard.app import create_app
    from .database import Database

    db = ctx.db or Database(ctx.settings.database_path)
    if ctx.scan is not None and ctx.scan.candidates:
        db.replace_latest(0, ctx.scan.candidates, ctx.scan.started_at)
    client = TestClient(create_app(ctx.settings, db))
    for path in ("/", "/api/status", "/api/opportunities", "/api/trades?kind=baseline", "/api/analytics?dataset=backtest", "/api/config"):
        response = client.get(path)
        report.check(response.status_code == 200, f"GET {path} -> {response.status_code}")
    opps = client.get("/api/opportunities").json()["opportunities"]
    if opps:
        detail = client.get("/api/opportunity", params={"key": opps[0]["key"]})
        report.check(detail.status_code == 200, f"detail view for {opps[0]['market'][:50]!r}")


# ----------------------------------------------------------------- phase 7


def phase7(ctx: Context, report: Report, send: bool) -> None:
    from .alerts import SAMPLE_ALERT, format_alert, senders_from_settings

    estimated = [c for c in (ctx.scan.candidates if ctx.scan else []) if c.status in ("TRADE", "HOLDING")]
    if estimated:
        best = max(estimated, key=lambda c: c.edge.ev_per_share or 0)
        text = format_alert(best.to_dict(), header="TEST MESSAGE - verification run")
    else:
        text = format_alert(SAMPLE_ALERT, header="TEST MESSAGE - SAMPLE values for layout only, not a live opportunity")
    print("\n".join(f"         {line}" for line in text.splitlines()))
    report.check(text.rstrip().endswith("PAPER TRADE ONLY."), "alert ends with PAPER TRADE ONLY.")
    senders, problems = senders_from_settings(ctx.settings)
    for problem in problems:
        report.warn(problem)
    if not send:
        report.info("alerts not sent (add --send-alert to deliver this test message)")
        return
    for sender in senders:
        try:
            sender.send(text)
            report.ok(f"test alert delivered via {sender.name}")
        except Exception as exc:
            report.fail(f"{sender.name}: {exc}")


PHASES = {
    1: ("Polymarket data + favorite scanner", phase1),
    2: ("price / order-book history storage", phase2),
    3: ("paper trading fills and settlement data", phase3),
    4: ("analytics on a real historical backtest sample", phase4),
    5: ("external probability sources", phase5),
    6: ("dashboard API", phase6),
    7: ("alerts", phase7),
}
