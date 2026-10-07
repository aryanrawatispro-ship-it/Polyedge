"""Command-line interface.

    favorite-hunter scan      one scan, print favorites in the price band
    favorite-hunter run       continuous loop: scan, store history (and later phases)
    favorite-hunter history   stored price history for a market or token
    favorite-hunter trades    paper positions (model picks and baseline observations)
    favorite-hunter settle    settle paper positions whose markets resolved
    favorite-hunter db-stats  row counts and data-source health
    favorite-hunter verify    live checks against the Polymarket APIs
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings, load_settings
from .http import DataUnavailable
from .market_scanner import FavoriteCandidate, MarketScanner, ScanResult
from .polymarket_client import PolymarketClient
from .timeutil import humanize_hours

PAPER_BANNER = "PAPER TRADING ONLY - no real orders are ever placed."


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs(value) >= 10_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:,.0f}"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render_table(rows: list[list[str]], headers: list[str], align_right: set[int] | None = None) -> str:
    align_right = align_right or set()
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.rjust(widths[i]) if i in align_right else cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(r) for r in rows)
    return "\n".join(lines)


def candidate_row(c: FavoriteCandidate) -> list[str]:
    estimate = getattr(c.estimate, "probability", None) if c.estimate is not None else None
    edge = c.edge.probability_edge if c.edge else None
    roi = c.edge.expected_roi if c.edge else None
    side = c.outcome if len(c.outcome) <= 10 else c.outcome[:9] + "…"
    return [
        _truncate(c.market.question, 58),
        side,
        _fmt_price(c.entry_price),
        _fmt_price(c.best_ask),
        _fmt_price(c.spread),
        _fmt_usd(c.ask_depth_usd),
        f"{c.fee_per_share:.4f}" + ("" if c.fee.known else "*"),
        _fmt_pct(c.break_even),
        _fmt_pct(estimate) if estimate is not None else "DATA UNAVAILABLE",
        "-" if edge is None else f"{edge * 100:+.1f}pt",
        "-" if roi is None else f"{roi * 100:+.1f}%",
        humanize_hours(c.hours_to_resolution()),
        _fmt_usd(c.market.liquidity),
        c.category,
    ]


CANDIDATE_HEADERS = [
    "Market", "Side", "Price", "BestAsk", "Spread", "Depth2c", "Fee/sh", "BreakEven",
    "EstProb", "Edge", "ExpROI", "TimeLeft", "Liquidity", "Category",
]


def print_scan(result: ScanResult, *, limit: int, sort: str) -> None:
    print(PAPER_BANNER)
    summary = result.summary()
    print(
        f"Scan at {summary['started_at']} in {summary['duration_seconds']}s: "
        f"{summary['markets_fetched']} markets fetched, {summary['binary_markets']} binary, "
        f"{summary['tradable_markets']} tradable, {summary['books_received']}/{summary['books_requested']} "
        f"books, {summary['favorites_in_band']} sides with executable price in band"
    )
    if not result.data_available:
        print("\nPOLYMARKET DATA UNAVAILABLE")
        for error in result.errors:
            print(f"  {error}")
        return
    candidates = sort_candidates(result.candidates, sort)
    rows = [candidate_row(c) for c in candidates[:limit]]
    print()
    print(render_table(rows, CANDIDATE_HEADERS, align_right={2, 3, 4, 5, 6, 7, 9, 10, 12}))
    if any(not c.fee.known for c in candidates):
        print("\n* fee schedule unavailable: worst-case taker fee assumed")
    print(
        "\nPrice = VWAP to buy the reference stake from the live ask book (fees excluded); "
        "BreakEven = price + taker fee per share."
    )


def sort_candidates(candidates: list[FavoriteCandidate], sort: str) -> list[FavoriteCandidate]:
    if sort == "ev":
        def key(c: FavoriteCandidate) -> tuple[int, float]:
            ev = c.edge.ev_per_share if c.edge and c.edge.ev_per_share is not None else None
            return (0, -ev) if ev is not None else (1, c.hours_to_resolution() or 1e9)
        return sorted(candidates, key=key)
    if sort == "time":
        return sorted(candidates, key=lambda c: c.hours_to_resolution() if c.hours_to_resolution() is not None else 1e9)
    if sort == "price":
        return sorted(candidates, key=lambda c: c.entry_price or 0, reverse=True)
    if sort == "depth":
        return sorted(candidates, key=lambda c: c.ask_depth_usd, reverse=True)
    return candidates


def cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    client = PolymarketClient.from_settings(settings)
    scanner = MarketScanner(client, settings)
    result = scanner.scan()
    if args.json:
        payload = {"summary": result.summary(), "candidates": [c.to_dict() for c in result.candidates]}
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_scan(result, limit=args.limit, sort=args.sort)
    return 0 if result.data_available else 2


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    from .runner import Runner

    print(PAPER_BANNER)
    runner = Runner(settings)
    runner.run_forever(interval=args.interval, cycles=args.cycles)
    return 0


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    from .database import Database

    db = Database(settings.database_path)
    tokens: list[tuple[str, str]] = []
    market = db.get_market(args.id)
    if market:
        outcomes = json.loads(market["outcomes"] or "[]")
        for outcome, token in zip(outcomes, json.loads(market["token_ids"] or "[]")):
            tokens.append((token, outcome))
        print(f"{market['question']}  (market {market['market_id']})")
    else:
        tokens.append((args.id, "token"))
    for token, label in tokens:
        rows = db.price_history(token, limit=args.limit)
        print(f"\n{label} token {token}: {len(rows)} snapshots")
        table = [
            [r["ts"][:19], _fmt_price(r["best_bid"]), _fmt_price(r["best_ask"]), _fmt_price(r["spread"]),
             _fmt_usd(r["ask_depth_usd"]), _fmt_price(r["entry_vwap"]), humanize_hours(r["hours_to_resolution"])]
            for r in rows
        ]
        if table:
            print(render_table(table, ["Time (UTC)", "Bid", "Ask", "Spread", "AskDepth", "VWAP", "TimeLeft"], {1, 2, 3, 4, 5}))
    return 0


def cmd_trades(settings: Settings, args: argparse.Namespace) -> int:
    from .database import Database
    from .paper_trader import PaperTrader

    db = Database(settings.database_path)
    trader = PaperTrader(db, settings)
    print(PAPER_BANNER)
    bank = trader.bankroll()
    print(
        f"Paper bankroll: start ${bank['starting']:,.2f} | realized PnL ${bank['realized_pnl']:+,.2f} | "
        f"open cost ${bank['open_cost']:,.2f} | cash ${bank['cash']:,.2f} | equity (marked) ${bank['equity_marked']:,.2f}"
    )
    where = "kind=?" if args.kind != "all" else "1=1"
    params: list[object] = [args.kind] if args.kind != "all" else []
    if args.status != "all":
        where += " AND status" + (" = 'open'" if args.status == "open" else " != 'open'")
    rows = db.query(f"SELECT * FROM paper_trades WHERE {where} ORDER BY opened_at DESC LIMIT ?", [*params, args.limit])
    table = [
        [
            str(r["trade_id"]), r["kind"], r["opened_at"][:16], _truncate(r["question"] or "", 48), r["outcome"],
            _fmt_price(r["entry_price"]), _fmt_pct(r["est_prob"]),
            "-" if r["edge"] is None else f"{r['edge'] * 100:+.1f}pt",
            _fmt_usd(r["amount_invested"]), r["time_bucket"] or "-", r["status"],
            "-" if r["pnl"] is None else f"{r['pnl']:+,.2f}",
            _fmt_price(r["last_mark"]),
        ]
        for r in rows
    ]
    print()
    print(render_table(table, ["#", "Kind", "Opened", "Market", "Side", "Entry", "EstProb", "Edge", "Cost", "Bucket", "Status", "PnL", "Mark"], {5, 6, 7, 8, 11, 12}))
    return 0


def cmd_settle(settings: Settings, args: argparse.Namespace) -> int:
    from .database import Database
    from .paper_trader import PaperTrader

    db = Database(settings.database_path)
    client = PolymarketClient.from_settings(settings)
    settled = PaperTrader(db, settings).settle(client)
    print(f"settled {len(settled)} paper positions")
    for row in settled:
        print(f"  #{row['trade_id']} {row['kind']} {_truncate(row['question'] or '', 60)} {row['outcome']}: {row['status']} PnL {row['pnl']:+.2f}")
    return 0


def cmd_db_stats(settings: Settings, args: argparse.Namespace) -> int:
    from .database import Database

    db = Database(settings.database_path)
    print(f"database: {settings.database_path}")
    for table, count in db.stats().items():
        print(f"  {table:22s} {count:>10,}")
    print("\ndata sources:")
    for row in db.source_status():
        print(f"  {row['source']:14s} ok={row['ok_count']} errors={row['error_count']} last_ok={row['last_ok']} last_error={row['last_error']}")
    scans = db.recent_scans(5)
    if scans:
        print("\nrecent scans:")
        for scan in scans:
            status = "ok" if scan["data_available"] else f"DATA UNAVAILABLE {scan['errors']}"
            print(f"  #{scan['scan_id']} {scan['started_at']} markets={scan['markets_fetched']} favorites={scan['candidates']} {status}")
    return 0


def cmd_verify(settings: Settings, args: argparse.Namespace) -> int:
    from .verify import run_verification

    return run_verification(settings, phase=args.phase, sample=args.sample)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="favorite-hunter", description="Polymarket favorite edge scanner (paper trading only)")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan once and print favorites in the price band")
    scan.add_argument("--limit", type=int, default=50)
    scan.add_argument("--sort", choices=["ev", "time", "price", "depth"], default="ev")
    scan.add_argument("--json", action="store_true")
    scan.set_defaults(func=cmd_scan)

    run = sub.add_parser("run", help="continuous scan loop with history storage")
    run.add_argument("--interval", type=float, default=None, help="seconds between scans")
    run.add_argument("--cycles", type=int, default=None, help="stop after N cycles")
    run.set_defaults(func=cmd_run)

    history = sub.add_parser("history", help="stored price history for a market id or token id")
    history.add_argument("id")
    history.add_argument("--limit", type=int, default=50)
    history.set_defaults(func=cmd_history)

    trades = sub.add_parser("trades", help="paper positions and PnL")
    trades.add_argument("--kind", choices=["model", "baseline", "all"], default="model")
    trades.add_argument("--status", choices=["open", "closed", "all"], default="all")
    trades.add_argument("--limit", type=int, default=50)
    trades.set_defaults(func=cmd_trades)

    settle = sub.add_parser("settle", help="settle paper positions whose markets resolved")
    settle.set_defaults(func=cmd_settle)

    stats = sub.add_parser("db-stats", help="database row counts and data-source health")
    stats.set_defaults(func=cmd_db_stats)

    verify = sub.add_parser("verify", help="check live Polymarket data and the calculations")
    verify.add_argument("--phase", type=int, default=1)
    verify.add_argument("--sample", type=int, default=5)
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings(args.config)
    try:
        return args.func(settings, args)
    except DataUnavailable as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

