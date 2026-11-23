"""Manual exit of algo positions (one, selected, all), open and close times, and each day's open P&L high/low."""

import copy

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk.algo import AlgoError
from desk.openpeaks import OpenPeaks
from desk.pnl import Ledger
from tests.test_desk_positional import SPEC, engine_for
from tests.test_desk_trading import Clock, candles_for

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


def bought(tmp_path):
    """A paper positional algo that has bought, with its target/stoploss GTT placed."""
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 09:36")
    engine, prices = engine_for(tmp_path, feed, clock)
    algo_id = engine.create(SPEC)["id"]
    engine.set_enabled(algo_id, True)
    feed["bars"] = pd.concat([history, candles_for("2026-11-16", [100, 104, 105, 106])])
    prices["p"] = 106.0
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] > 0 and algo["protect"]
    return engine, clock, prices, algo_id


def test_exit_cancels_the_gtt_sells_and_works_on_a_stopped_algo(tmp_path):
    engine, clock, prices, algo_id = bought(tmp_path)
    algo = engine.get(algo_id)
    assert algo["entry_at"] == "2026-11-16 09:36:00"
    gtt = algo["protect"]
    engine.set_enabled(algo_id, False)                     # stopped, still holding
    clock.set("2026-11-16 11:00")
    out = engine.request_exit(algo_id)
    assert out["position"] == 0 and out["protect"] is None and not out["enabled"] and not out["exit_requested"]
    assert out["entry_at"] is None
    assert engine.desk.paper.quantity("NSE:RELIANCE") == 0
    assert engine.desk.paper.gtt(gtt)["status"] != "active"      # cannot fire later on shares no longer held
    with pytest.raises(AlgoError, match="stopped and holds nothing"):
        engine.request_exit(algo_id)


def test_an_exit_outside_market_hours_waits_and_a_stopped_algo_is_still_worked(tmp_path):
    engine, clock, prices, algo_id = bought(tmp_path)
    engine.set_enabled(algo_id, False)
    clock.set("2026-11-16 18:00")
    out = engine.request_exit(algo_id)
    assert out["position"] > 0 and out["exit_requested"] and "waits for the market" in out["status"]
    clock.set("2026-11-17 09:20")
    engine._retry_at.clear()
    assert algo_id in engine.due()                        # stopped, but the exit request keeps it on the clock
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == 0 and not algo["enabled"] and not algo["exit_requested"]


def test_an_exit_on_a_running_algo_with_nothing_held_just_stops_it(tmp_path):
    clock = Clock("2026-11-16 10:00")
    engine, _ = engine_for(tmp_path, {"bars": None}, clock)
    algo_id = engine.create(SPEC)["id"]
    engine.set_enabled(algo_id, True)
    out = engine.request_exit(algo_id)
    assert not out["enabled"] and out["status"] == "Stopped"


def test_open_peaks_keep_each_days_high_and_low_per_trade_and_per_algo(tmp_path):
    clock = Clock("2026-11-16 10:00")
    peaks = OpenPeaks(None, tmp_path / "peaks.json", now=clock)
    legs = lambda a, b: [{"key": "k1", "source": "fno:Condor", "unrealized": a},       # noqa: E731
                         {"key": "k2", "source": "fno:Condor", "unrealized": b}]
    peaks.record("paper", legs(100.0, 50.0))
    clock.set("2026-11-16 11:30")
    peaks.record("paper", legs(900.0, -100.0))
    clock.set("2026-11-16 14:00")
    peaks.record("paper", legs(-300.0, 20.0))
    clock.set("2026-11-17 09:30")
    peaks.record("paper", legs(40.0, 10.0))
    days = peaks.trade("k1")
    assert [d["day"] for d in days] == ["2026-11-17", "2026-11-16"]
    assert days[1] == {"day": "2026-11-16", "high": 900.0, "high_at": "11:30:00", "low": -300.0, "low_at": "14:00:00"}
    algo = peaks.source("paper", "fno:Condor")[1]
    assert algo["high"] == 800.0 and algo["low"] == -280.0     # the legs added up, not each leg's own peak
    # A leg without a price would make the algo's total wrong: no algo sample then; the priced leg still counts.
    clock.set("2026-11-17 10:00")
    peaks.record("paper", [{"key": "k1", "source": "fno:Condor", "unrealized": 5000.0},
                           {"key": "k2", "source": "fno:Condor", "unrealized": None}])
    assert peaks.trade("k1")[0]["high"] == 5000.0 and peaks.source("paper", "fno:Condor")[0]["high"] == 50.0
    # Kept across a restart; the journal gets the best and worst of all days.
    again = OpenPeaks(None, tmp_path / "peaks.json", now=clock)
    trades = again.attach([{"key": "k1"}, {"key": "none"}])
    assert trades[0]["best_open"] == 5000.0 and trades[0]["worst_open"] == -300.0 and len(trades[0]["daily"]) == 2
    assert trades[1]["best_open"] is None and trades[1]["daily"] == []


