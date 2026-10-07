"""Text rendering of analytics reports for the CLI."""

from __future__ import annotations

from .analytics import INSUFFICIENT, LOSING, WINNING, Report, Stats


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _usd(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)), "  ".join("-" * w for w in widths)]
    last = len(headers) - 1
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) if i in (0, last) else cell.rjust(widths[i]) for i, cell in enumerate(row))
        )
    return "\n".join(lines)


def verdict_label(stats: Stats) -> str:
    if stats.verdict == INSUFFICIENT:
        return f"INSUFFICIENT DATA (n={stats.n_bets})"
    marker = {WINNING: "+", LOSING: "!!"}.get(stats.verdict, "=")
    suffix = "" if stats.significant else " (not significant)"
    return f"{marker} {stats.verdict}{suffix}"


STATS_HEADERS = [
    "Group", "Bets", "WinRate", "95% CI", "AvgEntry", "BreakEven", "Expected", "Gap", "ROI", "PnL",
    "MaxDD", "LargestLoss", "LoseStreak", "Verdict",
]


def stats_row(s: Stats) -> list[str]:
    ci = "-" if s.win_rate_ci_low is None else f"{s.win_rate_ci_low * 100:.1f}-{s.win_rate_ci_high * 100:.1f}%"
    gap = "-" if s.edge_vs_break_even is None else f"{s.edge_vs_break_even * 100:+.1f}pt"
    return [
        s.group, str(s.n_bets), _pct(s.win_rate), ci, "-" if s.avg_entry_price is None else f"{s.avg_entry_price:.3f}",
        _pct(s.break_even_win_rate), _pct(s.expected_win_rate), gap, _pct(s.roi, 2), _usd(s.total_pnl),
        _usd(s.max_drawdown), _usd(s.largest_loss), str(s.longest_losing_streak), verdict_label(s),
    ]


def render_report(report: Report, *, dimensions: list[str] | None = None) -> str:
    out = [f"=== {report.dataset} ==="]
    o = report.overall
    out.append(
        f"{o.n_bets} settled bets | win rate {_pct(o.win_rate)} vs break-even {_pct(o.break_even_win_rate)} | "
        f"ROI {_pct(o.roi, 2)} | PnL {_usd(o.total_pnl)} | max drawdown {_usd(o.max_drawdown)} | "
        f"largest loss {_usd(o.largest_loss)} | longest losing streak {o.longest_losing_streak}"
    )
    if o.wins_needed_per_loss:
        out.append(f"At the average cost, one loss wipes out ~{o.wins_needed_per_loss:.1f} wins.")
    out.append(f"Verdict: {verdict_label(o)} - {o.note}")

    out.append("\nEXTREME FAVORITES (is the realised win rate high enough to justify the price?)")
    out.append(_table([stats_row(s) for s in report.extreme], STATS_HEADERS))

    for dim in dimensions or list(report.breakdowns):
        rows = report.breakdowns.get(dim) or []
        if not rows:
            continue
        out.append(f"\nBy {dim.replace('_', ' ')}")
        out.append(_table([stats_row(s) for s in rows], STATS_HEADERS))

    for title, bins in (("model probability", report.calibration_model), ("price-implied probability", report.calibration_price)):
        if not bins or not any(b.n for b in bins):
            continue
        out.append(f"\nCalibration by {title}")
        rows = [
            [b.label, str(b.n), _pct(b.predicted), _pct(b.actual),
             "-" if b.ci_low is None else f"{b.ci_low * 100:.1f}-{b.ci_high * 100:.1f}%",
             "-" if b.predicted is None or b.actual is None else f"{(b.actual - b.predicted) * 100:+.1f}pt"]
            for b in bins if b.n
        ]
        out.append(_table(rows, ["Bin", "N", "Predicted", "Actual", "95% CI", "Actual-Pred"]))
    return "\n".join(out)
