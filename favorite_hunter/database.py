"""SQLite persistence: markets, scans, price/order-book history, opportunities,
paper trades, alerts and data-source health.

Snapshot policy keeps the database small enough to run for weeks: a price
snapshot is written when the top of book changes or every
``snapshot_interval_seconds``; a full order book when its hash changes and the
interval has elapsed. Books behind every trade decision are stored in full
inside the trade/opportunity record regardless of this policy.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Market, OrderBook
from .orderbook_engine import depth_within
from .timeutil import iso, parse_dt, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    condition_id TEXT,
    question TEXT,
    slug TEXT,
    event_id TEXT,
    event_title TEXT,
    event_slug TEXT,
    category TEXT,
    tags TEXT,
    outcomes TEXT,
    token_ids TEXT,
    description TEXT,
    resolution_source TEXT,
    end_date TEXT,
    game_start_time TEXT,
    neg_risk INTEGER,
    fee_rate REAL,
    fee_exponent REAL,
    fee_source TEXT,
    first_seen TEXT,
    last_seen TEXT,
    closed INTEGER DEFAULT 0,
    resolved_outcome INTEGER,
    resolution_payouts TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_condition ON markets(condition_id);

CREATE TABLE IF NOT EXISTS scans (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    markets_fetched INTEGER,
    tradable_markets INTEGER,
    books_requested INTEGER,
    books_received INTEGER,
    candidates INTEGER,
    opportunities INTEGER,
    trades_opened INTEGER,
    data_available INTEGER,
    errors TEXT
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    ts TEXT,
    market_id TEXT,
    token_id TEXT,
    outcome_index INTEGER,
    best_bid REAL,
    best_ask REAL,
    spread REAL,
    midpoint REAL,
    bid_depth_usd REAL,
    ask_depth_usd REAL,
    entry_vwap REAL,
    last_trade_price REAL,
    hours_to_resolution REAL,
    book_server_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_price_token_ts ON price_snapshots(token_id, ts);
CREATE INDEX IF NOT EXISTS idx_price_market_ts ON price_snapshots(market_id, ts);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    ts TEXT,
    market_id TEXT,
    token_id TEXT,
    book_hash TEXT,
    book_server_ts TEXT,
    bids TEXT,
    asks TEXT
);
CREATE INDEX IF NOT EXISTS idx_book_token_ts ON orderbook_snapshots(token_id, ts);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER,
    ts TEXT,
    key TEXT,
    market_id TEXT,
    token_id TEXT,
    outcome_index INTEGER,
    outcome TEXT,
    question TEXT,
    category TEXT,
    entry_price REAL,
    best_ask REAL,
    fee_per_share REAL,
    break_even REAL,
    est_prob REAL,
    prob_uncertainty REAL,
    edge REAL,
    ev_per_share REAL,
    roi REAL,
    confidence REAL,
    score REAL,
    status TEXT,
    opportunity_type TEXT,
    hours_to_resolution REAL,
    time_bucket TEXT,
    liquidity REAL,
    volume REAL,
    ask_depth_usd REAL,
    max_exec_usd REAL,
    spread REAL,
    skip_reasons TEXT,
    risk_flags TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_key_ts ON opportunities(key, ts);
CREATE INDEX IF NOT EXISTS idx_opp_scan ON opportunities(scan_id);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    opened_at TEXT,
    key TEXT,
    market_id TEXT,
    condition_id TEXT,
    token_id TEXT,
    outcome_index INTEGER,
    outcome TEXT,
    question TEXT,
    event_id TEXT,
    category TEXT,
    entry_price REAL,
    best_ask REAL,
    fee_per_share REAL,
    avg_cost REAL,
    shares REAL,
    notional REAL,
    fees REAL,
    amount_invested REAL,
    est_prob REAL,
    prob_uncertainty REAL,
    edge REAL,
    ev_per_share REAL,
    expected_profit REAL,
    expected_roi REAL,
    confidence REAL,
    score REAL,
    opportunity_type TEXT,
    strategy TEXT,
    hours_to_resolution REAL,
    time_bucket TEXT,
    entry_bucket TEXT,
    liquidity REAL,
    volume REAL,
    ask_depth_usd REAL,
    spread REAL,
    resolution_time TEXT,
    fill_json TEXT,
    detail_json TEXT,
    status TEXT DEFAULT 'open',
    resolved_at TEXT,
    resolution_outcome INTEGER,
    payout_per_share REAL,
    payout REAL,
    pnl REAL,
    roi REAL,
    last_mark REAL,
    last_mark_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(kind, status);
CREATE INDEX IF NOT EXISTS idx_trades_key ON paper_trades(key, kind);

CREATE TABLE IF NOT EXISTS latest_opportunities (
    key TEXT PRIMARY KEY,
    scan_id INTEGER,
    ts TEXT,
    status TEXT,
    category TEXT,
    ev_per_share REAL,
    score REAL,
    confidence REAL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    key TEXT,
    channel TEXT,
    edge REAL,
    score REAL,
    message TEXT,
    ok INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(key, ts);

CREATE TABLE IF NOT EXISTS source_status (
    source TEXT PRIMARY KEY,
    last_ok TEXT,
    last_error TEXT,
    last_error_ts TEXT,
    ok_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS backtest_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    market_id TEXT,
    condition_id TEXT,
    question TEXT,
    category TEXT,
    outcome_index INTEGER,
    outcome TEXT,
    observed_at TEXT,
    hours_before_end REAL,
    time_bucket TEXT,
    price REAL,
    entry_bucket TEXT,
    fee_per_share REAL,
    won INTEGER,
    payout_per_share REAL,
    volume REAL,
    liquidity REAL,
    end_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_bt_run ON backtest_observations(run_id);
"""


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, default=str)


