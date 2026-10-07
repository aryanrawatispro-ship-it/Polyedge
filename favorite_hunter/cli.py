"""Command-line interface.

    favorite-hunter scan      one scan, print favorites in the price band
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

