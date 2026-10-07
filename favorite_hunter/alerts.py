"""Telegram / Discord alerts for opportunities that pass every filter.

Secrets come from the environment only:
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and/or DISCORD_WEBHOOK_URL.

An opportunity alerts once; it re-alerts only after ``cooldown_minutes`` and
only if its edge improved by at least ``realert_edge_improvement``. Every
message says PAPER TRADE ONLY.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol

import httpx

from .config import Settings
from .database import Database
from .market_scanner import FavoriteCandidate
from .timeutil import iso, parse_dt, utcnow

log = logging.getLogger(__name__)

ALERT_STATUSES = {"TRADE", "HOLDING"}
MAX_ALERTS_PER_CYCLE = 10
FAILED_RETRY_SECONDS = 300


def _minutes_text(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 0:
        return "past scheduled end"
    minutes = int(round(hours * 60))
    if minutes < 120:
        return f"{minutes} minutes"
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def _pct(value: float | None, *, signed: bool = False, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:{'+' if signed else ''}.1f}%{suffix}"


def format_alert(d: dict[str, Any], *, header: str | None = None) -> str:
    """Alert text from an opportunity's audit dict (``FavoriteCandidate.to_dict()``)."""
    est = d.get("estimate") or {}
    edge = d.get("edge") or {}
    confidence = (d.get("confidence") or {}).get("value")
    score = (d.get("score") or {}).get("value")
    risks = list(dict.fromkeys((est.get("loss_scenarios") or []) + ((d.get("rules") or {}).get("risks") or [])))[:3]
    lines = [header, ""] if header else []
    lines += [
        "POLYMARKET FAVORITE EDGE",
        "Market:", str(d.get("question")),
        "Side:", str(d.get("outcome", "")).upper(),
        "Ask:", f"${d['best_ask']:.3f}" if d.get("best_ask") is not None else "n/a",
        "Estimated Probability:", _pct(est.get("probability")) if est.get("probability") is not None else "DATA UNAVAILABLE",
        "Edge:", _pct(edge.get("probability_edge"), signed=True, suffix=" (after fees)"),
        "Expected ROI:", _pct(edge.get("expected_roi")),
        "Confidence:", f"{confidence:.0f}/100" if confidence is not None else "n/a",
        "Favorite Edge Score:", f"{score:.0f}/100" if score is not None else "n/a",
        "Time Remaining:", _minutes_text(d.get("hours_to_resolution")),
        "Available Size:", f"${d['max_exec_usd']:,.0f}" if d.get("max_exec_usd") is not None else "n/a",
        "Opportunity Type:", str(d.get("opportunity_type") or "n/a"),
        "Main Reason:", est.get("main_reason") or "see dashboard",
        "Risk:", " / ".join(risks) if risks else "see dashboard",
    ]
    if d.get("url"):
        lines += ["Link:", d["url"]]
    lines.append("PAPER TRADE ONLY.")
    return "\n".join(lines)


# Layout preview for `alerts-test` when no opportunity exists yet. Every value
# is labelled SAMPLE so it can never be mistaken for a real signal.
SAMPLE_ALERT: dict[str, Any] = {
    "question": "SAMPLE - Will Team A win?",
    "outcome": "Yes",
    "best_ask": 0.87,
    "estimate": {
        "probability": 0.94,
        "main_reason": "SAMPLE: Team A leads 2-0 in the 78th minute.",
        "loss_scenarios": ["Possible comeback", "match abandonment", "resolution-rule issue"],
    },
    "edge": {"probability_edge": 0.07, "expected_roi": 0.08},
    "confidence": {"value": 91},
    "score": {"value": 84},
    "hours_to_resolution": 0.7,
    "max_exec_usd": 180,
    "opportunity_type": "LIVE_EVENT_EDGE",
}


class Sender(Protocol):
    name: str

    def send(self, text: str) -> None: ...


class TelegramSender:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self.token = token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=15.0)

    def send(self, text: str) -> None:
        response = self.client.post(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            json={"chat_id": self.chat_id, "text": text[:4096], "disable_web_page_preview": True},
        )
        if response.status_code != 200 or not response.json().get("ok"):
            raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text[:200]}")


