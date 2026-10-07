import pytest
from fastapi.testclient import TestClient

from favorite_hunter.config import Settings
from favorite_hunter.dashboard.app import create_app
from favorite_hunter.database import Database
from favorite_hunter.evaluator import Evaluator
from favorite_hunter.probability.engine import ProbabilityEngine
from favorite_hunter.runner import Runner
from favorite_hunter.scoring import compute_favorite_edge_score

from .factories import NOW, clob_book, gamma_market, mirrored_no_book
from .fakes import FakeClient
from .test_evaluation import StubEngine, est, evaluate, make_candidate


def test_favorite_edge_score_components_and_weights():
    c = evaluate(make_candidate(), est(0.97, sources=("A", "B"), quality=0.8, certainty=0.9))
    score = compute_favorite_edge_score(c, Settings(), NOW)
    comps = score.components
    assert comps["probability_edge"] == pytest.approx(70.0)  # 7pt edge / 10pt full marks
    assert comps["source_reliability"] == pytest.approx(100 * (0.75 * 0.8 + 0.25 * 0.75))
    assert comps["time_remaining"] == pytest.approx(85.0)  # 1-6h bucket
    assert comps["event_certainty"] == pytest.approx(90.0)
    w = {"probability_edge": 0.30, "source_reliability": 0.20, "time_remaining": 0.15, "liquidity": 0.15, "spread": 0.10, "event_certainty": 0.10}
    assert score.value == pytest.approx(sum(w[k] * comps[k] for k in w))


def test_score_weights_are_configurable_and_normalised():
    settings = Settings()
    settings.score_weights.probability_edge = 100
    for name in ("source_reliability", "time_remaining", "liquidity", "spread", "event_certainty"):
        setattr(settings.score_weights, name, 0)
    c = evaluate(make_candidate(), est(0.97), settings)
    assert compute_favorite_edge_score(c, settings, NOW).value == pytest.approx(70.0)


def test_no_estimate_no_score():
    from favorite_hunter.probability.base import ProbabilityEstimate

    c = evaluate(make_candidate(), ProbabilityEstimate.unavailable(0, "x", "no data"))
    assert compute_favorite_edge_score(c, Settings(), NOW) is None


@pytest.fixture()
def seeded():
    settings = Settings()
    market = gamma_market("1", best_bid=0.89, best_ask=0.90, fees_enabled=False)
    other = gamma_market("2", question="Will the album debut at #1?", best_bid=0.93, best_ask=0.94, fees_enabled=False)
    yes = clob_book("11", bids=[(0.89, 2000)], asks=[(0.90, 2000)])
    yes2 = clob_book("21", bids=[(0.93, 2000)], asks=[(0.94, 2000)])
    client = FakeClient([market, other], {"11": yes, "12": mirrored_no_book("12", yes), "21": yes2, "22": mirrored_no_book("22", yes2)})
    db = Database(":memory:")

    class OnlyFirst(StubEngine):
        def applies(self, candidate):
            return candidate.market.id == "1"

    evaluator = Evaluator(settings, ProbabilityEngine(settings, [OnlyFirst(est(0.97))]), compute_favorite_edge_score)
    Runner(settings, client=client, db=db, evaluator=evaluator, clock=lambda: NOW).run_once()
    return TestClient(create_app(settings, db)), db


def test_dashboard_endpoints(seeded):
    client, db = seeded
    assert client.get("/").status_code == 200 and "Favorite Hunter" in client.get("/").text
    status = client.get("/api/status").json()
    assert status["mode"] == "PAPER TRADING ONLY" and status["synthetic_data"] is False
    assert status["last_scan"]["data_available"] == 1

    opps = client.get("/api/opportunities").json()["opportunities"]
    assert [o["status"] for o in opps] == ["HOLDING", "DATA UNAVAILABLE"]  # sorted by EV, unavailable last
    top = opps[0]
    assert top["edge"] == pytest.approx(0.07) and top["score"] is not None and top["confidence"] is not None
    assert opps[1]["est_prob"] is None and opps[1]["confidence"] is None

    detail = client.get("/api/opportunity", params={"key": top["key"]}).json()
    assert detail["detail"]["estimate"]["probability"] == 0.97
    assert detail["detail"]["description"]  # full rules text for the audit view
    assert detail["stored_book"]["asks"][0] == [0.9, 2000.0]
    assert detail["trades"][0]["kind"] == "model"
    assert client.get("/api/opportunity", params={"key": "nope:0"}).status_code == 404

    trades = client.get("/api/trades", params={"kind": "model"}).json()
    assert trades["bankroll"]["open_cost"] > 0 and len(trades["trades"]) == 1
    baseline = client.get("/api/trades", params={"kind": "baseline"}).json()
    assert len(baseline["trades"]) == 2 and baseline["bankroll"] is None

    for dataset in ("model", "baseline", "backtest"):
        report = client.get("/api/analytics", params={"dataset": dataset}).json()
        assert report["overall"]["verdict"] == "INSUFFICIENT DATA" and len(report["extreme"]) == 3
    assert client.get("/api/analytics", params={"dataset": "bogus"}).status_code == 400

    config = client.get("/api/config").json()
    assert config["filters"]["min_edge"] == 0.04
    assert set(config["secrets_configured"]) == {"telegram", "discord", "odds_api"}
    assert all(isinstance(v, bool) for v in config["secrets_configured"].values())  # never the secret values


def test_synthetic_flag_is_exposed(seeded):
    client, db = seeded
    db.set_meta("synthetic_data", "1")
    assert client.get("/api/status").json()["synthetic_data"] is True


def test_dashboard_has_no_write_endpoints(seeded):
    client, _ = seeded
    for path in ("/api/opportunities", "/api/trades", "/api/status"):
        assert client.post(path).status_code == 405
