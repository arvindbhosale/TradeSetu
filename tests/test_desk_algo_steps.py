"""Stock algos book profit in steps: part of the shares sold at each level, the GTT placed again for the rest."""

import copy

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk.algo import AlgoError
from tests.test_desk_positional import SPEC, engine_for
from tests.test_desk_trading import Clock, candles_for

pytestmark = pytest.mark.unit
STEPS = {"unit": "pct", "steps": [{"at": 3, "size": 50}], "stop_after": "cost"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


def bought(tmp_path, spec):
    history = pd.concat([candles_for(d, [100.0] * 75) for d in ("2026-11-12", "2026-11-13")])
    feed, clock = {"bars": history}, Clock("2026-11-16 09:36")
    engine, prices = engine_for(tmp_path, feed, clock)
    algo_id = engine.create(spec)["id"]
    engine.set_enabled(algo_id, True)
    feed["bars"] = pd.concat([history, candles_for("2026-11-16", [100, 104, 105, 106])])
    prices["p"] = 100.0
    engine.tick()

    def at(moment, price):
        clock.set(f"2026-11-16 {moment}")
        prices["p"] = price
        engine._retry_at.clear()
        engine.tick()
        return engine.get(algo_id)

    return engine, engine.get(algo_id), at


def test_bad_steps_are_refused_in_plain_words(tmp_path):
    engine, _, _ = bought(tmp_path, SPEC)
    with pytest.raises(AlgoError, match="more than the 100% bought"):
        engine.create({**SPEC, "name": "Two", "steps": {"steps": [{"at": 2, "size": 70}, {"at": 4, "size": 40}]}})
    with pytest.raises(AlgoError, match="higher than the one before"):
        engine.create({**SPEC, "name": "Two", "steps": {"steps": [{"at": 4, "size": 20}, {"at": 2, "size": 20}]}})


def test_a_positional_algo_sells_half_at_the_step_and_moves_the_gtt_stop_to_cost(tmp_path):
    engine, algo, at = bought(tmp_path, {**SPEC, "steps": STEPS})
    held, entry = algo["position"], algo["average_price"]
    assert held > 0 and algo["entry_qty"] == held and algo["steps_done"] == 0
    first_gtt = engine.desk.paper.gtt(algo["protect"])
    assert first_gtt["quantity"] == held and first_gtt["stop"] < entry

    algo = at("10:00", round(entry * 1.035, 2))                         # past +3%: half the shares sold
    assert algo["position"] == held - held // 2 and algo["steps_done"] == 1
    assert any("profit step 1" in e["message"] for e in algo["log"])
    gtt = engine.desk.paper.gtt(algo["protect"])
    assert gtt["quantity"] == algo["position"]                         # placed again for the shares left
    assert gtt["stop"] == pytest.approx(entry, abs=0.05)                # and its stoploss moved to cost
    assert engine.desk.paper.gtt(first_gtt["id"])["status"] == "cancelled"
    sells = [r for r in engine.desk.journal_rows() if r["side"] == "SELL" and not r.get("gtt")]
    assert len(sells) == 1 and sells[0]["quantity"] == held // 2

    algo = at("10:05", round(entry * 1.04, 2))                         # no second step: nothing more is sold
    assert algo["position"] == held - held // 2


def test_an_intraday_algo_books_steps_on_the_live_price_then_its_target(tmp_path):
    spec = {**SPEC, "name": "Intra", "trade_type": "intraday", "exits": {"unit": "pct", "stop_loss": 3, "target": 8},
            "steps": {"unit": "pct", "steps": [{"at": 2, "size": 25}, {"at": 4, "size": 25}]}}
    engine, algo, at = bought(tmp_path, spec)
    held, entry = algo["position"], algo["average_price"]
    algo = at("10:00", round(entry * 1.021, 2))
    assert algo["position"] == held - held // 4
    algo = at("10:05", round(entry * 1.041, 2))
    assert algo["position"] == held - 2 * (held // 4)
    algo = at("10:10", round(entry * 1.09, 2))                          # the target sells the rest
    assert algo["position"] == 0 and any("target" in e["message"] for e in algo["log"])


def test_a_live_step_that_does_not_fill_is_tried_again(tmp_path):
    engine, algo, at = bought(tmp_path, {**SPEC, "steps": STEPS})
    algo["mode"] = "live"
    algo["pending"] = {"order_id": "X1", "side": "SELL", "quantity": 10, "price": 103.0, "variety": "regular",
                       "placed": "2026-11-16 10:00", "step": 1}
    algo["steps_done"] = 1

    class Orders:
        connected = True

        @staticmethod
        def orders():
            return [{"order_id": "X1", "status": "CANCELLED", "filled_quantity": 0, "status_message": "test"}]

    engine.desk.kite = Orders()
    assert engine._reconcile(algo)
    assert algo["steps_done"] == 0 and algo["pending"] is None


def test_a_day_candle_algo_watches_its_steps_during_market_hours(tmp_path):
    from tests.test_desk_trading import DOWN, UP, algo_engine, flat
    clock, feed = Clock("2026-11-16 16:00"), {"bars": flat(DOWN, "2026-11-16")}
    engine, prices = algo_engine(tmp_path, feed, clock)
    algo_id = engine.create({"name": "Daily", "symbol": "RELIANCE", "strategy": "sma_cross",
                             "strategy_params": {"fast": 3, "slow": 10}, "budget": 25_000, "mode": "paper",
                             "exits": {"unit": "pct", "stop_loss": 5},
                             "steps": {"unit": "pct", "steps": [{"at": 4, "size": 50}], "stop_after": "cost"}})["id"]
    engine.set_enabled(algo_id, True)
    engine.tick()
    clock.set("2026-11-20 16:00")
    feed["bars"] = flat(UP, "2026-11-20")
    prices["p"] = 120.0
    engine.tick()
    clock.set("2026-11-23 09:30")
    prices["p"] = 121.0
    engine.tick()
    algo = engine.get(algo_id)
    held, entry = algo["position"], algo["average_price"]
    assert held and algo["protect"]
    clock.set("2026-11-23 11:00")                                     # market hours: due for its step
    prices["p"] = round(entry * 1.05, 2)
    assert algo_id in engine.due()
    engine._retry_at.clear()
    engine.tick()
    algo = engine.get(algo_id)
    assert algo["position"] == held - held // 2 and algo["steps_done"] == 1
    assert engine.desk.paper.gtt(algo["protect"])["trigger"] == pytest.approx(entry, abs=0.05)
    assert algo["stop_kind"] == "stoploss at cost after a profit step"
