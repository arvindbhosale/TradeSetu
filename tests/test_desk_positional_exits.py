"""Positional trades are held until expiry (index options), the signal (futures, stocks), a stoploss or a target:
the daily loss limit closes only intraday algos of the account that hit it, "exit days before expiry" counts trading
days in the algo as in the backtest, an intraday (MIS) algo squares off at its exit time even without a price, and
index option buying and futures can be positional on intraday candles too."""

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from desk import futures as fut, market, options as opt
from desk.algo import AlgoError
from desk.fnoalgo import FnoAlgoEngine, same_day
from desk.scaleout import NONE as NO_STEPS
from tests.test_desk_fno_algo_intraday import CALLS_5M, _engine, _five_minute
from tests.test_desk_fno_algos import CONDOR, NEAR, Clock, journal, setup

pytestmark = pytest.mark.unit


def test_the_daily_loss_limit_closes_only_intraday_algos_of_its_account(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, _ = setup(tmp_path, clock)
    day = engine.create({**CONDOR, "name": "Day condor"})
    held = engine.create({**CONDOR, "name": "Held condor", "schedule": "positional", "weekdays": [1], "min_days": 2})
    for algo in (day, held):
        engine.set_enabled(algo["id"], True)
        assert engine.evaluate(algo["id"])["algo"]["book"]["phase"] == "open"
    clock.set("2026-11-17 10:00")
    desk.flatten_days["live"] = "2026-11-17"                       # the other account: nothing happens here
    assert engine.evaluate(day["id"])["algo"]["book"] is not None
    desk.flatten_days["paper"] = "2026-11-17"
    out = engine.evaluate(day["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"] == "daily loss limit"
    out = engine.evaluate(held["id"])
    assert out["algo"]["book"]["phase"] == "open"                   # positional (NRML): held


def test_the_loss_limit_flattens_only_the_account_that_hit_it(tmp_path):
    from tests.test_desk_safety import make_desk, trade

    clock = Clock("2026-11-16 10:00")
    desk, prices, _ = make_desk(tmp_path, clock, daily_loss_limit=2000.0, max_order_value=500_000.0)
    trade(desk, quantity=100)
    prices["p"] = 1375.0
    assert desk.check_loss_limit() == ["paper"]
    assert desk.flattening("paper", "2026-11-16") and not desk.flattening("live", "2026-11-16")


def test_days_before_expiry_count_trading_days():
    friday = Clock("2026-11-20 10:00")()
    assert FnoAlgoEngine.sessions_to(NEAR, friday) == 2              # Friday and Monday before a Tuesday expiry
    assert FnoAlgoEngine.sessions_to(NEAR, Clock("2026-11-24 10:00")()) == 0
    saved = set(market.HOLIDAYS)
    try:
        market.set_holidays(["2026-11-23"])
        assert FnoAlgoEngine.sessions_to(NEAR, friday) == 1
    finally:
        market.set_holidays(saved)


def test_a_positional_structure_leaves_two_trading_days_before_expiry_at_the_exit_time(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Weekly condor", "schedule": "positional", "weekdays": [1],
                          "exit_days_before": 2, "min_days": 2})
    engine.set_enabled(algo["id"], True)
    assert engine.evaluate(algo["id"])["algo"]["book"]["near"] == NEAR
    clock.set("2026-11-20 10:00")                                   # Friday: 2 trading days left, before the exit time
    assert engine.evaluate(algo["id"])["algo"]["book"]["phase"] == "open"
    clock.set("2026-11-20 14:55")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("expiry")
    assert out["algo"]["blocked_day"] == "2026-11-20"               # no new entry the same day, as the backtest


def test_an_intraday_algo_squares_off_at_its_exit_time_without_a_price(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    clock.set("2026-11-17 14:00")
    engine.evaluate(algo["id"])                                     # the last prices seen
    for g in book["legs"]:
        prices.override[g["instrument"]] = None
    desk.last_price = lambda instrument: (None, "")
    clock.set("2026-11-17 14:55")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"] == "exit time 14:55"
    assert "squared off at the last price seen" in " ".join(x["message"] for x in out["algo"]["log"])
    assert desk.pnl("paper")["open"] == []


def test_a_positional_signal_algo_on_intraday_candles_holds_overnight(tmp_path):
    bars = _five_minute()
    clock, engine, desk, _ = _engine(tmp_path, "2026-11-18 11:02", bars)
    with pytest.raises(AlgoError, match="Day candles are positional"):
        engine.create({**CALLS_5M, "name": "Bad", "interval": "1d", "trade_type": "intraday"})
    algo = engine.create({**CALLS_5M, "trade_type": "positional"})
    assert not same_day(algo) and "positional: held overnight" in algo["log"][-1]["message"]
    assert same_day(engine.create({**CALLS_5M, "name": "Day calls"}))  # the default stays intraday (MIS)
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    engine.get(algo["id"])["ready"] = True
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None
    assert {r["product"] for r in journal(desk)} == {"NRML"}
    clock.set("2026-11-18 15:05")
    assert engine.evaluate(algo["id"])["algo"]["book"] is not None  # no square-off
    clock.set("2026-11-19 09:40")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is not None and out["algo"]["book"]["day"] == "2026-11-18"


def _vix(days):
    return pd.Series(14.0, index=pd.DatetimeIndex(pd.to_datetime(list(days))))


def test_an_option_held_to_expiry_settles_at_the_expiry_day_close():
    days = pd.bdate_range("2026-10-26", periods=30)
    closes = np.full(len(days), 25000.0)
    bars = pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1, "Close": closes}, index=days)
    bars.loc[pd.Timestamp("2026-11-24"), ["Close", "High"]] = 25230.0        # the expiry day closes up
    run = opt.run_options("NIFTY", bars, pd.Series(1, index=bars.index), _vix(days), capital=500_000, lots=1,
                          lot_size=65, min_days=4, expiry_weekday=1, slippage_pct=0, steps=NO_STEPS)
    trade = next(t for t in run["trades"] if t["contract"].startswith("NIFTY 24 Nov"))
    assert trade["exit"] == "expiry" and trade["exit_date"].startswith("2026-11-24")
    assert trade["exit_price"] == pytest.approx(230.0)


