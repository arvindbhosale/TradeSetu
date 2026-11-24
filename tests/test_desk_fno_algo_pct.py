"""F&O algos take the backtests' % stoploss and target for the whole position, so an algo made from a backtest exits
where the backtest did: an option strategy's in % of its premium, a future's in % of its price."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from desk.algo import AlgoError
from tests.test_desk_fno_algo_steps import decay
from tests.test_desk_fno_algos import CONDOR, Clock, setup

pytestmark = pytest.mark.unit


def test_the_percent_exits_are_checked_for_each_kind(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match="Stoploss, % of premium"):
        engine.create({**CONDOR, "stop_pct": 1001})
    with pytest.raises(AlgoError, match="Target, % of premium"):
        engine.create({**CONDOR, "target_pct": 101})
    with pytest.raises(AlgoError, match="Stoploss, % of price"):
        engine.create({"name": "Fut", "kind": "futures", "underlying": "NIFTY", "strategy": "sma_cross", "stop_pct": 60})
    algo = engine.create({**CONDOR, "stop_pct": 150, "target_pct": 50})
    assert algo["stop_pct"] == 150 and algo["target_pct"] == 50
    assert "whole position stoploss 150% of the premium, target 50% of the premium" in algo["log"][-1]["message"]
    plain = engine.create({**CONDOR, "name": "Plain"})
    assert plain["stop_pct"] == 0 and plain["target_pct"] == 0


@pytest.mark.parametrize(("factor", "word"), [(0.5, "target"), (1.7, "stoploss")])
def test_a_condor_closes_at_its_percent_of_the_premium(tmp_path, factor, word):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "stop_pct": 60, "target_pct": 40})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["net_open"] > 0                                     # a credit: the % is of what was received
    clock.set("2026-11-17 10:30")
    decay(prices, book, 0.9)                                        # 10% of the credit: neither level
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None
    decay(prices, book, factor)                                     # +50% or −70% of the credit
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None
    assert out["algo"]["history"][-1]["reason"].startswith(f"{word}: ")
    assert "of the premium" in out["algo"]["history"][-1]["reason"]
    assert desk.pnl("paper")["open"] == []


def test_a_future_closes_at_its_percent_of_the_price(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock, max_order_value=500_000.0)
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(26000, 24000, 80), np.linspace(24000, 25500, 40)]
    bars = pd.DataFrame({"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e6},
                        index=days)
    engine.fetch = lambda symbol, interval="1d": bars
    algo = engine.create({"name": "Nifty fut", "kind": "futures", "direction": "long", "underlying": "NIFTY",
                          "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20}, "mode": "paper",
                          "entry_time": "09:35", "exit_time": "15:10", "stop_pct": 1, "target_pct": 3})
    engine.set_enabled(algo["id"], True)
    engine.get(algo["id"])["ready"] = True                         # as if the cross happened today
    book = engine.evaluate(algo["id"])["algo"]["book"]
    leg = book["legs"][0]
    first = leg["first"]
    clock.set("2026-11-17 11:00")
    prices.override[leg["instrument"]] = round(first * 0.995, 2)   # −0.5%: held
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None
    prices.override[leg["instrument"]] = round(first * 0.988, 2)   # −1.2%: past the 1% stoploss
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None
    assert out["algo"]["history"][-1]["reason"].startswith("stoploss: the future moved -1.2")
    assert desk.paper.quantity(leg["instrument"]) == 0


def test_the_percent_exits_can_change_while_legs_are_open(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None
    edited = engine.update(algo["id"], {**CONDOR, "stop_pct": 80, "target_pct": 30})
    assert edited["stop_pct"] == 80 and edited["target_pct"] == 30
