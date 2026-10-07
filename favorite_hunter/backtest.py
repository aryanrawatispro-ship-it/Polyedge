"""Historical favorite backtest on resolved Polymarket markets.

For each recently resolved binary market, the price of each side is sampled at
fixed offsets before the market closed (one offset per time bucket). Every side
whose sampled price falls inside the favorite band becomes an observation
("what if we had bought this favorite then?"), settled with the market's
actual payout.

Limitations, shown with every report:
* prices come from Polymarket's price history (traded/marked prices), not
  executable asks, so an assumed slippage is added to every entry;
* order-book depth is unknown historically, so each observation is a flat
  stake and capacity is not modelled;
* this measures blind favorites by price, not the probability model (the
  model needs the external data that existed at the time).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .categories import detect_category
from .config import Settings
from .database import Database
from .http import DataUnavailable
from .models import FeeSchedule, parse_market
from .paper_trader import entry_bucket, resolution_from_gamma
from .polymarket_client import PolymarketClient
from .timeutil import iso, parse_dt, utcnow

log = logging.getLogger(__name__)

# One sample time per time bucket: hours before the market closed.
SAMPLE_OFFSETS_HOURS: dict[str, float] = {"<1h": 0.5, "1-6h": 3.0, "6-24h": 12.0, "1-3d": 48.0, "3d+": 120.0}


@dataclass
class BacktestConfig:
    days: int = 30
    max_markets: int = 500
    min_volume: float = 10_000.0
    slippage: float = 0.005  # assumed cost above the historical price (about half a 1c spread)
    bucket_seconds: int = 300
    offsets_hours: dict[str, float] = field(default_factory=lambda: dict(SAMPLE_OFFSETS_HOURS))


@dataclass
class BacktestRun:
    run_id: str
    started_at: datetime
    config: BacktestConfig
    markets_scanned: int = 0
    markets_used: int = 0
    observations: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": iso(self.started_at),
            "days": self.config.days,
            "min_volume": self.config.min_volume,
            "slippage": self.config.slippage,
            "markets_scanned": self.markets_scanned,
            "markets_used": self.markets_used,
            "observations": self.observations,
            "skipped": self.skipped,
            "errors": self.errors,
        }


def price_at(points: list[tuple[datetime, float]], when: datetime, max_age: timedelta) -> float | None:
    """Last price at or before ``when`` that is no older than ``max_age``."""
    best: tuple[datetime, float] | None = None
    for ts, price in points:
        if ts > when:
            break
        best = (ts, price)
    if best is None or when - best[0] > max_age:
        return None
    return best[1]


def fee_for(raw: dict[str, Any], settings: Settings) -> FeeSchedule:
    market = parse_market(raw)
    if market is not None and market.fee_schedule is not None:
        return market.fee_schedule
    return FeeSchedule(
        rate=settings.fees.unknown_fee_rate,
        exponent=settings.fees.unknown_fee_exponent,
        source="worst-case assumption",
        known=False,
    )


def run_backtest(
    client: PolymarketClient,
    db: Database,
    settings: Settings,
    cfg: BacktestConfig | None = None,
    *,
    now: datetime | None = None,
) -> BacktestRun:
    cfg = cfg or BacktestConfig()
    now = now or utcnow()
    run = BacktestRun(run_id=now.strftime("%Y%m%dT%H%M%SZ"), started_at=now, config=cfg)
    band = settings.scanner
    try:
        raw_markets = list(
            client.iter_markets(
                closed=True,
                page_size=min(500, cfg.max_markets),
                max_markets=cfg.max_markets,
                end_date_min=now - timedelta(days=cfg.days),
                end_date_max=now,
                volume_num_min=cfg.min_volume,
                include_tag=True,
            )
        )
    except DataUnavailable as exc:
        run.errors.append(str(exc))
        return run

    rows = []
    longest = max(cfg.offsets_hours.values())
    for raw in raw_markets:
        run.markets_scanned += 1
        market = parse_market(raw)
        if market is None:
            run.skip("not a binary CLOB market")
            continue
        resolution = resolution_from_gamma(raw)
        if resolution is None:
            run.skip("not finally resolved")
            continue
        closed_at = parse_dt(raw.get("closedTime")) or market.end_date
        if closed_at is None:
            run.skip("no close time")
            continue
        try:
            points = client.get_price_history(
                market.token_ids[0],
                start=closed_at - timedelta(hours=longest + 6),
                end=closed_at,
                bucket_seconds=cfg.bucket_seconds,
            )
        except DataUnavailable as exc:
            run.skip("price history unavailable")
            log.debug("history unavailable for %s: %s", market.id, exc.reason)
            continue
        if not points:
            run.skip("empty price history")
            continue
        fee = fee_for(raw, settings)
        category = detect_category(market)
        used = False
        for bucket, hours in cfg.offsets_hours.items():
            when = closed_at - timedelta(hours=hours)
            max_age = timedelta(hours=max(0.5, hours * 0.1))
            yes_price = price_at(points, when, max_age)
            if yes_price is None:
                continue
            for side, price in ((0, yes_price), (1, 1.0 - yes_price)):
                entry = round(price + cfg.slippage, 6)
                if not band.price_min <= entry <= band.price_max:
                    continue
                payout = resolution.payouts[side]
                rows.append(
                    (
                        run.run_id, market.id, market.condition_id, market.question, category, side,
                        market.outcomes[side], iso(when), hours, bucket, entry, entry_bucket(entry),
                        fee.fee_per_share(entry), int(payout >= 0.999), payout, market.volume,
                        market.liquidity, iso(closed_at),
                    )
                )
                used = True
        if used:
            run.markets_used += 1
        else:
            run.skip("no side inside the price band at the sample times")
    if rows:
        with db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO backtest_observations (
                    run_id, market_id, condition_id, question, category, outcome_index, outcome,
                    observed_at, hours_before_end, time_bucket, price, entry_bucket, fee_per_share, won,
                    payout_per_share, volume, liquidity, end_date
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
    run.observations = len(rows)
    return run


def load_observations(db: Database, run_id: str | None = None) -> list[dict[str, Any]]:
    if run_id is None:
        latest = db.query("SELECT run_id FROM backtest_observations ORDER BY id DESC LIMIT 1")
        if not latest:
            return []
        run_id = latest[0]["run_id"]
    return db.query("SELECT * FROM backtest_observations WHERE run_id=? ORDER BY end_date", (run_id,))
