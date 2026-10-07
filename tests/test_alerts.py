import json
from datetime import timedelta

import httpx
import pytest
import respx

from favorite_hunter.alerts import SAMPLE_ALERT, AlertManager, DiscordSender, TelegramSender, format_alert, senders_from_settings
from favorite_hunter.config import Settings
from favorite_hunter.database import Database
from favorite_hunter.evaluator import Evaluator
from favorite_hunter.probability.engine import ProbabilityEngine
from favorite_hunter.runner import Runner
from favorite_hunter.scoring import compute_favorite_edge_score

from .factories import NOW, clob_book, gamma_market, mirrored_no_book
from .fakes import FakeClient
from .test_evaluation import StubEngine, est, make_candidate


class Recorder:
    def __init__(self, name="recorder", fail=False):
        self.name = name
        self.fail = fail
        self.messages = []

    def send(self, text):
        if self.fail:
            raise RuntimeError("HTTP 500")
        self.messages.append(text)


def scored_candidate(p=0.97, settings=None):
    settings = settings or Settings()
    c = make_candidate()
    Evaluator(settings, ProbabilityEngine(settings, [StubEngine(est(p, sources=("A", "B", "C"), quality=0.9, certainty=0.95))]),
              compute_favorite_edge_score)([c], NOW)
    return c


def alert_settings(**kw):
    s = Settings()
    s.alerts.enabled = True
    s.alerts.min_favorite_edge_score = 50
    s.alerts.min_confidence = 50
    for k, v in kw.items():
        setattr(s.alerts, k, v)
    return s


def test_format_matches_spec_fields():
    text = format_alert(scored_candidate().to_dict())
    for label in ("POLYMARKET FAVORITE EDGE", "Market:", "Side:", "Ask:", "Estimated Probability:", "Edge:", "Expected ROI:",
                  "Confidence:", "Time Remaining:", "Available Size:", "Main Reason:", "Risk:"):
        assert label in text
    assert text.splitlines()[-1] == "PAPER TRADE ONLY."
    assert "YES" in text and "$0.900" in text and "97.0%" in text and "+7.0% (after fees)" in text
    sample = format_alert(SAMPLE_ALERT, header="TEST")
    assert sample.startswith("TEST") and "SAMPLE" in sample


def test_alert_once_then_only_on_material_improvement_after_cooldown():
    settings = alert_settings(cooldown_minutes=60, realert_edge_improvement=0.02)
    db = Database(":memory:")
    rec = Recorder()
    manager = AlertManager(settings, db, [rec])
    c = scored_candidate(0.97)
    assert c.status == "TRADE"
    assert len(manager.process([c], NOW)) == 1
    assert manager.process([c], NOW + timedelta(minutes=5)) == []  # duplicate
    assert manager.process([c], NOW + timedelta(hours=2)) == []  # cooldown over but no improvement
    better = scored_candidate(0.995)  # edge 7pt -> 9.5pt
    assert manager.process([better], NOW + timedelta(minutes=30)) == []  # still in cooldown
    assert len(manager.process([better], NOW + timedelta(hours=2))) == 1
    assert len(rec.messages) == 2
    rows = db.query("SELECT channel, ok, edge FROM alerts ORDER BY id")
    assert [r["ok"] for r in rows] == [1, 1] and rows[1]["edge"] == pytest.approx(0.095)


def test_thresholds_and_disabled():
    db = Database(":memory:")
    rec = Recorder()
    strict = alert_settings(min_favorite_edge_score=99)
    assert AlertManager(strict, db, [rec]).process([scored_candidate()], NOW) == []
    off = alert_settings()
    off.alerts.enabled = False
    assert AlertManager(off, db, [rec]).process([scored_candidate()], NOW) == []
    no_edge = scored_candidate(0.92)  # 2pt edge -> NO EDGE status
    assert no_edge.status == "NO EDGE"
    assert AlertManager(alert_settings(), db, [rec]).process([no_edge], NOW) == []
    assert rec.messages == []


def test_failed_send_is_recorded_and_retried_later():
    db = Database(":memory:")
    manager = AlertManager(alert_settings(), db, [Recorder("discord", fail=True)])
    c = scored_candidate()
    result = manager.process([c], NOW)
    assert result == [{"key": c.key, "channel": "discord", "ok": False, "error": "HTTP 500"}]
    assert manager.process([c], NOW + timedelta(minutes=1)) == []  # back-off
    assert len(manager.process([c], NOW + timedelta(minutes=10))) == 1  # retried


def test_missing_secrets_are_reported(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    s = alert_settings(telegram=True, discord=True)
    senders, problems = senders_from_settings(s)
    assert senders == [] and len(problems) == 2
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/abc")
    senders, problems = senders_from_settings(s)
    assert [x.name for x in senders] == ["discord"] and len(problems) == 1


@respx.mock
def test_telegram_and_discord_requests():
    tg = respx.post("https://api.telegram.org/botTOKEN/sendMessage").mock(return_value=httpx.Response(200, json={"ok": True}))
    TelegramSender("TOKEN", "42").send("hello")
    body = json.loads(tg.calls[0].request.content)
    assert body == {"chat_id": "42", "text": "hello", "disable_web_page_preview": True}
    respx.post("https://api.telegram.org/botBAD/sendMessage").mock(return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"}))
    with pytest.raises(RuntimeError):
        TelegramSender("BAD", "42").send("hello")
    dc = respx.post("https://discord.com/api/webhooks/1/abc").mock(return_value=httpx.Response(204))
    DiscordSender("https://discord.com/api/webhooks/1/abc").send("x" * 2500)
    assert len(json.loads(dc.calls[0].request.content)["content"]) == 2000


def test_runner_sends_alert_for_new_trade():
    market = gamma_market("1", best_bid=0.89, best_ask=0.90, fees_enabled=False)
    yes = clob_book("11", bids=[(0.89, 2000)], asks=[(0.90, 2000)])
    client = FakeClient([market], {"11": yes, "12": mirrored_no_book("12", yes)})
    settings = alert_settings()
    db = Database(":memory:")
    rec = Recorder()
    evaluator = Evaluator(settings, ProbabilityEngine(settings, [StubEngine(est(0.97, sources=("A", "B", "C"), quality=0.9, certainty=0.95))]), compute_favorite_edge_score)
    runner = Runner(settings, client=client, db=db, evaluator=evaluator, alerter=AlertManager(settings, db, [rec]), clock=lambda: NOW)
    summary = runner.run_once().summary()
    assert summary["trades_opened"] == 1 and summary["alerts_sent"] == 1
    assert "PAPER TRADE ONLY." in rec.messages[0]
    assert runner.run_once().summary()["alerts_sent"] == 0  # no duplicate next cycle
