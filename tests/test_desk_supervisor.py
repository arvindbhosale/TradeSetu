"""The supervisor: it may hold entries, close a position or turn the kill switch on, and nothing else."""

import copy
import json

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk.algo import AlgoError
from desk.supervisor import Supervisor, parse_reply
from tests.test_desk_fno_algos import CONDOR, setup as fno_setup
from tests.test_desk_trading import Clock, algo_engine, candles_for

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


class NoFno:
    def list(self):
        return []

    def get(self, algo_id):
        raise AlgoError("no such F&O algo")


HISTORY = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
SPEC = {"name": "Intra", "symbol": "RELIANCE", "strategy": "ema_cross", "strategy_params": {"fast": 2, "slow": 5},
        "budget": 50_000, "mode": "paper", "interval": "5m"}


def bought(tmp_path, **settings):
    """A paper intraday algo that has just bought at 106."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    feed, clock = {"bars": HISTORY}, Clock("2026-11-16 09:36")
    engine, prices = algo_engine(tmp_path, feed, clock, **settings)
    algo_id = engine.create(SPEC)["id"]
    engine.set_enabled(algo_id, True)

    def at(moment, closes, price):
        clock.set(f"2026-11-16 {moment}")
        feed["bars"] = pd.concat([HISTORY, candles_for("2026-11-16", closes)])
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        return engine.get(algo_id)

    algo = at("09:36", [100, 104, 105, 106], 106.0)
    assert algo["position"] > 0
    return engine, algo_id, clock, prices, at


def test_an_exit_request_sells_even_with_the_kill_switch_on_and_stops_the_algo(tmp_path):
    engine, algo_id, clock, prices, at = bought(tmp_path)
    engine.desk.settings.update({"kill_switch": True})
    algo = engine.request_exit(algo_id, "Exit pressed")
    assert algo["position"] == 0 and not algo["enabled"] and not algo["exit_requested"] and algo["status"] == "Stopped"
    assert [r["side"] for r in engine.desk.journal_rows()][:2] == ["SELL", "BUY"]
    with pytest.raises(AlgoError, match="stopped"):
        engine.request_exit(algo_id)


def test_held_entries_block_buys_until_released_but_not_sells(tmp_path):
    feed, clock = {"bars": HISTORY}, Clock("2026-11-16 09:36")
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create(SPEC)["id"]
    engine.set_enabled(algo_id, True)
    engine.hold_entries(algo_id, "2026-11-16", "test")
    feed["bars"] = pd.concat([HISTORY, candles_for("2026-11-16", [100, 104, 105, 106])])
    prices["p"] = 106.0
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and algo["status"] == "Running: new buys held"
    engine.release_hold(algo_id)
    clock.set("2026-11-16 09:41")
    feed["bars"] = pd.concat([HISTORY, candles_for("2026-11-16", [100, 104, 105, 106, 107])])
    engine._retry_at.clear()
    engine.tick()
    assert engine.get(algo_id)["position"] > 0


def test_the_backstop_closes_a_losing_paper_position_only_in_protect_mode(tmp_path):
    engine, algo_id, clock, prices, at = bought(tmp_path, supervisor_mode="watch", supervisor_loss_pct=5)
    sup = Supervisor(engine.desk, engine, NoFno(), path=tmp_path / "sup.json", now=clock)
    prices["p"] = 100.0                                                   # 5.7% below the buy
    out = sup.check_rules()
    assert [e["action"] for e in out] == ["exit"] and not out[0]["applied"] and "Watch mode" in out[0]["note"]
    assert engine.get(algo_id)["position"] > 0
    assert sup.check_rules() == []                                       # once a day, not every minute

    engine2, algo2, clock2, prices2, _ = bought(tmp_path / "b", supervisor_mode="protect", supervisor_loss_pct=5)
    sup2 = Supervisor(engine2.desk, engine2, NoFno(), path=tmp_path / "sup2.json", now=clock2)
    prices2["p"] = 100.0
    out = sup2.check_rules()
    assert out[0]["applied"] and out[0]["source"] == "rule"
    algo = engine2.get(algo2)
    assert algo["position"] == 0 and not algo["enabled"]
    assert any("Supervisor rule" in e["message"] for e in algo["log"])


def test_near_the_daily_loss_limit_every_algo_on_that_account_stops_new_entries(tmp_path):
    engine, algo_id, clock, prices, at = bought(tmp_path, supervisor_mode="protect", daily_loss_limit=500)
    sup = Supervisor(engine.desk, engine, NoFno(), path=tmp_path / "sup.json", now=clock)
    held = engine.get(algo_id)["position"]
    prices["p"] = 106.0 - 450 / held                                     # a ₹450 loss: past 80% of 500
    out = sup.check_rules()
    assert [(e["action"], e["applied"]) for e in out] == [("hold", True)]
    assert engine.get(algo_id)["hold_until"] == "2026-11-16" and engine.get(algo_id)["position"] == held


def ai_supervisor(tmp_path, reply, **settings):
    engine, algo_id, clock, prices, at = bought(tmp_path, supervisor_mode="protect", supervisor_ai=True, **settings)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return reply(algo_id) if callable(reply) else reply

    sup = Supervisor(engine.desk, engine, NoFno(), ask=ask, path=tmp_path / "sup.json", now=clock)
    return sup, engine, algo_id, asked


def test_the_ai_can_only_use_the_allowed_actions_on_real_algos(tmp_path):
    def reply(algo_id):
        return "<think>hmm</think>Here: " + json.dumps({"summary": "One algo is in trouble.", "actions": [
            {"type": "buy", "algo": algo_id, "reason": "looks cheap"},
            {"type": "exit", "algo": "made-up", "reason": "bad"},
            {"type": "hold", "algo": algo_id, "reason": ""},
            {"type": "exit", "algo": algo_id, "reason": "blocked while holding"}]})

    sup, engine, algo_id, asked = ai_supervisor(tmp_path, reply)
    result = sup.review()
    assert result["error"] is None and result["summary"] == "One algo is in trouble."
    assert algo_id in asked[0] and '"shares"' in asked[0]
    notes = [(e["action"], e["applied"], e["note"]) for e in sup.status()["log"]][::-1]
    assert notes[0][:2] == ("buy", False) and "not an allowed action" in notes[0][2]
    assert notes[1][:2] == ("exit", False) and "no running algo" in notes[1][2]
    assert notes[2][:2] == ("hold", False) and "no reason" in notes[2][2]
    assert notes[3][:2] == ("exit", True)
    assert engine.get(algo_id)["position"] == 0


def test_the_ai_has_a_daily_action_cap_and_a_broken_reply_does_nothing(tmp_path):
    def reply(algo_id):
        return json.dumps({"summary": "", "actions": [{"type": "hold", "algo": algo_id, "reason": "first"},
                                                      {"type": "kill_switch", "algo": None, "reason": "second"}]})

    sup, engine, algo_id, _ = ai_supervisor(tmp_path, reply, supervisor_max_actions=1)
    sup.review()
    log = sup.status()["log"]
    assert [e["applied"] for e in log] == [False, True] and "used its 1 actions" in log[0]["note"]
    assert not engine.desk.settings.get("kill_switch") and sup.status()["actions_today"] == 1

    sup2, engine2, algo2, _ = ai_supervisor(tmp_path / "b", "I would close everything!")
    result = sup2.review()
    assert "JSON" in result["error"] and sup2.status()["log"] == []
    assert engine2.get(algo2)["position"] > 0


class LiveStub:
    def __init__(self):
        self.algo = {"id": "a1", "name": "Live one", "mode": "live", "enabled": True, "position": 10,
                     "average_price": 100.0, "symbol": "RELIANCE", "log": [], "status": "Running"}
        self.exits = []

    def list(self):
        return [self.algo]

    def get(self, algo_id):
        if algo_id != "a1":
            raise AlgoError("no such algo")
        return self.algo

    def request_exit(self, algo_id, reason):
        self.exits.append(reason)


def test_live_algos_are_left_alone_unless_settings_allow_it(tmp_path):
    feed, clock = {"bars": HISTORY}, Clock("2026-11-16 10:00")
    engine, _ = algo_engine(tmp_path, feed, clock, supervisor_mode="protect")
    stub = LiveStub()
    sup = Supervisor(engine.desk, stub, NoFno(), path=tmp_path / "sup.json", now=clock)
    entry = sup.act("ai", "exit", "stock", "a1", "blocked")
    assert not entry["applied"] and "Live algo" in entry["note"] and stub.exits == []
    entry = sup.act("rule", "kill_switch", None, None, "x")
    assert not entry["applied"] and not engine.desk.settings.get("kill_switch")
    engine.desk.settings.update({"supervisor_live": True})
    assert sup.act("ai", "exit", "stock", "a1", "blocked")["applied"] and stub.exits == ["AI supervisor: blocked"]


def test_an_fno_algo_with_held_entries_opens_nothing(tmp_path):
    from tests.test_desk_fno_algos import Clock as FnoClock

    clock = FnoClock("2026-11-17 09:35")
    engine, desk, prices = fno_setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    engine.hold_entries(algo["id"], "2026-11-17", "test")
    out = engine.evaluate(algo["id"])
    assert out["status"] == "Running: new entries held" and out["algo"]["book"] is None
    engine.release_hold(algo["id"])
    assert engine.evaluate(algo["id"])["algo"]["book"]["phase"] == "open"


def test_replies_are_parsed_strictly():
    assert parse_reply('```json\n{"summary": "ok", "actions": []}\n```') == ("ok", [])
    for bad in ("nothing", '{"actions": "exit"}', "[1, 2]", '{"summary": "x", "actions": [1]}', "{broken"):
        with pytest.raises(ValueError):
            parse_reply(bad)


def test_supervisor_api_and_settings(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    settings = Settings(tmp_path / "settings.json")
    app = create_app(settings=settings, token="t", fetch=lambda symbol, interval="1d": None)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        status = client.get("/api/supervisor").json()
        assert status["settings"]["mode"] == "off" and status["log"] == []
        assert client.put("/api/settings", json={"supervisor_mode": "nonsense"}).status_code == 400
        assert client.put("/api/settings", json={"supervisor_minutes": 2}).status_code == 400
        ok = client.put("/api/settings", json={"supervisor_mode": "watch", "supervisor_minutes": 10})
        assert ok.status_code == 200 and client.get("/api/supervisor").json()["settings"]["minutes"] == 10
        assert client.post("/api/supervisor/check").json() == {"actions": []}
        assert client.post("/api/algos/nope/exit").status_code == 400
    app.state.kite.disconnect()