class Database:
    def __init__(self, path: str | Path, *, snapshot_interval_seconds: float = 300.0, book_levels: int = 20):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.book_levels = book_levels
        # last stored top-of-book / hash per token, for the snapshot policy
        self._last_price: dict[str, tuple[datetime, float | None, float | None]] = {}
        self._last_book: dict[str, tuple[datetime, str | None]] = {}
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, tuple(params)).fetchall()]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a statement; returns the number of affected rows."""
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run an INSERT; returns the new row id."""
        with self._lock:
            return int(self._conn.execute(sql, tuple(params)).lastrowid)

    # ---------------------------------------------------------------- markets

    def upsert_markets(self, markets: Iterable[tuple[Market, str]], now: datetime | None = None) -> None:
        """Insert or refresh market metadata. ``markets`` yields (market, category)."""
        stamp = iso(now or utcnow())
        rows = []
        for market, category in markets:
            event = market.event
            fee = market.fee_schedule
            rows.append(
                (
                    market.id, market.condition_id, market.question, market.slug,
                    event.id if event else None, event.title if event else None,
                    event.slug if event else None, category, _json(market.all_tags()),
                    _json(market.outcomes), _json(market.token_ids), market.description,
                    market.resolution_source, iso(market.end_date), iso(market.game_start_time),
                    None if market.neg_risk is None else int(market.neg_risk),
                    fee.rate if fee else None, fee.exponent if fee else None, fee.source if fee else None,
                    stamp, stamp, int(bool(market.closed)),
                )
            )
        if not rows:
            return
        with self.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO markets (
                    market_id, condition_id, question, slug, event_id, event_title, event_slug,
                    category, tags, outcomes, token_ids, description, resolution_source, end_date,
                    game_start_time, neg_risk, fee_rate, fee_exponent, fee_source, first_seen,
                    last_seen, closed
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(market_id) DO UPDATE SET
                    question=excluded.question, category=excluded.category, tags=excluded.tags,
                    description=excluded.description, end_date=excluded.end_date,
                    game_start_time=excluded.game_start_time,
                    fee_rate=COALESCE(excluded.fee_rate, markets.fee_rate),
                    fee_exponent=COALESCE(excluded.fee_exponent, markets.fee_exponent),
                    fee_source=COALESCE(excluded.fee_source, markets.fee_source),
                    last_seen=excluded.last_seen, closed=excluded.closed
                """,
                rows,
            )

    def mark_resolved(
        self, market_id: str, *, outcome: int | None, payouts: list[float] | None, resolved_at: datetime | None
    ) -> None:
        self.execute(
            "UPDATE markets SET closed=1, resolved_outcome=?, resolution_payouts=?, resolved_at=? WHERE market_id=?",
            (outcome, _json(payouts), iso(resolved_at), market_id),
        )

    def get_market(self, market_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM markets WHERE market_id=?", (market_id,))
        return rows[0] if rows else None

    # ------------------------------------------------------------------ scans

    def start_scan(self, started_at: datetime) -> int:
        return self.insert("INSERT INTO scans(started_at) VALUES (?)", (iso(started_at),))

    def finish_scan(self, scan_id: int, summary: dict[str, Any], *, opportunities: int = 0, trades_opened: int = 0) -> None:
        self.execute(
            """
            UPDATE scans SET finished_at=?, markets_fetched=?, tradable_markets=?, books_requested=?,
                books_received=?, candidates=?, opportunities=?, trades_opened=?, data_available=?, errors=?
            WHERE scan_id=?
            """,
            (
                iso(utcnow()), summary.get("markets_fetched"), summary.get("tradable_markets"),
                summary.get("books_requested"), summary.get("books_received"),
                summary.get("favorites_in_band"), opportunities, trades_opened,
                int(bool(summary.get("data_available"))), _json(summary.get("errors")), scan_id,
            ),
        )

    def recent_scans(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM scans ORDER BY scan_id DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------- snapshots

    def record_book_snapshots(
        self,
        scan_id: int | None,
        items: Iterable[tuple[str, int, OrderBook, float | None, float | None]],
        *,
        depth_window: float,
        now: datetime | None = None,
        force: bool = False,
    ) -> tuple[int, int]:
        """Store price (and possibly full book) snapshots.

        ``items`` yields (market_id, outcome_index, book, entry_vwap,
        hours_to_resolution). Returns (price rows, book rows) written.
        """
        now = now or utcnow()
        stamp = iso(now)
        price_rows = []
        book_rows = []
        for market_id, outcome_index, book, entry_vwap, hours in items:
            token = book.token_id
            last = self._last_price.get(token)
            changed = last is None or (last[1], last[2]) != (book.best_bid, book.best_ask)
            due = last is None or (now - last[0]).total_seconds() >= self.snapshot_interval_seconds
            if force or changed or due:
                price_rows.append(
                    (
                        scan_id, stamp, market_id, token, outcome_index, book.best_bid, book.best_ask,
                        book.spread, book.midpoint,
                        depth_within(book.bids, book.best_bid, depth_window, side="bid"),
                        depth_within(book.asks, book.best_ask, depth_window, side="ask"),
                        entry_vwap, book.last_trade_price, hours, iso(book.server_time),
                    )
                )
                self._last_price[token] = (now, book.best_bid, book.best_ask)
            last_book = self._last_book.get(token)
            book_due = last_book is None or (
                last_book[1] != book.book_hash
                and (now - last_book[0]).total_seconds() >= self.snapshot_interval_seconds
            )
            if force or book_due:
                levels = book.to_dict(self.book_levels)
                book_rows.append(
                    (
                        scan_id, stamp, market_id, token, book.book_hash, iso(book.server_time),
                        json.dumps(levels["bids"]), json.dumps(levels["asks"]),
                    )
                )
                self._last_book[token] = (now, book.book_hash)
        with self.transaction() as conn:
            if price_rows:
                conn.executemany(
                    """
                    INSERT INTO price_snapshots (
                        scan_id, ts, market_id, token_id, outcome_index, best_bid, best_ask, spread,
                        midpoint, bid_depth_usd, ask_depth_usd, entry_vwap, last_trade_price,
                        hours_to_resolution, book_server_ts
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    price_rows,
                )
            if book_rows:
                conn.executemany(
                    """
                    INSERT INTO orderbook_snapshots (
                        scan_id, ts, market_id, token_id, book_hash, book_server_ts, bids, asks
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    book_rows,
                )
        return len(price_rows), len(book_rows)

    def price_history(self, token_id: str, *, since: datetime | None = None, limit: int = 5000) -> list[dict[str, Any]]:
        if since is not None:
            return self.query(
                "SELECT * FROM price_snapshots WHERE token_id=? AND ts>=? ORDER BY ts LIMIT ?",
                (token_id, iso(since), limit),
            )
        return self.query(
            "SELECT * FROM (SELECT * FROM price_snapshots WHERE token_id=? ORDER BY ts DESC LIMIT ?) ORDER BY ts",
            (token_id, limit),
        )

    def latest_book(self, token_id: str) -> dict[str, Any] | None:
        rows = self.query(
            "SELECT * FROM orderbook_snapshots WHERE token_id=? ORDER BY ts DESC LIMIT 1", (token_id,)
        )
        if not rows:
            return None
        row = rows[0]
        row["bids"] = json.loads(row["bids"])
        row["asks"] = json.loads(row["asks"])
        return row

    # ------------------------------------------------------ latest snapshot

    def replace_latest(self, scan_id: int, candidates: Iterable[Any], now: datetime) -> int:
        """Replace the dashboard's current view with this scan's candidates."""
        rows = []
        for c in candidates:
            rows.append(
                (
                    c.key, scan_id, iso(now), c.status, c.category,
                    c.edge.ev_per_share if c.edge else None,
                    getattr(c.score, "value", None), getattr(c.confidence, "value", None),
                    json.dumps(c.to_dict(), default=str),
                )
            )
        with self.transaction() as conn:
            conn.execute("DELETE FROM latest_opportunities")
            conn.executemany(
                "INSERT INTO latest_opportunities (key, scan_id, ts, status, category, ev_per_share, score, confidence, detail_json) VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return len(rows)

    def latest_opportunities(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM latest_opportunities")
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return rows

    def latest_opportunity(self, key: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM latest_opportunities WHERE key=?", (key,))
        if not rows:
            return None
        row = rows[0]
        row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return row

    # ---------------------------------------------------------- source health

    def record_source(self, source: str, ok: bool, error: str | None = None, now: datetime | None = None) -> None:
        stamp = iso(now or utcnow())
        if ok:
            self.execute(
                """
                INSERT INTO source_status(source, last_ok, ok_count) VALUES (?, ?, 1)
                ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok, ok_count=source_status.ok_count+1
                """,
                (source, stamp),
            )
        else:
            self.execute(
                """
                INSERT INTO source_status(source, last_error, last_error_ts, error_count) VALUES (?, ?, ?, 1)
                ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error,
                    last_error_ts=excluded.last_error_ts, error_count=source_status.error_count+1
                """,
                (source, error, stamp),
            )

    def record_source_health(self, source: str, health: Any) -> None:
        """Merge an HttpClient SourceHealth delta into source_status."""
        self.execute(
            """
            INSERT INTO source_status(source, last_ok, last_error, last_error_ts, ok_count, error_count)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                last_ok=COALESCE(excluded.last_ok, source_status.last_ok),
                last_error=COALESCE(excluded.last_error, source_status.last_error),
                last_error_ts=COALESCE(excluded.last_error_ts, source_status.last_error_ts),
                ok_count=source_status.ok_count + excluded.ok_count,
                error_count=source_status.error_count + excluded.error_count
            """,
            (source, iso(health.last_ok), health.last_error, iso(health.last_error_at), health.ok, health.errors),
        )

    def source_status(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM source_status ORDER BY source")

    def meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def stats(self) -> dict[str, int]:
        tables = ["markets", "scans", "price_snapshots", "orderbook_snapshots", "opportunities", "paper_trades", "alerts"]
        return {t: self.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in tables}


def parse_row_time(row: dict[str, Any], key: str) -> datetime | None:
    return parse_dt(row.get(key))