def test_option_buying_and_futures_backtests_can_be_positional_on_intraday_candles():
    days = ("2026-11-16", "2026-11-17", "2026-11-18")
    stamps = [pd.Timestamp(f"{d} 09:15") + pd.Timedelta(minutes=15 * i) for d in days for i in range(25)]
    closes = np.linspace(25000, 25300, len(stamps))
    bars = pd.DataFrame({"Open": closes, "High": closes + 2, "Low": closes - 2, "Close": closes},
                        index=pd.DatetimeIndex(stamps))
    signal = pd.Series(1, index=bars.index)
    kw = {"capital": 500_000, "lots": 1, "lot_size": 65, "min_days": 2, "intraday": True, "square_off": "15:00",
          "steps": NO_STEPS}
    day = opt.run_options("NIFTY", bars, signal, _vix(days), **kw)
    assert len([t for t in day["trades"] if t["exit"] == "square-off"]) >= 2
    held = opt.run_options("NIFTY", bars, signal, _vix(days), positional=True, **kw)
    assert len(held["trades"]) == 1 and held["trades"][0]["open"] and held["trade_type"] == "positional"
    fkw = {"underlying": "NIFTY", "lot_size": 65, "intraday": True, "square_off": "15:00", "continuous": False}
    assert all(t["exit"] == "square-off" for t in fut.run_futures(bars, signal, **fkw)["trades"])
    run = fut.run_futures(bars, signal, positional=True, **fkw)
    assert len(run["trades"]) == 1 and run["trades"][0]["open"] and run["params"]["positional"]