def test_open_peaks_sample_only_in_market_hours_on_trading_days(tmp_path):
    class Desk:
        class kite:
            connected, tools = False, ()

        def __init__(self, ledger):
            self.ledger = ledger

        def last_price(self, instrument):
            return 110.0, "test"

    clock = Clock("2026-11-16 09:20")
    ledger = Ledger(tmp_path / "fills.jsonl", now=clock)
    ledger.add("paper", "algo:Swing", "NSE", "RELIANCE", "BUY", 10, 100.0, charges=0)
    peaks = OpenPeaks(Desk(ledger), tmp_path / "peaks.json", now=clock)
    peaks.tick()
    key = ledger.report("paper")["open"][0]["key"]
    assert peaks.trade(key)[0]["high"] == 100.0                # (110 - 100) x 10
    assert peaks.source("paper", "algo:Swing")[0]["high"] == 100.0
    clock.set("2026-11-15 10:00")                              # a Sunday
    peaks._last = None
    Desk.last_price = lambda self, i: (200.0, "test")
    peaks.tick()
    assert [d["day"] for d in peaks.trade(key)] == ["2026-11-16"]


def test_legs_say_when_each_position_opened_and_closed(tmp_path):
    clock = Clock("2026-11-16 09:20")
    ledger = Ledger(tmp_path / "fills.jsonl", now=clock)
    ledger.add("paper", "algo:Swing", "NSE", "RELIANCE", "BUY", 10, 100.0, charges=0)
    ledger.add("paper", "algo:Swing", "NSE", "TCS", "BUY", 5, 3000.0, charges=0)
    clock.set("2026-11-18 14:05")
    ledger.add("paper", "algo:Swing", "NSE", "RELIANCE", "SELL", 10, 105.0, charges=0)
    rows = {r["symbol"]: r for r in ledger.legs("algo:Swing")}
    assert rows["RELIANCE"]["opened_at"] == "2026-11-16 09:20:00" and rows["RELIANCE"]["closed_at"] == "2026-11-18 14:05:00"
    assert rows["TCS"]["opened_at"] == "2026-11-16 09:20:00" and rows["TCS"]["closed_at"] is None


def test_the_exit_many_api_checks_its_input_and_reports_each_algo(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t0ken", fetch=lambda *a, **k: None)
    with TestClient(app, headers={"X-Desk-Token": "t0ken"}) as client:
        assert client.post("/api/algo-exits", json={"mode": "everything"}).status_code == 400
        assert client.post("/api/algo-exits", json={"mode": "paper"}).json() == {"results": []}
        out = client.post("/api/algo-exits", json={"stock": ["nope"], "fno": ["nope"]}).json()["results"]
        assert [(r["kind"], r["ok"]) for r in out] == [("fno", False), ("stock", False)]
        journal = client.get("/api/journal?mode=paper").json()
        assert journal["trades"] == []
