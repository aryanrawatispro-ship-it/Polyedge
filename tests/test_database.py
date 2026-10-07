from datetime import timedelta

from favorite_hunter.config import Settings
from favorite_hunter.database import Database
from favorite_hunter.models import parse_book, parse_market
from favorite_hunter.runner import Runner

from .factories import NOW, clob_book, gamma_market, mirrored_no_book
from .fakes import FakeClient


def make_db(interval=300):
    return Database(":memory:", snapshot_interval_seconds=interval)


def test_upsert_markets_is_idempotent_and_keeps_fee():
    db = make_db()
    fee = {"exponent": 1, "rate": "0.05", "takerOnly": True, "rebateRate": "0"}
    market = parse_market(gamma_market("1", fee_schedule=fee, tags=["nba"]))
    db.upsert_markets([(market, "sports")], now=NOW)
    db.upsert_markets([(parse_market(gamma_market("1")), "sports")], now=NOW + timedelta(minutes=1))
    row = db.get_market("1")
    assert row["category"] == "sports" and row["fee_rate"] == 0.05  # not overwritten by NULL
    assert row["first_seen"] < row["last_seen"]
    assert db.stats()["markets"] == 1


def test_snapshot_policy_writes_on_change_or_interval():
    db = make_db(interval=300)
    book = parse_book(clob_book("t", bids=[(0.89, 100)], asks=[(0.90, 100)]))
    item = [("m", 0, book, 0.9, 5.0)]
    assert db.record_book_snapshots(1, item, depth_window=0.02, now=NOW) == (1, 1)
    # unchanged top of book, same hash, 60s later -> nothing written
    assert db.record_book_snapshots(2, item, depth_window=0.02, now=NOW + timedelta(seconds=60)) == (0, 0)
    # top of book moves -> price snapshot; book hash unchanged -> no book row
    moved = parse_book(clob_book("t", bids=[(0.90, 100)], asks=[(0.91, 100)]))
    moved.book_hash = book.book_hash
    assert db.record_book_snapshots(3, [("m", 0, moved, 0.91, 4.9)], depth_window=0.02, now=NOW + timedelta(seconds=120)) == (1, 0)
    # interval elapsed with a new hash -> both written
    moved.book_hash = "new"
    assert db.record_book_snapshots(4, [("m", 0, moved, 0.91, 4.8)], depth_window=0.02, now=NOW + timedelta(seconds=500)) == (1, 1)
    history = db.price_history("t")
    assert [r["best_ask"] for r in history] == [0.90, 0.91, 0.91]
    assert history[0]["ask_depth_usd"] == 90.0
    latest = db.latest_book("t")
    assert latest["asks"] == [[0.91, 100.0]] and latest["book_hash"] == "new"


def test_runner_persists_scan_markets_and_snapshots():
    market = gamma_market("1", best_bid=0.89, best_ask=0.90, fees_enabled=False)
    yes = clob_book("11", bids=[(0.89, 500)], asks=[(0.90, 400)])
    client = FakeClient([market], {"11": yes, "12": mirrored_no_book("12", yes)})
    db = make_db()
    runner = Runner(Settings(), client=client, db=db, clock=lambda: NOW)
    report = runner.run_once()
    summary = report.summary()
    assert summary["favorites_in_band"] == 1
    assert summary["price_snapshots_written"] == 1 and summary["book_snapshots_written"] == 1
    stats = db.stats()
    assert stats["scans"] == 1 and stats["markets"] == 1 and stats["price_snapshots"] == 1
    scan = db.recent_scans(1)[0]
    assert scan["data_available"] == 1 and scan["candidates"] == 1
    assert db.source_status()[0]["ok_count"] == 1


def test_runner_records_data_unavailable():
    db = make_db()
    runner = Runner(Settings(), client=FakeClient([], {}, fail=True), db=db, clock=lambda: NOW)
    report = runner.run_once()
    assert not report.scan.data_available
    scan = db.recent_scans(1)[0]
    assert scan["data_available"] == 0 and "blocked by network proxy" in scan["errors"]
    status = db.source_status()[0]
    assert status["error_count"] == 1 and "403" in status["last_error"]
    assert db.stats()["price_snapshots"] == 0
