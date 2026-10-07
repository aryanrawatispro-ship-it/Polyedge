"""Dashboard: a read-only JSON API over the database plus a static dark UI.

There is no endpoint that can place an order: the dashboard only reads what
the scan loop wrote.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..analytics import bets_from_backtest, bets_from_trades, build_report
from ..backtest import load_observations
from ..config import Settings
from ..database import Database
from ..paper_trader import PaperTrader
from ..timeutil import parse_dt, utcnow

STATIC_DIR = Path(__file__).parent / "static"
STATUS_SORT = {"TRADE": 0, "HOLDING": 1, "FILTERED": 2, "NO EDGE": 3, "CONFLICT": 4, "DATA UNAVAILABLE": 5}


def summarize_opportunity(row: dict[str, Any]) -> dict[str, Any]:
    d = row["detail"]
    edge = d.get("edge") or {}
    estimate = d.get("estimate") or {}
    return {
        "key": row["key"],
        "market": d.get("question"),
        "event": d.get("event_title"),
        "url": d.get("url"),
        "side": d.get("outcome"),
        "category": d.get("category"),
        "price": d.get("entry_price"),
        "best_ask": d.get("best_ask"),
        "fee_per_share": d.get("fee_per_share"),
        "break_even": d.get("break_even"),
        "est_prob": estimate.get("probability"),
        "uncertainty": estimate.get("uncertainty"),
        "edge": edge.get("probability_edge"),
        "ev_per_share": edge.get("ev_per_share"),
        "expected_roi": edge.get("expected_roi"),
        "confidence": (d.get("confidence") or {}).get("value") if estimate.get("probability") is not None else None,
        "score": (d.get("score") or {}).get("value"),
        "hours_to_resolution": d.get("hours_to_resolution"),
        "time_remaining": d.get("time_remaining"),
        "time_bucket": d.get("time_bucket"),
        "liquidity": d.get("liquidity"),
        "ask_depth_usd": d.get("ask_depth_usd"),
        "max_exec_usd": d.get("max_exec_usd"),
        "spread": d.get("spread"),
        "status": row["status"],
        "opportunity_type": d.get("opportunity_type"),
        "skip_reasons": d.get("skip_reasons") or [],
        "data_status": estimate.get("data_status"),
        "main_reason": estimate.get("main_reason"),
        "scanned_at": d.get("scanned_at"),
    }


def create_app(settings: Settings, db: Database | None = None) -> FastAPI:
    app = FastAPI(title="Favorite Hunter", docs_url=None, redoc_url=None, openapi_url=None)
    db = db or Database(settings.database_path)
    trader = PaperTrader(db, settings)
    app.state.db = db

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        scans = db.recent_scans(1)
        last = scans[0] if scans else None
        now = utcnow()
        age = None
        if last and last.get("started_at"):
            started = parse_dt(last["started_at"])
            age = (now - started).total_seconds() if started else None
        return {
            "mode": "PAPER TRADING ONLY",
            # Databases seeded by tests/demo scripts carry this flag; the UI shows a banner.
            "synthetic_data": db.meta("synthetic_data") == "1",
            "now": now.isoformat(),
            "last_scan": last,
            "last_scan_age_seconds": age,
            "sources": db.source_status(),
            "bankroll": trader.bankroll(),
            "counts": db.stats(),
        }

    @app.get("/api/opportunities")
    def opportunities(include_unavailable: bool = Query(True)) -> dict[str, Any]:
        rows = [summarize_opportunity(r) for r in db.latest_opportunities()]
        if not include_unavailable:
            rows = [r for r in rows if r["status"] != "DATA UNAVAILABLE"]
        rows.sort(key=lambda r: (r["ev_per_share"] is None, -(r["ev_per_share"] or 0.0), STATUS_SORT.get(r["status"], 9)))
        return {"opportunities": rows, "count": len(rows)}

    @app.get("/api/opportunity")
    def opportunity(key: str) -> dict[str, Any]:
        row = db.latest_opportunity(key)
        if row is None:
            history_rows = db.query("SELECT * FROM opportunities WHERE key=? ORDER BY id DESC LIMIT 1", (key,))
            if not history_rows:
                raise HTTPException(404, "unknown opportunity")
            detail = json.loads(history_rows[0]["detail_json"])
            status_value = history_rows[0]["status"]
        else:
            detail, status_value = row["detail"], row["status"]
        token = detail.get("token_id")
        prices = db.price_history(token, limit=2000) if token else []
        estimates = db.query(
            "SELECT ts, est_prob, entry_price, edge, status FROM opportunities WHERE key=? ORDER BY id DESC LIMIT 500", (key,)
        )
        book = db.latest_book(token) if token else None
        trades = db.query("SELECT trade_id, kind, opened_at, status, amount_invested, entry_price, pnl FROM paper_trades WHERE key=? ORDER BY trade_id", (key,))
        return {
            "status": status_value,
            "detail": detail,
            "price_history": [{"ts": p["ts"], "best_bid": p["best_bid"], "best_ask": p["best_ask"]} for p in prices],
            "estimate_history": list(reversed(estimates)),
            "stored_book": book,
            "trades": trades,
            "settings": {"min_edge": settings.filters.min_edge, "reference_stake_usd": settings.scanner.reference_stake_usd},
        }

    @app.get("/api/trades")
    def trades(kind: str = "model", status_filter: str = Query("all", alias="status"), limit: int = 500) -> dict[str, Any]:
        where = ["kind = ?"]
        params: list[Any] = [kind]
        if status_filter == "open":
            where.append("status = 'open'")
        elif status_filter == "closed":
            where.append("status != 'open'")
        rows = db.query(
            f"SELECT * FROM paper_trades WHERE {' AND '.join(where)} ORDER BY opened_at DESC LIMIT ?", [*params, limit]
        )
        for row in rows:
            row.pop("detail_json", None)
            row.pop("fill_json", None)
            if row["status"] == "open" and row.get("last_mark") is not None:
                row["unrealized_pnl"] = row["shares"] * row["last_mark"] - row["amount_invested"]
        return {"bankroll": trader.bankroll() if kind == "model" else None, "trades": rows}

    @app.get("/api/analytics")
    def analytics(dataset: str = "model", min_sample: int = 30, run_id: str | None = None) -> dict[str, Any]:
        if dataset == "backtest":
            bets = bets_from_backtest(load_observations(db, run_id))
        elif dataset in ("model", "baseline"):
            bets = bets_from_trades(db.query("SELECT * FROM paper_trades WHERE kind=? AND status != 'open'", (dataset,)))
        else:
            raise HTTPException(400, "dataset must be model, baseline or backtest")
        report = build_report(bets, dataset, min_sample=min_sample)
        payload = report.to_dict()
        payload["n_bets"] = len(bets)
        if dataset == "backtest":
            runs = db.query("SELECT run_id, COUNT(*) AS n FROM backtest_observations GROUP BY run_id ORDER BY run_id DESC LIMIT 20")
            payload["runs"] = runs
        return payload

    @app.get("/api/scans")
    def scans(limit: int = 50) -> dict[str, Any]:
        return {"scans": db.recent_scans(limit)}

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "scanner": settings.scanner.model_dump(),
            "filters": settings.filters.model_dump(),
            "fees": settings.fees.model_dump(),
            "paper": settings.paper.model_dump(),
            "score_weights": settings.score_weights.model_dump(),
            "confidence_weights": settings.confidence_weights.model_dump(),
            "alerts": settings.alerts.model_dump(),
            "secrets_configured": {
                "telegram": bool(settings.telegram_bot_token and settings.telegram_chat_id),
                "discord": bool(settings.discord_webhook_url),
                "odds_api": bool(settings.odds_api_key),
            },
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def run_dashboard(settings: Settings, *, host: str, port: int, with_runner: bool) -> None:
    import uvicorn

    db = Database(settings.database_path)
    if with_runner:
        from ..evaluator import Evaluator
        from ..runner import Runner

        runner = Runner(settings, db=db, evaluator=Evaluator.from_settings(settings))
        thread = threading.Thread(target=runner.run_forever, name="scan-loop", daemon=True)
        thread.start()
    uvicorn.run(create_app(settings, db), host=host, port=port, log_level="warning")
