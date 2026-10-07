"""Continuous scan loop: scan -> persist history -> (later phases) evaluate,
paper trade, alert and settle."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config import Settings
from .database import Database
from .market_scanner import MarketScanner, ScanResult
from .polymarket_client import PolymarketClient
from .timeutil import iso, utcnow

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    scan_id: int
    started_at: datetime
    scan: ScanResult
    price_rows: int = 0
    book_rows: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "started_at": iso(self.started_at),
            **self.scan.summary(),
            "price_snapshots_written": self.price_rows,
            "book_snapshots_written": self.book_rows,
            **self.extras,
        }


class Runner:
    def __init__(
        self,
        settings: Settings,
        *,
        client: PolymarketClient | None = None,
        db: Database | None = None,
    ):
        self.settings = settings
        self.client = client or PolymarketClient.from_settings(settings)
        self.db = db or Database(settings.database_path)
        self.scanner = MarketScanner(self.client, settings)

    def run_once(self) -> CycleReport:
        started = utcnow()
        scan_id = self.db.start_scan(started)
        result = self.scanner.scan(now=started)
        report = CycleReport(scan_id=scan_id, started_at=started, scan=result)
        self.db.record_source("polymarket", result.data_available, "; ".join(result.errors) or None)
        if result.data_available:
            self._persist(scan_id, result, report)
        self.db.finish_scan(scan_id, result.summary())
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

    def run_forever(self, *, interval: float | None = None, cycles: int | None = None) -> None:
        interval = interval or self.settings.loop_interval_seconds
        done = 0
        while cycles is None or done < cycles:
            t0 = time.monotonic()
            try:
                report = self.run_once()
                summary = report.summary()
                log.info(
                    "scan %s: %s markets, %s favorites in band, %s price/%s book snapshots%s",
                    summary["scan_id"], summary["markets_fetched"], summary["favorites_in_band"],
                    summary["price_snapshots_written"], summary["book_snapshots_written"],
                    "" if summary["data_available"] else f" | DATA UNAVAILABLE: {summary['errors']}",
                )
            except Exception:  # keep the loop alive; the error is logged with traceback
                log.exception("scan cycle failed")
            done += 1
            if cycles is not None and done >= cycles:
                break
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))