class DiscordSender:
    name = "discord"

    def __init__(self, webhook_url: str, client: httpx.Client | None = None):
        self.webhook_url = webhook_url
        self.client = client or httpx.Client(timeout=15.0)

    def send(self, text: str) -> None:
        response = self.client.post(self.webhook_url, json={"content": text[:2000]})
        if response.status_code not in (200, 204):
            raise RuntimeError(f"Discord HTTP {response.status_code}: {response.text[:200]}")


def senders_from_settings(settings: Settings) -> tuple[list[Sender], list[str]]:
    senders: list[Sender] = []
    problems: list[str] = []
    cfg = settings.alerts
    if cfg.telegram:
        if settings.telegram_bot_token and settings.telegram_chat_id:
            senders.append(TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id))
        else:
            problems.append("alerts.telegram is on but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set")
    if cfg.discord:
        if settings.discord_webhook_url:
            senders.append(DiscordSender(settings.discord_webhook_url))
        else:
            problems.append("alerts.discord is on but DISCORD_WEBHOOK_URL is not set")
    return senders, problems


class AlertManager:
    def __init__(self, settings: Settings, db: Database, senders: list[Sender]):
        self.settings = settings
        self.db = db
        self.senders = senders

    @classmethod
    def from_settings(cls, settings: Settings, db: Database) -> "AlertManager":
        senders, problems = senders_from_settings(settings)
        for problem in problems:
            log.warning(problem)
        return cls(settings, db, senders)

    def eligible(self, c: FavoriteCandidate) -> bool:
        cfg = self.settings.alerts
        if c.status not in ALERT_STATUSES or c.edge is None or c.edge.probability_edge is None:
            return False
        score = getattr(c.score, "value", None)
        confidence = getattr(c.confidence, "value", None)
        return score is not None and score >= cfg.min_favorite_edge_score and confidence is not None and confidence >= cfg.min_confidence

    def should_alert(self, c: FavoriteCandidate, now: datetime) -> bool:
        cfg = self.settings.alerts
        edge = c.edge.probability_edge  # type: ignore[union-attr]
        last_ok = self.db.query("SELECT ts, edge FROM alerts WHERE key=? AND ok=1 ORDER BY id DESC LIMIT 1", (c.key,))
        if last_ok:
            last_ts = parse_dt(last_ok[0]["ts"])
            if last_ts and (now - last_ts).total_seconds() < cfg.cooldown_minutes * 60:
                return False
            return edge >= (last_ok[0]["edge"] or 0.0) + cfg.realert_edge_improvement
        last_fail = self.db.query("SELECT ts FROM alerts WHERE key=? AND ok=0 ORDER BY id DESC LIMIT 1", (c.key,))
        if last_fail:
            failed_at = parse_dt(last_fail[0]["ts"])
            if failed_at and (now - failed_at).total_seconds() < FAILED_RETRY_SECONDS:
                return False
        return True

    def process(self, candidates: list[FavoriteCandidate], now: datetime | None = None) -> list[dict]:
        now = now or utcnow()
        if not self.settings.alerts.enabled or not self.senders:
            return []
        due = [c for c in candidates if self.eligible(c) and self.should_alert(c, now)]
        due.sort(key=lambda c: -(c.edge.ev_per_share or 0.0))  # type: ignore[union-attr]
        sent = []
        for c in due[:MAX_ALERTS_PER_CYCLE]:
            text = format_alert(c.to_dict())
            for sender in self.senders:
                ok, error = True, None
                try:
                    sender.send(text)
                except Exception as exc:  # network/API failure: recorded, retried later
                    ok, error = False, str(exc)
                    log.warning("%s alert failed for %s: %s", sender.name, c.key, exc)
                self.db.insert(
                    "INSERT INTO alerts (ts, key, channel, edge, score, message, ok, error) VALUES (?,?,?,?,?,?,?,?)",
                    (iso(now), c.key, sender.name, c.edge.probability_edge, getattr(c.score, "value", None), text, int(ok), error),  # type: ignore[union-attr]
                )
                sent.append({"key": c.key, "channel": sender.name, "ok": ok, "error": error})
        return sent
