"""F&O algos made from a backtest: one exact expiry (the algo turns itself off once it is over), an expiry further
ahead, and bought calls or puts on a strategy's signal (your own legs with a direction)."""

import numpy as np
import pandas as pd
import pytest

from desk.algo import AlgoError
from tests.test_desk_fno_algos import CONDOR, MONTH_END, NEAR, NEXT, Clock, setup

pytestmark = pytest.mark.unit


def test_an_algo_on_one_expiry_trades_it_and_turns_itself_off_once_it_is_over(tmp_path):
    clock = Clock("2026-11-24 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Next week condor", "expiry": NEXT})
    assert algo["expiry"] == NEXT and "1 Dec 2026 expiry only" in algo["log"][-1]["message"]
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["near"] == NEXT                       # not the nearer 24 Nov expiry
    assert {g["expiry"] for g in out["algo"]["book"]["legs"]} == {NEXT}
    clock.set("2026-11-24 14:55")                                   # intraday exit: flat, still running for its expiry
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["enabled"]
    assert engine.finish_expired() == []
    clock.set("2026-12-01 14:55")                                   # its expiry day, at the exit time: over
    assert engine.finish_expired() == [algo["id"]]
    stopped = engine.get(algo["id"])
    assert not stopped["enabled"] and stopped["status"].startswith("Finished")
    assert "turned itself off" in stopped["log"][-1]["message"]
    with pytest.raises(AlgoError, match="choose another expiry"):
        engine.set_enabled(algo["id"], True)
    # Editing it to a later expiry makes it startable again.
    engine.update(algo["id"], {**CONDOR, "expiry": MONTH_END})
    assert engine.set_enabled(algo["id"], True)["enabled"]


def test_an_exact_expiry_algo_holding_legs_closes_them_before_it_turns_off(tmp_path):
    clock = Clock("2026-11-23 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Expiry week", "schedule": "positional", "expiry": NEAR, "weekdays": [0]})
    engine.set_enabled(algo["id"], True)
    assert engine.evaluate(algo["id"])["algo"]["book"]["near"] == NEAR
    clock.set("2026-11-24 15:00")                                   # past the exit time on expiry day, legs still open
    assert engine.finish_expired() == []                            # not turned off while it holds legs
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("expiry")
    assert engine.finish_expired() == [algo["id"]]                  # flat now: the next tick turns it off
    assert engine.get(algo["id"])["status"].startswith("Finished")


def test_expiry_ahead_picks_a_later_expiry_each_time(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "expiry_ahead": 1})
    assert algo["expiry_ahead"] == 1 and algo["expiry"] is None
    engine.set_enabled(algo["id"], True)
    assert engine.evaluate(algo["id"])["algo"]["book"]["near"] == NEXT
    far = engine.create({**CONDOR, "name": "Too far", "expiry_ahead": 3})
    engine.set_enabled(far["id"], True)
    out = engine.evaluate(far["id"])
    assert out["algo"]["book"] is None and "fourth" in out["algo"]["log"][-1]["message"]


@pytest.mark.parametrize("change, fragment", [
    ({"expiry": "2026-11-10"}, "has passed"), ({"expiry": "2026-11-26"}, "no option expiry on 26 Nov 2026"),
    ({"expiry": "27/11/2026"}, "date like"), ({"expiry_ahead": 4}, "Expiries ahead"),
    ({"kind": "futures", "strategy": "sma_cross", "expiry": NEXT}, "near-month future"),
    ({"expiry": NEAR, "min_days": 10}, "minimum of 10 days"),
])
def test_bad_expiry_choices_are_refused(tmp_path, change, fragment):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match=fragment):
        engine.create({**CONDOR, **change})


def test_an_edit_that_leaves_out_the_expiry_keeps_it(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    algo = engine.create({**CONDOR, "expiry": MONTH_END})
    assert engine.update(algo["id"], {**CONDOR, "lots": 2})["expiry"] == MONTH_END
    assert engine.update(algo["id"], {**CONDOR, "lots": 2, "expiry": None})["expiry"] is None


def test_the_expiry_list_for_the_pickers(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-25 09:00"))
    assert engine.listed_expiries("NIFTY", "weekly") == [NEXT, MONTH_END]
    assert engine.listed_expiries("nifty", "monthly") == [MONTH_END]
    assert engine.listed_expiries("NIFTY", "weekly", min_days=10) == [MONTH_END]
    assert engine.listed_expiries("BANKNIFTY", "monthly") == []
    with pytest.raises(AlgoError):
        engine.listed_expiries("XYZ")


def _bars():
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(26000, 24000, 80), np.linspace(24000, 25500, 40)]
    return pd.DataFrame({"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e6}, index=days)


BOUGHT_CALL = {"name": "Nifty calls", "kind": "structure", "structure": "custom", "bias": "bull", "underlying": "NIFTY",
               "legs": [{"side": "BUY", "type": "CE", "strikes": -1, "lots": 1, "expiry": "near"}], "expiry_kind": "weekly",
               "schedule": "signal", "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20},
               "entry_time": "09:20", "exit_time": "15:15", "lots": 1, "leg_scope": "bought",
               "leg_exits": {"unit": "pct", "stop_loss": 30, "target": 60}, "max_loss": 5000, "mode": "paper"}


def test_bought_calls_follow_the_signal_like_the_options_backtest(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    engine.fetch = lambda symbol, interval="1d": _bars()
    algo = engine.create(BOUGHT_CALL)
    assert algo["bias"] == "bull" and algo["leg_exits"]["stop_loss"] == 30 and not algo["naked"]
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])                                     # the uptrend is on: waits for a fresh signal
    engine.get(algo["id"])["ready"] = True
    book = engine.evaluate(algo["id"])["algo"]["book"]
    [leg] = book["legs"]
    assert (leg["type"], leg["action"], leg["strike"]) == ("CE", 1, 24950.0)   # one strike in the money (spot 25,000)
    # Bought puts lean the other way: on while the strategy is flat, so not now.
    puts = engine.create({**BOUGHT_CALL, "name": "Nifty puts", "bias": "bear",
                          "legs": [{"side": "BUY", "type": "PE", "strikes": 1, "lots": 1, "expiry": "near"}]})
    engine.set_enabled(puts["id"], True)
    engine.get(puts["id"])["ready"] = True
    assert engine.evaluate(puts["id"])["algo"]["book"] is None


@pytest.mark.parametrize("change, fragment", [({"bias": None}, "directional"), ({"bias": "up"}, "bull or bear")])
def test_your_own_legs_on_a_signal_need_a_direction(tmp_path, change, fragment):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match=fragment):
        engine.create({**BOUGHT_CALL, **change})
