"""Continuous loop: scan -> persist history -> evaluate -> paper trade ->
record baselines -> mark to market -> settle resolved positions."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .database import Database
from .http import DataUnavailable
from .market_scanner import FavoriteCandidate, MarketScanner, ScanResult
from .opportunities import OpportunityRecorder
from .paper_trader import PaperTrader
from .polymarket_client import PolymarketClient
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)

STATUS_TRADE = "TRADE"
STATUS_HOLDING = "HOLDING"

# evaluate(candidates, now) fills estimate/edge/confidence/score/status in place
Evaluator = Callable[[list[FavoriteCandidate], datetime], None]


@dataclass
class CycleReport:
    scan_id: int
    started_at: datetime
    scan: ScanResult
    price_rows: int = 0
    book_rows: int = 0
    opportunities: int = 0
    trades_opened: list[int] = field(default_factory=list)
    baselines_recorded: int = 0
    marked: int = 0
    settled: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "started_at": iso(self.started_at),
            **self.scan.summary(),
            "price_snapshots_written": self.price_rows,
            "book_snapshots_written": self.book_rows,
            "opportunities": self.opportunities,
            "trades_opened": len(self.trades_opened),
            "baselines_recorded": self.baselines_recorded,
            "positions_marked": self.marked,
            "positions_settled": len(self.settled),
            "alerts_sent": sum(1 for a in self.alerts if a.get("ok")),
            **self.extras,
        }


class Runner:
    def __init__(
        self,
        settings: Settings,
        *,
        client: PolymarketClient | None = None,
        db: Database | None = None,
        evaluator: Evaluator | None = None,
        on_cycle: Callable[["CycleReport"], None] | None = None,
        clock: Callable[[], datetime] = utcnow,
        alerter: Any = None,
    ):
        self.settings = settings
        self.client = client or PolymarketClient.from_settings(settings)
        self.db = db or Database(settings.database_path)
        self.scanner = MarketScanner(self.client, settings)
        self.trader = PaperTrader(self.db, settings)
        self.recorder = OpportunityRecorder(self.db)
        self.evaluator = evaluator
        self.on_cycle = on_cycle
        self.clock = clock
        self.alerter = alerter
        self._last_settle: datetime | None = None

    def run_once(self) -> CycleReport:
        started = self.clock()
        scan_id = self.db.start_scan(started)
        result = self.scanner.scan(now=started)
        report = CycleReport(scan_id=scan_id, started_at=started, scan=result)
        self.db.record_source("polymarket scan", result.data_available, "; ".join(result.errors) or None)
        if result.data_available:
            self._persist(scan_id, result, report)
            if self.evaluator is not None:
                self.evaluator(result.candidates, started)
                report.opportunities = self._record_opportunities(scan_id, result.candidates, started)
            self._trade(result.candidates, report, started)
            self._mark_held(result.candidates)
            if self.alerter is not None:
                report.alerts = self.alerter.process(result.candidates, started)
            self.db.replace_latest(scan_id, result.candidates, started)
            self._mark(result, report, started)
        self._maybe_settle(report, started)
        self._flush_source_health()
        self.db.finish_scan(
            scan_id, result.summary(), opportunities=report.opportunities, trades_opened=len(report.trades_opened)
        )
        if self.on_cycle is not None:
            self.on_cycle(report)
        return report

    def _persist(self, scan_id: int, result: ScanResult, report: CycleReport) -> None:
        self.db.upsert_markets(((c.market, c.category) for c in result.candidates), now=result.started_at)
        items = (
            (c.market.id, c.outcome_index, c.book, c.entry_price, c.hours_to_resolution())
            for c in result.candidates
        )
        report.price_rows, report.book_rows = self.db.record_book_snapshots(
            scan_id, items, depth_window=self.settings.scanner.depth_window, now=result.started_at
        )

    def _record_opportunities(self, scan_id: int, candidates: list[FavoriteCandidate], now: datetime) -> int:
        return self.recorder.record(scan_id, candidates, now)

    def _trade(self, candidates: list[FavoriteCandidate], report: CycleReport, now: datetime) -> None:
        approved = sorted(
            (c for c in candidates if c.status == STATUS_TRADE),
            key=lambda c: -(c.edge.ev_per_share if c.edge and c.edge.ev_per_share is not None else 0.0),
        )
        for candidate in approved:
            trade_id = self.trader.open_model_trade(candidate, now)
            if trade_id is not None:
                report.trades_opened.append(trade_id)
        for candidate in candidates:
            if self.trader.record_baseline(candidate, now) is not None:
                report.baselines_recorded += 1

    def _flush_source_health(self) -> None:
        clients = [getattr(self.client, "http", None)]
        engine = getattr(self.evaluator, "engine", None)
        clients.append(getattr(engine, "http", None))
        for http in clients:
            if http is None or not hasattr(http, "drain_health"):
                continue
            for source, health in http.drain_health().items():
                self.db.record_source_health(source, health)

    def _mark_held(self, candidates: list[FavoriteCandidate]) -> None:
        """Flag candidates already held as a paper position (shown as HOLDING)."""
        held = {row["key"] for row in self.trader.open_trades(kind="model")}
        for candidate in candidates:
            if candidate.key in held and candidate.status == STATUS_TRADE:
                candidate.status = STATUS_HOLDING

    def _mark(self, result: ScanResult, report: CycleReport, now: datetime) -> None:
        books = dict(result.books)
        missing = [t["token_id"] for t in self.trader.open_trades(kind=None) if t["token_id"] not in books]
        if missing:
            try:
                books.update(self.client.get_books(missing, batch_size=self.settings.scanner.book_batch_size))
            except DataUnavailable as exc:
                log.warning("mark-to-market books unavailable: %s", exc)
        report.marked = self.trader.mark_to_market(books, now)

    def _maybe_settle(self, report: CycleReport, now: datetime) -> None:
        interval = self.settings.paper.settle_interval_seconds
        if self._last_settle is not None and (now - self._last_settle).total_seconds() < interval:
            return
        self._last_settle = now
        report.settled = self.trader.settle(self.client, now)

    def run_forever(self, *, interval: float | None = None, cycles: int | None = None) -> None:
        interval = interval or self.settings.loop_interval_seconds
        done = 0
        while cycles is None or done < cycles:
            t0 = time.monotonic()
            try:
                summary = self.run_once().summary()
                log.info(
                    "scan %s: %s markets, %s favorites, %s opportunities, %s trades, %s baselines, %s settled%s",
                    summary["scan_id"], summary["markets_fetched"], summary["favorites_in_band"],
                    summary["opportunities"], summary["trades_opened"], summary["baselines_recorded"],
                    summary["positions_settled"],
                    "" if summary["data_available"] else f" | DATA UNAVAILABLE: {summary['errors']}",
                )
            except Exception:  # keep the loop alive; the error is logged with traceback
                log.exception("scan cycle failed")
            done += 1
            if cycles is not None and done >= cycles:
                break
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
